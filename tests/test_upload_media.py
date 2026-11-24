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
        if method == "GET" and endpoint == PROJECT_API:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": 10, "path_with_namespace": "frontend/web-v1"}).encode(), b""
            )
        if method == "POST" and endpoint == f"{PROJECT_API}/uploads":
            form_arg = next((a for a in argv[7:] if a.startswith("--form=file=@")), "")
            assert form_arg, argv
            uploaded_path = Path(form_arg.split("@", 1)[1])
            assert uploaded_path.is_file()
            self.uploaded_name = uploaded_path.name
            rel_url = f"/uploads/{UPLOAD_ID}/{self.uploaded_name}"
            # 模拟旧式 /frontend/web-v1/uploads/... 返回，验证脚本自动纠偏为 /-/project/10/uploads/...
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
        self.assertEqual(res["web_url"], f"https://git.example.com/-/project/10/uploads/{UPLOAD_ID}/A1-flow.mp4")
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

    def test_check_accepts_full_web_url_and_rewrites_project_upload_web_url(self) -> None:
        fake = FakeGitLabMedia(self.mp4_bytes)
        # 无论传入旧式 /frontend/web-v1/uploads/... 还是相对路径，check 都回读成功并输出有效 /-/project/10/uploads/... 直链
        legacy_url = f"https://git.example.com/frontend/web-v1/uploads/{UPLOAD_ID}/A1-flow.mp4"
        code, res = self.run_cli(fake, "check", "--repo", "/repo", "--url", legacy_url, "--file", str(self.mp4))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["sha256"], self.mp4_sha)
        self.assertEqual(res["url"], f"/uploads/{UPLOAD_ID}/A1-flow.mp4")
        self.assertEqual(res["web_url"], f"https://git.example.com/-/project/10/uploads/{UPLOAD_ID}/A1-flow.mp4")

    def test_jev_evidence_guard_blocks_blank_and_exposed_credentials_unless_error_alt(self) -> None:
        png = self.tmp / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        fake = FakeGitLabMedia(png.read_bytes())

        class FakeMediaJev:
            def __init__(self, blank: float, cred: float) -> None:
                self.blank, self.cred = blank, cred

            def decide(self, **_: Any) -> Any:
                return UM.JC.JevResult.from_payload({
                    "model": "jev-1.14.0",
                    "answers": {
                        "blank_or_crash": {"type": "noul", "noul": self.blank},
                        "credential_exposed": {"type": "noul", "noul": self.cred},
                    },
                })

        uploader_cred = UM.MediaUploader(Path("/repo"), run=fake, jev=FakeMediaJev(blank=0.05, cred=0.91))
        with self.assertRaises(UM.MediaUploadError) as ctx_cred:
            uploader_cred.create(png, alt="登录页面截图", max_mb=10.0)
        self.assertEqual(ctx_cred.exception.stage, "evidence_guard")
        self.assertIn("明文密码或密钥", str(ctx_cred.exception))

        uploader_blank = UM.MediaUploader(Path("/repo"), run=fake, jev=FakeMediaJev(blank=0.95, cred=0.01))
        with self.assertRaises(UM.MediaUploadError) as ctx_blank:
            uploader_blank.create(png, alt="首页正常渲染", max_mb=10.0)
        self.assertEqual(ctx_blank.exception.stage, "evidence_guard")
        self.assertIn("空白页", str(ctx_blank.exception))

        # 验收条目本身测报错/空态且在 alt 中注明时，不误拦空白/错误页
        res_allowed = uploader_blank.create(png, alt="AC3 接口异常空态兜底验证", max_mb=10.0)
        self.assertTrue(res_allowed["ok"])


if __name__ == "__main__":
    unittest.main()
