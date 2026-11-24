"""聚焦验证 member-wait.py：成员断网、报错或屏幕长时间不动时，不再被当成「正在干活」一直等下去。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("member_wait", ROOT / "scripts/member-wait.py")
assert SPEC is not None and SPEC.loader is not None
MW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MW
SPEC.loader.exec_module(MW)

TASK = "group-proj-47"
FOOTER = ["", "› Ask Codex", "  GPT · ~/wt", "  ← for agents"]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class World:
    """回放 herdr agent get / agent read；screens 按调用次序给屏幕，用完重复最后一屏。"""

    def __init__(self, statuses: list[str], screens: list[str]) -> None:
        self.statuses, self.screens = statuses, screens
        self.gets = self.reads = 0

    def __call__(self, *args: str) -> str:
        if args[:2] == ("agent", "get"):
            status = self.statuses[min(self.gets, len(self.statuses) - 1)]
            self.gets += 1
            return json.dumps({"result": {"agent": {"name": "i47-code-review", "pane_id": "w1:p9",
                                                    "agent_status": status}}})
        screen = self.screens[min(self.reads, len(self.screens) - 1)]
        self.reads += 1
        return "\n".join(screen.split("\n") + FOOTER)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        patcher = patch.object(MW, "STATE_DIR", Path(tmp.name) / "state")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent: list[str] = []
        patcher = patch.object(MW, "ntfy", lambda title, body: self.sent.append(body) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    def run_wait(self, world: World, repo: bool = True) -> tuple[int, str]:
        with patch.object(MW, "herdr", world):
            return MW.wait("i47-code-review", {"idle", "done", "blocked"}, self.repo if repo else None,
                           TASK if repo else None, self.clock, self.clock.sleep)

    def top(self) -> dict[str, str]:
        tr = MW.load_record()
        return tr.read_top(tr.state_path(self.repo, TASK).read_text(encoding="utf-8"))


class WaitTests(Case):
    def test_network_error_held_for_five_minutes_means_stuck_and_issue_says_so(self) -> None:
        # 2026-11-19 #484：评审断网后屏幕停在 Reconnecting，herdr 仍报 working，干等约 2 小时。
        world = World(["working"], ["• Reconnecting... waiting for network (2h 05m 43s • esc to interrupt)\n"
                                    "  └ Connection failed: error sending request"])
        code, text = self.run_wait(world)
        self.assertEqual(code, MW.EXIT_STUCK)
        self.assertIn("开新会话", text)
        self.assertLessEqual(self.clock.now - 1_000_000.0, MW.ERROR_GRACE_S + MW.POLL_S)
        self.assertTrue(self.top()["当前阶段"].startswith("⚠ i47-code-review 卡住"))
        self.assertEqual(self.sent, [])

    def test_brief_disconnect_that_recovers_is_not_stuck(self) -> None:
        screens = ["Reconnecting... waiting for network"] * 4 + [f"• 读第 {n} 个文件" for n in range(40)]
        code, _ = self.run_wait(World(["working"], screens))
        self.assertEqual(code, MW.EXIT_TIMEOUT)

    def test_quiet_screen_for_fifteen_minutes_is_stuck_across_rewaits(self) -> None:
        # 续挂不能把「屏幕多久没变」清零，否则每 10 分钟重来一次，永远判不出卡住。
        world = World(["working"], ["• Working (31s • esc to interrupt)"])
        self.assertEqual(self.run_wait(world, repo=False)[0], MW.EXIT_TIMEOUT)
        code, text = self.run_wait(world, repo=False)
        self.assertEqual(code, MW.EXIT_STUCK)
        self.assertIn("分钟没有任何变化", text)

    def test_login_or_proxy_failure_needs_user_and_sends_ntfy(self) -> None:
        world = World(["working"], ["unexpected status 503 Service Unavailable: CC Switch local proxy failed"])
        code, _ = self.run_wait(world)
        self.assertEqual(code, MW.EXIT_USER)
        self.assertIn("503 Service Unavailable", self.top()["待用户事项"])
        self.assertEqual(len(self.sent), 1)

    def test_second_stall_within_an_hour_stops_retrying(self) -> None:
        stuck = ["Reconnecting... waiting for network"]
        self.assertEqual(self.run_wait(World(["working"], stuck))[0], MW.EXIT_STUCK)
        MW.state_file("w1:p9").unlink()
        self.clock.now += 1200
        self.assertEqual(self.run_wait(World(["working"], stuck))[0], MW.EXIT_USER)
        self.assertEqual(len(self.sent), 1)

    def test_reaching_target_state_returns_at_once(self) -> None:
        code, text = self.run_wait(World(["working", "done"], ["• 写结论", "常规评审通过"]))
        self.assertEqual((code, text), (MW.EXIT_OK, "i47-code-review 已到 done"))

    def test_changing_screen_waits_at_most_ten_minutes(self) -> None:
        code, _ = self.run_wait(World(["working"], [f"• 第 {n} 步" for n in range(100)]))
        self.assertEqual(code, MW.EXIT_TIMEOUT)
        self.assertLessEqual(self.clock.now - 1_000_000.0, MW.MAX_WAIT_S + MW.POLL_S)

    def test_reads_visible_source_while_working_to_avoid_agent_not_idle(self) -> None:
        seen_sources: list[str] = []

        def strict_herdr(*args: str) -> str:
            if args[:2] == ("agent", "get"):
                if seen_sources:
                    return json.dumps({"result": {"agent": {"name": "i494-dev-fe", "pane_id": "w18:pAJ",
                                                            "agent_status": "done"}}})
                return json.dumps({"result": {"agent": {"name": "i494-dev-fe", "pane_id": "w18:pAJ",
                                                        "agent_status": "working"}}})
            source = args[args.index("--source") + 1]
            seen_sources.append(source)
            if source == "recent-unwrapped":
                raise MW.WaitError('herdr agent read 失败：{"error":{"code":"agent_not_idle"}}')
            return "\n".join(["• Editing file.vue"] + FOOTER)

        with patch.object(MW, "herdr", strict_herdr):
            code, text = MW.wait("i494-dev-fe", {"idle", "done", "blocked"}, None, None, self.clock, self.clock.sleep)
        self.assertEqual((code, text), (MW.EXIT_OK, "i494-dev-fe 已到 done"))
        self.assertEqual(seen_sources, ["visible"])

    def test_missing_agent_cleans_state_and_returns_clean_error(self) -> None:
        calls = 0

        def vanishing_herdr(*args: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps({"result": {"agent": {"name": "i494-test", "pane_id": "w18:pB2",
                                                        "agent_status": "working"}}})
            if calls == 2:
                return "\n".join(["• Running browser"] + FOOTER)
            raise MW.WaitError('herdr agent get 失败：{"error":{"code":"agent_not_found"}}')

        with patch.object(MW, "herdr", vanishing_herdr):
            code, text = MW.wait("i494-test", {"idle", "done"}, None, None, self.clock, self.clock.sleep)
        self.assertEqual(code, MW.EXIT_ERROR)
        self.assertIn("已不存在", text)
        self.assertFalse(MW.state_file("w18:pB2").exists())

    def test_jev_detects_interactive_prompt_at_three_minutes_and_caches_per_digest(self) -> None:
        calls = 0

        class FakeWaitJev:
            def decide(self, **_: Any) -> Any:
                nonlocal calls
                calls += 1
                return MW.JC.JevResult.from_payload({
                    "model": "jev-1.14.0",
                    "answers": {
                        "stall_kind": {
                            "type": "choice",
                            "choice": "interactive_prompt",
                            "confidence": 0.90,
                            "probabilities": {
                                "normal_running": 0.03,
                                "interactive_prompt": 0.92,
                                "auth_or_quota": 0.03,
                                "network_drop": 0.02,
                            },
                        }
                    },
                })

        world = World(["working"], ["? Overwrite local config file? (y/N): "])
        with patch.object(MW, "herdr", world):
            code, text = MW.wait(
                "i47-code-review", {"idle", "done"}, self.repo, TASK,
                self.clock, self.clock.sleep, jev=FakeWaitJev(),
            )
        self.assertEqual(code, MW.EXIT_USER)
        self.assertIn("交互确认", text)
        self.assertEqual(calls, 1)
        self.assertLessEqual(self.clock.now - 1_000_000.0, MW.JEV_QUIET_S + MW.POLL_S)
        self.assertEqual(len(self.sent), 1)

    def test_jev_network_drop_respects_five_minute_grace_without_duplicate_api_calls(self) -> None:
        calls = 0

        class FakeWaitJev:
            def decide(self, **_: Any) -> Any:
                nonlocal calls
                calls += 1
                return MW.JC.JevResult.from_payload({
                    "model": "jev-1.14.0",
                    "answers": {
                        "stall_kind": {
                            "type": "choice",
                            "choice": "network_drop",
                            "confidence": 0.88,
                            "probabilities": {
                                "normal_running": 0.04,
                                "interactive_prompt": 0.02,
                                "auth_or_quota": 0.04,
                                "network_drop": 0.90,
                            },
                        }
                    },
                })

        world = World(["working"], ["[transport] upstream socket closed unexpectedly, retrying #14"])
        with patch.object(MW, "herdr", world):
            code, text = MW.wait(
                "i47-code-review", {"idle", "done"}, self.repo, TASK,
                self.clock, self.clock.sleep, jev=FakeWaitJev(),
            )
        self.assertEqual(code, MW.EXIT_STUCK)
        self.assertIn("断网或连接挂死", text)
        # 180s 查询一次并缓存到指纹文件，300s 宽限期满直接生效，不每 30s 重复调用 API。
        self.assertEqual(calls, 1)
        self.assertGreaterEqual(self.clock.now - 1_000_000.0, MW.ERROR_GRACE_S)
        self.assertLessEqual(self.clock.now - 1_000_000.0, MW.ERROR_GRACE_S + MW.POLL_S)


if __name__ == "__main__":
    unittest.main()
