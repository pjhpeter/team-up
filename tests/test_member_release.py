"""聚焦验证阶段中成员释放脚本：支持精确单窗格释放与待上线前 --all-idle 批量安全释放空闲成员。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "member_release", Path(__file__).resolve().parents[1] / "scripts/member-release.py"
)
assert SPEC is not None and SPEC.loader is not None
MR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MR
SPEC.loader.exec_module(MR)

SCHED = "delivery-scheduler-w1-ps"


def make_agent(name: str, pane: str, status: str = "idle", ready: bool = True, tab: str = "w1:t1") -> dict:
    return {
        "name": name,
        "pane_id": pane,
        "tab_id": tab,
        "workspace_id": pane.split(":")[0],
        "agent_status": status,
        "interactive_ready": ready,
        "agent_session": {"value": f"sess-{pane}"},
    }


class FakeHerdr:
    def __init__(self, agents: list[dict]) -> None:
        self.agents = [dict(a) for a in agents]
        self.panes = [
            {"pane_id": a["pane_id"], "tab_id": a.get("tab_id", "w1:t1"), "workspace_id": a["workspace_id"]}
            for a in agents
        ]
        self.tabs: list[dict] = []
        self.closed: list[str] = []

    def __call__(self, *args: str, timeout: int = 30) -> dict:
        cmd = args[:2]
        if cmd == ("agent", "list"):
            alive = {p["pane_id"] for p in self.panes}
            # 模拟真实 herdr agent list 不带 interactive_ready 字段
            return {
                "result": {
                    "agents": [
                        {k: v for k, v in a.items() if k != "interactive_ready"}
                        for a in self.agents
                        if a["pane_id"] in alive
                    ]
                }
            }
        if cmd == ("agent", "get"):
            target = args[2]
            found = next(a for a in self.agents if a["pane_id"] == target or a["name"] == target)
            return {"result": {"agent": dict(found)}}
        if cmd == ("pane", "list"):
            return {"result": {"panes": list(self.panes)}}
        if cmd == ("tab", "list"):
            return {"result": {"tabs": list(self.tabs)}}
        if cmd == ("pane", "close"):
            self.closed.append(args[2])
            self.panes = [p for p in self.panes if p["pane_id"] != args[2]]
            return {}
        raise AssertionError(f"unexpected herdr {args}")


class MemberReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"HWF_STATE_DIR": str(self.state), "HERDR_PANE_ID": "w1:pS"})
        env.start()
        self.addCleanup(env.stop)
        patcher = patch.object(MR._WR, "CLOSE_WAIT_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_registry(self, entries: dict[str, tuple[str, str, str]]) -> None:
        data = {
            pane: {"scheduler": owner, "profile": profile, "tab_id": tab, "at": "2026-11-23T12:00:00"}
            for pane, (owner, profile, tab) in entries.items()
        }
        (self.state / "members.json").write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, fake: FakeHerdr, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with patch.object(MR._WR, "herdr", fake), contextlib.redirect_stdout(out):
            code = MR.main(list(argv))
        return code, out.getvalue()

    def test_single_release_verifies_session_and_closes_pane(self) -> None:
        self.write_registry({"w1:pA": (SCHED, "arch", "w1:t1")})
        fake = FakeHerdr([make_agent(SCHED, "w1:pS", status="working"), make_agent("i47-arch", "w1:pA")])
        with self.assertRaisesRegex(ValueError, "成员会话已变化"):
            self.run_cli(fake, "--issue", "47", "--pane", "w1:pA", "--session", "wrong")
        code, out = self.run_cli(fake, "--issue", "47", "--pane", "w1:pA", "--session", "sess-w1:pA")
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.closed, ["w1:pA"])
        self.assertEqual(json.loads((self.state / "members.json").read_text()), {})

    def test_all_idle_closes_only_own_ready_idle_members_for_issue(self) -> None:
        self.write_registry({
            "w1:pA": (SCHED, "arch", "w1:t1"),
            "w1:pA2": (SCHED, "arch", "w1:t1"),
            "w1:pDEAD": (SCHED, "precheck", "w1:t1"),
            "w1:pB": (SCHED, "dev-fe", "w1:t1"),
            "w1:pC": (SCHED, "test", "w1:t1"),
            "w1:pOTHER_ISSUE": (SCHED, "arch", "w1:t1"),
            "w1:pOTHER_SCHED": ("delivery-scheduler-other", "arch", "w1:t2"),
        })
        fake = FakeHerdr([
            make_agent(SCHED, "w1:pS", status="working"),
            make_agent("i47-arch", "w1:pA", status="idle", ready=True),
            make_agent("i47-arch-2", "w1:pA2", status="done", ready=True),
            make_agent("i47-dev-fe", "w1:pB", status="working", ready=False),
            make_agent("i47-test", "w1:pC", status="idle", ready=False),
            make_agent("i40-arch", "w1:pOTHER_ISSUE", status="idle", ready=True),
            make_agent("i47-arch-3", "w1:pOTHER_SCHED", status="idle", ready=True, tab="w1:t2"),
        ])
        code, out = self.run_cli(fake, "--issue", "47", "--all-idle")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(fake.closed), ["w1:pA", "w1:pA2"])
        self.assertIn("i47-dev-fe / w1:pB（状态 working，保留）", out)
        self.assertIn("i47-test / w1:pC（成员未空闲或尚未就绪，保留）", out)
        remained = set(json.loads((self.state / "members.json").read_text()).keys())
        self.assertEqual(remained, {"w1:pB", "w1:pC", "w1:pOTHER_ISSUE", "w1:pOTHER_SCHED"})

    def test_all_idle_is_idempotent_when_no_idle_members_remain(self) -> None:
        self.write_registry({})
        fake = FakeHerdr([make_agent(SCHED, "w1:pS", status="working")])
        code, out = self.run_cli(fake, "--issue", "47", "--all-idle")
        self.assertEqual(code, 0)
        self.assertIn("没有空闲可释放的成员", out)

    def test_all_idle_closes_exited_shell_panes_registered_or_in_issue_member_tab(self) -> None:
        self.write_registry({
            "w1:pSHELL_REG": (SCHED, "code-review", "w1:t2"),
            "w1:pUNVERIFIED": (SCHED, "dev", "w1:t1"),
        })
        fake = FakeHerdr([make_agent(SCHED, "w1:pS", status="idle")])
        fake.panes.extend([
            {
                "pane_id": "w1:pSHELL_REG",
                "tab_id": "w1:t2",
                "workspace_id": "w1",
                "agent_status": "unknown",
                "label": "code-review-gpt-6.2-astra-核验最小范围",
            },
            {
                "pane_id": "w1:pSHELL_ORPHAN",
                "tab_id": "w1:t3",
                "workspace_id": "w1",
                "agent_status": "unknown",
                "label": "test-gpt-6.2-sol-#47 冒烟恢复",
            },
            {
                "pane_id": "w1:pOTHER_ORPHAN",
                "tab_id": "w1:t4",
                "workspace_id": "w1",
                "agent_status": "unknown",
                "label": "test-gpt-6.2-sol-#48 冒烟恢复",
            },
            {
                "pane_id": "w1:pUSER_SHELL",
                "tab_id": "w1:t9",
                "workspace_id": "w1",
                "agent_status": "unknown",
            },
            {
                "pane_id": "w1:pUNVERIFIED",
                "tab_id": "w1:t1",
                "workspace_id": "w1",
                "agent_status": "unknown",
            },
        ])
        fake.tabs = [
            {"tab_id": "w1:t1", "workspace_id": "w1", "label": "#47 调度 测试"},
            {"tab_id": "w1:t2", "workspace_id": "w1", "label": "#47 成员"},
            {"tab_id": "w1:t3", "workspace_id": "w1", "label": "#47 成员"},
            {"tab_id": "w1:t4", "workspace_id": "w1", "label": "#48 成员"},
            {"tab_id": "w1:t9", "workspace_id": "w1", "label": "终端"},
        ]
        code, out = self.run_cli(fake, "--issue", "47", "--all-idle")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(fake.closed), ["w1:pSHELL_ORPHAN", "w1:pSHELL_REG"])
        remained = set(json.loads((self.state / "members.json").read_text()).keys())
        self.assertEqual(remained, {"w1:pUNVERIFIED"})


if __name__ == "__main__":
    unittest.main()
