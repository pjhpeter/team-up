"""聚焦验证测试浏览器准备脚本：每次新建空目录、按项目配置登录、缺配置/缺脚本时以测试分支为准、失败说清卡在哪。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "test_browser", Path(__file__).resolve().parents[1] / "scripts/test-browser.py"
)
assert SPEC is not None and SPEC.loader is not None
TB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TB
SPEC.loader.exec_module(TB)

LOGIN = ["node", "scripts/test/login.mjs", "--url", "{url}", "--account", "{account}", "--profile", "{profile}"]
CONFIG = "branches:\n  test: develop\ntest_browser:\n  login: " + json.dumps(LOGIN) + "\n"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        self.wt = self.root / "wt"
        self.out = self.root / "out"
        (self.repo / "docs/agents").mkdir(parents=True)
        (self.wt / "scripts/test").mkdir(parents=True)
        (self.wt / "scripts/test/login.mjs").write_text("// branch copy", encoding="utf-8")
        self.remote: dict[str, str] = {}
        self.runs: list[tuple[list[str], Path]] = []
        self.login_stdout = '{"ok": true, "screenshot": "/s.png"}'
        self.login_code = 0
        for target, value in (("main_repo", lambda wt: self.repo), ("from_test_branch", self.fake_remote)):
            patcher = patch.object(TB, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(TB.subprocess, "run", self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(TB.os.environ, {"CHROME_PATH": "/chrome"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_remote(self, worktree: Path, branch: str, rel: str):
        return self.remote.get(f"{branch}:{rel}")

    def fake_run(self, argv, cwd=None, **kw):
        self.runs.append((list(argv), Path(cwd)))
        return subprocess.CompletedProcess(argv, self.login_code, self.login_stdout, "")

    def write_config(self, text: str = CONFIG) -> None:
        (self.repo / TB.PROJECT_FILE).write_text(text, encoding="utf-8")

    def ready(self) -> dict:
        return TB.ready(self.wt, "http://localhost:3469", "test-102", self.out)


class ReadyTest(Case):
    def test_fresh_profile_login_and_open_args(self) -> None:
        self.write_config()
        stale = self.out / "browser-profile/Default"
        stale.mkdir(parents=True)
        (stale / "Local Storage").write_text("old account", encoding="utf-8")
        out = self.ready()
        profile = self.out / "browser-profile"
        self.assertFalse((profile / "Default").exists())  # 旧目录整个删掉重建
        (argv, cwd), = self.runs
        self.assertEqual(cwd, self.wt)
        self.assertEqual(argv, ["node", "scripts/test/login.mjs", "--url", "http://localhost:3469", "--account",
                                "test-102", "--profile", str(profile)])
        self.assertEqual(out["open"], {"name": "test", "url": "http://localhost:3469", "headed": True,
                                       "app": {"path": "/chrome", "args": [f"--user-data-dir={profile}",
                                                                           *TB.CHROME_ARGS]}})
        self.assertEqual(out["login_screenshot"], "/s.png")

    def test_config_and_script_fall_back_to_test_branch(self) -> None:
        self.write_config("branches:\n  test: develop\n")
        self.remote["develop:" + TB.PROJECT_FILE] = CONFIG
        self.remote["develop:scripts/test/login.mjs"] = "// develop copy"
        (self.wt / "scripts/test/login.mjs").unlink()
        self.ready()
        fetched = self.wt / "node_modules/.cache/hwf/login.mjs"
        self.assertEqual(fetched.read_text(encoding="utf-8"), "// develop copy")
        self.assertEqual(self.runs[0][0][1], str(fetched))

    def test_missing_everywhere_is_config_error(self) -> None:
        self.write_config("branches:\n  test: develop\n")
        with self.assertRaises(TB.ReadyError) as caught:
            self.ready()
        self.assertEqual(caught.exception.stage, "config")
        self.assertFalse(self.runs)

    def test_missing_script_without_test_branch(self) -> None:
        self.write_config("test_browser:\n  login: " + json.dumps(LOGIN) + "\n")
        (self.wt / "scripts/test/login.mjs").unlink()
        with self.assertRaises(TB.ReadyError) as caught:
            self.ready()
        self.assertEqual(caught.exception.stage, "login-script")

    def test_login_failure_reports_stage_and_exit_1(self) -> None:
        self.write_config()
        self.login_code = 1
        self.login_stdout = 'noise\n{"ok": false, "stage": "fill-form", "message": "找不到输入框"}'
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = TB.main([str(self.wt), "--url", "http://x", "--account", "a", "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buf.getvalue()),
                         {"ok": False, "stage": "login:fill-form", "message": "找不到输入框"})


if __name__ == "__main__":
    unittest.main()
