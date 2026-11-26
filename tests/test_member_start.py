"""聚焦验证按角色领取成员：参数只取配置、只认登记名复用本团队空闲成员、没有才新建。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "member_start", Path(__file__).resolve().parents[1] / "scripts/member-start.py"
)
assert SPEC is not None and SPEC.loader is not None
MS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MS
SPEC.loader.exec_module(MS)

GLOBAL = """
profiles:
  scheduler: {kind: omp, args: ["--approval-mode", "yolo"]}
  arch: {kind: claude, args: ["--model", "claude-opus-5-5", "--effort", "medium"]}
  dev: {kind: omp, args: ["--model", "jc-gpt/gpt-6.1-sol", "--thinking", "low"]}
  test: {kind: omp, args: ["--model", "jc-gpt/gpt-6.1-sol", "--thinking", "low"]}
  nomodel: {kind: omp, args: ["--thinking", "medium"]}
"""


def agent(name: str, pane: str, tab: str = "w1:t1", status: str = "idle", kind: str = "claude") -> dict:
    return {"name": name, "pane_id": pane, "tab_id": tab, "workspace_id": "w1", "agent_status": status,
            "agent": kind, "agent_session": {"value": f"session-{pane}"}}


class FakeHerdr:
    """只模拟本脚本用到的命令；记录调用，便于断言没有手写参数、没有多建 pane。"""

    def __init__(self, team: list[dict], width: int = 200, height: int = 48, tab: str = "#453 调度 水印") -> None:
        self.agents = team
        self.tab_label = tab
        self.calls: list[tuple[str, ...]] = []
        self.width, self.height = width, height
        self.argv: dict[str, list[str]] = {}
        self.cwd: dict[str, str] = {}  # pane → 工作目录；没写的取 run_claim 的 --cwd
        self.stuck_cwd: set[str] = set()
        self.default_cwd = ""

    def __call__(self, *args: str, timeout: int = 30) -> dict:
        self.calls.append(args)
        cmd = args[:2]
        if cmd == ("agent", "list"):
            return {"result": {"agents": list(self.agents)}}
        if cmd == ("pane", "run"):
            pane_id, command = args[2], args[3]
            if command.startswith("cd -- "):
                import shlex
                if pane_id not in self.stuck_cwd:
                    self.cwd[pane_id] = shlex.split(command)[2]
            else:
                self.agents = [a for a in self.agents if a["pane_id"] != pane_id]
            return {}
        if cmd == ("pane", "rename"):
            return {}
        if cmd == ("tab", "get"):
            return {"result": {"tab": {"label": self.tab_label}}}
        if cmd == ("agent", "rename"):
            next(a for a in self.agents if a["pane_id"] == args[2])["name"] = args[3]
            return {}
        if cmd == ("pane", "layout"):
            panes = [{"pane_id": a["pane_id"], "rect": {"width": self.width, "height": self.height}}
                     for a in self.agents if a["tab_id"] == "w1:t1"][:1]
            return {"result": {"layout": {"panes": panes}}}
        if cmd == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:pNEW", "tab_id": "w1:t1"}}}
        if cmd == ("tab", "create"):
            return {"result": {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": "w1:pTAB"}}}
        if cmd == ("agent", "start"):
            name, kind, pane = args[2], args[4], args[6]
            self.argv[pane] = list(args[args.index("--") + 1:])
            self.agents.append(agent(name, pane, kind=kind, status="idle") | {"agent_session": {"value": "new"}})
            return {}
        if cmd == ("agent", "get"):
            return {"result": {"agent": next(a for a in self.agents if a["name"] == args[2])}}
        if cmd == ("pane", "get"):
            return {"result": {"pane": {"pane_id": args[2], "cwd": self.cwd.get(args[2], self.default_cwd)}}}
        if cmd == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": a["pane_id"], "workspace_id": a.get("workspace_id", "w1")}
                                         for a in self.agents]}}
        if cmd == ("pane", "close"):
            self.agents = [a for a in self.agents if a["pane_id"] != args[2]]
            return {}
        if cmd == ("pane", "process-info"):
            argv = self.argv.get(args[3], ["claude", "--model", "jc-gpt/gpt-6.1-sol"])
            return {"result": {"process_info": {"foreground_processes": [{"argv": argv}]}}}
        raise AssertionError(f"unexpected herdr call {args}")

    def starts(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[:2] == ("agent", "start")]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "global.yaml").write_text(GLOBAL, encoding="utf-8")
        env = {"HWF_GLOBAL_CONFIG": str(self.dir / "global.yaml"), "HWF_STATE_DIR": str(self.dir / "state"),
               "HERDR_PANE_ID": "w1:pS"}
        for key, value in env.items():
            patcher = patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("main_repo", lambda cwd: None), ("EXIT_WAIT_S", 2), ("CWD_SWITCH_WAIT_S", 0.05)):
            patcher = patch.object(MS, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_claim(self, fake: FakeHerdr, profile: str = "arch", **kw: object) -> dict:
        fake.default_cwd = str(self.dir)
        with patch.object(MS, "herdr", fake):
            return MS.claim(profile, kw.get("task", "#453核查"), str(self.dir), kw.get("exclude", []),
                            kw.get("keep_session", False), kw.get("model_override"), kw.get("authorized_by"))


SCHED = agent("delivery-scheduler-w1-ps", "w1:pS", kind="omp", status="working")


class ProfileTest(Case):
    def test_whole_entry_override_and_model_required(self) -> None:
        local = self.dir / "local.yaml"
        local.write_text("profiles:\n  arch: {kind: codex, args: ['-m', 'gpt-x']}\n", encoding="utf-8")
        self.assertEqual(MS.load_profile("arch", [self.dir / "global.yaml", local]),
                         {"kind": "codex", "args": ["-m", "gpt-x"]})
        with self.assertRaisesRegex(MS.ClaimError, "没写模型"):
            MS.load_profile("nomodel", [self.dir / "global.yaml"])
        with self.assertRaisesRegex(MS.ClaimError, "没有角色"):
            MS.load_profile("ghost", [self.dir / "global.yaml"])
        with self.assertRaisesRegex(MS.ClaimError, "入口启动"):
            MS.load_profile("scheduler", [self.dir / "global.yaml"])

    def test_droid_model_comes_from_settings_file(self) -> None:
        settings = self.dir / "droid.json"
        settings.write_text('{"sessionDefaultSettings": {"model": "claude-opus-5-5", "reasoningEffort": "low"}}',
                            encoding="utf-8")
        cfg = self.dir / "droid.yaml"
        cfg.write_text(f"profiles:\n  dev-mini: {{kind: droid, args: ['--settings', '{settings}', '--auto', 'high']}}\n"
                       "  bad: {kind: droid, args: ['--settings', '/nope.json']}\n", encoding="utf-8")
        profile = MS.load_profile("dev-mini", [cfg])
        self.assertEqual(MS.model_of(profile["args"]), "claude-opus-5-5")
        self.assertEqual(MS.pane_label("dev-mini", profile["args"], "#453"), "dev-mini-claude-opus-5-5-#453")
        with self.assertRaisesRegex(MS.ClaimError, "没写模型"):
            MS.load_profile("bad", [cfg])
        with self.assertRaisesRegex(MS.ClaimError, "--settings"):
            MS.override_model(profile["args"], "x/y")

    def test_label(self) -> None:
        self.assertEqual(MS.pane_label("dev", ["--model", "jc-gpt/gpt-6.1-sol"], "#453"), "dev-gpt-6.1-sol-#453")
        self.assertEqual(
            MS.pane_label("dev", ["--model", "jc-gpt/gpt-6.1-sol"], "核查最小范围", "#453"),
            "dev-gpt-6.1-sol-#453 核查最小范围",
        )


class ClaimTest(Case):
    def test_reuses_idle_arch_by_name_even_if_title_changed(self) -> None:
        """#453 事故：架构师窗口标题被改成「Issue 453 AI生成水印」，仍须按登记名认出并复用。"""
        arch = agent("i453-arch", "w1:pT") | {"terminal_title": "Issue 453 AI生成水印"}
        fake = FakeHerdr([SCHED, agent("delivery-dev-w1-pd", "w1:pD", kind="omp"), arch])
        out = self.run_claim(fake)
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("restarted", "i453-arch", "w1:pT"))
        self.assertFalse([c for c in fake.calls if c[:2] in (("pane", "split"), ("tab", "create"))])
        (start,) = fake.starts()
        self.assertEqual(start[2:7], ("i453-arch", "--kind", "claude", "--pane", "w1:pT"))
        self.assertEqual(list(start[start.index("--") + 1:]), ["--model", "claude-opus-5-5", "--effort", "medium"])

    def test_creates_with_profile_args_when_all_busy(self) -> None:
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pt", "w1:pT", status="working")])
        out = self.run_claim(fake, "dev")
        self.assertEqual((out["action"], out["name"]), ("created", "i453-dev"))
        (start,) = fake.starts()
        self.assertEqual(start[4], "omp")
        self.assertIn(("pane", "rename", "w1:pNEW", "dev-gpt-6.1-sol-#453核查"), fake.calls)
        self.assertLess(fake.calls.index(("pane", "rename", "w1:pNEW", "dev-gpt-6.1-sol-#453核查")),
                        fake.calls.index(start))

    def test_excluded_member_counts_as_busy(self) -> None:
        fake = FakeHerdr([SCHED, agent("i453-arch", "w1:pT")])
        out = self.run_claim(fake, exclude=["i453-arch"])
        self.assertEqual(out["action"], "created")
        self.assertEqual(out["other_same_role"], ["i453-arch"])

    def test_other_schedulers_members_are_not_reused(self) -> None:
        other = agent("delivery-arch-w1-px", "w1:pX", tab="w1:t5")
        fake = FakeHerdr([SCHED, other])
        self.assertEqual(self.run_claim(fake)["action"], "created")
        MS.record({"pane_id": "w1:pX", "tab_id": "w1:t5"}, "delivery-scheduler-other", "arch")
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-px", "w1:pX")])  # 同 tab 但登记簿归别人
        self.assertEqual(self.run_claim(fake)["action"], "created")

    def test_registry_extends_team_to_other_tabs(self) -> None:
        MS.record({"pane_id": "w1:pD", "tab_id": "w1:t2"}, SCHED["name"], "dev")
        fake = FakeHerdr([SCHED, agent("i453-arch", "w1:pA", tab="w1:t2")])
        self.assertEqual(self.run_claim(fake)["action"], "restarted")

    def test_keep_session_only_when_running_args_match(self) -> None:
        arch = agent("i453-arch", "w1:pT")
        fake = FakeHerdr([SCHED, arch])
        fake.argv["w1:pT"] = ["claude", "--model", "claude-opus-5-5", "--effort", "medium", "--x"]
        self.assertEqual(self.run_claim(fake, keep_session=True)["action"], "kept")
        self.assertFalse(fake.starts())
        fake = FakeHerdr([SCHED, agent("i453-arch", "w1:pT")])  # 跑的是 sol：必须重启
        self.assertEqual(self.run_claim(fake, keep_session=True)["action"], "restarted")
        wrong_kind = FakeHerdr([SCHED, agent("i453-arch", "w1:pT", kind="codex")])  # kind 变了也必须重启
        wrong_kind.argv["w1:pT"] = ["codex", "--model", "claude-opus-5-5", "--effort", "medium"]
        self.assertEqual(self.run_claim(wrong_kind, keep_session=True)["action"], "restarted")

    def test_crowded_tab_opens_new_tab(self) -> None:
        fake = FakeHerdr([SCHED], width=100, height=20)
        out = self.run_claim(fake)
        self.assertEqual((out["pane_id"], out["tab_id"]), ("w1:pTAB", "w1:t9"))

    def test_model_override_needs_authorization(self) -> None:
        with self.assertRaisesRegex(MS.ClaimError, "authorized-by"):
            self.run_claim(FakeHerdr([SCHED]), "dev", model_override="x/y")
        fake = FakeHerdr([SCHED])
        out = self.run_claim(fake, "dev", model_override="jc-gpt/gpt-6-astra", authorized_by="note_1")
        (start,) = fake.starts()
        self.assertEqual(list(start[start.index("--") + 1:]), ["--model", "jc-gpt/gpt-6-astra", "--thinking", "low"])
        self.assertEqual(out["label"], "dev-gpt-6-astra-#453核查")

    def test_tab_must_carry_issue_number(self) -> None:
        with self.assertRaisesRegex(MS.ClaimError, "name-tab.py"):
            self.run_claim(FakeHerdr([SCHED], tab="hwf-agent"))
        self.assertEqual(self.run_claim(FakeHerdr([SCHED], tab="待建issue 调度 新需求"))["name"], "new-arch")

    def test_name_taken_gets_suffix_and_new_tab_carries_issue(self) -> None:
        busy = agent("i453-arch", "w1:pT", status="working")
        fake = FakeHerdr([SCHED, busy], width=100, height=20)
        out = self.run_claim(fake)
        self.assertEqual(out["name"], "i453-arch-2")
        self.assertIn("#453 成员", [c for c in fake.calls if c[:2] == ("tab", "create")][0])

    def test_dev_does_not_claim_dev_fe(self) -> None:
        fake = FakeHerdr([SCHED, agent("delivery-dev-fe-w1-pv", "w1:pV", kind="omp"),
                          agent("i453-dev-fe", "w1:pW", kind="omp")])
        self.assertEqual(self.run_claim(fake, "dev")["action"], "created")

    def test_dev_does_not_claim_dev_mini(self) -> None:
        fake = FakeHerdr([SCHED, agent("i453-dev-mini", "w1:pM", kind="droid")])
        self.assertEqual(self.run_claim(fake, "dev")["action"], "created")

    def test_pre_issue_member_renamed_once_issue_exists(self) -> None:
        fake = FakeHerdr([SCHED, agent("new-arch", "w1:pT")])
        fake.argv["w1:pT"] = ["claude", "--model", "claude-opus-5-5", "--effort", "medium"]
        out = self.run_claim(fake, keep_session=True)
        self.assertEqual((out["action"], out["name"]), ("kept", "i453-arch"))
        self.assertIn(("agent", "rename", "w1:pT", "i453-arch"), fake.calls)

    def test_other_issue_member_is_not_reused(self) -> None:
        """2026-11-09 #453：#464 还没收尾的开发被拉来复用，在 #464 的 worktree 里重启。"""
        fake = FakeHerdr([SCHED, agent("i464-dev", "w1:pD", kind="omp", status="done")])
        out = self.run_claim(fake, "dev")
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("created", "i453-dev", "w1:pNEW"))
        self.assertEqual(out["other_same_role"], ["i464-dev"])
        self.assertFalse([c for c in fake.calls if c[:2] == ("pane", "run")])  # 没碰 #464 的会话

    def test_legacy_name_is_not_reused(self) -> None:
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pt", "w1:pT")])
        self.assertEqual(self.run_claim(fake)["action"], "created")

    def test_member_in_other_worktree_switches_cwd_and_restarts(self) -> None:
        fake = FakeHerdr([SCHED, agent("i453-dev", "w1:pD", kind="omp")])
        fake.cwd["w1:pD"] = str(self.dir / "old-worktree")
        fake.argv["w1:pD"] = ["omp", "--model", "jc-gpt/gpt-6.1-sol", "--thinking", "low"]
        # 跨目录时即使传 keep_session=True 也必须退出旧会话、切换 cwd 后在原窗格重启，不新建 -2 副本
        out = self.run_claim(fake, "dev", keep_session=True)
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("restarted", "i453-dev", "w1:pD"))
        self.assertFalse([c for c in fake.calls if c[:2] in (("pane", "split"), ("tab", "create"))])
        self.assertEqual(os.path.realpath(fake.cwd["w1:pD"]), os.path.realpath(self.dir))

    def test_prefers_same_cwd_idle_and_prunes_cross_cwd_duplicates(self) -> None:
        fake = FakeHerdr([
            SCHED,
            agent("i453-dev", "w1:pOLD", kind="omp", status="done"),
            agent("i453-dev-2", "w1:pCUR", kind="omp", status="idle"),
        ])
        fake.cwd["w1:pOLD"] = str(self.dir / "old-worktree")
        fake.cwd["w1:pCUR"] = str(self.dir)
        fake.argv["w1:pCUR"] = ["omp", "--model", "jc-gpt/gpt-6.1-sol", "--thinking", "low"]
        out = self.run_claim(fake, "dev", keep_session=True)
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("kept", "i453-dev-2", "w1:pCUR"))
        self.assertEqual(out["pruned_idle"], ["i453-dev"])

    def test_refuses_start_when_pane_cwd_switch_fails(self) -> None:
        fake = FakeHerdr([SCHED, agent("i453-dev", "w1:pD", kind="omp")])
        fake.cwd["w1:pD"] = str(self.dir / "old-worktree")
        fake.stuck_cwd.add("w1:pD")
        with self.assertRaisesRegex(MS.ClaimError, "切换工作目录"):
            self.run_claim(fake, "dev")
        self.assertFalse(fake.starts())

    def test_caller_must_be_registered_scheduler(self) -> None:
        fake = FakeHerdr([agent("delivery-dev-w1-ps", "w1:pS", kind="omp")])
        with self.assertRaisesRegex(MS.ClaimError, "调度者"):
            self.run_claim(fake)

    def test_prunes_extra_unexcluded_idle_duplicates_and_dead_registry_panes(self) -> None:
        # 2026-11-20 #494：未及时 member-release 导致 i494-dev-fe..12 堆积；下次领取同角色时自动回收多余空闲副本。
        MS.record({"pane_id": "w1:pDEAD", "tab_id": "w1:t1"}, SCHED["name"], "dev")
        MS.record({"pane_id": "w1:pD2", "tab_id": "w1:t1"}, SCHED["name"], "dev")
        MS.record({"pane_id": "w1:pD3", "tab_id": "w1:t1"}, SCHED["name"], "dev")
        fake = FakeHerdr([
            SCHED,
            agent("i453-dev-3", "w1:pD3", kind="omp", status="done"),
            agent("i453-dev", "w1:pD1", kind="omp", status="idle"),
            agent("i453-dev-2", "w1:pD2", kind="omp", status="done"),
            agent("i453-dev-4", "w1:pD4", kind="omp", status="idle"),
        ])
        out = self.run_claim(fake, "dev", exclude=["i453-dev-4"])
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("restarted", "i453-dev", "w1:pD1"))
        self.assertEqual(out["pruned_idle"], ["i453-dev-2", "i453-dev-3"])
        self.assertEqual(out["other_same_role"], ["i453-dev-4"])
        reg = MS.read_registry()
        self.assertEqual(set(reg.keys()), {"w1:pD1"})
        self.assertEqual(reg["w1:pD1"].get("issue"), "#453")

    def test_claim_also_prunes_duplicate_idle_panes_of_sibling_roles(self) -> None:
        fake = FakeHerdr([
            SCHED,
            agent("i453-arch", "w1:pA1", status="idle"),
            agent("i453-arch-2", "w1:pA2", status="done"),
            agent("i453-test", "w1:pT1", kind="omp", status="idle"),
            agent("i453-test-2", "w1:pT2", kind="omp", status="done"),
            agent("i453-test-3", "w1:pT3", kind="omp", status="working"),
        ])
        out = self.run_claim(fake, "dev")
        self.assertEqual((out["action"], out["name"]), ("created", "i453-dev"))
        # 顺带把同 issue 其他角色堆积的多余空闲副本收敛到每角色最多 1 个候补，working 的不碰。
        self.assertEqual(sorted(out["pruned_idle"]), ["i453-arch-2", "i453-test-2"])
        remaining_names = {a["name"] for a in fake.agents}
        self.assertEqual(remaining_names, {"delivery-scheduler-w1-ps", "i453-arch", "i453-test", "i453-test-3", "i453-dev"})

    def test_agy_adds_session_trusted_workspace_and_supports_keep_session_and_override(self) -> None:
        import json as _json
        cfg = self.dir / "agy.yaml"
        cfg.write_text(
            "profiles:\n"
            "  arch: {kind: agy, args: ['--model', 'gemini-4-neon-xhigh', '--effort', 'xhigh', '--mode', 'auto', '--dangerously-skip-permissions']}\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"HWF_GLOBAL_CONFIG": str(cfg)}):
            fake = FakeHerdr([SCHED])
            out = self.run_claim(fake, "arch")
            self.assertEqual((out["action"], out["kind"]), ("created", "agy"))
            self.assertEqual(out["label"], "arch-gemini-4-neon-xhigh-#453核查")
            (start,) = fake.starts()
            started_args = list(start[start.index("--") + 1:])
            self.assertIn("--settings", started_args)
            settings_val = _json.loads(started_args[started_args.index("--settings") + 1])
            self.assertIn(os.path.realpath(self.dir), settings_val["trustedWorkspaces"])

            # 同目录 --keep-session 时 running_args_match 应命中已拼好 --settings 的命令行，直接复用
            out2 = self.run_claim(fake, "arch", keep_session=True)
            self.assertEqual(out2["action"], "kept")

            # --model-override 只替换 --model，保留 --effort 与会话级 --settings，旧 agy 会话按 /exit 退出重启
            out3 = self.run_claim(
                fake, "arch", model_override="gemini-4-neon-high",
                authorized_by="https://git.bootingai.net/x/-/issues/453#note_1",
            )
            self.assertEqual(out3["action"], "restarted")
            self.assertIn(("pane", "run", "w1:pNEW", "/exit"), fake.calls)
            self.assertIn("gemini-4-neon-high", out3["args"])
            self.assertEqual(out3["args"].count("--settings"), 1)

        # 配置里已显式带 --settings 时不重复追加
        explicit = ["--model", "gemini-4-neon-xhigh", "--settings", '{"trustedWorkspaces":["/x"]}']
        self.assertEqual(MS.prepare_launch_args("agy", explicit, str(self.dir)), explicit)


if __name__ == "__main__":
    unittest.main()
