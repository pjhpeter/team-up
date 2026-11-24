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
        env = kw.get("env") or {}
        cred_file = env.get("HWF_TEST_CREDENTIAL_FILE")
        self.last_env = dict(env)
        self.last_input = kw.get("input")
        self.cred_existed_during_run = bool(cred_file and Path(cred_file).exists())
        self.runs.append((list(argv), Path(cwd) if cwd else self.wt))
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

    def test_account_from_env_masks_output_and_cleans_credential_file_and_scrubs_errors(self) -> None:
        self.write_config()
        secrets_md = self.root / "accounts.md"
        secrets_md.write_text(
            "| 账号 | 密码 |\n|---|---|\n| `pengjiahui@lookai.cc` | `Sup3rS3cret!88` |\n",
            encoding="utf-8",
        )
        buf = io.StringIO()
        with (
            patch.dict(TB.os.environ, {
                "HWF_TEST_ACCOUNT": "pengjiahui@lookai.cc",
                "HWF_TEST_ACCOUNTS_FILE": str(secrets_md),
            }),
            contextlib.redirect_stdout(buf),
        ):
            code = TB.main([str(self.wt), "--url", "http://localhost:3469", "--out", str(self.out)])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["account"], "pe***@lookai.cc")
        self.assertNotIn("pengjiahui@lookai.cc", buf.getvalue())
        self.assertNotIn("Sup3rS3cret!88", buf.getvalue())
        self.assertTrue(self.cred_existed_during_run)
        self.assertFalse(Path(self.last_env["HWF_TEST_CREDENTIAL_FILE"]).exists())

        # 登录报错里若意外回显了明文密码和完整账号，输出必须自动脱敏。
        self.login_code = 1
        self.login_stdout = '{"ok": false, "stage": "auth", "message": "pengjiahui@lookai.cc bad pass Sup3rS3cret!88"}'
        err_buf = io.StringIO()
        with (
            patch.dict(TB.os.environ, {
                "HWF_TEST_ACCOUNT": "pengjiahui@lookai.cc",
                "HWF_TEST_ACCOUNTS_FILE": str(secrets_md),
            }),
            contextlib.redirect_stdout(err_buf),
        ):
            code2 = TB.main([str(self.wt), "--url", "http://localhost:3469", "--out", str(self.out)])
        self.assertEqual(code2, 1)
        self.assertNotIn("Sup3rS3cret!88", err_buf.getvalue())
        self.assertNotIn("pengjiahui@lookai.cc", err_buf.getvalue())
        self.assertIn("pe***@lookai.cc", err_buf.getvalue())

    def test_ego_mode_logins_in_isolated_space_and_blocks_clear_origin_on_prod(self) -> None:
        secrets_md = self.root / "accounts.md"
        secrets_md.write_text("| `13812345678` | `Pass#2026` |\n", encoding="utf-8")
        self.login_stdout = '{"ok": true, "stage": "done", "tab_id": 7, "final_url": "https://test.lookai.cc/home"}'
        with patch.dict(TB.os.environ, {"HWF_TEST_ACCOUNTS_FILE": str(secrets_md)}):
            out = TB.ready(
                self.wt, "https://test.lookai.cc/login", "13812345678", self.out,
                mode="ego", space_name="team-test", clear_origin=True,
            )
        self.assertEqual(out["mode"], "ego")
        self.assertEqual(out["account"], "13***78")
        self.assertEqual(out["open"]["space_name"], "team-test")
        self.assertEqual(self.runs[-1][0], ["ego-browser", "nodejs"])
        self.assertNotIn("Pass#2026", json.dumps(out))

        # 生产域名严禁传 --clear-origin。
        with patch.dict(TB.os.environ, {"HWF_TEST_ACCOUNTS_FILE": str(secrets_md)}):
            with self.assertRaises(TB.ReadyError) as caught:
                TB.ready(
                    self.wt, "https://app.lookai.cc/home", "13812345678", self.out,
                    mode="ego", space_name="team-test", clear_origin=True,
                )
        self.assertEqual(caught.exception.stage, "clear-origin")


if __name__ == "__main__":
    unittest.main()
