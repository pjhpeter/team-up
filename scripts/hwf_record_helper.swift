import AppKit
import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import ScreenCaptureKit

// macOS 原生录屏与 H.264 MP4 合成辅助程序（供 ~/.hwf/scripts/test-record.py 编译缓存后调用）。
// 为什么用原生实现：本机不依赖外部 ffmpeg，且直接输出 shouldOptimizeForNetworkUse 的低码率 H.264 .mp4，
// 避免原始 ScreenCaptureKit .mov 单段膨胀到上百 MB 无法上传 GitLab。

struct FrameEntry: Decodable {
  let path: String
  let ms: Int64
}

func jsonOut(_ dict: [String: Any]) {
  if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
     let text = String(data: data, encoding: .utf8) {
    print(text)
    fflush(stdout)
  }
}

func fail(_ stage: String, _ message: String) -> Never {
  jsonOut(["ok": false, "stage": stage, "error": message])
  Foundation.exit(1)
}

func argValue(_ args: [String], _ flag: String) -> String? {
  guard let idx = args.firstIndex(of: flag), idx + 1 < args.count else { return nil }
  return args[idx + 1]
}

func evenDimension(_ raw: Double) -> Int {
  let rounded = max(2, Int(raw.rounded()))
  return rounded & ~1
}

func scaledDimensions(width: Double, height: Double, maxWidth: Int) -> (Int, Int) {
  let cap = Double(max(320, maxWidth))
  let scale = width > cap ? (cap / width) : 1.0
  return (evenDimension(width * scale), evenDimension(height * scale))
}

func listWindows(args: [String]) {
  let pids: Set<Int32> = Set(
    (argValue(args, "--pids") ?? "")
      .split(separator: ",")
      .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
  )
  let appFilter = argValue(args, "--app")?.lowercased()
  let hasAccess = CGPreflightScreenCaptureAccess()
  let rawList = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []

  var items: [[String: Any]] = []
  for info in rawList {
    guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0,
          let wid = info[kCGWindowNumber as String] as? UInt32,
          let pid = info[kCGWindowOwnerPID as String] as? Int32,
          let bounds = info[kCGWindowBounds as String] as? [String: Double] else {
      continue
    }
    let w = bounds["Width"] ?? 0
    let h = bounds["Height"] ?? 0
    let alpha = info[kCGWindowAlpha as String] as? Double ?? 1.0
    if w < 200 || h < 150 || alpha <= 0.01 { continue }
    if !pids.isEmpty && !pids.contains(pid) { continue }
    let owner = info[kCGWindowOwnerName as String] as? String ?? ""
    let title = info[kCGWindowName as String] as? String ?? ""
    if let filter = appFilter, !filter.isEmpty {
      let haystack = "\(owner) \(title)".lowercased()
      if !haystack.contains(filter) { continue }
    }
    items.append([
      "window_id": Int(wid),
      "pid": Int(pid),
      "app": owner,
      "title": title,
      "x": Int(bounds["X"] ?? 0),
      "y": Int(bounds["Y"] ?? 0),
      "width": Int(w),
      "height": Int(h),
    ])
  }
  jsonOut([
    "ok": true,
    "screen_capture_access": hasAccess,
    "windows": items,
  ])
}

func makeH264Settings(width: Int, height: Int, bitrate: Int, keyframeInterval: Int) -> [String: Any] {
  return [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: width,
    AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [
      AVVideoAverageBitRateKey: bitrate,
      AVVideoMaxKeyFrameIntervalKey: max(8, keyframeInterval),
      AVVideoProfileLevelKey: AVVideoProfileLevelH264MainAutoLevel,
      AVVideoAllowFrameReorderingKey: false,
    ],
  ]
}

func copyBGRA(_ src: CVPixelBuffer) -> CVPixelBuffer? {
  let w = CVPixelBufferGetWidth(src)
  let h = CVPixelBufferGetHeight(src)
  let fmt = CVPixelBufferGetPixelFormatType(src)
  var dst: CVPixelBuffer?
  let attrs: [CFString: Any] = [
    kCVPixelBufferCGImageCompatibilityKey: true,
    kCVPixelBufferCGBitmapContextCompatibilityKey: true,
    kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
  ]
  guard CVPixelBufferCreate(kCFAllocatorDefault, w, h, fmt, attrs as CFDictionary, &dst) == kCVReturnSuccess,
        let out = dst else {
    return nil
  }
  CVPixelBufferLockBaseAddress(src, .readOnly)
  CVPixelBufferLockBaseAddress(out, [])
  defer {
    CVPixelBufferUnlockBaseAddress(out, [])
    CVPixelBufferUnlockBaseAddress(src, .readOnly)
  }
  let srcBytesPerRow = CVPixelBufferGetBytesPerRow(src)
  let dstBytesPerRow = CVPixelBufferGetBytesPerRow(out)
  guard let srcBase = CVPixelBufferGetBaseAddress(src),
        let dstBase = CVPixelBufferGetBaseAddress(out) else {
    return nil
  }
  let copyBytes = min(srcBytesPerRow, dstBytesPerRow)
  for row in 0..<h {
    memcpy(dstBase.advanced(by: row * dstBytesPerRow), srcBase.advanced(by: row * srcBytesPerRow), copyBytes)
  }
  return out
}

final class WindowStreamSink: NSObject, SCStreamOutput, SCStreamDelegate {
  private let lock = NSLock()
  private let writer: AVAssetWriter
  private let input: AVAssetWriterInput
  private let adaptor: AVAssetWriterInputPixelBufferAdaptor
  private var firstPts: CMTime?
  private var lastRelativePts: CMTime = .zero
  private var lastPixelBuffer: CVPixelBuffer?
  private(set) var frameCount: Int = 0
  private(set) var streamError: String?

  init(writer: AVAssetWriter, input: AVAssetWriterInput, adaptor: AVAssetWriterInputPixelBufferAdaptor) {
    self.writer = writer
    self.input = input
    self.adaptor = adaptor
  }

  var hasFirstFrame: Bool {
    lock.lock()
    defer { lock.unlock() }
    return frameCount > 0
  }

  func stream(_ stream: SCStream, didStopWithError error: Error) {
    lock.lock()
    streamError = error.localizedDescription
    lock.unlock()
  }

  func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
    guard outputType == .screen, CMSampleBufferIsValid(sampleBuffer) else { return }
    guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
          let statusRaw = attachments.first?[.status] as? Int,
          let status = SCFrameStatus(rawValue: statusRaw),
          status == .complete,
          let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
      return
    }
    let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
    lock.lock()
    defer { lock.unlock() }
    if writer.status == .failed {
      streamError = writer.error?.localizedDescription ?? "AVAssetWriter failed"
      return
    }
    if firstPts == nil {
      guard writer.startWriting() else {
        streamError = writer.error?.localizedDescription ?? "AVAssetWriter startWriting failed"
        return
      }
      writer.startSession(atSourceTime: .zero)
      firstPts = pts
    }
    guard let basePts = firstPts else { return }
    var relPts = CMTimeSubtract(pts, basePts)
    if frameCount > 0 && CMTimeCompare(relPts, lastRelativePts) <= 0 {
      relPts = CMTimeAdd(lastRelativePts, CMTime(value: 1, timescale: 600))
    }
    if input.isReadyForMoreMediaData {
      if adaptor.append(imageBuffer, withPresentationTime: relPts) {
        lastRelativePts = relPts
        // 保留最近一帧独立拷贝：窗口静止期间无新帧推送时，收尾补一帧保证总时长完整。
        if let copied = copyBGRA(imageBuffer) {
          lastPixelBuffer = copied
        }
        frameCount += 1
      } else {
        streamError = writer.error?.localizedDescription ?? "AVAssetWriter append failed"
      }
    }
  }

  func appendTrailingHoldIfNeeded(totalSeconds: Double) {
    lock.lock()
    defer { lock.unlock() }
    guard frameCount > 0, let buf = lastPixelBuffer, writer.status == .writing else { return }
    let targetPts = CMTime(seconds: max(totalSeconds, lastRelativePts.seconds + 0.1), preferredTimescale: 600)
    if CMTimeCompare(targetPts, lastRelativePts) > 0 && input.isReadyForMoreMediaData {
      if adaptor.append(buf, withPresentationTime: targetPts) {
        lastRelativePts = targetPts
        frameCount += 1
      }
    }
  }
}

func recordWindow(args: [String]) async {
  guard CGPreflightScreenCaptureAccess() else {
    fail("permission", "Screen Recording permission is not granted for the current terminal/process")
  }
  guard let outPath = argValue(args, "--out") else { fail("args", "Missing --out") }
  let readyPath = argValue(args, "--ready-file")
  let stopPath = argValue(args, "--stop-file")
  let maxDuration = Double(argValue(args, "--max-duration") ?? "300") ?? 300.0
  let fps = max(1, min(30, Int(argValue(args, "--fps") ?? "8") ?? 8))
  let maxWidth = Int(argValue(args, "--max-width") ?? "1440") ?? 1440
  let bitrate = Int(argValue(args, "--bitrate") ?? "600000") ?? 600_000
  let targetWid = UInt32(argValue(args, "--window-id") ?? "0") ?? 0
  let pids: Set<pid_t> = Set(
    (argValue(args, "--pids") ?? "")
      .split(separator: ",")
      .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
  )

  await MainActor.run { _ = NSApplication.shared }
  let content: SCShareableContent
  do {
    content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
  } catch {
    fail("shareable-content", "Failed to enumerate shareable windows: \(error.localizedDescription)")
  }

  let candidates = content.windows.filter { win in
    if targetWid > 0 {
      if win.windowID != targetWid { return false }
      if !pids.isEmpty, let ownerPid = win.owningApplication?.processID, !pids.contains(ownerPid) {
        return false
      }
      return true
    }
    guard let ownerPid = win.owningApplication?.processID, pids.contains(ownerPid) else { return false }
    return win.isOnScreen && win.frame.width >= 200 && win.frame.height >= 150
  }.sorted { ($0.frame.width * $0.frame.height) > ($1.frame.width * $1.frame.height) }

  guard let window = candidates.first else {
    fail("window-not-found", "No matching on-screen window found for pids=\(Array(pids)) window_id=\(targetWid)")
  }

  let (outW, outH) = scaledDimensions(width: Double(window.frame.width), height: Double(window.frame.height), maxWidth: maxWidth)
  let outURL = URL(fileURLWithPath: outPath)
  try? FileManager.default.removeItem(at: outURL)
  try? FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)

  let writer: AVAssetWriter
  do {
    writer = try AVAssetWriter(outputURL: outURL, fileType: .mp4)
  } catch {
    fail("writer-init", "Failed to initialize AVAssetWriter: \(error.localizedDescription)")
  }
  // 把 moov 元数据前置，保证 GitLab 网页内嵌 <video> 可以边下边播。
  writer.shouldOptimizeForNetworkUse = true

  let input = AVAssetWriterInput(
    mediaType: .video,
    outputSettings: makeH264Settings(width: outW, height: outH, bitrate: bitrate, keyframeInterval: fps * 2)
  )
  input.expectsMediaDataInRealTime = true
  let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: [
      kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
      kCVPixelBufferWidthKey as String: outW,
      kCVPixelBufferHeightKey as String: outH,
    ]
  )
  guard writer.canAdd(input) else { fail("writer-input", "Cannot add H.264 input to AVAssetWriter") }
  writer.add(input)

  let scConfig = SCStreamConfiguration()
  scConfig.width = outW
  scConfig.height = outH
  scConfig.showsCursor = true
  scConfig.capturesAudio = false
  scConfig.pixelFormat = kCVPixelFormatType_32BGRA
  scConfig.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))

  // 只捕获目标单窗口，不录桌面背景、通知弹窗或用户的其他应用窗口。
  let filter = SCContentFilter(desktopIndependentWindow: window)
  let sink = WindowStreamSink(writer: writer, input: input, adaptor: adaptor)
  let stream = SCStream(filter: filter, configuration: scConfig, delegate: sink)
  let queue = DispatchQueue(label: "hwf.window.record")

  var gotSignal = false
  signal(SIGTERM, SIG_IGN)
  signal(SIGINT, SIG_IGN)
  let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
  let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
  sigTerm.setEventHandler { gotSignal = true }
  sigInt.setEventHandler { gotSignal = true }
  sigTerm.resume()
  sigInt.resume()

  do {
    try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: queue)
    try await stream.startCapture()
  } catch {
    fail("start-capture", "Failed to start window capture: \(error.localizedDescription)")
  }

  let startAt = Date()
  let readyDeadline = startAt.addingTimeInterval(4.0)
  while !sink.hasFirstFrame && sink.streamError == nil && Date() < readyDeadline {
    try? await Task.sleep(nanoseconds: 50_000_000)
  }
  if let err = sink.streamError {
    try? await stream.stopCapture()
    fail("capture-init", err)
  }
  if !sink.hasFirstFrame {
    try? await stream.stopCapture()
    fail("first-frame-timeout", "Window capture started but produced no frame within 4s (window may be minimized or off-screen)")
  }

  if let readyPath = readyPath {
    let readyMeta: [String: Any] = [
      "window_id": Int(window.windowID),
      "pid": Int(window.owningApplication?.processID ?? 0),
      "title": window.title ?? "",
      "width": outW,
      "height": outH,
    ]
    if let data = try? JSONSerialization.data(withJSONObject: readyMeta) {
      try? data.write(to: URL(fileURLWithPath: readyPath), options: .atomic)
    }
  }

  let deadline = startAt.addingTimeInterval(maxDuration)
  while !gotSignal && sink.streamError == nil && Date() < deadline {
    if let sp = stopPath, FileManager.default.fileExists(atPath: sp) {
      break
    }
    try? await Task.sleep(nanoseconds: 120_000_000)
  }

  let elapsed = Date().timeIntervalSince(startAt)
  try? await stream.stopCapture()
  queue.sync {}
  sink.appendTrailingHoldIfNeeded(totalSeconds: elapsed)
  input.markAsFinished()
  await writer.finishWriting()

  if writer.status != .completed {
    fail("writer-finish", writer.error?.localizedDescription ?? "AVAssetWriter did not complete")
  }
  let size = (try? FileManager.default.attributesOfItem(atPath: outPath)[.size] as? Int) ?? 0
  jsonOut([
    "ok": true,
    "out": outPath,
    "window_id": Int(window.windowID),
    "width": outW,
    "height": outH,
    "frames": sink.frameCount,
    "bytes": size,
  ])
}

func loadCGImage(_ path: String) -> CGImage? {
  let url = URL(fileURLWithPath: path)
  guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
  return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

func drawIntoPixelBuffer(image: CGImage, pool: CVPixelBufferPool, width: Int, height: Int) -> CVPixelBuffer? {
  var pb: CVPixelBuffer?
  guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &pb) == kCVReturnSuccess,
        let buf = pb else {
    return nil
  }
  CVPixelBufferLockBaseAddress(buf, [])
  defer { CVPixelBufferUnlockBaseAddress(buf, []) }
  guard let base = CVPixelBufferGetBaseAddress(buf) else { return nil }
  let bpr = CVPixelBufferGetBytesPerRow(buf)
  let colorSpace = CGColorSpaceCreateDeviceRGB()
  let bitmapInfo = CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
  guard let ctx = CGContext(
    data: base,
    width: width,
    height: height,
    bitsPerComponent: 8,
    bytesPerRow: bpr,
    space: colorSpace,
    bitmapInfo: bitmapInfo
  ) else {
    return nil
  }
  ctx.setFillColor(red: 0.08, green: 0.08, blue: 0.09, alpha: 1.0)
  ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
  ctx.interpolationQuality = .high

  let imgW = Double(image.width)
  let imgH = Double(image.height)
  let scale = min(Double(width) / imgW, Double(height) / imgH)
  let drawW = imgW * scale
  let drawH = imgH * scale
  let drawX = (Double(width) - drawW) / 2.0
  let drawY = (Double(height) - drawH) / 2.0
  ctx.draw(image, in: CGRect(x: drawX, y: drawY, width: drawW, height: drawH))
  return buf
}

func encodeFrames(args: [String]) async {
  guard let manifestPath = argValue(args, "--manifest") else { fail("args", "Missing --manifest") }
  guard let outPath = argValue(args, "--out") else { fail("args", "Missing --out") }
  let maxWidth = Int(argValue(args, "--max-width") ?? "1440") ?? 1440
  let bitrate = Int(argValue(args, "--bitrate") ?? "600000") ?? 600_000

  guard let raw = try? String(contentsOfFile: manifestPath, encoding: .utf8) else {
    fail("manifest", "Cannot read manifest: \(manifestPath)")
  }
  let decoder = JSONDecoder()
  var entries: [FrameEntry] = []
  for line in raw.split(whereSeparator: \.isNewline) {
    if let data = line.data(using: .utf8),
       let item = try? decoder.decode(FrameEntry.self, from: data),
       FileManager.default.fileExists(atPath: item.path) {
      entries.append(item)
    }
  }
  guard let firstEntry = entries.first, let firstImage = loadCGImage(firstEntry.path) else {
    fail("empty-frames", "No valid image frames found in manifest")
  }

  let (outW, outH) = scaledDimensions(width: Double(firstImage.width), height: Double(firstImage.height), maxWidth: maxWidth)
  let outURL = URL(fileURLWithPath: outPath)
  try? FileManager.default.removeItem(at: outURL)
  try? FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)

  let writer: AVAssetWriter
  do {
    writer = try AVAssetWriter(outputURL: outURL, fileType: .mp4)
  } catch {
    fail("writer-init", "Failed to initialize AVAssetWriter: \(error.localizedDescription)")
  }
  writer.shouldOptimizeForNetworkUse = true

  let input = AVAssetWriterInput(
    mediaType: .video,
    outputSettings: makeH264Settings(width: outW, height: outH, bitrate: bitrate, keyframeInterval: 16)
  )
  input.expectsMediaDataInRealTime = false
  let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: [
      kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
      kCVPixelBufferWidthKey as String: outW,
      kCVPixelBufferHeightKey as String: outH,
    ]
  )
  guard writer.canAdd(input) else { fail("writer-input", "Cannot add video input") }
  writer.add(input)
  guard writer.startWriting() else {
    fail("writer-start", writer.error?.localizedDescription ?? "startWriting failed")
  }
  writer.startSession(atSourceTime: .zero)

  var lastMs: Int64 = -20
  var lastImage: CGImage = firstImage
  var written = 0

  for (idx, entry) in entries.enumerated() {
    guard let img = (idx == 0 ? firstImage : loadCGImage(entry.path)) else { continue }
    while !input.isReadyForMoreMediaData {
      try? await Task.sleep(nanoseconds: 10_000_000)
    }
    guard let pool = adaptor.pixelBufferPool,
          let pb = drawIntoPixelBuffer(image: img, pool: pool, width: outW, height: outH) else {
      fail("pixel-buffer", "Failed to render frame \(idx)")
    }
    let ptsMs = written == 0 ? 0 : max(entry.ms, lastMs + 20)
    if !adaptor.append(pb, withPresentationTime: CMTime(value: ptsMs, timescale: 1000)) {
      fail("append", writer.error?.localizedDescription ?? "Failed to append frame \(idx)")
    }
    lastMs = ptsMs
    lastImage = img
    written += 1
  }

  // 末尾多停留 500ms，防止最后一帧结果在播放器结束瞬间一闪而过或单帧时长为 0。
  while !input.isReadyForMoreMediaData {
    try? await Task.sleep(nanoseconds: 10_000_000)
  }
  if let pool = adaptor.pixelBufferPool,
     let pb = drawIntoPixelBuffer(image: lastImage, pool: pool, width: outW, height: outH) {
    let endMs = max(lastMs + 500, 500)
    _ = adaptor.append(pb, withPresentationTime: CMTime(value: endMs, timescale: 1000))
    written += 1
  }

  input.markAsFinished()
  await writer.finishWriting()
  if writer.status != .completed {
    fail("writer-finish", writer.error?.localizedDescription ?? "AVAssetWriter failed to finish")
  }
  let size = (try? FileManager.default.attributesOfItem(atPath: outPath)[.size] as? Int) ?? 0
  jsonOut([
    "ok": true,
    "out": outPath,
    "width": outW,
    "height": outH,
    "frames": written,
    "bytes": size,
  ])
}

func verifyVideo(args: [String]) async {
  guard let filePath = argValue(args, "--file") else { fail("args", "Missing --file") }
  guard FileManager.default.fileExists(atPath: filePath) else {
    fail("missing", "Video file does not exist: \(filePath)")
  }
  let size = (try? FileManager.default.attributesOfItem(atPath: filePath)[.size] as? Int) ?? 0
  if size <= 0 {
    fail("empty", "Video file is empty: \(filePath)")
  }

  let asset = AVURLAsset(url: URL(fileURLWithPath: filePath))
  do {
    let isPlayable = try await asset.load(.isPlayable)
    let duration = try await asset.load(.duration)
    let tracks = try await asset.loadTracks(withMediaType: .video)
    guard isPlayable, let track = tracks.first else {
      fail("unplayable", "Video has no playable video track")
    }
    let durSec = duration.seconds
    if !durSec.isFinite || durSec < 0.05 {
      fail("duration", "Video duration is invalid (\(durSec)s)")
    }
    let natural = try await track.load(.naturalSize)
    let transform = try await track.load(.preferredTransform)
    let transformed = natural.applying(transform)
    let w = Int(abs(transformed.width).rounded())
    let h = Int(abs(transformed.height).rounded())
    if w < 2 || h < 2 {
      fail("dimensions", "Invalid video dimensions \(w)x\(h)")
    }

    // 实际解码一帧图像，拦住仅有文件头但帧流损坏的半成品文件。
    let gen = AVAssetImageGenerator(asset: asset)
    gen.appliesPreferredTrackTransform = true
    let sampleAt = CMTime(seconds: min(0.1, durSec / 2.0), preferredTimescale: 600)
    _ = try await gen.image(at: sampleAt)

    let sizeMb = (Double(size) / (1024.0 * 1024.0) * 100.0).rounded() / 100.0
    jsonOut([
      "ok": true,
      "file": filePath,
      "bytes": size,
      "size_mb": sizeMb,
      "duration_s": (durSec * 100.0).rounded() / 100.0,
      "width": w,
      "height": h,
    ])
  } catch {
    fail("decode", "Video verification failed: \(error.localizedDescription)")
  }
}

@main
struct HwfRecordHelper {
  static func main() async {
    let args = Array(CommandLine.arguments.dropFirst())
    guard let cmd = args.first else {
      fail("args", "Subcommand required: windows | record-window | encode-frames | verify")
    }
    let rest = Array(args.dropFirst())
    switch cmd {
    case "windows":
      listWindows(args: rest)
    case "record-window":
      await recordWindow(args: rest)
    case "encode-frames":
      await encodeFrames(args: rest)
    case "verify":
      await verifyVideo(args: rest)
    default:
      fail("args", "Unknown subcommand: \(cmd)")
    }
  }
}
