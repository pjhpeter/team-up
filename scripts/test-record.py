#!/usr/bin/env python3
"""统一自动化测试录屏工具：覆盖网页（ego-browser）与桌面客户端（Electron 单窗口），输出可内嵌播放的小体积 H.264 .mp4。

    # 网页测试（只录本轮 ego-browser TaskSpace 的被测页面，不失效 @ref 快照引用，切人工接管时自动暂停）
    python3 ~/.hwf/scripts/test-record.py start --mode ego --space-id <ID> [--page p1] --out <证据目录>/case-1.mp4
    python3 ~/.hwf/scripts/test-record.py stop --out <证据目录>/case-1.mp4

    # Electron 桌面客户端测试（只录指定进程树或窗口 ID 的单个窗口，无声、不录桌面或其他应用）
    python3 ~/.hwf/scripts/test-record.py windows --pid <客户端或启动进程PID>
    python3 ~/.hwf/scripts/test-record.py start --mode window --pid <PID> [--window-id <WID>] --out <证据目录>/case-1.mp4
    python3 ~/.hwf/scripts/test-record.py stop --out <证据目录>/case-1.mp4

    # 独立校验已有录屏能否正常解码播放
    python3 ~/.hwf/scripts/test-record.py verify --file <证据目录>/case-1.mp4

退出码：0 成功（stdout 一行 JSON）；1 失败（stdout 一行 JSON 含 stage 与 error）；2 参数错误。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
SWIFT_SOURCE = SCRIPTS_DIR / "hwf_record_helper.swift"
BIN_CACHE_DIR = SCRIPTS_DIR.parent / "state" / "bin"

DEFAULT_EGO_FPS = 4.0
DEFAULT_WINDOW_FPS = 8
DEFAULT_MAX_DURATION_S = 300
DEFAULT_MAX_WIDTH = 1440
DEFAULT_BITRATE = 600_000
WARN_SIZE_BYTES = 20 * 1024 * 1024

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RecordError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def sidecar_dir(out_mp4: Path) -> Path:
    return out_mp4.parent / f".{out_mp4.name}.recording"


def parse_last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def ensure_helper_binary(swift_src: Path = SWIFT_SOURCE, cache_dir: Path = BIN_CACHE_DIR) -> Path:
    """按源码哈希编译缓存 macOS 原生辅助二进制，加文件锁防止并发测试重复编译互踩。"""
    if not swift_src.is_file():
        raise RecordError("helper-source", f"Missing Swift helper source: {swift_src}")
    digest = hashlib.sha256(swift_src.read_bytes()).hexdigest()[:12]
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary = cache_dir / f"hwf-record-helper-{digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary

    lock_file = cache_dir / "compile.lock"
    with lock_file.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        swiftc_bin = shutil.which("swiftc")
        if sys.platform != "darwin" or not swiftc_bin:
            raise RecordError("helper-compile", "swiftc on macOS is required to compile hwf_record_helper.swift")
        arch = platform.machine() or "arm64"
        tmp_bin = cache_dir / f".hwf-record-helper-{digest}.{os.getpid()}"
        cmd = [
            swiftc_bin,
            "-O",
            "-parse-as-library",
            "-target",
            f"{arch}-apple-macosx15.0",
            str(swift_src),
            "-o",
            str(tmp_bin),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if res.returncode != 0 or not tmp_bin.is_file():
            detail = (res.stderr or res.stdout).strip()[:500]
            raise RecordError("helper-compile", f"Failed to compile hwf_record_helper.swift: {detail}")
        tmp_bin.chmod(0o755)
        tmp_bin.replace(binary)
    return binary


def validate_out_path(out_mp4: Path, run: Runner = subprocess.run) -> Path:
    """校验输出为 .mp4，且落在仓库内时必须位于已 gitignore 的证据目录，防止污染工作树根目录。"""
    resolved = out_mp4.expanduser().resolve()
    if resolved.suffix.lower() != ".mp4":
        raise RecordError("out-path", f"--out must end with .mp4, got: {resolved.name}")
    probe_dir = resolved.parent
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    top = run(["git", "-C", str(probe_dir), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if top.returncode == 0:
        repo = Path(top.stdout.strip()).resolve()
        if resolved.parent == repo:
            raise RecordError("out-path", "Recording output cannot be placed directly in the repository root")
        if repo in resolved.parents:
            ignored = run(["git", "-C", str(repo), "check-ignore", "-q", str(resolved)],
                          capture_output=True, text=True, check=False)
            if ignored.returncode != 0:
                raise RecordError(
                    "out-path",
                    f"Recording inside git repo must be gitignored (use <repo>/.hwf/tmp/<task>/testing/<round>/ or <repo>/.tmp/testing/<round>/): {resolved}",
                )
    return resolved


def process_tree_pids(root_pid: int, run: Runner = subprocess.run) -> list[int]:
    """把启动脚本 PID 展开为整棵子进程树，兼容传入 pnpm / electron-vite 包装进程 PID 的场景。"""
    if root_pid <= 0:
        return []
    ps = run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False)
    children: dict[int, list[int]] = {}
    for line in ps.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen: list[int] = []
    queue = [root_pid]
    visited: set[int] = set()
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        seen.append(cur)
        queue.extend(children.get(cur, ()))
    return seen


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def build_ego_sampler_js(
    space_id: int,
    page_label: str,
    frames_dir: Path,
    manifest_path: Path,
    ready_path: Path,
    stop_path: Path,
    interval_ms: int,
    max_duration_ms: int,
) -> str:
    cfg = json.dumps(
        {
            "spaceId": space_id,
            "pageLabel": page_label,
            "framesDir": str(frames_dir),
            "manifestPath": str(manifest_path),
            "readyPath": str(ready_path),
            "stopPath": str(stop_path),
            "intervalMs": max(150, interval_ms),
            "maxDurationMs": max(1000, max_duration_ms),
        }
    )
    # 使用官方 page.screenshot 接口按帧抓图，避免 page.cdp 使前台测试脚本的 @ref 快照引用失效；
    # 一旦空间切给用户手动操作（ownership !== "agent"）立即跳过抓帧，避免把手动输入的密码录进视频。
    return f"""
const fs = await import("node:fs");
const fsp = await import("node:fs/promises");
const path = await import("node:path");
const CFG = {cfg};

const initial = (await listTaskSpaces()).find((s) => s.id === CFG.spaceId);
if (!initial) {{
  console.error(JSON.stringify({{ ok: false, stage: "space-not-found", error: `TaskSpace ${{CFG.spaceId}} does not exist` }}));
  process.exit(1);
}}
if (initial.ownership !== "agent") {{
  console.error(JSON.stringify({{ ok: false, stage: "space-ownership", error: `TaskSpace ${{CFG.spaceId}} ownership is ${{initial.ownership}}, expected agent` }}));
  process.exit(1);
}}

const task = await taskSpace(CFG.spaceId);
const startAt = Date.now();
let seq = 0;
let readyWritten = false;

while (Date.now() - startAt < CFG.maxDurationMs && !fs.existsSync(CFG.stopPath)) {{
  const tickStart = Date.now();
  const spaceEntry = (await listTaskSpaces()).find((s) => s.id === CFG.spaceId);
  if (!spaceEntry) {{
    break;
  }}
  if (spaceEntry.ownership !== "agent") {{
    await new Promise((r) => setTimeout(r, 300));
    continue;
  }}
  try {{
    let targetLabel = CFG.pageLabel;
    if (!targetLabel) {{
      const tabs = await task.tabs();
      const activeManaged = tabs.find((t) => t.active && t.label);
      if (activeManaged) {{
        targetLabel = activeManaged.label;
      }} else {{
        const pages = await task.pages();
        targetLabel = pages[0]?.label || "p1";
      }}
    }}
    const page = task.page(targetLabel);
    const frameFile = path.join(CFG.framesDir, `frame-${{String(seq).padStart(5, "0")}}.png`);
    const elapsedMs = Math.max(0, Date.now() - startAt);
    await page.screenshot({{ path: frameFile }});
    await fsp.appendFile(CFG.manifestPath, JSON.stringify({{ path: frameFile, ms: elapsedMs }}) + "\\n", "utf8");
    seq += 1;
    if (!readyWritten) {{
      await fsp.writeFile(CFG.readyPath, JSON.stringify({{ space_id: CFG.spaceId, page: targetLabel }}), "utf8");
      readyWritten = true;
    }}
  }} catch (err) {{
    // 页面跳转瞬间可能短暂拒绝单帧截图，跳过本帧继续录制。
    if (!readyWritten && Date.now() - startAt > 6000) {{
      console.error(JSON.stringify({{ ok: false, stage: "ego-capture", error: String(err) }}));
      process.exit(1);
    }}
  }}
  const waitMs = Math.max(30, CFG.intervalMs - (Date.now() - tickStart));
  await new Promise((r) => setTimeout(r, waitMs));
}}
console.log(JSON.stringify({{ ok: true, captured_frames: seq }}));
"""


def run_ego_worker(args: argparse.Namespace) -> int:
    work_dir = args.work_dir.resolve()
    frames_dir = work_dir / "frames"
    manifest_path = work_dir / "manifest.jsonl"
    ready_path = work_dir / "ready.json"
    stop_path = work_dir / "stop"
    done_path = work_dir / "done.json"
    frames_dir.mkdir(parents=True, exist_ok=True)

    ego_bin = shutil.which("ego-browser")
    if not ego_bin:
        done_path.write_text(json.dumps({"ok": False, "stage": "ego-missing", "error": "ego-browser CLI not found in PATH"}), encoding="utf-8")
        return 1

    js_code = build_ego_sampler_js(
        space_id=args.space_id,
        page_label=args.page or "",
        frames_dir=frames_dir,
        manifest_path=manifest_path,
        ready_path=ready_path,
        stop_path=stop_path,
        interval_ms=round(1000.0 / max(0.5, args.fps)),
        max_duration_ms=int(args.max_duration * 1000),
    )

    sampler = subprocess.Popen(
        [ego_bin, "nodejs"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def forward_stop(*_: Any) -> None:
        stop_path.touch()

    signal.signal(signal.SIGTERM, forward_stop)
    signal.signal(signal.SIGINT, forward_stop)

    out_text, err_text = sampler.communicate(input=js_code)
    combined = f"{out_text}\n{err_text}".strip()
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        parsed = parse_last_json(combined)
        err_msg = str(parsed.get("error") or combined[:400] or "ego-browser captured 0 frames")
        stage = str(parsed.get("stage") or "ego-capture")
        done_path.write_text(json.dumps({"ok": False, "stage": stage, "error": err_msg}), encoding="utf-8")
        return 1

    encode_res = subprocess.run(
        [
            str(args.helper_bin),
            "encode-frames",
            "--manifest",
            str(manifest_path),
            "--out",
            str(args.out),
            "--max-width",
            str(args.max_width),
            "--bitrate",
            str(args.bitrate),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    encoded = parse_last_json(encode_res.stdout)
    if encode_res.returncode != 0 or not encoded.get("ok"):
        err_msg = str(encoded.get("error") or (encode_res.stderr or encode_res.stdout).strip()[:400] or "Frame encoding failed")
        done_path.write_text(json.dumps({"ok": False, "stage": str(encoded.get("stage") or "encode"), "error": err_msg}), encoding="utf-8")
        return 1

    done_path.write_text(json.dumps(encoded), encoding="utf-8")
    return 0


def cmd_windows(args: argparse.Namespace, helper_bin: Path, run: Runner = subprocess.run) -> dict[str, Any]:
    if not args.pid and not args.app:
        raise RecordError("args", "windows requires --pid <PID> or --app <keyword> so unrelated user windows are not listed")
    cmd = [str(helper_bin), "windows"]
    if args.pid:
        pids = process_tree_pids(args.pid, run=run)
        cmd += ["--pids", ",".join(str(p) for p in pids)]
    if args.app:
        cmd += ["--app", args.app]
    res = run(cmd, capture_output=True, text=True, timeout=15, check=False)
    data = parse_last_json(res.stdout)
    if res.returncode != 0 or not data.get("ok"):
        raise RecordError(str(data.get("stage") or "windows"), str(data.get("error") or res.stderr.strip() or "Failed to list windows"))
    return data


def cmd_verify(file_path: Path, helper_bin: Path, run: Runner = subprocess.run) -> dict[str, Any]:
    resolved = file_path.expanduser().resolve()
    res = run([str(helper_bin), "verify", "--file", str(resolved)], capture_output=True, text=True, timeout=30, check=False)
    data = parse_last_json(res.stdout)
    if res.returncode != 0 or not data.get("ok"):
        raise RecordError(str(data.get("stage") or "verify"), str(data.get("error") or res.stderr.strip() or "Video verification failed"))
    if int(data.get("bytes") or 0) > WARN_SIZE_BYTES:
        data["warning"] = "Video exceeds 20 MB; consider splitting long test flows by scenario"
    return data


def cmd_start(args: argparse.Namespace, helper_bin: Path, run: Runner = subprocess.run) -> dict[str, Any]:
    out_mp4 = validate_out_path(args.out, run=run)
    work_dir = sidecar_dir(out_mp4)
    state_file = work_dir / "state.json"
    if state_file.is_file():
        old_state = json.loads(state_file.read_text(encoding="utf-8"))
        old_pid = int(old_state.get("worker_pid") or 0)
        if is_pid_alive(old_pid):
            raise RecordError("already-running", f"Recording already active for {out_mp4} (worker_pid={old_pid}); stop it first")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    if out_mp4.exists():
        out_mp4.unlink()
    work_dir.mkdir(parents=True, mode=0o700)

    ready_path = work_dir / "ready.json"
    stop_path = work_dir / "stop"
    log_path = work_dir / "worker.log"

    if args.mode == "ego":
        if args.space_id is None:
            raise RecordError("args", "--space-id is required when --mode ego")
        fps = float(args.fps if args.fps is not None else DEFAULT_EGO_FPS)
        worker_cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_ego-worker",
            "--helper-bin",
            str(helper_bin),
            "--work-dir",
            str(work_dir),
            "--space-id",
            str(args.space_id),
            "--page",
            args.page or "",
            "--out",
            str(out_mp4),
            "--fps",
            str(fps),
            "--max-duration",
            str(args.max_duration),
            "--max-width",
            str(args.max_width),
            "--bitrate",
            str(args.bitrate),
        ]
        ready_timeout_s = 12.0
    else:
        if not args.pid and not args.window_id:
            raise RecordError("args", "--pid or --window-id is required when --mode window")
        fps_int = max(1, round(args.fps if args.fps is not None else DEFAULT_WINDOW_FPS))
        pids = process_tree_pids(args.pid, run=run) if args.pid else []
        worker_cmd = [
            str(helper_bin),
            "record-window",
            "--out",
            str(out_mp4),
            "--ready-file",
            str(ready_path),
            "--stop-file",
            str(stop_path),
            "--fps",
            str(fps_int),
            "--max-duration",
            str(args.max_duration),
            "--max-width",
            str(args.max_width),
            "--bitrate",
            str(args.bitrate),
        ]
        if pids:
            worker_cmd += ["--pids", ",".join(str(p) for p in pids)]
        if args.window_id:
            worker_cmd += ["--window-id", str(args.window_id)]
        ready_timeout_s = 8.0

    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            worker_cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + ready_timeout_s
    while time.monotonic() < deadline:
        if ready_path.is_file():
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    if not ready_path.is_file():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        done_file = work_dir / "done.json"
        log_text = done_file.read_text(encoding="utf-8") if done_file.is_file() else (log_path.read_text(encoding="utf-8") if log_path.is_file() else "")
        parsed = parse_last_json(log_text)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise RecordError(
            str(parsed.get("stage") or "start-timeout"),
            str(parsed.get("error") or log_text.strip()[:400] or "Recorder did not become ready in time"),
        )

    ready_meta = json.loads(ready_path.read_text(encoding="utf-8") or "{}")
    state = {
        "mode": args.mode,
        "worker_pid": proc.pid,
        "out": str(out_mp4),
        "started_at": time.time(),
        "ready": ready_meta,
    }
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "mode": args.mode,
        "out": str(out_mp4),
        "worker_pid": proc.pid,
        **ready_meta,
    }


def cmd_stop(out_arg: Path, helper_bin: Path, run: Runner = subprocess.run) -> dict[str, Any]:
    out_mp4 = out_arg.expanduser().resolve()
    work_dir = sidecar_dir(out_mp4)
    state_file = work_dir / "state.json"
    if not state_file.is_file():
        if out_mp4.is_file():
            verified = cmd_verify(out_mp4, helper_bin, run=run)
            return {**verified, "already_stopped": True}
        raise RecordError("not-running", f"No active recording found for {out_mp4}")

    state = json.loads(state_file.read_text(encoding="utf-8"))
    worker_pid = int(state.get("worker_pid") or 0)
    (work_dir / "stop").touch()

    deadline = time.monotonic() + 45.0
    while is_pid_alive(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.15)

    if is_pid_alive(worker_pid):
        try:
            os.kill(worker_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        grace = time.monotonic() + 5.0
        while is_pid_alive(worker_pid) and time.monotonic() < grace:
            time.sleep(0.1)

    done_file = work_dir / "done.json"
    log_path = work_dir / "worker.log"
    if not out_mp4.is_file():
        raw = done_file.read_text(encoding="utf-8") if done_file.is_file() else (log_path.read_text(encoding="utf-8") if log_path.is_file() else "")
        parsed = parse_last_json(raw)
        raise RecordError(
            str(parsed.get("stage") or "stop-missing-output"),
            str(parsed.get("error") or raw.strip()[:400] or f"Recorder exited without writing {out_mp4}"),
        )

    verified = cmd_verify(out_mp4, helper_bin, run=run)
    extra: dict[str, Any] = {"mode": state.get("mode")}
    raw_info = done_file.read_text(encoding="utf-8") if done_file.is_file() else (log_path.read_text(encoding="utf-8") if log_path.is_file() else "")
    worker_info = parse_last_json(raw_info)
    if "frames" in worker_info:
        extra["frames"] = worker_info["frames"]
    shutil.rmtree(work_dir, ignore_errors=True)
    return {**verified, **extra, "out": str(out_mp4)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统一自动化测试录屏工具（ego-browser 网页 / Electron 单窗口）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_win = sub.add_parser("windows", help="按进程或应用名查询可录制的单窗口")
    p_win.add_argument("--pid", type=int, help="被测应用或其启动进程的 PID（自动展开子进程树）")
    p_win.add_argument("--app", help="应用名或窗口标题关键词")

    p_start = sub.add_parser("start", help="启动后台录屏，确认捕获到首帧后返回")
    p_start.add_argument("--mode", choices=("ego", "window"), required=True)
    p_start.add_argument("--out", type=Path, required=True, help="输出 .mp4 文件绝对路径（须在证据目录内）")
    p_start.add_argument("--space-id", type=int, help="ego 模式必填：ego-browser TaskSpace 数字 ID")
    p_start.add_argument("--page", help="ego 模式可选：固定录制某个页面标签（如 p1）；不填默认跟随当前活跃受管标签")
    p_start.add_argument("--pid", type=int, help="window 模式：Electron 客户端或启动命令 PID")
    p_start.add_argument("--window-id", type=int, help="window 模式：指定 CGWindowID")
    p_start.add_argument("--fps", type=float, help="帧率（ego 默认 4，window 默认 8）")
    p_start.add_argument("--max-duration", type=int, default=DEFAULT_MAX_DURATION_S, help="最长录制秒数上限（默认 300s 自动收尾）")
    p_start.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH, help="视频最大像素宽度（默认 1440）")
    p_start.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE, help="H.264 平均码率 bps（默认 600000）")

    p_stop = sub.add_parser("stop", help="停止后台录屏、合成并校验可播放性")
    p_stop.add_argument("--out", type=Path, required=True, help="start 时传入的同一个 .mp4 路径")

    p_verify = sub.add_parser("verify", help="校验 .mp4 是否存在、可解码、时长与尺寸有效")
    p_verify.add_argument("--file", type=Path, required=True)

    p_worker = sub.add_parser("_ego-worker")
    p_worker.add_argument("--helper-bin", type=Path, required=True)
    p_worker.add_argument("--work-dir", type=Path, required=True)
    p_worker.add_argument("--space-id", type=int, required=True)
    p_worker.add_argument("--page", default="")
    p_worker.add_argument("--out", type=Path, required=True)
    p_worker.add_argument("--fps", type=float, required=True)
    p_worker.add_argument("--max-duration", type=int, required=True)
    p_worker.add_argument("--max-width", type=int, required=True)
    p_worker.add_argument("--bitrate", type=int, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None, run: Runner = subprocess.run) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "_ego-worker":
        return run_ego_worker(args)

    try:
        # 先校验命令行参数与路径约束，避免参数错误时无谓触发 Swift 编译（同时兼容 Linux CI 单测）。
        if args.command == "windows" and not args.pid and not args.app:
            raise RecordError("args", "windows requires --pid <PID> or --app <keyword> so unrelated user windows are not listed")
        if args.command == "start":
            validate_out_path(args.out, run=run)
            if args.mode == "ego" and args.space_id is None:
                raise RecordError("args", "--space-id is required when --mode ego")
            if args.mode == "window" and not args.pid and not args.window_id:
                raise RecordError("args", "--pid or --window-id is required when --mode window")
        helper_bin = ensure_helper_binary()
        if args.command == "windows":
            result = cmd_windows(args, helper_bin, run=run)
        elif args.command == "start":
            result = cmd_start(args, helper_bin, run=run)
        elif args.command == "stop":
            result = cmd_stop(args.out, helper_bin, run=run)
        else:
            result = cmd_verify(args.file, helper_bin, run=run)
    except RecordError as error:
        print(json.dumps({"ok": False, "stage": error.stage, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
