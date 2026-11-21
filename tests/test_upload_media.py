"""聚焦验证媒体附件上传脚本：从 origin 定项目、拦截裸 .mov 与损坏 MP4、通过 glab --form 上传并回读核对 SHA-256。"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

SPEC = importlib.util.spec_from_file_location(
    "upload_media", Path(__file__).resolve().parents[1] / "scripts/upload-media.py"
)
assert SPEC is not None and SPEC.loader is not None
UM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = UM
SPEC.loader.exec_module(UM)

PROJECT_API = "projects/frontend%2Fweb-v1"
UPLOAD_ID = "a" * 32


class FakeGitLabMedia:
    def __init__(self, remote_bytes: bytes, origin: str = "git@git.example.com:frontend/web-v1.git") -> None:
        self.origin = origin
        self.remote_bytes = remote_bytes
        self.calls: list[list[str]] = []
        self.uploaded_name = ""

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if argv[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(argv, 0, (self.origin + "\n").encode(), b"")
        assert argv[:4] == ["glab", "api", "--hostname", "git.example.com"], argv
        method, endpoint = argv[5], argv[6]
        if method == "POST" and endpoint == f"{PROJECT_API}/uploads":
            form_arg = next((a for a in argv[7:] if a.startswith("--form=file=@")), "")
            assert form_arg, argv
            uploaded_path = Path(form_arg.split("@", 1)[1])
            assert uploaded_path.is_file()
            self.uploaded_name = uploaded_path.name
            rel_url = f"/uploads/{UPLOAD_ID}/{self.uploaded_name}"
            resp = {
                "id": 42,
                "alt": uploaded_path.stem,
                "url": rel_url,
                "full_path": f"/frontend/web-v1{rel_url}",
                "markdown": f"![{uploaded_path.stem}]({rel_url})",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(resp).encode(), b"")
        if method == "GET" and endpoint.startswith(f"{PROJECT_API}/uploads/{UPLOAD_ID}/"):
            return subprocess.CompletedProcess(argv, 0, self.remote_bytes, b"")
        return subprocess.CompletedProcess(argv, 1, b"", f"unexpected {method} {endpoint}".encode())


class UploadMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.mp4 = self.tmp / "A1-flow.mp4"
        # 最小合法 ftyp 盒头签名
        self.mp4_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"video-payload"
        self.mp4.write_bytes(self.mp4_bytes)
        self.mp4_sha = hashlib.sha256(self.mp4_bytes).hexdigest()

    def run_cli(self, fake: FakeGitLabMedia, *args: str) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = UM.main(list(args), run=fake)
        return code, json.loads(out.getvalue())

    def test_create_mp4_uploads_via_glab_form_and_verifies_sha256(self) -> None:
        fake = FakeGitLabMedia(self.mp4_bytes)
        code, res = self.run_cli(
            fake, "create", "--repo", "/repo", "--file", str(self.mp4), "--alt", "A1 登录与创建录屏"
        )
        self.assertEqual(code, 0, res)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sha256"], self.mp4_sha)
        self.assertEqual(res["url"], f"/uploads/{UPLOAD_ID}/A1-flow.mp4")
        self.assertEqual(res["web_url"], f"https://git.example.com/frontend/web-v1/uploads/{UPLOAD_ID}/A1-flow.mp4")
        self.assertEqual(res["markdown"], f"![A1 登录与创建录屏](/uploads/{UPLOAD_ID}/A1-flow.mp4)")

    def test_rejects_raw_mov_with_actionable_hint(self) -> None:
        mov = self.tmp / "raw.mov"
        mov.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"heavy")
        fake = FakeGitLabMedia(b"")
        code, res = self.run_cli(fake, "create", "--repo", "/repo", "--file", str(mov))
        self.assertEqual((code, res["ok"], res["stage"]), (1, False, "format"))
        self.assertIn("test-record.py", res["error"])
        self.assertEqual(len([c for c in fake.calls if c[0] == "glab"]), 0)

    def test_rejects_corrupt_mp4_missing_ftyp(self) -> None:
        bad = self.tmp / "corrupt.mp4"
        bad.write_bytes(b"not-an-mp4-header-at-all-1234567890")
        fake = FakeGitLabMedia(b"")
        code, res = self.run_cli(fake, "create", "--repo", "/repo", "--file", str(bad))
        self.assertEqual((code, res["stage"]), (1, "corrupt"))

    def test_readback_sha256_mismatch_fails(self) -> None:
        fake = FakeGitLabMedia(self.mp4_bytes + b"tampered")
        code, res = self.run_cli(fake, "create", "--repo", "/repo", "--file", str(self.mp4))
        self.assertEqual((code, res["stage"]), (1, "readback"))
        self.assertIn("SHA-256", res["error"])

    def test_check_accepts_full_web_url_and_optional_local_file(self) -> None:
        fake = FakeGitLabMedia(self.mp4_bytes)
        full_url = f"https://git.example.com/frontend/web-v1/uploads/{UPLOAD_ID}/A1-flow.mp4"
        code, res = self.run_cli(fake, "check", "--repo", "/repo", "--url", full_url, "--file", str(self.mp4))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["sha256"], self.mp4_sha)
        self.assertEqual(res["url"], f"/uploads/{UPLOAD_ID}/A1-flow.mp4")


if __name__ == "__main__":
    unittest.main()
