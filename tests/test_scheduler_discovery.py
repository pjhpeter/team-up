"""聚焦验证调度者发现入口：复用/新建契约、INDEX 归属、双目录归属、fail closed。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "discovery", Path(__file__).resolve().parents[1] / "scripts/find-scheduler.py"
)
assert SPEC is not None and SPEC.loader is not None
DISCOVERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DISCOVERY  # dataclass 需要能通过 __module__ 找回模块。
SPEC.loader.exec_module(DISCOVERY)

ROOT = tempfile.mkdtemp(prefix="hwf-discovery-")
OTHER = os.path.join(ROOT, "other-project")


def tearDownModule() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)


def candidate(**overrides: Any) -> dict[str, Any]:
    # 故意不带 `agent`（kind）字段：身份只看登记名。
    agent: dict[str, Any] = {
        "name": "delivery-scheduler-w9-p2", "cwd": ROOT,
        "agent_status": "idle", "pane_id": "w9:p2",
    }
    agent.update(overrides)
    return agent


def write_index(rows: list[tuple[str, str, str]], states: dict[str, str] | None = None) -> None:
    """写测试仓库的 INDEX（和可选 STATE.md）；每个用例自己声明需要的任务。"""
    tmp = Path(ROOT) / ".hwf/tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    lines = ["# 任务索引", "", "| 任务标识 | 输入原文 | 调度者 | 状态 |", "|---|---|---|---|"]
    lines += [f"| `{task}` | 需求 \\| 原文 | `{name}` / `w9:p2` | {status} |" for task, name, status in rows]
    (tmp / "INDEX.md").write_text("\n".join(lines) + "\n")
    for task, text in (states or {}).items():
        (tmp / task).mkdir()
        (tmp / task / "STATE.md").write_text(text)


def fake_herdr(
    agents: list[dict[str, Any]] | None = None,
    list_stdout: str | None = None,
    list_rc: int = 0,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        # 只允许 agent list：任何其他 herdr 调用（包括读屏）都让测试失败。
        if argv[1:] != ["agent", "list"]:
            raise AssertionError(f"unexpected herdr call: {argv}")
        stdout = list_stdout if list_stdout is not None else json.dumps(
            {"result": {"agents": agents or []}})
        return subprocess.CompletedProcess(argv, list_rc, stdout, "list error")

    return run


def run_cli(side_effect: Any, env: str = "1") -> tuple[int, str, str, MagicMock]:
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {"HERDR_ENV": env}), \
         patch.object(DISCOVERY.subprocess, "run", side_effect=side_effect) as run, \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = DISCOVERY.cli([ROOT])
    return rc, out.getvalue(), err.getvalue(), run


class CleanRepo(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(Path(ROOT) / ".hwf", ignore_errors=True)  # 默认没有 INDEX：用例间不串状态。
        inbox_state = Path(ROOT) / "inbox-state.json"
        inbox_state.unlink(missing_ok=True)
        patcher = patch.object(DISCOVERY, "INBOX_STATE", inbox_state)
        patcher.start()
        self.addCleanup(patcher.stop)
        claims = Path(ROOT) / "scheduler-claims.json"
        claims.unlink(missing_ok=True)
        claims_patcher = patch.object(DISCOVERY, "CLAIMS_PATH", claims)
        claims_patcher.start()
        self.addCleanup(claims_patcher.stop)


def write_origins(*urls: str, repo: str = ROOT) -> None:
    """模拟 GitLab 收件箱状态：这些发起 issue 仍未被用户关闭。"""
    origins = {url: {"repo": repo} for url in urls}
    DISCOVERY.INBOX_STATE.write_text(json.dumps({"origins": origins}))


class SelectionTests(CleanRepo):
    def test_reuses_idle_and_done(self) -> None:
        for state in ("idle", "done"):
            with self.subTest(state=state):
                selection = DISCOVERY.select_scheduler([candidate(agent_status=state)], ROOT)
                self.assertEqual(selection.candidates, ["w9:p2"])
                self.assertEqual(selection.skipped, [])

    def test_kind_is_not_part_of_identity(self) -> None:
        for kind in ("omp", "pi", "codex"):
            with self.subTest(kind=kind):
                selection = DISCOVERY.select_scheduler([candidate(agent=kind)], ROOT)
                self.assertEqual(selection.candidates, ["w9:p2"])

    def test_busy_states_are_unavailable_not_absent(self) -> None:
        for state in ("working", "blocked", "unknown"):
            with self.subTest(state=state):
                selection = DISCOVERY.select_scheduler([candidate(agent_status=state)], ROOT)
                self.assertEqual(selection.candidates, [])
                self.assertEqual(len(selection.skipped), 1)

    def test_unready_and_missing_pane_are_unavailable(self) -> None:
        agents = [candidate(interactive_ready=False), candidate(launch_pending=True),
                  candidate(pane_id=None)]
        selection = DISCOVERY.select_scheduler(agents, ROOT)
        self.assertEqual(selection.candidates, [])
        self.assertEqual(len(selection.skipped), 3)

    def test_unregistered_and_other_repository_are_ignored(self) -> None:
        agents = [candidate(name="pi"), candidate(name="delivery-arch-w9-p3"),
                  candidate(cwd=OTHER), candidate(cwd=OTHER, foreground_cwd=OTHER)]
        selection = DISCOVERY.select_scheduler(agents, ROOT)
        self.assertEqual(selection.candidates, [])
        self.assertEqual(selection.skipped, [])

    def test_non_string_names_are_ignored(self) -> None:
        for name in (None, 42):
            with self.subTest(name=name):
                selection = DISCOVERY.select_scheduler([candidate(name=name)], ROOT)
                self.assertEqual(selection.candidates, [])
                self.assertEqual(selection.skipped, [])

    def test_either_directory_identifies_repository_member(self) -> None:
        # 任一目录匹配即属于本仓库：idle 但目录不一致 → 计入不可用，而不是被忽略。
        agent = candidate(cwd=OTHER, foreground_cwd=ROOT)
        selection = DISCOVERY.select_scheduler([agent], ROOT)
        self.assertEqual(selection.candidates, [])
        self.assertEqual(len(selection.skipped), 1)
        self.assertIn("directory mismatch", selection.skipped[0])

    def test_directory_mismatch_blocks_reuse_both_ways(self) -> None:
        for agent in (candidate(cwd=ROOT, foreground_cwd=OTHER),
                      candidate(cwd=OTHER, foreground_cwd=ROOT)):
            with self.subTest(cwd=agent["cwd"], foreground_cwd=agent["foreground_cwd"]):
                selection = DISCOVERY.select_scheduler([agent], ROOT)
                self.assertEqual(selection.candidates, [])
                self.assertIn("directory mismatch", selection.skipped[0])

    def test_reusable_when_every_reported_directory_matches(self) -> None:
        for agent in (candidate(cwd=None, foreground_cwd=ROOT),
                      candidate(cwd=ROOT, foreground_cwd=ROOT),
                      candidate(cwd=ROOT)):
            with self.subTest(cwd=agent.get("cwd"), foreground_cwd=agent.get("foreground_cwd")):
                self.assertEqual(DISCOVERY.select_scheduler([agent], ROOT).candidates, ["w9:p2"])

    def test_busy_candidate_does_not_hide_idle_candidate(self) -> None:
        agents = [candidate(agent_status="working", pane_id="w9:p1"), candidate()]
        self.assertEqual(DISCOVERY.select_scheduler(agents, ROOT).candidates, ["w9:p2"])

    def test_multiple_candidates_use_plain_lexicographic_order(self) -> None:
        agents = [candidate(pane_id="w9:p2"), candidate(pane_id="w9:p10")]
        for ordering in (agents, list(reversed(agents))):
            with self.subTest(first=ordering[0]["pane_id"]):
                self.assertEqual(DISCOVERY.select_scheduler(ordering, ROOT).candidates,
                                 ["w9:p10", "w9:p2"])


class CliContractTests(CleanRepo):
    def test_idle_writes_only_pane_id_without_newline(self) -> None:
        rc, out, err, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out, err), (0, "w9:p2", ""))

    def test_multiple_candidates_write_first_only(self) -> None:
        agents = [candidate(pane_id="w9:p2"), candidate(pane_id="w9:p10")]
        rc, out, _, _ = run_cli(fake_herdr(agents))
        self.assertEqual((rc, out), (0, "w9:p10"))

    def test_no_scheduler_exits_zero_with_empty_stdout(self) -> None:
        rc, out, err, _ = run_cli(fake_herdr([candidate(cwd=OTHER)]))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_unavailable_schedulers_mean_create_another(self) -> None:
        # 多调度者并行：忙碌、目录漂移都不再阻止新建，只把原因写到 stderr。
        for agent in (candidate(agent_status="working"),
                      candidate(cwd=ROOT, foreground_cwd=OTHER, agent_status="working"),
                      candidate(cwd=OTHER, foreground_cwd=ROOT)):
            with self.subTest(agent=agent):
                rc, out, err, _ = run_cli(fake_herdr([agent]))
                self.assertEqual((rc, out), (0, ""))
                self.assertIn("w9:p2", err)

    def test_idle_owner_of_open_delivery_is_not_reused(self) -> None:
        write_index([("frontend-web-v1-364", "delivery-scheduler-w9-p2", "in-progress（S0）")])
        rc, out, err, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("open delivery", err)

    def test_idle_scheduler_with_only_finished_tasks_is_reused(self) -> None:
        write_index([("a-1", "delivery-scheduler-w9-p2", "delivered（closed）"),
                     ("a-2", "delivery-scheduler-w9-p2", "done")])
        rc, out, _, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out), (0, "w9:p2"))


class ClaimTests(CleanRepo):
    """两个入口几乎同时挑调度者（2026-11-09 #464/#465 都被塞给 p23）：先挑的占座，后挑的不再选它。"""

    def test_second_entry_skips_just_picked_scheduler_and_creates_new(self) -> None:
        herdr = fake_herdr([candidate()])
        first = run_cli(herdr)
        second = run_cli(herdr)
        self.assertEqual(first[:2], (0, "w9:p2"))
        self.assertEqual(second[:2], (0, ""))  # 空 → 入口新建一个调度者
        self.assertIn("just picked by another entry", second[2])

    def test_second_entry_takes_next_idle_scheduler(self) -> None:
        herdr = fake_herdr([candidate(), candidate(name="delivery-scheduler-w9-p3", pane_id="w9:p3")])
        self.assertEqual(run_cli(herdr)[1], "w9:p2")
        self.assertEqual(run_cli(herdr)[1], "w9:p3")

    def test_claim_expires(self) -> None:
        herdr = fake_herdr([candidate()])
        run_cli(herdr)
        DISCOVERY.CLAIMS_PATH.write_text(json.dumps({"w9:p2": 1.0}))  # 很久以前的占座
        self.assertEqual(run_cli(herdr)[1], "w9:p2")

    def test_no_pick_writes_no_claim_and_corrupt_claims_do_not_block(self) -> None:
        run_cli(fake_herdr([]))
        self.assertEqual(json.loads(DISCOVERY.CLAIMS_PATH.read_text()), {})
        DISCOVERY.CLAIMS_PATH.write_text("not json")
        self.assertEqual(run_cli(fake_herdr([candidate()]))[1], "w9:p2")

    def test_owner_of_does_not_claim(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HERDR_ENV": "1"}), \
             patch.object(DISCOVERY.subprocess, "run", side_effect=fake_herdr([candidate()])), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            DISCOVERY.cli([ROOT, "--owner-of", "https://g/x/-/issues/1"])
        self.assertFalse(DISCOVERY.CLAIMS_PATH.exists())
        self.assertEqual(run_cli(fake_herdr([candidate()]))[1], "w9:p2")


class OriginBindingTests(CleanRepo):
    ORIGIN = "https://git.example/frontend/web-v1/-/work_items/7"

    def test_unclosed_origin_keeps_scheduler_busy_after_done(self) -> None:
        # 调度者写了 done，但用户还没关闭发起 issue：不能接下一个需求。
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "done（待用户关闭发起 issue）")])
        write_origins(self.ORIGIN)
        rc, out, err, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("open delivery", err)
        # 用户在发起 issue 的评论仍要送到原调度者。
        owner = DISCOVERY.owner_of(self.ORIGIN, ROOT, [candidate()])
        self.assertEqual((owner["task"], owner["pane"]), ("frontend-web-v1-7", "w9:p2"))

    def test_user_closing_origin_frees_scheduler(self) -> None:
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "done（待用户关闭发起 issue）")])
        write_origins()
        rc, out, _, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out), (0, "w9:p2"))

    def test_origin_of_other_repository_does_not_bind(self) -> None:
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "done")])
        write_origins(self.ORIGIN, repo=OTHER)
        rc, out, _, _ = run_cli(fake_herdr([candidate()]))
        self.assertEqual((rc, out), (0, "w9:p2"))


class OwnerTests(CleanRepo):
    URL = "https://git.example/frontend/web-v1/-/issues/{}"

    def owner(self, iid: int, agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        info = DISCOVERY.owner_of(self.URL.format(iid), ROOT, agents if agents is not None else [candidate()])
        return {"task": info["task"], "pane": info["pane"]}  # 路由只看这两项

    def test_origin_issue_maps_by_task_id(self) -> None:
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "in-progress")])
        self.assertEqual(self.owner(7), {"task": "frontend-web-v1-7", "pane": "w9:p2"})

    def test_child_issue_maps_through_state(self) -> None:
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "in-progress")],
                    states={"frontend-web-v1-7": "子 issue #70；上线清单 https://git.example/frontend/web-v1/-/work_items/71"})
        self.assertEqual(self.owner(70)["task"], "frontend-web-v1-7")
        self.assertEqual(self.owner(71)["task"], "frontend-web-v1-7")
        # 号码前缀相同但不是同一个 issue。
        self.assertIsNone(self.owner(700)["task"])

    def test_short_reference_only_counts_within_same_project(self) -> None:
        write_index([("other-repo-9", "delivery-scheduler-w9-p2", "in-progress")],
                    states={"other-repo-9": "见 #70"})
        self.assertIsNone(self.owner(70)["task"])

    def test_finished_origin_still_routes_to_its_own_task(self) -> None:
        # #380 回归：发起 issue 已收尾，另一个在途任务的 STATE 顺带提到它，评论仍回到 #380 的任务。
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "closed（用户关闭）"),
                     ("frontend-web-v1-8", "delivery-scheduler-w9-p5", "in-progress")],
                    states={"frontend-web-v1-8": "与 #7 并行，未复用对方成员"})
        agents = [candidate(), candidate(name="delivery-scheduler-w9-p5", pane_id="w9:p5")]
        self.assertEqual(self.owner(7, agents), {"task": "frontend-web-v1-7", "pane": "w9:p2"})

    def test_owner_busy_with_another_task_is_not_targeted(self) -> None:
        # 原调度者收尾后被复用去做别的任务：不把旧任务的评论塞进新任务的会话，交给入口接续。
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "closed"),
                     ("frontend-web-v1-9", "delivery-scheduler-w9-p2", "in-progress")])
        self.assertEqual(self.owner(7), {"task": "frontend-web-v1-7", "pane": None})

    def test_owner_with_earlier_open_sibling_is_still_targeted(self) -> None:
        # #422 回归：#417 与 #422 都由 #408 拆出、同一调度者并行推进；#422 收尾后的评论仍回给它，不另起调度者。
        write_index([("frontend-web-v1-417", "delivery-scheduler-w9-p2", "in-progress"),
                     ("frontend-web-v1-422", "delivery-scheduler-w9-p2", "delivered（待用户合并）")])
        self.assertEqual(self.owner(422), {"task": "frontend-web-v1-422", "pane": "w9:p2"})

    def test_owner_with_later_sibling_mentioning_it_is_still_targeted(self) -> None:
        write_index([("frontend-web-v1-7", "delivery-scheduler-w9-p2", "closed"),
                     ("frontend-web-v1-9", "delivery-scheduler-w9-p2", "in-progress")],
                    states={"frontend-web-v1-9": "与 #7 同一批拆出"})
        self.assertEqual(self.owner(7), {"task": "frontend-web-v1-7", "pane": "w9:p2"})

    def test_offline_owner(self) -> None:
        write_index([("frontend-web-v1-8", "delivery-scheduler-w9-p9", "held")])
        self.assertEqual(self.owner(8), {"task": "frontend-web-v1-8", "pane": None})

class FailClosedTests(CleanRepo):
    def assert_failed(self, rc: int, out: str) -> None:
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_requires_managed_pane_before_discovery(self) -> None:
        rc, out, _, run = run_cli(fake_herdr([candidate()]), env="0")
        self.assert_failed(rc, out)
        run.assert_not_called()

    def test_inventory_failure_does_not_mean_no_scheduler(self) -> None:
        rc, out, err, _ = run_cli(fake_herdr(list_rc=1))
        self.assert_failed(rc, out)
        self.assertIn("exited 1", err)

    def test_cli_exit_two_reports_version_mismatch(self) -> None:
        rc, out, err, _ = run_cli(fake_herdr(list_rc=2))
        self.assert_failed(rc, out)
        self.assertIn("syntax/version mismatch", err)

    def test_null_result_raises_type_error_and_fails_closed(self) -> None:
        rc, out, _, _ = run_cli(fake_herdr(list_stdout=json.dumps({"result": None})))
        self.assert_failed(rc, out)

    def test_invalid_json_and_bad_shape_fail_closed(self) -> None:
        for stdout in ("not json", json.dumps({"result": {"agents": {}}}),
                       json.dumps({"result": {"agents": ["x"]}}), json.dumps({})):
            with self.subTest(stdout=stdout):
                rc, out, _, _ = run_cli(fake_herdr(list_stdout=stdout))
                self.assert_failed(rc, out)

    def test_timeout_and_missing_binary_fail_closed(self) -> None:
        for error in (subprocess.TimeoutExpired("herdr", 10), FileNotFoundError("herdr")):
            with self.subTest(error=type(error).__name__):
                rc, out, _, _ = run_cli(error)
                self.assert_failed(rc, out)

    def test_corrupt_index_or_inbox_state_fails_closed(self) -> None:
        DISCOVERY.INBOX_STATE.write_text("not json")
        rc, out, _, _ = run_cli(fake_herdr([candidate()]))
        self.assert_failed(rc, out)


if __name__ == "__main__":
    unittest.main()
