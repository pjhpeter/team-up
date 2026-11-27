#!/usr/bin/env python3
"""统一角色配置 roles.yaml / role_config.py 与各入口集成单测。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HWF_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = HWF_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import jev_client as JC
import proactive as PRO
import retrospect as RETRO
import role_config as RC
import secretary as SEC


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MEMBER_START = _load_script_module("member_start_for_role_test", "member-start.py")
START_SCHEDULER = _load_script_module("start_scheduler_for_role_test", "start-scheduler.py")
GITLAB_INBOX = _load_script_module("gitlab_inbox_for_role_test", "gitlab-inbox.py")
FEISHU_BRIDGE = _load_script_module("feishu_bridge_for_role_test", "feishu-bridge.py")


def _write_roles(home: Path, content: str) -> Path:
    path = home / "roles.yaml"
    path.write_text(content, encoding="utf-8")
    RC.clear_cache()
    return path


class RepoConsistencyTests(unittest.TestCase):
    def test_real_roles_yaml_valid_and_synced_with_herdr_config(self) -> None:
        RC.clear_cache()
        # 仓库一致性单测只校验入库默认配置，不受开发者本机 roles.local.yaml 覆盖影响
        data = RC.load_roles(HWF_ROOT, include_local=False)
        self.assertEqual(data["default_profile"], "scheduler")
        for role in (
            "scheduler", "arch", "arch-lite", "dev", "dev-fe", "dev-be",
            "test-writer", "test", "test-herdr", "precheck", "code-review",
        ):
            self.assertIn(role, data["team"])
        for svc in ("secretary", "proactive", "retrospect", "typesafe"):
            self.assertIn(svc, data["services"])

        in_sync, target = RC.sync_herdr_config(HWF_ROOT, check_only=True)
        self.assertTrue(in_sync, f"{target} 与 roles.yaml 未同步，请运行 role_config.py sync")

    def test_cli_check_and_show_succeed(self) -> None:
        res_check = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(HWF_ROOT), "check"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res_check.returncode, 0, res_check.stderr)
        self.assertIn("[已同步]", res_check.stdout)

        res_show = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(HWF_ROOT), "show"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res_show.returncode, 0, res_show.stderr)
        self.assertIn("[交付团队 team]", res_show.stdout)
        self.assertIn("[外围服务 services]", res_show.stdout)
        self.assertIn("secretary", res_show.stdout)
        self.assertIn("retrospect", res_show.stdout)

    def test_gitlab_inbox_config_no_duplicate_model_fields(self) -> None:
        cfg = json.loads((HWF_ROOT / "gitlab-inbox.config.json").read_text(encoding="utf-8"))
        for key in ("model", "thinking", "reasoning_effort"):
            self.assertNotIn(key, cfg.get("proactive", {}))
            self.assertNotIn(key, cfg.get("retrospect", {}))
            self.assertNotIn(key, cfg.get("typesafe", {}))

    def test_herdr_config_uses_portable_paths_across_checkouts(self) -> None:
        """确保入库的 herdr/config.yaml 不含本机绝对用户目录，且在任意 CI 检出路径下 check 均通过。"""
        rendered = (HWF_ROOT / "herdr" / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("/home/", rendered)
        self.assertIn("~/.hwf/scheduler-omp.yml", rendered)

        with tempfile.TemporaryDirectory(prefix="hwf-ci-checkout-") as tmp:
            ci_dir = Path(tmp) / "builds" / "team-up"
            (ci_dir / "herdr").mkdir(parents=True)
            (ci_dir / "roles.yaml").write_text((HWF_ROOT / "roles.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            (ci_dir / "herdr" / "config.yaml").write_text(rendered, encoding="utf-8")
            RC.clear_cache()

            in_sync, target = RC.sync_herdr_config(ci_dir, check_only=True)
            self.assertTrue(in_sync, f"模拟 CI 检出目录 {target} 判定不同步")
            res = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(ci_dir), "check"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)


class ValidationAndTeamCompilationTests(unittest.TestCase):
    def test_validation_errors(self) -> None:
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data(["not-a-map"])
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data({"team": {"dev": {"tool": "unknown", "model": "x"}}})
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data({"team": {"dev": {"tool": "codex"}}})
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data({"default_profile": "missing", "team": {"dev": {"tool": "codex", "model": "gpt-5.4"}}})
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data({"services": {"secretary": {"tool": "badtool", "model": "x"}}})
        with self.assertRaises(RC.RoleConfigError):
            RC.validate_roles_data({"services": {"secretary": {"tool": "omp"}}})

        # scheduler 允许省略 model（默认跟随当前会话）
        valid = RC.validate_roles_data({"team": {"scheduler": {"tool": "omp", "thinking": "medium"}}})
        self.assertEqual(valid["team"]["scheduler"]["tool"], "omp")

    def test_compile_all_six_team_tools_and_sync_drift_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-roles-team-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: omp
    omp_config: ~/.hwf/member-omp.yml
    thinking: medium
    yolo: true
  arch:
    tool: agy
    model: gemini-4-neon-xhigh
    thinking: xhigh
    yolo: true
  dev:
    tool: codex
    model: gpt-5.4
    thinking: xhigh
    auto_compact_tokens: 180000
    yolo: true
    extra_args: ["--oss"]
  reviewer:
    tool: claude
    model: claude-opus-4-7
    thinking: high
    auto_compact_tokens: 200000
    yolo: false
  droid-role:
    tool: droid
    model: gemini-4-carbon
    thinking: high
    yolo: true
  droid-custom:
    tool: droid
    settings_file: ~/.hwf/herdr/custom-droid.json
    yolo: false
  pi-role:
    tool: pi
    model: sonnet
    thinking: low
services: {}
""".strip() + "\n",
            )
            in_sync_before, _ = RC.sync_herdr_config(home, check_only=True)
            self.assertFalse(in_sync_before)

            changed, target = RC.sync_herdr_config(home, check_only=False)
            self.assertTrue(changed)
            self.assertEqual(target, home / "herdr" / "config.yaml")

            # 再次 sync 无变动，且 check 通过
            changed2, _ = RC.sync_herdr_config(home, check_only=False)
            self.assertFalse(changed2)
            in_sync_after, _ = RC.sync_herdr_config(home, check_only=True)
            self.assertTrue(in_sync_after)
            yaml_text = target.read_text(encoding="utf-8")
            self.assertNotIn(str(home), yaml_text)
            self.assertIn("~/.hwf/member-omp.yml", yaml_text)
            self.assertIn("~/.hwf/herdr/droid-droid-role.json", yaml_text)
            self.assertIn("~/.hwf/herdr/custom-droid.json", yaml_text)

            compiled = RC.compile_herdr_config(RC.load_roles(home), hwf_home=home, write_settings=False)
            p = compiled["profiles"]
            self.assertEqual(
                p["scheduler"],
                {"kind": "omp", "args": ["--config", str(home / "member-omp.yml"), "--thinking", "medium", "--approval-mode", "yolo"]},
            )
            self.assertEqual(
                p["arch"],
                {"kind": "agy", "args": ["--model", "gemini-4-neon-xhigh", "--effort", "xhigh", "--mode", "auto", "--dangerously-skip-permissions"]},
            )
            self.assertEqual(
                p["dev"],
                {
                    "kind": "codex",
                    "args": [
                        "-m", "gpt-5.4",
                        "-c", "model_reasoning_effort=xhigh",
                        "-c", "model_auto_compact_token_limit=180000",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--oss",
                    ],
                },
            )
            self.assertEqual(
                p["reviewer"],
                {"kind": "claude", "args": ["--model", "claude-opus-4-7", "--effort", "high", "--autocompact", "200000"]},
            )
            droid_settings = home / "herdr" / "droid-droid-role.json"
            self.assertTrue(droid_settings.is_file())
            self.assertEqual(
                json.loads(droid_settings.read_text(encoding="utf-8")),
                {"sessionDefaultSettings": {"model": "gemini-4-carbon", "reasoningEffort": "high"}},
            )
            self.assertEqual(
                p["droid-role"],
                {"kind": "droid", "args": ["--settings", str(droid_settings), "--auto", "high"]},
            )
            self.assertEqual(
                p["droid-custom"],
                {"kind": "droid", "args": ["--settings", str(home / "herdr" / "custom-droid.json")]},
            )
            self.assertEqual(
                p["pi-role"],
                {"kind": "pi", "args": ["--model", "sonnet", "--thinking", "low"]},
            )

            # 篡改托管 droid JSON 时 check 应立即发现不同步
            droid_settings.write_text('{"sessionDefaultSettings": {"model": "wrong"}}\n', encoding="utf-8")
            in_sync_drifted, bad_path = RC.sync_herdr_config(home, check_only=True)
            self.assertFalse(in_sync_drifted)
            self.assertEqual(bad_path, droid_settings)


class HeadlessAdapterTests(unittest.TestCase):
    def test_build_headless_commands_all_tools_and_modes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-headless-") as tmp:
            pfile = Path(tmp) / "prompt.txt"
            pfile.write_text("file prompt body", encoding="utf-8")
            cwd = Path(tmp) / "repo"

            # omp
            cmd_omp_nt = RC.build_headless_command(
                "omp", model="m1", thinking="low", prompt="hi", system_prompt="sys", mode="no_tools"
            )
            self.assertEqual(
                cmd_omp_nt,
                [
                    "omp", "-p", "--model", "m1", "--thinking", "low",
                    "--no-session", "--mode", "json", "--tools", "read",
                    "--no-context-files", "--no-skills", "--no-rules", "--no-extensions", "--no-lsp",
                    "--system-prompt", "sys", "hi",
                ],
            )
            cmd_omp_ro = RC.build_headless_command("omp", model="m1", prompt="hi", mode="readonly")
            self.assertIn("read,grep,glob", cmd_omp_ro)
            self.assertNotIn("read,grep,find,ls", cmd_omp_ro)

            # droid
            fake_bin = lambda name, _fb: f"/fake/{name}"
            cmd_droid_nt = RC.build_headless_command(
                "droid", model="gemini-4-carbon", thinking="medium", prompt="hi",
                system_prompt="sys", mode="no_tools", bin_resolver=fake_bin,
            )
            self.assertEqual(
                cmd_droid_nt,
                [
                    "/fake/droid", "exec", "-m", "gemini-4-carbon", "-r", "medium",
                    "-o", "stream-json", "--only-tools", "Read", "--disable-builtin-skills",
                    "--append-system-prompt", "sys", "hi",
                ],
            )
            cmd_droid_retro = RC.build_headless_command(
                "droid", model="gemini-4-carbon", thinking="high",
                prompt_file=pfile, cwd=cwd, mode="retrospect", bin_resolver=fake_bin,
            )
            self.assertEqual(
                cmd_droid_retro,
                ["/fake/droid", "exec", "-m", "gemini-4-carbon", "-r", "high", "--cwd", str(cwd), "-f", str(pfile)],
            )

            # claude
            cmd_claude_nt = RC.build_headless_command(
                "claude", model="claude-sonnet-4-6", thinking="low", prompt="hi", system_prompt="sys", mode="no_tools"
            )
            self.assertEqual(
                cmd_claude_nt,
                [
                    "claude", "-p", "--model", "claude-sonnet-4-6", "--effort", "low",
                    "--no-session-persistence", "--output-format", "json",
                    "--tools", "", "--disable-slash-commands", "--system-prompt", "sys", "hi",
                ],
            )
            cmd_claude_ro = RC.build_headless_command(
                "claude", model="claude-sonnet-4-6", prompt_file=pfile, mode="retrospect"
            )
            self.assertIn("--permission-mode", cmd_claude_ro)
            self.assertEqual(cmd_claude_ro[-1], "file prompt body")

            # codex
            cmd_codex = RC.build_headless_command(
                "codex", model="gpt-5.4-mini", thinking="low", prompt="hi",
                system_prompt="sys", cwd=cwd, mode="readonly",
            )
            self.assertEqual(
                cmd_codex,
                [
                    "codex", "exec", "-m", "gpt-5.4-mini", "-c", "model_reasoning_effort=low",
                    "-s", "read-only", "--ephemeral", "--skip-git-repo-check",
                    "-C", str(cwd), "--json", "sys\n\nhi",
                ],
            )

            # agy
            cmd_agy_nt = RC.build_headless_command(
                "agy", model="gemini-4-neon-xhigh", thinking="high", prompt="hi", mode="no_tools"
            )
            self.assertEqual(
                cmd_agy_nt,
                [
                    "agy", "-p", "--model", "gemini-4-neon-xhigh", "--effort", "high",
                    "--mode", "plan", "--disable-slash-commands", "--output-format", "stream-json", "hi",
                ],
            )
            cmd_agy_ro = RC.build_headless_command("agy", model="gemini-4-neon-xhigh", prompt="hi", mode="readonly")
            self.assertIn("text", cmd_agy_ro)

    def test_parse_headless_outputs_and_forbid_tools(self) -> None:
        # omp
        omp_ok = json.dumps({
            "type": "turn_end",
            "message": {"stopReason": "stop", "content": [{"type": "text", "text": '{"ok":1}'}], "usage": {"totalTokens": 42}},
        })
        self.assertEqual(RC.parse_headless_output("omp", omp_ok, forbid_tools=True), ('{"ok":1}', 42, None))
        omp_tool = json.dumps({"type": "tool_execution_start", "name": "read"})
        self.assertEqual(RC.parse_headless_output("omp", omp_tool, forbid_tools=True), (None, 0, "tool_used"))

        # claude
        claude_ok = json.dumps({"type": "result", "is_error": False, "num_turns": 1, "result": "hello", "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertEqual(RC.parse_headless_output("claude", claude_ok, forbid_tools=True), ("hello", 15, None))
        claude_tool = json.dumps({"type": "result", "is_error": False, "num_turns": 2, "result": "hi"})
        self.assertEqual(RC.parse_headless_output("claude", claude_tool, forbid_tools=True), (None, 0, "tool_used"))
        claude_err = json.dumps({"type": "result", "is_error": True, "result": "boom"})
        self.assertEqual(RC.parse_headless_output("claude", claude_err, forbid_tools=True), (None, 0, "interrupted_turn"))

        # codex
        codex_ok = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex reply"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 8}}),
        ])
        self.assertEqual(RC.parse_headless_output("codex", codex_ok, forbid_tools=True), ("codex reply", 28, None))
        codex_tool = json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
        self.assertEqual(RC.parse_headless_output("codex", codex_tool, forbid_tools=True), (None, 0, "tool_used"))

        # droid / agy stream-json + text
        droid_stream_ok = json.dumps({"type": "completion", "finalText": "droid answer", "usage": {"totalTokens": 99}})
        self.assertEqual(RC.parse_headless_output("droid", droid_stream_ok, mode="no_tools", forbid_tools=True), ("droid answer", 99, None))
        droid_stream_tool = json.dumps({"type": "tool_call", "toolName": "Read"})
        self.assertEqual(RC.parse_headless_output("droid", droid_stream_tool, mode="no_tools", forbid_tools=True), (None, 0, "tool_used"))
        self.assertEqual(RC.parse_headless_output("droid", "plain report", mode="retrospect", forbid_tools=False), ("plain report", 0, None))
        agy_stream_ok = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "agy reply"}]}})
        self.assertEqual(RC.parse_headless_output("agy", agy_stream_ok, mode="no_tools", forbid_tools=True), ("agy reply", 0, None))


class HotReloadAndIntegrationTests(unittest.TestCase):
    def test_secretary_hot_reloads_tool_and_model_from_roles_yaml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-sec-hot-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
team: {}
services:
  secretary:
    tool: omp
    model: gpt-5.4-mini
    thinking: low
    timeout_s: 30
""".strip() + "\n",
            )
            seen_cmds: list[list[str]] = []

            def fake_run(cmd, **_kw):
                seen_cmds.append(list(cmd))
                if cmd[0] == "omp":
                    out = json.dumps({
                        "type": "turn_end",
                        "message": {"stopReason": "stop", "content": [{"type": "text", "text": '{"action":"answer"}'}]},
                    })
                else:
                    out = json.dumps({"type": "result", "is_error": False, "num_turns": 1, "result": '{"action":"answer"}'})
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

            class DummyBridge:
                project = "frontend/web-v1"
                projects: dict[str, str] = {}
                host = "git.example.com"
                state: dict[str, object] = {}
                cfg: dict[str, object] = {}

                def run(self, cmd, **kw):
                    return fake_run(cmd, **kw)

            sec = SEC.Secretary(DummyBridge(), home=home)
            text1 = sec.ask_model("hello")
            self.assertIsNone(sec.intent_error)
            self.assertEqual(text1, '{"action":"answer"}')
            self.assertEqual(seen_cmds[0][0], "omp")
            self.assertIn("gpt-5.4-mini", seen_cmds[0])

            # 不重启实例，直接修改 roles.yaml 切到 claude
            _write_roles(
                home,
                """
team: {}
services:
  secretary:
    tool: claude
    model: claude-haiku-4-5
    thinking: minimal
    timeout_s: 45
""".strip() + "\n",
            )
            text2 = sec.ask_model("hello again")
            self.assertIsNone(sec.intent_error)
            self.assertEqual(text2, '{"action":"answer"}')
            self.assertEqual(seen_cmds[1][0], "claude")
            self.assertIn("claude-haiku-4-5", seen_cmds[1])
            self.assertIn("minimal", seen_cmds[1])

            # 切到 claude 后若发生工具调用（num_turns > 1），forbid_tools 依然拦截
            sec.bridge.run = lambda cmd, **_kw: subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"type": "result", "is_error": False, "num_turns": 2, "result": "x"}), stderr=""
            )
            self.assertIsNone(sec.ask_model("try tool"))
            self.assertEqual(sec.intent_error, "tool_used")

    def test_proactive_hot_reloads_model_without_clobbering_runtime_cfg(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-pro-hot-") as tmp:
            home = Path(tmp)
            rules = home / "proactive-rules.md"
            rules.write_text("# 规则\n", encoding="utf-8")
            _write_roles(
                home,
                """
team: {}
services:
  proactive:
    tool: omp
    model: gpt-5.4-mini
    thinking: low
""".strip() + "\n",
            )
            seen_cmds: list[list[str]] = []

            def fake_run(cmd, **_kw):
                seen_cmds.append(list(cmd))
                if cmd[0] == "omp":
                    out = json.dumps({
                        "type": "turn_end",
                        "message": {"stopReason": "stop", "content": [{"type": "text", "text": "ok-omp"}]},
                    })
                else:
                    out = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok-codex"}})
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

            scout = PRO.Scout(
                {"host": "git.example.com", "projects": {"frontend/web-v1": tmp}},
                files=PRO.Files(home / "state"),
                run=fake_run,
                rules_path=rules,
                hwf_home=home,
            )
            scout.cfg["daily_cap"] = 3
            self.assertEqual(scout.ask_model("judge"), "ok-omp")
            self.assertEqual(scout.cfg["daily_cap"], 3)
            self.assertEqual(seen_cmds[0][0], "omp")

            # 热切换到 codex
            _write_roles(
                home,
                """
team: {}
services:
  proactive:
    tool: codex
    model: gpt-5.4-mini
    thinking: medium
""".strip() + "\n",
            )
            self.assertEqual(scout.ask_model("judge 2"), "ok-codex")
            self.assertEqual(scout.ask_readonly_analysis("analyze", home / "repo"), "ok-codex")
            self.assertEqual(scout.cfg["daily_cap"], 3)
            self.assertEqual(seen_cmds[1][:2], ["codex", "exec"])
            self.assertEqual(seen_cmds[2][:2], ["codex", "exec"])

    def test_retrospect_hot_reloads_tool_and_updates_html_label(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-retro-hot-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
team: {}
services:
  retrospect:
    tool: claude
    model: claude-opus-4-7
    thinking: high
    timeout_s: 120
""".strip() + "\n",
            )
            seen_cmds: list[list[str]] = []

            def fake_run(cmd, **_kw):
                seen_cmds.append(list(cmd))
                out = json.dumps({"type": "result", "is_error": False, "num_turns": 1, "result": '{"summary":"ok"}'})
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

            retro = RETRO.Retrospect(
                {"host": "git.example.com"},
                files=RETRO.Files(home / "state"),
                run=fake_run,
                hwf_home=home,
            )
            prompt_file = home / "retro-prompt.txt"
            prompt_file.write_text("review logs", encoding="utf-8")
            self.assertEqual(retro.ask_model(prompt_file), '{"summary":"ok"}')
            self.assertEqual(seen_cmds[0][0], "claude")
            self.assertEqual(retro._model_label(), "claude (claude-opus-4-7, high)")

            digest = RETRO.collect_digest(
                {"host": "git.example.com"},
                retro.files,
                1000.0,
                2000.0,
                run=lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
                hwf_home=home,
                omp_root=home / "omp",
            )
            analysis = RETRO.parse_analysis_json(json.dumps({
                "headline": "复盘",
                "problem_plain": "无问题",
                "solution_plain": "保持现状",
                "why_plain": "运行平稳",
                "items": [],
                "gate_updates": [],
            }))
            html = RETRO.render_html_report(
                digest,
                analysis,
                2000.0,
                model_label=retro._model_label(),
            )
            self.assertIn("claude (claude-opus-4-7, high)", html)
            self.assertNotIn("gemini-4-carbon", html)

    def test_jev_client_hot_reloads_model_from_roles_yaml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-jev-hot-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
team: {}
services:
  typesafe:
    tool: typesafe
    model: openai/gpt-4o-mini
""".strip() + "\n",
            )
            with patch.object(JC, "HWF_HOME", home):
                client = JC.JevClient({"api_key": "test-key"})
                self.assertEqual(client._effective_model(), "openai/gpt-4o-mini")

                _write_roles(
                    home,
                    """
team: {}
services:
  typesafe:
    tool: typesafe
    model: anthropic/claude-haiku-4-5
""".strip() + "\n",
                )
                self.assertEqual(client._effective_model(), "anthropic/claude-haiku-4-5")

                # 显式传入 model 时以显式覆盖为准
                pinned = JC.JevClient({"api_key": "test-key", "model": "custom/model"})
                self.assertEqual(pinned._effective_model(), "custom/model")

    def test_member_start_and_start_scheduler_read_roles_yaml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-launch-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: claude
    model: claude-opus-4-7
    thinking: high
    yolo: true
  dev:
    tool: codex
    model: gpt-5.4
    thinking: medium
    yolo: true
  arch-droid:
    tool: droid
    model: gemini-4-carbon
    thinking: high
    yolo: true
services: {}
""".strip() + "\n",
            )
            with patch.object(MEMBER_START._RC, "HWF_HOME", home):
                self.assertEqual(MEMBER_START.global_config_path(), home / "roles.yaml")
                dev_profile = MEMBER_START.load_profile("dev", [MEMBER_START.global_config_path()])
                self.assertEqual(dev_profile["kind"], "codex")
                self.assertIn("gpt-5.4", dev_profile["args"])
                droid_profile = MEMBER_START.load_profile("arch-droid", [MEMBER_START.global_config_path()])
                self.assertEqual(droid_profile["kind"], "droid")
                self.assertEqual(MEMBER_START.model_of(droid_profile["args"]), "gemini-4-carbon")

            # start-scheduler 读取 team.scheduler 并叠加 issue 显式覆盖
            tool, args = START_SCHEDULER.resolve_scheduler_launch("", "", hwf_home=home)
            self.assertEqual(tool, "claude")
            self.assertIn("claude-opus-4-7", args)
            self.assertIn("high", args)

            tool2, args2 = START_SCHEDULER.resolve_scheduler_launch(
                "claude-sonnet-4-6", "low", hwf_home=home
            )
            self.assertEqual(tool2, "claude")
            self.assertIn("claude-sonnet-4-6", args2)
            self.assertIn("low", args2)

            # gitlab-inbox 在 scheduler 非 omp 时不调 omp models find
            with patch.object(GITLAB_INBOX, "HWF_HOME", home):
                called = False

                def fail_run(*_a, **_kw):
                    nonlocal called
                    called = True
                    raise AssertionError("should not invoke omp models find when scheduler tool != omp")

                inbox = GITLAB_INBOX.Inbox(
                    {"host": "git.example.com", "allowed_users": ["pengjh"]},
                    {},
                    run=fail_run,
                )
                model, thinking, err = inbox.scheduler_params("/model claude-sonnet-4-6\n/thinking low\n")
                self.assertFalse(called)
                self.assertEqual((model, thinking, err), ("claude-sonnet-4-6", "low", ""))

    def test_watched_scripts_include_role_config(self) -> None:
        self.assertIn(SCRIPTS_DIR / "role_config.py", GITLAB_INBOX.watched_scripts())
        self.assertIn(SCRIPTS_DIR / "role_config.py", FEISHU_BRIDGE.watched_scripts())


class LocalRolesOverrideTests(unittest.TestCase):
    def _write_local(self, home: Path, content: str) -> Path:
        path = home / "roles.local.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_sparse_field_merge_and_tool_switch_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-roles-local-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: omp
    model: null
    thinking: null
    omp_config: ~/.hwf/scheduler-omp.yml
  arch:
    tool: agy
    model: gemini-4-neon-xhigh
    thinking: xhigh
  dev:
    tool: codex
    model: gpt-6.2-sol
    thinking: medium
    auto_compact_tokens: 200000
    extra_args: ["--codex-flag"]
  code-review:
    tool: codex
    model: gpt-6.2-astra
    thinking: medium
    auto_compact_tokens: 200000
services:
  secretary:
    tool: omp
    model: jc-gpt/gpt-6-luna
    thinking: medium
    timeout_s: 120
""".strip() + "\n",
            )
            self._write_local(
                home,
                """
team:
  # 省略 tool：保留 codex / gpt-6.2-sol，仅改 thinking 并清空 auto_compact_tokens
  code-review:
    thinking: high
    auto_compact_tokens: null
  # 切换 tool：整条替换，不继承 codex 的 auto_compact_tokens 与 extra_args
  dev:
    tool: droid
    model: gemini-4-carbon
    thinking: high
  # 新增本机专属角色
  custom-fast:
    tool: claude
    model: claude-haiku-4-5
    thinking: low
services:
  secretary:
    thinking: low
""".strip() + "\n",
            )
            RC.clear_cache()
            loaded = RC.load_roles_with_sources(home, include_local=True)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            data, sources = loaded

            self.assertEqual(sources["team"], {"code-review", "dev", "custom-fast"})
            self.assertEqual(sources["services"], {"secretary"})

            # 未覆盖角色原样继承
            self.assertEqual(data["team"]["arch"]["tool"], "agy")
            self.assertEqual(data["team"]["arch"]["model"], "gemini-4-neon-xhigh")

            # 同工具字段级合并（且支持 null 清空）
            cr = data["team"]["code-review"]
            self.assertEqual(cr["tool"], "codex")
            self.assertEqual(cr["model"], "gpt-6.2-astra")
            self.assertEqual(cr["thinking"], "high")
            self.assertIsNone(cr["auto_compact_tokens"])

            # 换工具整条替换（不带入 codex 参数）
            dev = data["team"]["dev"]
            self.assertEqual(dev["tool"], "droid")
            self.assertEqual(dev["model"], "gemini-4-carbon")
            self.assertEqual(dev["thinking"], "high")
            self.assertNotIn("auto_compact_tokens", dev)
            self.assertNotIn("extra_args", dev)

            # 本机新增角色
            self.assertEqual(data["team"]["custom-fast"]["model"], "claude-haiku-4-5")

            # 后台服务字段级合并
            sec = data["services"]["secretary"]
            self.assertEqual(sec["tool"], "omp")
            self.assertEqual(sec["model"], "jc-gpt/gpt-6-luna")
            self.assertEqual(sec["thinking"], "low")
            self.assertEqual(sec["timeout_s"], 120)

            # include_local=False 仅返回默认 roles.yaml
            base_only = RC.load_roles(home, include_local=False)
            self.assertEqual(base_only["team"]["dev"]["tool"], "codex")
            self.assertNotIn("custom-fast", base_only["team"])

    def test_local_validation_errors_and_empty_comment_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-roles-local-err-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: omp
  dev:
    tool: codex
    model: gpt-6.2-sol
services: {}
""".strip() + "\n",
            )
            # 仅注释/空文件不报错，等价于无覆盖
            self._write_local(home, "# 留空表示全用默认\n")
            RC.clear_cache()
            data = RC.load_roles(home, include_local=True)
            self.assertEqual(data["team"]["dev"]["model"], "gpt-6.2-sol")

            # 换 tool 但漏写 model 必须报错，绝不把 codex 模型错配给 droid
            self._write_local(home, "team:\n  dev:\n    tool: droid\n")
            RC.clear_cache()
            with self.assertRaisesRegex(RC.RoleConfigError, "缺少必填字段 model"):
                RC.load_roles(home, include_local=True)

            # 顶层拼写错误（如 teams）必须报错，防止静默失效
            self._write_local(home, "teams:\n  dev:\n    thinking: high\n")
            RC.clear_cache()
            with self.assertRaisesRegex(RC.RoleConfigError, "未知顶层字段"):
                RC.load_roles(home, include_local=True)

            # 非对象顶层报错
            self._write_local(home, "- invalid\n")
            RC.clear_cache()
            with self.assertRaisesRegex(RC.RoleConfigError, "顶层必须是 YAML 映射对象"):
                RC.load_roles(home, include_local=True)

    def test_hot_reload_on_local_create_modify_and_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-roles-local-hot-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
team: {}
services:
  secretary:
    tool: omp
    model: base-model
    thinking: medium
""".strip() + "\n",
            )
            RC.clear_cache()
            self.assertEqual(RC.resolve_service_role("secretary", hwf_home=home)["model"], "base-model")

            # 新建 roles.local.yaml：不手工清缓存也能立即热生效
            local_path = self._write_local(home, "services:\n  secretary:\n    model: local-model-v1\n")
            self.assertEqual(RC.resolve_service_role("secretary", hwf_home=home)["model"], "local-model-v1")

            # 修改 roles.local.yaml（不同文件大小确保 mtime/size 签名变化）
            self._write_local(home, "services:\n  secretary:\n    model: local-model-version-two\n")
            self.assertEqual(
                RC.resolve_service_role("secretary", hwf_home=home)["model"],
                "local-model-version-two",
            )

            # 删除 roles.local.yaml：立即回退默认配置
            local_path.unlink()
            self.assertEqual(RC.resolve_service_role("secretary", hwf_home=home)["model"], "base-model")

    def test_sync_isolates_tracked_herdr_config_from_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-roles-sync-iso-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: omp
    omp_config: ~/.hwf/scheduler-omp.yml
  arch:
    tool: agy
    model: gemini-4-neon-xhigh
    thinking: xhigh
  dev:
    tool: codex
    model: gpt-6.2-sol
    thinking: medium
services:
  secretary:
    tool: omp
    model: jc-gpt/gpt-6-luna
    thinking: medium
""".strip() + "\n",
            )
            # 先同步一次默认配置，记录入库 herdr/config.yaml 内容
            RC.sync_herdr_config(home, check_only=False)
            tracked_path = home / "herdr" / "config.yaml"
            tracked_before = tracked_path.read_text(encoding="utf-8")

            # 写入本机覆盖：把 arch 切到 droid，把 scheduler 切到 claude
            self._write_local(
                home,
                """
team:
  scheduler:
    tool: claude
    model: claude-opus-4-7
    thinking: high
  arch:
    tool: droid
    model: gemini-4-carbon
    thinking: high
""".strip() + "\n",
            )
            RC.clear_cache()

            # 跑 CLI sync：入库 herdr/config.yaml 必须一字不动，且不得在 herdr/ 下生成 droid-arch.json
            res_sync = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(home), "sync"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(res_sync.returncode, 0, res_sync.stderr)
            self.assertEqual(tracked_path.read_text(encoding="utf-8"), tracked_before)
            self.assertFalse((home / "herdr" / "droid-arch.json").exists())

            # 本机不入库 herdr/config.local.yaml 与 state/droid-arch.json 正确生成
            local_herdr = home / "herdr" / "config.local.yaml"
            self.assertTrue(local_herdr.is_file())
            local_text = local_herdr.read_text(encoding="utf-8")
            self.assertIn("claude-opus-4-7", local_text)
            state_droid = home / "state" / "droid-arch.json"
            self.assertTrue(state_droid.is_file())
            self.assertIn(str(state_droid), local_text)
            self.assertEqual(
                json.loads(state_droid.read_text(encoding="utf-8")),
                {"sessionDefaultSettings": {"model": "gemini-4-carbon", "reasoningEffort": "high"}},
            )

            # CLI show 明确标注 [local] 与 [default]
            res_show = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(home), "show"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(res_show.returncode, 0, res_show.stderr)
            self.assertIn("已生效", res_show.stdout)
            self.assertIn("[local  ]", res_show.stdout)
            self.assertIn("[default]", res_show.stdout)

            # 再改 roles.local.yaml 但暂不 sync：CLI check 仍通过并给提示（改非 scheduler 无需 sync）
            self._write_local(
                home,
                """
team:
  dev:
    thinking: high
""".strip() + "\n",
            )
            res_check = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(home), "check"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(res_check.returncode, 0, res_check.stderr)
            self.assertIn("[提示]", res_check.stdout)

            # roles.local.yaml 写错时 CLI check 必须返回非 0
            self._write_local(home, "team:\n  dev:\n    tool: invalid_tool\n    model: x\n")
            res_bad = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "role_config.py"), "--home", str(home), "check"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(res_bad.returncode, 0)

    def test_member_start_and_start_scheduler_use_roles_local_without_dirtying_herdr(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-local-launch-") as tmp:
            home = Path(tmp)
            _write_roles(
                home,
                """
default_profile: scheduler
team:
  scheduler:
    tool: omp
    omp_config: ~/.hwf/scheduler-omp.yml
  dev:
    tool: codex
    model: gpt-6.2-sol
    thinking: medium
    auto_compact_tokens: 200000
  arch:
    tool: agy
    model: gemini-4-neon-xhigh
    thinking: xhigh
services: {}
""".strip() + "\n",
            )
            self._write_local(
                home,
                """
team:
  scheduler:
    tool: claude
    model: claude-opus-4-7
    thinking: high
  dev:
    thinking: high
  arch:
    tool: droid
    model: gemini-4-carbon
    thinking: high
""".strip() + "\n",
            )
            RC.clear_cache()

            # member-start 自动合并 roles.local.yaml，并把托管 droid 设置写到 state/ 而不是 herdr/
            with patch.object(MEMBER_START._RC, "HWF_HOME", home):
                dev_profile = MEMBER_START.load_profile("dev", [MEMBER_START.global_config_path()])
                self.assertEqual(dev_profile["kind"], "codex")
                self.assertIn("gpt-6.2-sol", dev_profile["args"])
                self.assertIn("model_reasoning_effort=high", dev_profile["args"])

                arch_profile = MEMBER_START.load_profile("arch", [MEMBER_START.global_config_path()])
                self.assertEqual(arch_profile["kind"], "droid")
                self.assertEqual(MEMBER_START.model_of(arch_profile["args"]), "gemini-4-carbon")
                self.assertTrue((home / "state" / "droid-arch.json").is_file())
                self.assertFalse((home / "herdr" / "droid-arch.json").exists())

            # start-scheduler 自动读取 roles.local.yaml 的 scheduler 覆盖
            tool, args = START_SCHEDULER.resolve_scheduler_launch("", "", hwf_home=home)
            self.assertEqual(tool, "claude")
            self.assertIn("claude-opus-4-7", args)
            self.assertIn("high", args)

            # 环境变量 HWF_ROLES_CONFIG 指定独立整文件时，不混入 HWF_HOME 下的 roles.local.yaml
            standalone = home / "standalone.yaml"
            standalone.write_text(
                "default_profile: dev\nteam:\n  dev:\n    tool: codex\n    model: only-standalone\nservices: {}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HWF_ROLES_CONFIG": str(standalone)}):
                RC.clear_cache()
                self.assertIsNone(RC.local_roles_path())
                self.assertEqual(RC.resolve_team_role("dev")["model"], "only-standalone")


if __name__ == "__main__":
    unittest.main()
