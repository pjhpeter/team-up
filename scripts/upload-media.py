#!/usr/bin/env python3
"""把自动化测试录屏（.mp4）或补充截图（.png/.jpg/.webp）上传为 GitLab 项目附件，并回读校验 SHA-256。

    python3 ~/.hwf/scripts/upload-media.py create --repo <仓库路径> --file <媒体文件> [--alt "A1-A3 验收录屏"]
    python3 ~/.hwf/scripts/upload-media.py check  --repo <仓库路径> --url </uploads/<secret>/<文件名> 或完整 URL> [--file <本地文件>]

为什么有它：
- GitLab issue 评论直接内嵌 `![说明](/uploads/<secret>/xxx.mp4)` 即可渲染原生 <video> 播放器，但此前只有上传文本代码片段的
  `upload-snippet.py`，没有媒体附件上传与回读核验入口，导致成员录好的视频只留在本地 `.hwf/tmp` 目录里。
- 拒绝裸 `.mov` 或超限大文件直接上传，统一要求先用 `test-record.py` 产出轻量 H.264 `.mp4`，避免上百 MB 原片卡死评论加载。
- 上传后立即通过 `GET projects/:id/uploads/:secret/:filename` 回读字节流比对 SHA-256，不一致即判失败（退出码 1）。

成功时 stdout 输出一行 JSON（ok、url、web_url、markdown、file_name、bytes、size_mb、sha256）；
失败时 stdout 输出一行 JSON（ok=false、stage、error），退出码 1；参数错误退出码 2。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote, urlparse

ALLOWED_EXTENSIONS = frozenset({".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif"})
DEFAULT_MAX_MB = 50.0
UPLOAD_PATH_RE = re.compile(r"(/uploads/[0-9a-fA-F]+/[^/?#\s)\"']+)")

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _load_init_project() -> Any:
    spec = importlib.util.spec_from_file_location("hwf_init_project", Path(__file__).resolve().parent / "init-project.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MediaUploadError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_upload_path(raw_url: str) -> str:
    """从相对路径 `/uploads/<secret>/<file>` 或完整 GitLab URL 中提取标准 `/uploads/...` 路径。"""
    candidate = raw_url.strip()
    if candidate.startswith(("http://", "https://")):
        candidate = urlparse(candidate).path
    match = UPLOAD_PATH_RE.search(candidate)
    if not match:
        raise MediaUploadError("url", f"Invalid GitLab upload URL or path: {raw_url}")
    return match.group(1)


def sanitize_alt(raw_alt: str) -> str:
    cleaned = " ".join(raw_alt.replace("[", "(").replace("]", ")").split()).strip()
    return cleaned or "evidence"


def validate_local_media(file: Path, max_mb: float) -> bytes:
    if not file.is_file():
        raise MediaUploadError("file", f"Media file does not exist: {file}")
    ext = file.suffix.lower()
    if ext == ".mov":
        raise MediaUploadError(
            "format",
            f"Raw .mov is not allowed ({file.name}); record H.264 .mp4 via ~/.hwf/scripts/test-record.py",
        )
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise MediaUploadError("format", f"Unsupported media extension {ext!r} ({file.name}); allowed: {allowed}")
    data = file.read_bytes()
    if not data:
        raise MediaUploadError("file", f"Media file is empty: {file}")
    max_bytes = int(max_mb * 1024 * 1024)
    if len(data) > max_bytes:
        actual_mb = round(len(data) / (1024 * 1024), 2)
        raise MediaUploadError("size", f"Media file is {actual_mb} MB, exceeding limit {max_mb} MB: {file.name}")
    # MP4 盒结构前 32 字节内必须带 ftyp 签名，防止未正常收尾的半截视频上传到 issue。
    if ext == ".mp4" and b"ftyp" not in data[:32]:
        raise MediaUploadError("corrupt", f"Invalid MP4 header (missing ftyp box): {file.name}")
    return data


class MediaUploader:
    def __init__(self, repo: Path, run: Runner = subprocess.run) -> None:
        self.run = run
        result = run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise MediaUploadError("origin", f"Failed to read git origin from {repo}: {detail}")
        try:
            self.host, self.project = _load_init_project().parse_remote(result.stdout.decode().strip())
        except Exception as exc:
            raise MediaUploadError("origin", str(exc)) from exc
        self.project_api = f"projects/{quote(self.project, safe='')}"

    def _glab(self, stage: str, method: str, endpoint: str, extra_args: Sequence[str] = (), timeout: int = 180) -> bytes:
        argv = ["glab", "api", "--hostname", self.host, "-X", method, endpoint, *extra_args]
        result = self.run(argv, capture_output=True, check=False, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise MediaUploadError(stage, f"glab api {method} {endpoint} failed: {detail[:400]}")
        return result.stdout

    def create(self, file: Path, alt: Optional[str], max_mb: float) -> dict[str, Any]:
        resolved = file.expanduser().resolve()
        data = validate_local_media(resolved, max_mb=max_mb)
        digest = sha256(data)

        with tempfile.TemporaryDirectory(prefix="hwf-upload-") as tmp:
            upload_target = resolved
            # glab --form 遇到含逗号的路径可能误切分字段，必要时在临时目录建同名副本再传。
            if any(ch in str(resolved) for ch in (",", ";", "\n")):
                upload_target = Path(tmp) / resolved.name
                shutil.copyfile(resolved, upload_target)
            raw = self._glab("upload", "POST", f"{self.project_api}/uploads", [f"--form=file=@{upload_target}"])

        try:
            meta = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise MediaUploadError("upload", f"Non-JSON upload response: {raw[:200]!r}") from exc

        rel_url = extract_upload_path(str(meta.get("url") or ""))
        full_path = str(meta.get("full_path") or f"/{self.project}{rel_url}")
        label = sanitize_alt(alt if alt is not None else resolved.stem)
        verified = self.verify(rel_url, expected_sha256=digest, full_path=full_path)
        return {
            **verified,
            "file_name": resolved.name,
            "alt": label,
            "markdown": f"![{label}]({rel_url})",
        }

    def verify(self, raw_url: str, expected_sha256: Optional[str] = None, full_path: Optional[str] = None) -> dict[str, Any]:
        rel_url = extract_upload_path(raw_url)
        # 路径段逐段 URL 编码以兼容中文文件名，同时保留 `/uploads/<secret>/<name>` 斜杠结构。
        encoded_tail = "/".join(quote(seg, safe="") for seg in rel_url.lstrip("/").split("/"))
        raw = self._glab("readback", "GET", f"{self.project_api}/{encoded_tail}")
        if not raw:
            raise MediaUploadError("readback", f"Uploaded media readback is empty: {rel_url}")
        online_digest = sha256(raw)
        if expected_sha256 is not None and online_digest != expected_sha256:
            raise MediaUploadError(
                "readback",
                f"SHA-256 mismatch for {rel_url}: online {online_digest} != local {expected_sha256}",
            )
        resolved_full = full_path or f"/{self.project}{rel_url}"
        return {
            "ok": True,
            "url": rel_url,
            "web_url": f"https://{self.host}{resolved_full}",
            "bytes": len(raw),
            "size_mb": round(len(raw) / (1024 * 1024), 2),
            "sha256": online_digest,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="上传测试录屏/截图到 GitLab 项目附件并回读核验")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="上传本地媒体文件并回读校验 SHA-256")
    p_create.add_argument("--repo", required=True, type=Path, help="本地仓库路径，用 origin 确定 GitLab 项目")
    p_create.add_argument("--file", required=True, type=Path, help="本地 .mp4 / .png 等媒体文件路径")
    p_create.add_argument("--alt", help="Markdown 内嵌说明文字（默认取文件名）")
    p_create.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB, help=f"单文件体积上限 MB（默认 {DEFAULT_MAX_MB:g}）")

    p_check = sub.add_parser("check", help="回读校验线上附件链接可访问，可选与本地文件核对 SHA-256")
    p_check.add_argument("--repo", required=True, type=Path, help="本地仓库路径，用 origin 确定 GitLab 项目")
    p_check.add_argument("--url", required=True, help="`/uploads/<secret>/<file>` 或完整 URL")
    p_check.add_argument("--file", type=Path, help="可选：本地原文件，传入时校验线上与本地 SHA-256 一致")
    return parser


def main(argv: Optional[Sequence[str]] = None, run: Runner = subprocess.run) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        uploader = MediaUploader(args.repo, run=run)
        if args.command == "create":
            result = uploader.create(args.file, alt=args.alt, max_mb=args.max_mb)
        else:
            expected = None
            if args.file is not None:
                if not args.file.is_file():
                    raise MediaUploadError("file", f"Local file does not exist: {args.file}")
                expected = sha256(args.file.read_bytes())
            result = uploader.verify(args.url, expected_sha256=expected)
    except MediaUploadError as exc:
        print(json.dumps({"ok": False, "stage": exc.stage, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
