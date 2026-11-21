"""聚焦验证统一录屏工具：仓库根目录护栏、子进程树展开、ego 采样脚本安全暂停逻辑与原生帧合成校验。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest

SPEC = importlib.util.spec_from_file_location(
    "test_record", Path(__file__).resolve().parents[1] / "scripts/test-record.py"
)
assert SPEC is not None and SPEC.loader is not None
TR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TR
SPEC.loader.exec_module(TR)


class TestRecordUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_validate_out_path_rejects_non_mp4_and_unignored_repo_paths(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / ".gitignore").write_text(".hwf/tmp/\n.tmp/\n", encoding="utf-8")

        with self.assertRaises(TR.RecordError) as e_ext:
            TR.validate_out_path(repo / ".hwf/tmp/t1/testing/r1/a.mov")
        self.assertEqual(e_ext.exception.stage, "out-path")

        # 严禁在仓库根或未忽略的 testing/ 顶层目录写录屏产物
        with self.assertRaises(TR.RecordError) as e_root:
            TR.validate_out_path(repo / "a.mp4")
        self.assertEqual(e_root.exception.stage, "out-path")

        with self.assertRaises(TR.RecordError) as e_unignored:
            TR.validate_out_path(repo / "testing/r1/a.mp4")
        self.assertEqual(e_unignored.exception.stage, "out-path")

        allowed = TR.validate_out_path(repo / ".hwf/tmp/t1/testing/r1/a.mp4")
        self.assertTrue(str(allowed).endswith(".hwf/tmp/t1/testing/r1/a.mp4"))

    def test_process_tree_pids_expands_children(self) -> None:
        def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            assert argv[:2] == ["ps", "-axo"]
            table = "  100   1\n  200 100\n  205 100\n  310 200\n  999   1\n"
            return subprocess.CompletedProcess(argv, 0, table, "")

        self.assertEqual(TR.process_tree_pids(100, run=fake_run), [100, 200, 205, 310])

    def test_windows_requires_pid_or_app_filter(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = TR.main(["windows"])
        res = json.loads(out.getvalue())
        self.assertEqual((code, res["ok"], res["stage"]), (1, False, "args"))

    def test_ego_sampler_js_uses_page_screenshot_and_skips_non_agent_ownership(self) -> None:
        js = TR.build_ego_sampler_js(
            space_id=12,
            page_label="",
            frames_dir=self.tmp / "frames",
            manifest_path=self.tmp / "manifest.jsonl",
            ready_path=self.tmp / "ready.json",
            stop_path=self.tmp / "stop",
            interval_ms=250,
            max_duration_ms=30_000,
        )
        # 必须走官方 page.screenshot 而非 page.cdp，且切人工接管时暂停抓帧
        self.assertIn("page.screenshot(", js)
        self.assertNotIn("page.cdp(", js)
        self.assertIn('spaceEntry.ownership !== "agent"', js)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc") is not None, "macOS AVFoundation encoder test")
    def test_encode_frames_and_verify_real_mp4(self) -> None:
        helper = TR.ensure_helper_binary()
        # 构造两张真实 320x200 PNG 帧，核验 encode-frames 合成与 AVFoundation 解码校验完整闭环
        frame_a = self.tmp / "f0.png"
        frame_b = self.tmp / "f1.png"
        import struct
        import zlib

        def make_png(rgb: tuple[int, int, int]) -> bytes:
            w, h = 320, 200
            raw_rows = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

            def chunk(tag: bytes, body: bytes) -> bytes:
                return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw_rows)) + chunk(b"IEND", b"")

        frame_a.write_bytes(make_png((30, 120, 220)))
        frame_b.write_bytes(make_png((40, 180, 90)))
        manifest = self.tmp / "manifest.jsonl"
        manifest.write_text(
            json.dumps({"path": str(frame_a), "ms": 0}) + "\n" + json.dumps({"path": str(frame_b), "ms": 600}) + "\n",
            encoding="utf-8",
        )
        out_mp4 = self.tmp / "encoded.mp4"
        enc = subprocess.run(
            [str(helper), "encode-frames", "--manifest", str(manifest), "--out", str(out_mp4)],
            capture_output=True,
            text=True,
            check=True,
        )
        enc_json = json.loads(enc.stdout.strip())
        self.assertTrue(enc_json["ok"])
        self.assertEqual((enc_json["width"], enc_json["height"]), (320, 200))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = TR.main(["verify", "--file", str(out_mp4)])
        ver = json.loads(out.getvalue())
        self.assertEqual(code, 0, ver)
        self.assertGreaterEqual(ver["duration_s"], 1.0)
        self.assertEqual((ver["width"], ver["height"]), (320, 200))


if __name__ == "__main__":
    unittest.main()
