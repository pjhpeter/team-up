#!/usr/bin/env python3
"""agy 调度者守卫（PreToolUse / Stop / install-hooks）与 reset-session.py agy 分支单元测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HWF_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = HWF_ROOT / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


GUARD = _load("hwf_agy_guard_test", "agy-scheduler-guard.py")
RESET = _load("hwf_reset_session_test", "reset-session.py")
START = _load("hwf_start_scheduler_test", "start-scheduler.py")


class AgyPreToolUseTests(unittest.TestCase):
    def test_blocks_ask_question_and_subagents(self) -> None:
        self.assertIsNotNone(GUARD.pre_tool_violation("ask_question", {"question": "选哪个？"}))
        for tool in ("invoke_subagent", "define_subagent", "browser_subagent", "manage_subagents", "run_workflow"):
            self.assertIsNotNone(GUARD.pre_tool_violation(tool, {}), f"{tool} should be denied")

    def test_blocks_record_guard_shell_and_file_violations(self) -> None:
        self.assertIsNotNone(GUARD.pre_tool_violation("run_command", {"CommandLine": "echo x >> .hwf/tmp/INDEX.md"}))
        self.assertIsNotNone(GUARD.pre_tool_violation("run_command", {"CommandLine": "glab issue note 42 -m hi"}))
        self.assertIsNotNone(
            GUARD.pre_tool_violation("run_command", {"CommandLine": "glab api projects/1/issues/2/notes -X POST -f body=hi"})
        )
        self.assertIsNotNone(GUARD.pre_tool_violation("run_command", {"CommandLine": "herdr agent wait my-dev"}))
        long_prompt = "herdr agent prompt my-dev " + ("x" * 2600)
        self.assertIsNotNone(GUARD.pre_tool_violation("run_command", {"CommandLine": long_prompt}))
        self.assertIsNotNone(
            GUARD.pre_tool_violation("write_to_file", {"TargetFile": "/repo/.hwf/tmp/INDEX.md"})
        )

    def test_allows_safe_commands_and_scripts(self) -> None:
        self.assertIsNone(
            GUARD.pre_tool_violation(
                "run_command",
                {"CommandLine": "python3 ~/.hwf/scripts/task-record.py note --kind todo --body-file /tmp/a.md"},
            )
        )
        self.assertIsNone(
            GUARD.pre_tool_violation(
                "run_command",
                {"CommandLine": "python3 ~/.hwf/scripts/member-wait.py dev-1 --repo /repo --task #1"},
            )
        )
        self.assertIsNone(GUARD.pre_tool_violation("run_command", {"CommandLine": "glab issue view 42"}))
        self.assertIsNone(GUARD.pre_tool_violation("write_to_file", {"TargetFile": "/repo/.hwf/tmp/t1/dispatch-dev.md"}))

    def test_non_scheduler_or_non_herdr_always_allowed(self) -> None:
        raw = json.dumps({"toolCall": {"name": "ask_question", "args": {}}})
        # 非 Herdr 环境毫秒放行
        self.assertEqual(GUARD.handle_pre_tool_use(raw, env={}), {"decision": "allow"})
        # Herdr 里非调度者（如 arch）放行
        with patch.object(GUARD, "_self_scheduler_name", return_value=""):
            self.assertEqual(
                GUARD.handle_pre_tool_use(raw, env={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:t1:p1"}),
                {"decision": "allow"},
            )
        # Herdr 里的 delivery-scheduler-* 被拦截
        with patch.object(GUARD, "_self_scheduler_name", return_value="delivery-scheduler-w1-t1-p1"):
            res = GUARD.handle_pre_tool_use(raw, env={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:t1:p1"})
            self.assertEqual(res["decision"], "deny")
            self.assertIn("交互提问框", res["reason"])


class AgyStopGuardTests(unittest.TestCase):
    def test_blocks_unwatched_active_member_when_fully_idle(self) -> None:
        agents = [
            {"pane_id": "p-sched", "name": "delivery-scheduler-w1", "agent_status": "working"},
            {"pane_id": "p-dev", "name": "demo-1-dev", "agent_status": "working"},
            {"pane_id": "p-other", "name": "other-dev", "agent_status": "working"},
        ]
        registry = {
            "p-dev": {"scheduler": "delivery-scheduler-w1", "profile": "dev"},
            "p-other": {"scheduler": "delivery-scheduler-other", "profile": "dev"},
        }
        reason, sig = GUARD.evaluate_stop(
            {"fullyIdle": True, "continuationActive": False, "terminationReason": "NO_TOOL_CALL"},
            agents=agents,
            self_pane="p-sched",
            self_scheduler="delivery-scheduler-w1",
            registry=registry,
            repo_dir="",
        )
        self.assertIsNotNone(reason)
        self.assertIn("demo-1-dev", reason or "")
        self.assertNotIn("other-dev", reason or "")
        self.assertEqual(sig, "unwatched:demo-1-dev:working")

        # 若有后台等待任务（fullyIdle=False），允许结束本轮等待唤醒
        reason_bg, _ = GUARD.evaluate_stop(
            {"fullyIdle": False, "continuationActive": False, "terminationReason": "NO_TOOL_CALL"},
            agents=agents,
            self_pane="p-sched",
            self_scheduler="delivery-scheduler-w1",
            registry=registry,
            repo_dir="",
        )
        self.assertIsNone(reason_bg)

    def test_blocks_silent_stall_when_in_progress_and_no_user_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".hwf/tmp/demo-42").mkdir(parents=True)
            (repo / ".hwf/tmp/INDEX.md").write_text(
                "| 时间 | 任务标识 | 输入 | 负责人 | 状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 2026-11-20 | `demo-42` | #42 | `delivery-scheduler-w1` | `in-progress` |\n",
                encoding="utf-8",
            )
            (repo / ".hwf/tmp/demo-42/STATE.md").write_text(
                "<!-- hwf-state-top -->\n"
                "- 当前阶段：开发中\n"
                "- 待用户事项：无\n"
                "- 下一动作：等开发完成后代码评审\n"
                "<!-- /hwf-state-top -->\n",
                encoding="utf-8",
            )
            reason, sig = GUARD.evaluate_stop(
                {"fullyIdle": True, "continuationActive": False, "terminationReason": "NO_TOOL_CALL"},
                agents=[{"pane_id": "p-sched", "name": "delivery-scheduler-w1", "agent_status": "working"}],
                self_pane="p-sched",
                self_scheduler="delivery-scheduler-w1",
                registry={},
                repo_dir=str(repo),
            )
            self.assertIsNotNone(reason)
            self.assertEqual(sig, "stall:demo-42")

            # 更新为有待用户事项后允许停止
            (repo / ".hwf/tmp/demo-42/STATE.md").write_text(
                "<!-- hwf-state-top -->\n"
                "- 当前阶段：待用户确认验收清单\n"
                "- 待用户事项：确认验收清单后回复 /go\n"
                "<!-- /hwf-state-top -->\n",
                encoding="utf-8",
            )
            reason2, _ = GUARD.evaluate_stop(
                {"fullyIdle": True, "continuationActive": False, "terminationReason": "NO_TOOL_CALL"},
                agents=[{"pane_id": "p-sched", "name": "delivery-scheduler-w1", "agent_status": "working"}],
                self_pane="p-sched",
                self_scheduler="delivery-scheduler-w1",
                registry={},
                repo_dir=str(repo),
            )
            self.assertIsNone(reason2)

    def test_install_hooks_preserves_existing_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_file = Path(tmp) / "hooks.json"
            hooks_file.write_text(
                json.dumps({"shared-memory": {"Stop": [{"type": "command", "command": "echo keep"}]}}),
                encoding="utf-8",
            )
            GUARD.install_hooks(hooks_file, script_path=Path("/opt/hwf/scripts/agy-scheduler-guard.py"))
            saved = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertIn("shared-memory", saved)
            self.assertIn("hwf-scheduler", saved)
            pre_group = saved["hwf-scheduler"]["PreToolUse"][0]
            self.assertIn("run_command", pre_group["matcher"])
            self.assertIn("ask_question", pre_group["matcher"])
            self.assertEqual(pre_group["hooks"][0]["type"], "command")
            self.assertIn("pre-tool-use", pre_group["hooks"][0]["command"])
            self.assertIn("stop", saved["hwf-scheduler"]["Stop"][0]["command"])


class ResetAndStartSchedulerAgyTests(unittest.TestCase):
    def test_reset_session_supports_agy_clear(self) -> None:
        fake_agent = {"agent": "agy", "agent_status": "done", "agent_session": None}
        fake_sent = type("R", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        with patch.object(RESET, "agent", return_value=fake_agent), \
             patch.object(RESET, "_agy_db_snapshot", side_effect=[{"old.db"}, {"old.db", "new.db"}]), \
             patch.object(RESET, "screen_text", return_value="Antigravity CLI 1.5.19"), \
             patch.object(RESET.subprocess, "run", return_value=fake_sent) as mock_run:
            out = RESET.reset("delivery-scheduler-w1")
        self.assertTrue(out.startswith("agy-cleared:"))
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][-1], "/clear")

    def test_start_scheduler_injects_trusted_workspaces_for_agy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "my-repo"
            repo.mkdir()
            kind, args = START.resolve_scheduler_launch("", "", repo=str(repo), hwf_home=HWF_ROOT)
            self.assertEqual(kind, "agy")
            self.assertIn("--model", args)
            self.assertIn("gemini-4-neon-medium", args)
            self.assertNotIn("--mode", args)
            self.assertIn("--settings", args)
            payload = json.loads(args[args.index("--settings") + 1])
            self.assertIn(str(repo), payload["trustedWorkspaces"])


if __name__ == "__main__":
    unittest.main()
