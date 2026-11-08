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
  nomodel: {kind: omp, args: ["--thinking", "medium"]}
"""


def agent(name: str, pane: str, tab: str = "w1:t1", status: str = "idle", kind: str = "claude") -> dict:
    return {"name": name, "pane_id": pane, "tab_id": tab, "workspace_id": "w1", "agent_status": status,
            "agent": kind, "agent_session": {"value": f"session-{pane}"}}


class FakeHerdr:
    """只模拟本脚本用到的命令；记录调用，便于断言没有手写参数、没有多建 pane。"""

    def __init__(self, team: list[dict], width: int = 200, height: int = 48) -> None:
        self.agents = team
        self.calls: list[tuple[str, ...]] = []
        self.width, self.height = width, height
        self.argv: dict[str, list[str]] = {}

    def __call__(self, *args: str, timeout: int = 30) -> dict:
        self.calls.append(args)
        cmd = args[:2]
        if cmd == ("agent", "list"):
            return {"result": {"agents": list(self.agents)}}
        if cmd == ("pane", "run"):
            self.agents = [a for a in self.agents if a["pane_id"] != args[2]]
            return {}
        if cmd == ("pane", "rename"):
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
        for target, value in (("main_repo", lambda cwd: None), ("EXIT_WAIT_S", 2)):
            patcher = patch.object(MS, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_claim(self, fake: FakeHerdr, profile: str = "arch", **kw: object) -> dict:
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

    def test_label(self) -> None:
        self.assertEqual(MS.pane_label("dev", ["--model", "jc-gpt/gpt-6.1-sol"], "#453"), "dev-gpt-6.1-sol-#453")


class ClaimTest(Case):
    def test_reuses_idle_arch_by_name_even_if_title_changed(self) -> None:
        """#453 事故：架构师窗口标题被改成「Issue 453 AI生成水印」，仍须按登记名认出并复用。"""
        arch = agent("delivery-arch-w1-pt", "w1:pT") | {"terminal_title": "Issue 453 AI生成水印"}
        fake = FakeHerdr([SCHED, agent("delivery-dev-w1-pd", "w1:pD", kind="omp"), arch])
        out = self.run_claim(fake)
        self.assertEqual((out["action"], out["name"], out["pane_id"]), ("restarted", "delivery-arch-w1-pt", "w1:pT"))
        self.assertFalse([c for c in fake.calls if c[:2] in (("pane", "split"), ("tab", "create"))])
        (start,) = fake.starts()
        self.assertEqual(start[2:7], ("delivery-arch-w1-pt", "--kind", "claude", "--pane", "w1:pT"))
        self.assertEqual(list(start[start.index("--") + 1:]), ["--model", "claude-opus-5-5", "--effort", "medium"])

    def test_creates_with_profile_args_when_all_busy(self) -> None:
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pt", "w1:pT", status="working")])
        out = self.run_claim(fake, "dev")
        self.assertEqual((out["action"], out["name"]), ("created", "delivery-dev-w1-pnew"))
        (start,) = fake.starts()
        self.assertEqual(start[4], "omp")
        self.assertIn(("pane", "rename", "w1:pNEW", "dev-gpt-6.1-sol-#453核查"), fake.calls)
        self.assertLess(fake.calls.index(("pane", "rename", "w1:pNEW", "dev-gpt-6.1-sol-#453核查")),
                        fake.calls.index(start))

    def test_excluded_member_counts_as_busy(self) -> None:
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pt", "w1:pT")])
        out = self.run_claim(fake, exclude=["delivery-arch-w1-pt"])
        self.assertEqual(out["action"], "created")
        self.assertEqual(out["other_same_role"], ["delivery-arch-w1-pt"])

    def test_other_schedulers_members_are_not_reused(self) -> None:
        other = agent("delivery-arch-w1-px", "w1:pX", tab="w1:t5")
        fake = FakeHerdr([SCHED, other])
        self.assertEqual(self.run_claim(fake)["action"], "created")
        MS.record({"pane_id": "w1:pX", "tab_id": "w1:t5"}, "delivery-scheduler-other", "arch")
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-px", "w1:pX")])  # 同 tab 但登记簿归别人
        self.assertEqual(self.run_claim(fake)["action"], "created")

    def test_registry_extends_team_to_other_tabs(self) -> None:
        MS.record({"pane_id": "w1:pD", "tab_id": "w1:t2"}, SCHED["name"], "dev")
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pa", "w1:pA", tab="w1:t2")])
        self.assertEqual(self.run_claim(fake)["action"], "restarted")

    def test_keep_session_only_when_running_args_match(self) -> None:
        arch = agent("delivery-arch-w1-pt", "w1:pT")
        fake = FakeHerdr([SCHED, arch])
        fake.argv["w1:pT"] = ["claude", "--model", "claude-opus-5-5", "--effort", "medium", "--x"]
        self.assertEqual(self.run_claim(fake, keep_session=True)["action"], "kept")
        self.assertFalse(fake.starts())
        fake = FakeHerdr([SCHED, agent("delivery-arch-w1-pt", "w1:pT")])  # 跑的是 sol：必须重启
        self.assertEqual(self.run_claim(fake, keep_session=True)["action"], "restarted")

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

    def test_caller_must_be_registered_scheduler(self) -> None:
        fake = FakeHerdr([agent("delivery-dev-w1-ps", "w1:pS", kind="omp")])
        with self.assertRaisesRegex(MS.ClaimError, "调度者"):
            self.run_claim(fake)


if __name__ == "__main__":
    unittest.main()
