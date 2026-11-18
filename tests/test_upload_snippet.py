"""聚焦验证 snippet 上传脚本：从 origin 定项目、JSON 带 Content-Type、回读可见性/文件名/内容不一致即失败、mark 先核对内容。"""

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
    "upload_snippet", Path(__file__).resolve().parents[1] / "scripts/upload-snippet.py"
)
assert SPEC is not None and SPEC.loader is not None
US = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = US
SPEC.loader.exec_module(US)

BASE = "projects/frontend%2Fweb-v1/snippets"


class FakeGitLab:
    """只模拟本脚本用到的 git remote 与 glab api 调用。"""

    def __init__(self, content: bytes, visibility: str = "internal", origin: str = "git@git.example.com:frontend/web-v1.git"):
        self.origin, self.content, self.visibility = origin, content, visibility
        self.title, self.file_name = "旧标题", "cases.md"
        self.calls: list[list[str]] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if argv[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(argv, 0, (self.origin + "\n").encode(), b"")
        assert argv[:4] == ["glab", "api", "--hostname", "git.example.com"], argv
        method, path = argv[5], argv[6]
        if "--input" in argv:
            assert argv[argv.index("-H") + 1] == "Content-Type: application/json"
            self.payloads.append(json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8")))
        if method == "POST" and path == BASE:
            payload = self.payloads[-1]
            self.title, self.file_name = payload["title"], payload["files"][0]["file_path"]
            return self.ok({"id": 7})
        if method == "PUT" and path == f"{BASE}/7":
            self.title = self.payloads[-1]["title"]
            return self.ok({"id": 7})
        if method == "GET" and path == f"{BASE}/7":
            return self.ok({"id": 7, "title": self.title, "visibility": self.visibility,
                            "web_url": "https://git.example.com/frontend/web-v1/-/snippets/7",
                            "files": [{"path": self.file_name}]})
        if method == "GET" and path == f"{BASE}/7/files/main/{self.file_name}/raw":
            return subprocess.CompletedProcess(argv, 0, self.content, b"")
        return subprocess.CompletedProcess(argv, 1, b"", f"unexpected {method} {path}".encode())

    @staticmethod
    def ok(body: dict[str, Any]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, json.dumps(body).encode(), b"")


class UploadSnippetTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "cases.md"
        self.data = "# 用例\n\n- A1 通过\n".encode()
        self.file.write_bytes(self.data)
        self.digest = hashlib.sha256(self.data).hexdigest()

    def main(self, fake: FakeGitLab, *args: str) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = US.main(list(args), run=fake)
        return code, json.loads(out.getvalue())

    def test_create_uses_origin_project_internal_and_verifies_content(self) -> None:
        fake = FakeGitLab(self.data)
        code, result = self.main(fake, "create", "--repo", "/repo", "--file", str(self.file),
                                 "--title", "487 测试用例（已评审）", "--description", "https://x/issues/487")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["id"], result["sha256"], result["visibility"]), (7, self.digest, "internal"))
        payload = fake.payloads[0]
        self.assertEqual(payload["visibility"], "internal")
        self.assertEqual(payload["files"], [{"file_path": "cases.md", "content": self.data.decode()}])
        self.assertIn(f"SHA-256 {self.digest}", payload["description"])

    def test_content_mismatch_fails_at_readback(self) -> None:
        fake = FakeGitLab(self.data + b"\n")
        code, result = self.main(fake, "create", "--repo", "/repo", "--file", str(self.file),
                                 "--title", "t", "--description", "d")
        self.assertEqual((code, result["ok"], result["stage"]), (1, False, "readback"))
        self.assertIn("SHA-256", result["error"])

    def test_non_internal_visibility_fails(self) -> None:
        fake = FakeGitLab(self.data, visibility="public")
        code, result = self.main(fake, "check", "--repo", "/repo", "--id", "7", "--file", str(self.file))
        self.assertEqual((code, result["stage"]), (1, "readback"))
        self.assertIn("public", result["error"])

    def test_mark_refuses_when_online_content_differs(self) -> None:
        fake = FakeGitLab(b"other version\n")
        code, result = self.main(fake, "mark", "--repo", "/repo", "--id", "7", "--file", str(self.file),
                                 "--title", "新标题", "--description", "已确认")
        self.assertEqual(code, 1, result)
        self.assertFalse(any(call[5] == "PUT" for call in fake.calls if call[0] == "glab"))

    def test_mark_updates_title_and_reads_back(self) -> None:
        fake = FakeGitLab(self.data)
        code, result = self.main(fake, "mark", "--repo", "/repo", "--id", "7", "--file", str(self.file),
                                 "--title", "487 最终技术方案 r2（已确认）", "--description", "确认时间 2026-11-18")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["title"], "487 最终技术方案 r2（已确认）")
        self.assertEqual(set(fake.payloads[0]), {"title", "description"})  # 不改内容

    def test_glab_failure_reports_stage(self) -> None:
        fake = FakeGitLab(self.data, origin="not-a-remote")
        code, result = self.main(fake, "check", "--repo", "/repo", "--id", "7", "--file", str(self.file))
        self.assertEqual((code, result["stage"]), (1, "origin"))


if __name__ == "__main__":
    unittest.main()
