"""聚焦验证 GitLab 收件箱：发起门槛、按归属转交评论、转交失败不丢不重。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from urllib.parse import unquote

SPEC = importlib.util.spec_from_file_location(
    "inbox", Path(__file__).resolve().parents[1] / "scripts/gitlab-inbox.py"
)
assert SPEC is not None and SPEC.loader is not None
INBOX = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INBOX
SPEC.loader.exec_module(INBOX)

REPO = "/repo/web-v1"
URL = "https://git.example/frontend/web-v1/-/issues/7"
SINCE = 4_000_000_000  # 参与 issue 的上一轮水位（2096 年），早于 note() 默认的创建时间 2099 年
CONFIG = {"host": "git.example", "allowed_users": ["pengjh"], "label": "herdr-auto",
          "projects": {"frontend/web-v1": REPO}}
# 模拟 omp models find：只有这两个模型；sol 只支持 low/medium/high。
MODELS = [{"selector": "jc-gpt/gpt-6.1-sol", "id": "gpt-6.1-sol", "thinking": ["low", "medium", "high"]},
          {"selector": "devin/claude-opus-5-5", "id": "claude-opus-5-5", "thinking": ["low", "medium", "high", "max"]},
          {"selector": "devin/claude-opus-5-5-fast", "id": "claude-opus-5-5-fast", "thinking": ["low"]}]


def todo(action: str = "assigned", author: str = "pengjh", labels: tuple[str, ...] = ("herdr-auto",),
         url: str = URL, project: str = "frontend/web-v1", todo_id: int = 1, description: str = "") -> dict[str, Any]:
    return {"id": todo_id, "action_name": action, "author": {"username": author}, "target_url": url,
            "project": {"path_with_namespace": project},
            "target": {"project_id": 10, "iid": 7, "state": "opened", "labels": list(labels),
                       "description": description}}


def note(note_id: int, author: str = "pengjh", system: bool = False, body: str = "",
         created_at: str = "2099-01-01T00:00:00.000Z") -> dict[str, Any]:
    return {"id": note_id, "author": {"username": author}, "system": system, "body": body or f"body {note_id}",
            "created_at": created_at}


class FakeWorld:
    """模拟 glab / find-scheduler / hwf / herdr，记录所有副作用。"""

    def __init__(self, todos: list[dict[str, Any]], notes: list[dict[str, Any]] | None = None,
                 owners: dict[str, tuple[str | None, str | None]] | None = None, prompt_rc: int = 0):
        # owners：issue URL → (任务标识, 调度者 pane)，模拟 find-scheduler --owner-of；默认 URL 归 w1:p1。
        self.todos, self.notes = todos, notes or []
        self.owners = owners if owners is not None else {URL: ("frontend-web-v1-7", "w1:p1")}
        self.prompt_rc = prompt_rc
        self.issue_state = "opened"
        self.issue_labels = ["状态:开发中"]
        self.issue_rc = 0
        self.issue_details: dict[str, dict[str, Any]] = {}
        self.detail_queries: list[str] = []
        self.index_status = "in-progress（§0 判定中）"
        self.agent_title = "π > 派 arch 判定复杂度"
        self.prompt_panes: list[str] = []
        self.opened_assigned: list[dict[str, Any]] = []  # 指派给本账号、带开关标签、仍打开的 issue
        self.authored: list[dict[str, Any]] = []  # 本账号建的、近期有更新的 issue
        self.by_iid: list[dict[str, Any]] = []  # 按 iids[] 查到的、近期有更新的 issue
        self.closed_followed: list[dict[str, Any]] = []  # 低频清理 followed 时按 state=closed 查到的已关单
        self.agents_list: list[dict[str, Any]] = []
        self.issue_queries: list[str] = []
        self.reset_rc = 0
        self.agent_status = "working"  # herdr agent get 返回的调度者状态
        self.screen = ""  # herdr agent read 返回的屏幕
        self.keys: list[list[str]] = []  # 每次 send-keys 发的键
        self.after_keys: str | None = "working"  # 按键后调度者变成的状态（None = 不变）
        self.resets: list[str] = []
        self.comments: list[str] = []
        self.launched: list[str] = []
        self.params: list[tuple[str, str]] = []  # 每次走入口传的 (模型, 思考强度)
        self.prompts: list[str] = []
        self.done: list[str] = []
        self.label_events: list[dict[str, Any]] = []  # resource_label_events
        self.label_updates: list[str] = []  # PUT 改标签
        self.label_update_rc = 0

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        out, rc = "", 0
        if argv[:2] == ["glab", "api"]:
            method, path = argv[5], argv[6]
            if method == "POST" and "/notes?body=" in path:
                self.comments.append(unquote(path.split("body=", 1)[1]))
            elif method == "POST":
                self.done.append(path)
            elif method == "PUT":
                rc = self.label_update_rc
                self.label_updates.append(unquote(path))
                out = "{}" if rc == 0 else "403 Forbidden"
            elif "/resource_label_events" in path:
                out = json.dumps(self.label_events)
            elif path == "user":
                out = json.dumps({"username": "pjhagent"})
            elif path.startswith("todos?"):
                out = json.dumps(self.todos)
            elif "/issues?state=opened" in path:
                out = json.dumps(self.opened_assigned)
            elif "/issues?state=closed" in path:
                self.issue_queries.append(path)
                out = json.dumps(self.closed_followed)
            elif "/issues?scope=all" in path:
                self.issue_queries.append(path)
                out = json.dumps(self.authored if "author_username=" in path else self.by_iid)
            elif "/notes/" in path:
                out = json.dumps(next(n for n in self.notes if path.endswith(f"/{n['id']}")))
            elif "/notes?" in path:
                out = json.dumps(sorted(self.notes, key=lambda n: -n["id"]))
            else:
                self.detail_queries.append(path)
                rc = self.issue_rc
                out = (json.dumps(self.issue_details.get(path, {"state": self.issue_state, "labels": self.issue_labels}))
                       if rc == 0 else "GitLab unavailable")
        elif argv[1].endswith("reset-session.py"):
            rc = self.reset_rc
            self.resets.append(argv[2])
            out = "/sessions/new-session.jsonl" if rc == 0 else "busy"
        elif argv[1].endswith("find-scheduler.py"):
            assert argv[3] == "--owner-of", argv  # 收件箱只用归属查询，入口发现交给 team-up。
            task, pane = self.owners.get(argv[4], (None, None))
            out = json.dumps({"task": task, "pane": pane, "live_pane": pane, "scheduler": "delivery-scheduler-w1-p1" if task else None,
                              "status": self.index_status if task else None})
        elif argv[:3] == ["herdr", "agent", "list"]:
            out = json.dumps({"result": {"agents": self.agents_list}}) if self.agents_list else ""
        elif argv[:3] == ["herdr", "agent", "get"]:
            out = json.dumps({"result": {"agent": {"agent_status": self.agent_status,
                                                   "terminal_title_stripped": self.agent_title}}})
        elif argv[0] == "hwf":
            values = [argv[i + 1] for i, a in enumerate(argv) if a == "--input"]
            inputs = dict(v.split("=", 1) for v in values)
            self.launched.append(inputs["task"])
            self.params.append((inputs["model"], inputs["thinking"]))
        elif argv[:3] == ["omp", "models", "find"]:
            out = json.dumps({"models": [m for m in MODELS if argv[3] in m["selector"]]})
        elif argv[:3] == ["herdr", "agent", "prompt"]:
            rc = self.prompt_rc
            self.prompt_panes.append(argv[3])
            self.prompts.append(argv[4])
        elif argv[:3] == ["herdr", "agent", "read"]:
            out = self.screen
        elif argv[:3] == ["herdr", "agent", "send-keys"]:
            self.keys.append(argv[4:])
            if self.after_keys is not None:
                self.agent_status = self.after_keys
        return subprocess.CompletedProcess(argv, rc, out if rc == 0 else "", out if rc else "")


def make(world: FakeWorld, state: dict[str, Any] | None = None) -> Any:
    return INBOX.Inbox(CONFIG, state if state is not None else {}, run=world)


class InboxTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_whitelisted_labeled_assignment_launches_once(self) -> None:
        world = FakeWorld([todo()], notes=[note(5)])
        inbox = make(world)
        inbox.poll()
        self.assertEqual(len(world.launched), 1)
        self.assertIn(URL, world.launched[0])
        self.assertIn("todos/1/mark_as_done", world.done)
        # 历史评论不转交；同一 issue 再次被指派也不重复发起。
        self.assertEqual(inbox.state["origins"][URL]["last_note_id"], 5)
        self.assertEqual(world.prompts, [])
        world.todos = [todo(todo_id=2)]
        inbox.poll()
        self.assertEqual(len(world.launched), 1)
        self.assertIn("todos/2/mark_as_done", world.done)

    def test_assignment_gates_leave_todo_untouched(self) -> None:
        cases = {"no label": todo(labels=()), "stranger": todo(author="someone"),
                 "self-assigned split": todo(author="pjhagent"), "other project": todo(project="x/y")}
        for name, item in cases.items():
            with self.subTest(name):
                world = FakeWorld([item])
                make(world).poll()
                self.assertEqual(world.launched, [])
                self.assertEqual(world.done, [])

    def test_parallel_assignments_launch_without_waiting(self) -> None:
        # 多调度者并行：在途交付不阻塞新指派，每个 issue 各走一次入口。
        other = "https://git.example/frontend/web-v1/-/issues/8"
        world = FakeWorld([todo(), todo(url=other, todo_id=2)])
        make(world).poll()
        self.assertEqual(len(world.launched), 2)
        self.assertIn("todos/2/mark_as_done", world.done)

    def test_origin_replies_forward_in_order_and_skip_noise(self) -> None:
        state = {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                   "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        notes = [note(5), note(6, author="pjhagent"), note(7, system=True), note(8), note(9)]
        world = FakeWorld([], notes=notes)
        inbox = make(world, state)
        inbox.poll()
        self.assertEqual(len(world.prompts), 2)
        self.assertIn("note_8", world.prompts[0])
        self.assertIn("--kind answer", world.prompts[0])
        self.assertIn("body 9", world.prompts[1])
        self.assertEqual(state["origins"][URL]["last_note_id"], 9)
        inbox.poll()
        self.assertEqual(len(world.prompts), 2)

    def test_rejected_prompt_holds_watermark_for_retry(self) -> None:
        state = {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                   "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        world = FakeWorld([], notes=[note(6), note(7)], prompt_rc=1)
        inbox = make(world, state)
        inbox.poll()
        self.assertEqual(state["origins"][URL]["last_note_id"], 5)
        world.prompt_rc = 0
        inbox.poll()
        self.assertEqual([p.count("body 6") + p.count("body 7") for p in world.prompts[-2:]], [1, 1])
        self.assertEqual(state["origins"][URL]["last_note_id"], 7)

    def test_origin_reply_without_live_scheduler_goes_through_entry_once(self) -> None:
        state = {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                   "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        world = FakeWorld([], notes=[note(6), note(7)], owners={URL: ("frontend-web-v1-7", None)})
        inbox = make(world, state)
        inbox.poll()
        # 第一条走入口恢复；新调度者登记前同一任务不再走入口，第二条保留到下一轮。
        self.assertEqual((len(world.launched), world.prompts), (1, []))
        self.assertIn("note_6", world.launched[0])
        self.assertEqual(state["origins"][URL]["last_note_id"], 6)
        world.owners = {URL: ("frontend-web-v1-7", "w2:p1")}
        inbox.poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("body 7", world.prompts[0])

    def test_mention_forwards_once_even_on_origin_issue(self) -> None:
        state = {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                   "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6)])
        make(world, state).poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("todos/1/mark_as_done", world.done)
        self.assertEqual(state["origins"][URL]["last_note_id"], 6)

    def test_mention_routes_to_the_owning_scheduler(self) -> None:
        child = "https://git.example/frontend/web-v1/-/issues/70"
        world = FakeWorld([todo("mentioned", url=f"{child}#note_6")], notes=[note(6)],
                          owners={child: ("frontend-web-v1-7", "w3:p1")})
        prompted: list[str] = []
        original = world.__call__
        def spy(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["herdr", "agent", "prompt"]:
                prompted.append(argv[3])
            return original(argv, **kw)
        make(spy).poll()  # type: ignore[arg-type]
        self.assertEqual(prompted, ["w3:p1"])

    def test_unowned_mention_becomes_new_request(self) -> None:
        other = "https://git.example/frontend/web-v1/-/issues/99"
        world = FakeWorld([todo("mentioned", url=f"{other}#note_6")], notes=[note(6)], owners={})
        make(world).poll()
        self.assertEqual(len(world.launched), 1)
        self.assertIn("body 6", world.launched[0])
        self.assertIn("todos/1/mark_as_done", world.done)

    def origin_state(self) -> dict[str, Any]:
        return {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                  "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}

    def test_reset_comment_starts_new_session_and_resumes(self) -> None:
        state = self.origin_state()
        reset = note(6) | {"body": " /NEW \n"}
        world = FakeWorld([], notes=[reset])
        make(world, state).poll()
        self.assertEqual(world.resets, ["w1:p1"])
        # 清空指令本身不转交；新会话收到的是接续指令，用户收到确认评论。
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("frontend-web-v1-7", world.prompts[0])
        self.assertIn("@pengjh", world.comments[0])
        self.assertEqual(state["origins"][URL]["last_note_id"], 6)

    def test_reset_waits_while_scheduler_is_working(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(6) | {"body": "/clear"}, note(7)])
        world.reset_rc = 1
        inbox = make(world, state)
        inbox.poll()
        # 清空未完成时不跳过它去转交后面的评论，避免后续指令落进旧会话。
        self.assertEqual((world.prompts, world.comments, state["origins"][URL]["last_note_id"]), ([], [], 5))
        world.reset_rc = 0
        inbox.poll()
        self.assertEqual(world.resets, ["w1:p1", "w1:p1"])
        self.assertIn("body 7", world.prompts[-1])

    def test_reset_without_live_scheduler_goes_through_entry(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/new"}], owners={URL: ("frontend-web-v1-7", None)})
        make(world, self.origin_state()).poll()
        self.assertEqual((world.resets, len(world.launched)), ([], 1))

    def test_user_closing_origin_notifies_scheduler_and_stops_tracking(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(5)])
        world.issue_state = "closed"
        make(world, state).poll()
        self.assertNotIn(URL, state["origins"])
        self.assertEqual(len(world.prompts), 1)

    def test_reset_command_tolerates_mentioning_the_agent(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/new @pjhagent"}])
        make(world, self.origin_state()).poll()
        self.assertEqual(world.resets, ["w1:p1"])

    def test_quiet_command_records_mode_forwards_once_and_confirms(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(6) | {"body": "/免打扰 @pjhagent"}])
        make(world, state).poll()
        self.assertEqual(state["origins"][URL]["notify"], "quiet")
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("免打扰", world.prompts[0])
        self.assertIn("不要再发确认评论", world.prompts[0])
        self.assertEqual(len(world.comments), 1)
        self.assertIn("已切到免打扰模式", world.comments[0])

    def test_verbose_english_alias_switches_back(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["notify"] = "quiet"
        world = FakeWorld([], notes=[note(6) | {"body": "/VERBOSE"}])
        make(world, state).poll()
        self.assertEqual(state["origins"][URL]["notify"], "verbose")
        self.assertIn("已切到详尽模式", world.comments[0])

    def test_notify_command_without_delivery_only_replies(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/quiet"}], owners={})
        make(world, self.origin_state()).poll()
        self.assertEqual((world.prompts, world.launched), ([], []))
        self.assertIn("没有对应的交付", world.comments[0])

    def test_notify_retries_when_scheduler_rejects_prompt(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(6) | {"body": "/详尽"}], prompt_rc=1)
        make(world, state).poll()
        self.assertEqual((world.comments, state["origins"][URL]["last_note_id"]), ([], 5))
        self.assertNotIn("notify", state["origins"][URL])

    def test_notify_word_inside_sentence_is_a_normal_message(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/免打扰 先别发了"}])
        make(world, self.origin_state()).poll()
        self.assertIn("/免打扰 先别发了", world.prompts[0])
    def test_reset_command_with_other_words_is_a_normal_message(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/new 然后继续"}])
        make(world, self.origin_state()).poll()
        self.assertEqual(world.resets, [])
        self.assertIn("/new 然后继续", world.prompts[0])

    def test_reopened_origin_is_tracked_again_from_its_watermark(self) -> None:
        # #380 回归：关闭→重开→/new，清空指令要作用于原任务，而不是被当成普通 @ 消息。
        state = self.origin_state()
        world = FakeWorld([], notes=[note(5)])
        world.issue_state = "closed"
        inbox = make(world, state)
        inbox.poll()
        self.assertNotIn(URL, state["origins"])
        world.issue_state = "opened"
        world.opened_assigned = [{"web_url": URL}]
        world.notes = [note(5), note(6) | {"body": "/new @pjhagent"}]
        world.todos = [todo("mentioned", url=f"{URL}#note_6")]
        inbox.poll()
        self.assertEqual(state["origins"][URL]["last_note_id"], 6)
        self.assertEqual(world.resets, ["w1:p1"])
        self.assertIn("todos/1/mark_as_done", world.done)

    def test_open_assigned_issue_never_launched_is_left_to_assignment(self) -> None:
        world = FakeWorld([])
        world.opened_assigned = [{"web_url": URL}]
        inbox = make(world)
        inbox.poll()
        self.assertEqual((inbox.state["origins"], world.launched), ({}, []))


    def test_comment_and_close_handles_the_comment_before_releasing(self) -> None:
        # GitLab「评论并关闭」：/new 和关闭同时到，先清空再收尾释放，不能丢掉 /new。
        state = self.origin_state()
        world = FakeWorld([], notes=[note(6) | {"body": "/new"}])
        world.issue_state = "closed"
        make(world, state).poll()
        self.assertEqual(world.resets, ["w1:p1"])
        self.assertNotIn(URL, state["origins"])
        self.assertEqual(state["closed_origins"][URL]["last_note_id"], 6)

    def test_close_waits_until_pending_comment_is_delivered(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(6)], prompt_rc=1)
        world.issue_state = "closed"
        inbox = make(world, state)
        inbox.poll()
        self.assertIn(URL, state["origins"])  # 评论没送到就不释放调度者
        world.prompt_rc = 0
        inbox.poll()
        self.assertNotIn(URL, state["origins"])
        self.assertIn("body 6", world.prompts[-2])


    def test_forwarded_comment_receipt_does_not_query_progress(self) -> None:
        # 转交成功就回执，即使 GitLab 进度查询不可用，也不能重发已送达的消息。
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6)])
        world.index_status = "delivered"
        world.agent_title = "旧任务已完成"
        world.issue_rc = 1
        inbox = make(world)
        inbox.poll()
        self.assertEqual(world.prompt_panes, ["w1:p1"])
        self.assertEqual(world.detail_queries, [])
        self.assertEqual(len(world.comments), 1)
        self.assertIn(URL, world.comments[0])
        self.assertNotIn(world.index_status, world.comments[0])
        self.assertNotIn(world.agent_title, world.comments[0])
        self.assertIn(6, inbox.state["forwarded"])
        self.assertIn("todos/1/mark_as_done", world.done)

    def test_entry_receipt_names_target_without_querying_progress(self) -> None:
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6)], owners={})
        world.issue_rc = 1
        make(world).poll()
        self.assertEqual(len(world.launched), 1)
        self.assertEqual(world.prompts, [])
        self.assertEqual(world.detail_queries, [])
        self.assertEqual(len(world.comments), 1)
        self.assertIn(URL, world.comments[0])
        self.assertIn("todos/1/mark_as_done", world.done)

    def test_no_receipt_when_delivery_fails(self) -> None:
        world = FakeWorld([], notes=[note(6)], prompt_rc=1)
        make(world, self.origin_state()).poll()
        self.assertEqual(world.comments, [])

    def test_status_is_answered_by_inbox_without_disturbing_scheduler(self) -> None:
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6) | {"body": "/status @pjhagent"}])
        make(world, self.origin_state()).poll()
        self.assertEqual(world.prompts, [])
        self.assertEqual(len(world.comments), 1)
        self.assertIn("delivery-scheduler-w1-p1", world.comments[0])
        self.assertIn("todos/1/mark_as_done", world.done)

    def test_status_uses_gitlab_labels_instead_of_delivered_index_and_shared_title(self) -> None:
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6, body="/status")])
        world.index_status = "delivered"
        world.agent_title = "π > #99 已交付"
        make(world).poll()
        self.assertEqual(world.detail_queries, ["projects/10/issues/7"])
        self.assertEqual(len(world.comments), 1)
        self.assertIn("状态:开发中", world.comments[0])
        self.assertNotIn(world.index_status, world.comments[0])
        self.assertNotIn("#99", world.comments[0])
        self.assertEqual((world.prompts, world.launched), ([], []))

    def test_status_includes_now_next_and_waiting_from_issue_progress_area(self) -> None:
        # 2026-11-19 #484：用户要能随时看到现在做到哪、下一步、在等谁。
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6, body="/status")])
        world.issue_details = {"projects/10/issues/7": {"state": "opened", "labels": ["状态:开发中"], "description":
                               "<!-- hwf:start -->\n## 进度\n\n**现在**：第 4 项收尾  \n**下一步**：重跑第 13 条\n"
                               "**等你处理**：拍板续传时机\n<!-- hwf:end -->\n用户原文 **现在**：不算"}}
        make(world).poll()
        self.assertIn("- 现在：第 4 项收尾\n- 下一步：重跑第 13 条\n- 等你处理：拍板续传时机\n", world.comments[0])
        self.assertEqual(world.comments[0].count("现在"), 1)

    def test_status_closed_takes_priority_over_stale_labels(self) -> None:
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6, body="/status")])
        world.issue_state = "closed"
        world.issue_labels = ["状态:等你处理"]
        make(world).poll()
        self.assertEqual(len(world.comments), 1)
        self.assertIn("已关闭", world.comments[0])
        self.assertNotIn(world.issue_labels[0], world.comments[0])
        self.assertEqual(world.prompts, [])

    def test_status_query_failure_does_not_fall_back_to_old_progress(self) -> None:
        for state, rc in (("opened", 1), (None, 0)):
            with self.subTest(state=state, rc=rc):
                world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")], notes=[note(6, body="/status")])
                world.issue_state, world.issue_rc = state, rc
                world.index_status = "delivered"
                world.agent_title = "π > #99 已交付"
                make(world).poll()
                self.assertEqual(world.detail_queries, ["projects/10/issues/7"])
                self.assertEqual(len(world.comments), 1)
                self.assertIn(URL, world.comments[0])
                self.assertIn("查询失败", world.comments[0])
                self.assertNotIn(world.index_status, world.comments[0])
                self.assertNotIn("#99", world.comments[0])
                self.assertNotIn(world.issue_labels[0], world.comments[0])
                self.assertEqual((world.prompts, world.launched), ([], []))

    def test_status_uses_target_project_even_with_same_iid_and_shared_repo(self) -> None:
        other = "https://git.example/another/project/-/issues/7"
        item = todo("mentioned", project="another/project", url=f"{other}#note_6")
        item["target"]["project_id"] = 20
        world = FakeWorld([item], notes=[note(6, body="/status")], owners={other: ("frontend-web-v1-7", "w1:p1")})
        world.issue_details = {"projects/10/issues/7": {"state": "closed", "labels": []},
                               "projects/20/issues/7": {"state": "opened", "labels": ["状态:测试中"]}}
        config = CONFIG | {"projects": {"frontend/web-v1": REPO, "another/project": REPO}}
        INBOX.Inbox(config, {}, run=world).poll()
        self.assertEqual(world.detail_queries, ["projects/20/issues/7"])
        self.assertIn(other, world.comments[0])
        self.assertIn("状态:测试中", world.comments[0])
        self.assertNotIn("已关闭", world.comments[0])

    def test_referenced_issue_does_not_retarget_reply_or_approval(self) -> None:
        other = "https://git.example/frontend/web-v1/-/issues/99"
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")],
                          notes=[note(6, body=f"参考 {other} 的处理，同意合并")],
                          owners={URL: ("frontend-web-v1-7", "w1:p1"), other: ("frontend-web-v1-99", "w2:p1")})
        inbox = make(world)
        inbox.poll()
        self.assertEqual(world.prompt_panes, ["w1:p1"])
        self.assertIn(f"{URL}#note_6", world.prompts[0])
        self.assertIn(world.notes[0]["body"], world.prompts[0])
        self.assertEqual(world.launched, [])
        self.assertEqual(set(inbox.state["followed"]), {URL})
        self.assertIn(URL, world.comments[0])
        self.assertNotIn(other, world.comments[0])

    def test_status_without_labels_or_live_owner_never_uses_index_progress(self) -> None:
        for owners in ({}, {URL: ("frontend-web-v1-7", None)}):
            with self.subTest(owners=owners):
                world = FakeWorld([todo("mentioned", url=f"{URL}#note_6")],
                                  notes=[note(6, body="/status")], owners=owners)
                world.issue_labels = ["herdr-auto"]
                world.index_status = "delivered"
                make(world).poll()
                self.assertEqual(world.detail_queries, ["projects/10/issues/7"])
                self.assertEqual(len(world.comments), 1)
                self.assertIn(URL, world.comments[0])
                self.assertNotIn(world.index_status, world.comments[0])
                self.assertNotIn("herdr-auto", world.comments[0])
                self.assertNotIn("已关闭", world.comments[0])
                self.assertEqual((world.prompts, world.launched), ([], []))

    def test_blocked_scheduler_is_reported_once_per_episode(self) -> None:
        state = self.origin_state()
        world = FakeWorld([], notes=[note(5)])
        world.agent_status = "blocked"
        inbox = make(world, state)
        inbox.poll()
        inbox.poll()
        self.assertEqual(len(world.comments), 1)
        self.assertIn("@pengjh", world.comments[0])
        world.agent_status = "working"
        inbox.poll()
        world.agent_status = "blocked"
        inbox.poll()
        self.assertEqual(len(world.comments), 2)


    def test_assignment_starts_in_chat_mode(self) -> None:
        world = FakeWorld([todo()], notes=[note(5)])
        inbox = make(world)
        inbox.poll()
        self.assertTrue(world.launched[0].startswith(INBOX.CHAT_TAG))
        self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.CHAT)

    def test_chat_mode_comment_tells_scheduler_not_to_deliver(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["mode"] = INBOX.CHAT
        world = FakeWorld([], notes=[note(6)])
        make(world, state).poll()
        self.assertIn("对话模式", world.prompts[0])
        self.assertIn("/go", world.prompts[0])
        self.assertIn("没点名派 arch", world.prompts[0])
        self.assertIn("你自己不分析", world.prompts[0])

    def test_go_switches_to_delivery_with_extra_instructions(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["mode"] = INBOX.CHAT
        world = FakeWorld([], notes=[note(6) | {"body": "@pjhagent /go 先只改文案"}])
        make(world, state).poll()
        self.assertEqual(state["origins"][URL]["mode"], INBOX.DELIVER)
        self.assertIn("发出 /go", world.prompts[0])
        self.assertIn("先只改文案", world.prompts[0])
        self.assertIn("开始跑交付流程", world.comments[0])
        # 之后的普通评论按交付模式转交。
        world.notes.append(note(7))
        make(world, state).poll()
        self.assertIn("交付模式", world.prompts[-1])

    def assert_debug_delivery_contract(self, text: str) -> None:
        self.assertIn("protocol/implementation.md", text)
        for role in ("dev", "dev-fe", "dev-be"):
            self.assertRegex(text, rf"(?<![\w-]){role}(?![\w-])")
        self.assertRegex(text, r"单元测试|单测")
        self.assertRegex(text, r"develop.*自动化.*release-<根编号>")
        self.assertNotIn("§0 复杂度判定开始", text)
        for obsolete in ("dev-mini", "dev-fe-mini", "dev-be-mini", "不测试", "只开发"):
            self.assertNotIn(obsolete, text)

    def test_debug_commands_route_existing_and_new_issues_to_delivery(self) -> None:
        aliases = ("debug", "DEBUG", "debug模式", "Debug 模式", "改bug", "改 BUG",
                   "修bug", "修 Bug", "极简", "极简模式", "极简 模式", "minimal", "MINIMAL")
        for alias in aliases:
            for prefix in ("", "/go ", "/GO "):
                for existing in (True, False):
                    body = f"@PJHAGENT {prefix}{alias}"
                    with self.subTest(body=body, existing=existing):
                        state = self.origin_state() if existing else {}
                        if existing:
                            state["origins"][URL]["mode"] = INBOX.CHAT
                        world = FakeWorld([] if existing else [todo("mentioned", url=f"{URL}#note_6")],
                                          notes=[note(6, body=body)], owners=None if existing else {})
                        inbox = make(world, state)
                        inbox.poll()
                        self.assertIn(URL, inbox.state["origins"])
                        self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.DELIVER)
                        self.assertEqual(inbox.state["origins"][URL]["initiator"], "pengjh")
                        dispatched = world.prompts if existing else world.launched
                        self.assertEqual(len(dispatched), 1)
                        self.assertFalse(dispatched[0].startswith(INBOX.CHAT_TAG))
                        self.assert_debug_delivery_contract(dispatched[0])
                        self.assertNotIn("用户补充", dispatched[0])
                        for obsolete in ("mini", "不测试", "只开发"):
                            self.assertNotIn(obsolete, " ".join(world.comments))

    def test_go_debug_keeps_extra_instructions(self) -> None:
        for command in ("debug", "修 Bug", "极简 模式", "MINIMAL"):
            with self.subTest(command=command):
                state = self.origin_state()
                state["origins"][URL]["mode"] = INBOX.CHAT
                extra = "按钮点不动，保留 debug 日志"
                world = FakeWorld([], notes=[note(6, body=f"/go {command} {extra}")])
                make(world, state).poll()
                self.assertEqual(state["origins"][URL]["mode"], INBOX.DELIVER)
                self.assert_debug_delivery_contract(world.prompts[0])
                self.assertIn(f"用户补充：{extra}", world.prompts[0])

    def test_debug_mentions_and_negations_remain_conversation(self) -> None:
        for body in ("不要 debug", "不要极简模式", "不是修bug", "这次不使用 minimal",
                     "debug 日志在哪里", "极简模式是什么", "请解释 /go debug", "按极简模式推进"):
            for existing in (True, False):
                with self.subTest(body=body, existing=existing):
                    state = self.origin_state() if existing else {}
                    if existing:
                        state["origins"][URL]["mode"] = INBOX.CHAT
                    world = FakeWorld([] if existing else [todo("mentioned", url=f"{URL}#note_6")],
                                      notes=[note(6, body=body)])
                    inbox = make(world, state)
                    inbox.poll()
                    if existing:
                        self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.CHAT)
                    else:
                        self.assertNotIn(URL, inbox.state["origins"])
                    self.assertEqual(len(world.prompts), 1)
                    self.assertIn(body, world.prompts[0])
                    self.assertEqual(world.launched, [])

    def test_debug_bridge_footer_routes_standalone_mode(self) -> None:
        for footer in ("_（来自飞书）_", "_（来自飞书秘书）_"):
            for existing in (True, False):
                with self.subTest(footer=footer, existing=existing):
                    state = self.origin_state() if existing else {}
                    if existing:
                        state["origins"][URL]["mode"] = INBOX.CHAT
                    body = f"极简模式\n\n{footer}"
                    world = FakeWorld([] if existing else [todo("mentioned", url=f"{URL}#note_6")],
                                      notes=[note(6, body=body)], owners=None if existing else {})
                    inbox = make(world, state)
                    inbox.poll()
                    self.assertIn(URL, inbox.state["origins"])
                    self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.DELIVER)
                    dispatched = world.prompts if existing else world.launched
                    self.assertEqual(len(dispatched), 1)
                    self.assert_debug_delivery_contract(dispatched[0])
                    self.assertNotIn("用户补充", dispatched[0])

    def test_debug_bridge_footer_preserves_noncommand_body(self) -> None:
        for footer in ("_（来自飞书）_", "_（来自飞书秘书）_"):
            for text in ("不要极简模式", "> 极简模式", "极简模式\n\n> 引用上一条消息",
                         "> 上一条消息\n\n极简模式", f"极简模式\n\n{footer}\n正文仍有内容",
                         "极简模式 _（来自其他系统）_"):
                with self.subTest(footer=footer, text=text):
                    state = self.origin_state()
                    state["origins"][URL]["mode"] = INBOX.CHAT
                    body = f"{text}\n\n{footer}"
                    world = FakeWorld([], notes=[note(6, body=body)])
                    make(world, state).poll()
                    self.assertEqual(state["origins"][URL]["mode"], INBOX.CHAT)
                    self.assertEqual(len(world.prompts), 1)
                    self.assertIn(body, world.prompts[0])
                    self.assertEqual(world.launched, [])

    def test_go_with_other_extra_is_not_debug(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["mode"] = INBOX.CHAT
        world = FakeWorld([], notes=[note(6) | {"body": "/go 这个不要走debug"}])
        make(world, state).poll()
        self.assertIn("§0 复杂度判定开始", world.prompts[0])

    def test_go_without_live_scheduler_launches_delivery_not_chat(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["mode"] = INBOX.CHAT
        world = FakeWorld([], notes=[note(6) | {"body": "/go"}], owners={URL: ("frontend-web-v1-7", None)})
        make(world, state).poll()
        self.assertEqual(len(world.launched), 1)
        self.assertFalse(world.launched[0].startswith(INBOX.CHAT_TAG))

    def test_go_on_unrelated_issue_makes_it_an_origin(self) -> None:
        other = "https://git.example/frontend/web-v1/-/issues/99"
        world = FakeWorld([todo("mentioned", url=f"{other}#note_6")], notes=[note(6) | {"body": "@pjhagent /go"}],
                          owners={})
        inbox = make(world)
        inbox.poll()
        self.assertEqual(inbox.state["origins"][other]["mode"], INBOX.DELIVER)
        self.assertEqual(inbox.state["origins"][other]["initiator"], "pengjh")
        self.assertFalse(world.launched[0].startswith(INBOX.CHAT_TAG))

    def test_go_while_already_delivering_only_forwards_extra(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/go"}])
        state = self.origin_state()  # 旧记录没有 mode：按交付处理
        make(world, state).poll()
        self.assertEqual(world.prompts, [])
        self.assertIn("已经在交付流程中", world.comments[0])


    def test_description_params_start_scheduler_and_stick(self) -> None:
        desc = "需求说明\n/model gpt-6.1-sol\n/thinking HIGH\n"
        world = FakeWorld([todo(description=desc)], notes=[note(5)], owners={URL: ("frontend-web-v1-7", None)})
        inbox = make(world)
        inbox.poll()
        # 模型短名解析成完整 selector；之后调度者退出、由评论走入口接手时沿用同样参数。
        self.assertEqual(world.params, [("jc-gpt/gpt-6.1-sol", "high")])
        inbox.state["launched_at"].clear()
        world.notes.append(note(6))
        inbox.poll()
        self.assertEqual(world.params[-1], ("jc-gpt/gpt-6.1-sol", "high"))

    def test_thinking_only_is_allowed(self) -> None:
        world = FakeWorld([todo(description="/thinking low")], notes=[note(5)])
        make(world).poll()
        self.assertEqual(world.params, [("", "low")])

    def test_no_params_use_defaults(self) -> None:
        world = FakeWorld([todo(description="随便写点需求，提到 /model 但不在行首")], notes=[note(5)])
        make(world).poll()
        self.assertEqual(world.params, [("", "")])

    def test_bad_params_wait_and_warn_once(self) -> None:
        cases = {"unknown model": "/model gpt-9", "ambiguous model": "/model claude-opus",
                 "unsupported thinking": "/model gpt-6.1-sol\n/thinking max"}
        for name, desc in cases.items():
            with self.subTest(name):
                world = FakeWorld([todo(description=desc)])
                inbox = make(world)
                inbox.poll()
                inbox.poll()
                self.assertEqual((world.launched, world.done, inbox.state["origins"]), ([], [], {}))
                self.assertEqual(len(world.comments), 1)
                self.assertIn("/model", world.comments[0])

    def test_mention_then_assignment_still_tracks_origin(self) -> None:
        # #362 事故：先 @本账号 走了入口，紧接着指派；指派落在冷却期内也必须登记为发起 issue，
        # 否则调度者一评论，GitLab 自动清掉指派待办，之后的回复与 /go 全部无人接收。
        world = FakeWorld([todo("directly_addressed", url=f"{URL}#note_6"), todo(todo_id=2)],
                          notes=[note(6)], owners={})
        inbox = make(world)
        inbox.poll()
        self.assertEqual(len(world.launched), 1)
        self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.CHAT)
        self.assertIn("todos/2/mark_as_done", world.done)
        world.todos = []
        world.owners = {URL: ("frontend-web-v1-7", "w2:p1")}
        world.notes = [note(6), note(7, author="pjhagent"), note(8) | {"body": "/go"}]
        inbox.poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("/go", world.prompts[0])
        self.assertEqual(inbox.state["origins"][URL]["mode"], INBOX.DELIVER)

    def test_failed_launch_does_not_leave_a_half_registered_origin(self) -> None:
        world = FakeWorld([todo()])
        original = world.__call__
        def failing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if argv[0] == "hwf":
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return original(argv, **kw)
        inbox = make(failing)  # type: ignore[arg-type]
        with self.assertRaises(INBOX.InboxError):
            inbox.poll()
        self.assertEqual((inbox.state["origins"], world.done), ({}, []))


    def issue(self, url: str = URL) -> dict[str, Any]:
        return {"web_url": url, "project_id": 10, "iid": 7, "state": "opened", "description": ""}

    def test_followed_issue_reply_forwards_without_mention(self) -> None:
        # #422 回归：用户在 Agent 建的子 issue 里直接回复、没有 @，也要转给负责的调度者。
        world = FakeWorld([], notes=[note(5, created_at="2000-01-01T00:00:00Z"), note(6, author="pjhagent"),
                                     note(7, system=True), note(8, body="MR 已经合并")])
        world.authored = [self.issue()]
        inbox = make(world, {"follow_since": SINCE})
        inbox.poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("note_8", world.prompts[0])
        self.assertIn("本账号参与的 issue 下的回复", world.prompts[0])
        self.assertTrue(any("已收到" in c for c in world.comments))
        inbox.poll()  # 已转交的不重复
        self.assertEqual(len(world.prompts), 1)

    def test_first_follow_round_only_sets_watermark(self) -> None:
        world = FakeWorld([], notes=[note(8)])
        world.authored = [self.issue()]
        inbox = make(world)
        inbox.poll()
        self.assertEqual((world.prompts, world.issue_queries), ([], []))
        self.assertIn("follow_since", inbox.state)

    def test_followed_failure_holds_watermark(self) -> None:
        world = FakeWorld([], notes=[note(8)], prompt_rc=1)
        world.authored = [self.issue()]
        inbox = make(world, {"follow_since": SINCE})
        inbox.poll()
        self.assertEqual(inbox.state["follow_since"], SINCE)
        world.prompt_rc = 0
        inbox.poll()
        self.assertEqual(sum("body 8" in p for p in world.prompts), 2)  # 第一次被拒，第二次送达
        self.assertNotEqual(inbox.state["follow_since"], SINCE)  # 全部送达后推进到本轮时间

    def test_mentioned_issue_is_followed_afterwards(self) -> None:
        other = "https://git.example/frontend/web-v1/-/issues/9"
        world = FakeWorld([todo("mentioned", url=f"{other}#note_6")], notes=[note(6, body="@pjhagent 看下")],
                          owners={other: ("frontend-web-v1-9", "w1:p1")})
        inbox = make(world, {"follow_since": SINCE})
        inbox.poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("@提及", world.prompts[0])
        self.assertIn(other, inbox.state["followed"])
        self.assertTrue(any("iids[]=7" in q for q in world.issue_queries))
        # 之后这个 issue 里不带 @ 的回复也转交；同一条已转交的 @ 评论不会被重复处理。
        world.todos, world.notes = [], [note(6, body="@pjhagent 看下"), note(10, body="补充一句")]
        world.by_iid = [self.issue(other)]
        inbox.poll()
        self.assertEqual(len(world.prompts), 2)
        self.assertIn("note_10", world.prompts[1])

    def test_origin_issue_is_not_double_polled_as_followed(self) -> None:
        state = {"follow_since": SINCE, "origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                                      "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        world = FakeWorld([], notes=[note(6)])
        world.authored = [self.issue()]
        make(world, state).poll()
        self.assertEqual(len(world.prompts), 1)


def label_event(action: str, name: str, at: str) -> dict[str, Any]:
    return {"action": action, "label": {"name": name}, "created_at": at}


# #481 实际的标签历史（2026-11-13）：两次进出「等你处理」，最后一次之前是「开发中」
EVENTS_481 = [label_event("add", "需求", "2026-11-12T12:41"), label_event("add", "状态:待开始", "2026-11-12T12:41"),
              label_event("add", "状态:等你处理", "2026-11-13T05:29"), label_event("remove", "状态:待开始", "2026-11-13T05:29"),
              label_event("add", "状态:开发中", "2026-11-13T05:32"), label_event("remove", "状态:等你处理", "2026-11-13T05:32"),
              label_event("add", "状态:等你处理", "2026-11-13T05:36"), label_event("remove", "状态:开发中", "2026-11-13T05:36")]


class WaitingLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def origin_state(self) -> dict[str, Any]:
        return {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7, "repo": REPO,
                                  "initiator": "pengjh", "last_note_id": 5}}}

    def test_previous_stage_follows_the_latest_wait(self) -> None:
        self.assertEqual(INBOX.previous_stage(EVENTS_481), "状态:开发中")
        self.assertIsNone(INBOX.previous_stage([label_event("add", "状态:等你处理", "2026-01-01")]))

    def test_user_reply_on_waiting_issue_restores_stage_and_tells_scheduler(self) -> None:
        world = FakeWorld([], notes=[note(6, body="我已经信任了"), note(7, body="还有一句")])
        world.issue_labels = ["需求", "状态:等你处理"]
        world.label_events = EVENTS_481
        make(world, self.origin_state()).poll()
        self.assertEqual(world.label_updates,
                         ["projects/10/issues/7?add_labels=状态:开发中&remove_labels=状态:等你处理"])  # 第二条不再改
        self.assertEqual(len(world.prompts), 2)
        self.assertIn("改回「状态:开发中」", world.prompts[0])
        self.assertIn("仍需要用户处理，改回「状态:等你处理」", world.prompts[0])
        self.assertNotIn("改回「状态:开发中」", world.prompts[1])

    def test_waiting_since_creation_is_left_alone(self) -> None:
        world = FakeWorld([], notes=[note(6)])
        world.issue_labels = ["状态:等你处理"]
        world.label_events = [label_event("add", "状态:等你处理", "2026-01-01")]
        make(world, self.origin_state()).poll()
        self.assertEqual(world.label_updates, [])
        self.assertEqual(len(world.prompts), 1)

    def test_not_waiting_needs_no_extra_queries(self) -> None:
        world = FakeWorld([], notes=[note(6)])
        world.label_events = EVENTS_481
        make(world, self.origin_state()).poll()
        self.assertEqual(world.label_updates, [])
        self.assertNotIn("改回", world.prompts[0])

    def test_label_update_failure_still_delivers(self) -> None:
        world = FakeWorld([], notes=[note(6)])
        world.issue_labels = ["状态:等你处理"]
        world.label_events = EVENTS_481
        world.label_update_rc = 1
        make(world, self.origin_state()).poll()
        self.assertEqual(len(world.prompts), 1)
        self.assertNotIn("改回", world.prompts[0])
        self.assertEqual(len(world.comments), 1)  # 回执照发

    def test_status_command_does_not_touch_label(self) -> None:
        world = FakeWorld([], notes=[note(6, body="/status")])
        world.issue_labels = ["状态:等你处理"]
        world.label_events = EVENTS_481
        make(world, self.origin_state()).poll()
        self.assertEqual(world.label_updates, [])

    def test_mention_on_waiting_issue_uses_todo_labels(self) -> None:
        world = FakeWorld([todo("mentioned", url=f"{URL}#note_6", labels=("状态:等你处理",))], notes=[note(6)])
        world.label_events = EVENTS_481
        make(world).poll()
        self.assertEqual(world.label_updates,
                         ["projects/10/issues/7?add_labels=状态:开发中&remove_labels=状态:等你处理"])


class ProtocolChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        rows = [type("Row", (), {"scheduler": "delivery-scheduler-a"})(), type("Row", (), {"scheduler": ""})()]
        fake_fs = type("FS", (), {"active_rows": staticmethod(lambda repo: rows)})
        fs_patcher = patch.object(INBOX, "find_scheduler_module", lambda: fake_fs)
        fs_patcher.start()
        self.addCleanup(fs_patcher.stop)

    def world(self, rev: str) -> FakeWorld:
        world = FakeWorld([])
        inner = world.__call__
        agents = [{"name": "delivery-scheduler-a", "pane_id": "w1:p1"}, {"name": "delivery-scheduler-idle", "pane_id": "w1:p9"},
                  {"name": "i1-dev", "pane_id": "w1:p2"}]

        def call(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, rev + "\n", "")
            if argv[:3] == ["herdr", "agent", "list"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agents": agents}}), "")
            return inner(argv, **kw)
        world.call = call  # type: ignore[attr-defined]
        return world

    def run_round(self, world: FakeWorld, state: dict[str, Any]) -> None:
        INBOX.Inbox(CONFIG, state, run=world.call).poll()  # type: ignore[attr-defined]

    def test_first_run_only_records_revision(self) -> None:
        state: dict[str, Any] = {}
        world = self.world("aaaa1111")
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "aaaa1111")
        self.assertEqual(world.prompts, [])

    def test_new_revision_notifies_busy_schedulers_once(self) -> None:
        state: dict[str, Any] = {"protocol_rev": "aaaa1111"}
        world = self.world("bbbb2222")
        self.run_round(world, state)
        self.assertEqual(world.prompt_panes, ["w1:p1"])  # 没有未收尾交付的调度者不打扰
        command = next(part for part in world.prompts[0].split("`") if part.startswith("git "))
        self.assertEqual(
            shlex.split(command)[3:],
            ["diff", "aaaa1111", "bbbb2222", "--", "PROTOCOL.md", "protocol/", "skills/", "roles.yaml", "herdr/"],
        )
        self.run_round(world, state)
        self.assertEqual(len(world.prompts), 1)

    def test_topic_only_commit_notifies_and_unrelated_commit_does_not(self) -> None:
        # 真正的 Git 历史验证路径范围，避免假执行器无论查哪个文件都返回同一个版本。
        with tempfile.TemporaryDirectory(prefix="hwf-protocol-") as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", directory, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()

            git("init", "--quiet")
            git("config", "user.name", "Protocol Test")
            git("config", "user.email", "protocol-test@example.invalid")
            git("config", "commit.gpgsign", "false")
            git("config", "core.hooksPath", "/dev/null")
            (root / "PROTOCOL.md").write_text("Protocol entry\n")
            (root / "protocol").mkdir()
            topic = root / "protocol/communication.md"
            topic.write_text("Original notification rules\n")
            git("add", ".")
            git("commit", "--quiet", "-m", "Initial protocol")
            old = git("rev-parse", "HEAD")
            state: dict[str, Any] = {"protocol_rev": old}
            world = self.world("")
            original_call = world.call

            def call(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
                if argv[0] == "git":
                    return subprocess.run(argv, capture_output=True, text=True)
                return original_call(argv, **kw)

            world.call = call
            topic.write_text("Updated notification rules\n")
            git("add", "protocol/")
            git("commit", "--quiet", "-m", "Update topic only")
            revised = git("rev-parse", "HEAD")
            with patch.object(INBOX, "HWF_HOME", root):
                self.run_round(world, state)
                self.assertEqual(state["protocol_rev"], revised)
                self.assertEqual(world.prompt_panes, ["w1:p1"])
                command = next(part for part in world.prompts[0].split("`") if part.startswith("git "))
                diff = git(*shlex.split(command)[3:])
                self.assertIn("+Updated notification rules", diff)
                self.run_round(world, state)
                (root / "unrelated.txt").write_text("Not a protocol change\n")
                git("add", "unrelated.txt")
                git("commit", "--quiet", "-m", "Unrelated change")
                self.run_round(world, state)
                self.assertEqual(state["protocol_rev"], revised)
                self.assertEqual(len(world.prompts), 1)
                (root / "skills/demo").mkdir(parents=True)
                (root / "skills/demo/SKILL.md").write_text("Skill rules\n")
                git("add", "skills/")
                git("commit", "--quiet", "-m", "Skill only")
                self.run_round(world, state)
                (root / "herdr").mkdir(parents=True)
                (root / "herdr/config.yaml").write_text("profiles: {}\n")
                git("add", "herdr/")
                git("commit", "--quiet", "-m", "Role config only")
                self.run_round(world, state)
                (root / "roles.yaml").write_text("default_profile: scheduler\nteam: {}\nservices: {}\n")
                git("add", "roles.yaml")
                git("commit", "--quiet", "-m", "Unified roles only")
                self.run_round(world, state)
            self.assertEqual(state["protocol_rev"], git("rev-parse", "HEAD"))
            self.assertEqual(len(world.prompts), 4)

    def test_failed_notice_keeps_old_revision_for_retry(self) -> None:
        state: dict[str, Any] = {"protocol_rev": "aaaa1111"}
        world = self.world("bbbb2222")
        world.prompt_rc = 1
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "aaaa1111")
        world.prompt_rc = 0
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "bbbb2222")
        self.assertEqual(world.prompt_panes, ["w1:p1", "w1:p1"])
        self.run_round(world, state)
        self.assertEqual(len(world.prompts), 2)

    def test_scheduler_query_failure_keeps_old_revision_for_retry(self) -> None:
        state: dict[str, Any] = {"protocol_rev": "aaaa1111"}
        world = self.world("bbbb2222")
        original_call = world.call

        def call(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["herdr", "agent", "list"]:
                return subprocess.CompletedProcess(argv, 1, "", "Agent list unavailable")
            return original_call(argv, **kw)

        world.call = call
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "aaaa1111")
        self.assertEqual(world.prompts, [])
        world.call = original_call
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "bbbb2222")
        self.assertEqual(world.prompt_panes, ["w1:p1"])

    def test_find_scheduler_module_really_loads(self) -> None:
        with patch.object(INBOX, "_FIND_SCHEDULER", None):
            fs = INBOX.Inbox.__init__.__globals__["find_scheduler_module"]()
        self.assertTrue(callable(fs.active_rows))

    def test_unknown_revision_changes_nothing(self) -> None:
        state: dict[str, Any] = {"protocol_rev": "aaaa1111"}
        world = self.world("")
        self.run_round(world, state)
        self.assertEqual(state["protocol_rev"], "aaaa1111")
        self.assertEqual(world.prompts, [])


if __name__ == "__main__":
    unittest.main()


# 2026-11-03 #452 调度者实际卡住的 omp 提问框（herdr agent read --source visible 截取）。
OMP_ASK = """
 ⎋ Asking how to unblock arch
╭─ Ask ─────────────────────────────────────────────────────────────╮
│ Claude Code 登录失效，arch 起不来。怎么继续？                       │
├───────────────────────────────────────────────────────────────────┤
│ ❯ ○ 我去 /login，完成后你重派 arch (Recommended)                   │
│       你在任意终端执行 claude → 输入 /login 完成登录，回一句「好了」。 │
│   ○ 换 omp + devin/claude-opus-5-5 顶上                            │
│       我立刻用 omp 执行体起一个分析会话，不用你动终端。              │
│   ○ 按额度降级用 jc-gpt/gpt-6-astra                                 │
│       用 jc-gpt/gpt-6-astra + thinking medium 起新的独立会话。       │
│   ○ Other (type your own)                                          │
├───────────────────────────────────────────────────────────────────┤
│ ⏎ select · n note · ↑/↓ move · ⎋ cancel · ⌥H hide                  │
╰───────────────────────────────────────────────────────────────────╯
"""

CLAUDE_PERMISSION = """
⏺ Bash(rm -rf build)
╭──────────────────────────────────────────╮
│ Bash command                             │
│                                          │
│   rm -rf build                           │
│                                          │
│ Do you want to proceed?                  │
│   1. Yes                                 │
│ ❯ 2. Yes, and don't ask again            │
│   3. No, and tell Claude what to do differently (esc) │
╰──────────────────────────────────────────╯
"""


class ParseDialogTest(unittest.TestCase):
    def test_omp_ask_box(self) -> None:
        dialog = INBOX.parse_dialog(OMP_ASK)
        assert dialog is not None
        self.assertEqual(dialog["question"], "Claude Code 登录失效，arch 起不来。怎么继续？")
        self.assertEqual([o["label"] for o in dialog["options"]][:2],
                         ["我去 /login，完成后你重派 arch (Recommended)", "换 omp + devin/claude-opus-5-5 顶上"])
        self.assertIn("回一句「好了」", dialog["options"][0]["detail"])
        self.assertEqual([o["typing"] for o in dialog["options"]], [False, False, False, True])
        self.assertEqual(dialog["cursor"], 0)

    def test_claude_numbered_prompt_tracks_cursor(self) -> None:
        dialog = INBOX.parse_dialog(CLAUDE_PERMISSION)
        assert dialog is not None
        self.assertEqual(dialog["question"], "Do you want to proceed?")
        self.assertEqual(len(dialog["options"]), 3)
        self.assertEqual(dialog["cursor"], 1)
        self.assertTrue(dialog["options"][2]["typing"])

    def test_unreadable_screens_are_left_to_the_user(self) -> None:
        self.assertIsNone(INBOX.parse_dialog("just some output\n"))
        self.assertIsNone(INBOX.parse_dialog("Pick one\n❯ ○ only option\n"))
        self.assertIsNone(INBOX.parse_dialog("Pick\n1. a\n3. b\n"))
        self.assertIsNone(INBOX.parse_dialog("Pick\n❯ 1. [ ] a\n  2. [x] b\n"))


class RemoteChoiceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {"origins": {URL: {"project": "frontend/web-v1", "project_id": 10, "iid": 7,
                                        "repo": REPO, "initiator": "pengjh", "last_note_id": 5}}}
        self.world = FakeWorld([], notes=[note(5)])
        self.world.agent_status = "blocked"
        self.world.screen = OMP_ASK
        self.inbox = make(self.world, self.state)
        self.inbox.sleep = lambda _: None

    def reply(self, note_id: int, body: str, author: str = "pengjh") -> None:
        self.world.notes.append(note(note_id, author=author, body=body))
        self.inbox.poll()

    def test_blocked_dialog_is_posted_with_numbered_options_once(self) -> None:
        self.inbox.poll()
        self.inbox.poll()
        self.assertEqual(len(self.world.comments), 1)
        posted = self.world.comments[0]
        self.assertIn("@pengjh", posted)
        self.assertIn("1. 我去 /login", posted)
        self.assertIn("4. Other (type your own)（要打字，只能到电脑上选）", posted)

    def test_number_reply_from_feishu_presses_keys_and_reports(self) -> None:
        self.inbox.poll()
        self.reply(6, "3\n\n_（来自飞书）_")
        self.assertEqual(self.world.keys, [["down", "down", "enter"]])
        self.assertEqual(self.world.prompts, [])  # 数字是作答，不转交调度者
        self.assertIn("已替你选了第 3 项", self.world.comments[-1])
        self.assertNotIn("blocked_dialog", self.state["origins"][URL])
        self.assertEqual(self.state["origins"][URL]["last_note_id"], 6)

    def test_first_option_is_just_enter_and_words_are_accepted(self) -> None:
        self.inbox.poll()
        self.reply(6, "选 1")
        self.assertEqual(self.world.keys, [["enter"]])

    def test_changed_dialog_is_not_answered(self) -> None:
        self.inbox.poll()
        self.world.screen = OMP_ASK.replace("怎么继续？", "要重试吗？")
        self.reply(6, "1")
        self.assertEqual(self.world.keys, [])
        self.assertIn("题目已经换了", self.world.comments[-1])
        self.assertIn("要重试吗？", self.world.comments[-1])
        self.reply(7, "2")  # 按新题目作答
        self.assertEqual(self.world.keys, [["down", "enter"]])

    def test_typing_option_and_out_of_range_are_refused(self) -> None:
        self.inbox.poll()
        self.reply(6, "4")
        self.reply(7, "9")
        self.assertEqual(self.world.keys, [])
        self.assertIn("要打字回答", self.world.comments[-2])
        self.assertIn("没有第 9 项", self.world.comments[-1])

    def test_still_blocked_on_same_question_is_reported(self) -> None:
        self.world.after_keys = None
        self.inbox.poll()
        self.reply(6, "2")
        self.assertIn("可能没生效", self.world.comments[-1])

    def test_number_after_scheduler_moved_on_presses_nothing(self) -> None:
        self.inbox.poll()
        self.world.agent_status = "working"
        self.world.notes.append(note(6, body="1"))
        self.inbox.handle_note(URL, REPO, self.world.notes[-1], self.state["origins"][URL])
        self.assertEqual(self.world.keys, [])
        self.assertIn("已经不在选择界面", self.world.comments[-1])

    def test_unreadable_blocked_screen_keeps_old_reminder(self) -> None:
        self.world.screen = "Allow tool? weird ui"
        self.inbox.poll()
        self.reply(6, "1")
        self.assertEqual(self.world.keys, [])
        self.assertIn("请到电脑上打开它的窗口处理", self.world.comments[0])


class LoadConfigTest(unittest.TestCase):
    """入库配置 + 本机配置合并：只盯两边都有的项目，本机可覆盖其余键，旧格式照常可用。"""

    def setUp(self) -> None:
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="hwf-inbox-cfg-"))
        self.shared, self.local = self.dir / "shared.json", self.dir / "local.json"

    def load(self, shared, local=None):
        self.shared.write_text(json.dumps(shared))
        if local is not None:
            self.local.write_text(json.dumps(local))
        return INBOX.load_config(self.shared, self.local)

    def test_merges_and_skips_projects_without_local_path(self) -> None:
        cfg = self.load({"host": "h", "allowed_users": ["u"], "label": "l", "projects": ["a/b", "c/d"]},
                        {"projects": {"a/b": "/r/ab", "x/y": "/r/xy"}, "allowed_users": ["v"]})
        self.assertEqual(cfg["projects"], {"a/b": "/r/ab"})
        self.assertEqual(cfg["allowed_users"], ["v"])
        self.assertEqual(cfg["host"], "h")

    def test_old_single_file_format_still_works(self) -> None:
        cfg = self.load({"host": "h", "allowed_users": ["u"], "label": "l", "projects": {"a/b": "/r/ab"}})
        self.assertEqual(cfg["projects"], {"a/b": "/r/ab"})

    def test_missing_shared_config_returns_none(self) -> None:
        self.assertIsNone(INBOX.load_config(self.dir / "nope.json", self.local))


class HotReloadTest(unittest.TestCase):
    def test_detects_valid_script_change_and_skips_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-inbox-reload-") as tmp:
            root = Path(tmp)
            s1 = root / "gitlab-inbox.py"
            s2 = root / "find-scheduler.py"
            s1.write_text("x = 1\n", encoding="utf-8")
            s2.write_text("y = 2\n", encoding="utf-8")
            paths = (s1, s2)
            base = INBOX.snapshot_mtimes(paths)
            self.assertEqual(INBOX.reloadable_changes(base, paths), [])

            # 语法错误的中间态不触发重载，避免常驻进程重启后直接崩溃。
            s1.write_text("def broken(:\n", encoding="utf-8")
            os_utime_ns = base[s1] + 1_000_000
            import os
            os.utime(s1, ns=(os_utime_ns, os_utime_ns))
            self.assertEqual(INBOX.reloadable_changes(base, paths), [])

            # 写入合法代码后触发重载。
            s1.write_text("x = 3\n", encoding="utf-8")
            os.utime(s1, ns=(os_utime_ns + 2_000_000, os_utime_ns + 2_000_000))
            self.assertEqual(INBOX.reloadable_changes(base, paths), [s1])


class ClosedChatOriginAndBackoffTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-close-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chat_mode_closed_origin_prompts_silent_wrapup_and_clears_stage_labels(self) -> None:
        state = {
            "origins": {
                URL: {
                    "project": "frontend/web-v1",
                    "project_id": 10,
                    "iid": 7,
                    "repo": REPO,
                    "initiator": "pengjh",
                    "mode": INBOX.CHAT,
                    "last_note_id": 5,
                }
            }
        }
        world = FakeWorld([], notes=[note(5)])
        world.issue_state = "closed"
        world.issue_labels = ["需求", "状态:待开始", "状态:等你处理", "herdr-auto"]
        make(world, state).poll()
        self.assertNotIn(URL, state["origins"])
        self.assertEqual(world.label_updates, ["projects/10/issues/7?remove_labels=状态:待开始,状态:等你处理"])
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("cancelled（对话模式未开工，用户已关闭 issue）", world.prompts[0])
        self.assertIn("禁止在 issue 发追问评论", world.prompts[0])

    def test_deliver_mode_closed_origin_still_checks_evidence_and_clears_stage_labels(self) -> None:
        state = {
            "origins": {
                URL: {
                    "project": "frontend/web-v1",
                    "project_id": 10,
                    "iid": 7,
                    "repo": REPO,
                    "initiator": "pengjh",
                    "mode": INBOX.DELIVER,
                    "last_note_id": 5,
                }
            }
        }
        world = FakeWorld([], notes=[note(5)])
        world.issue_state = "closed"
        world.issue_labels = ["状态:待上线", "herdr-auto"]
        make(world, state).poll()
        self.assertEqual(world.label_updates, ["projects/10/issues/7?remove_labels=状态:待上线"])
        self.assertIn("请用户重开继续或明确取消", world.prompts[0])

    def test_backoff_and_failure_suppression_recover_cleanly(self) -> None:
        self.assertEqual(INBOX.compute_poll_backoff(60, 0), 60)
        self.assertEqual(INBOX.compute_poll_backoff(60, 1), 60)
        self.assertEqual(INBOX.compute_poll_backoff(60, 2), 120)
        self.assertEqual(INBOX.compute_poll_backoff(60, 3), 240)
        self.assertEqual(INBOX.compute_poll_backoff(60, 4), 300)
        self.assertEqual(INBOX.compute_poll_backoff(60, 12), 300)

        tracker = INBOX.PollFailureTracker(heartbeat_every=3)
        self.assertEqual(tracker.record_failure(RuntimeError("TLS handshake timeout")), "poll failed: TLS handshake timeout")
        self.assertIsNone(tracker.record_failure(RuntimeError("TLS handshake timeout")))
        hb = tracker.record_failure(RuntimeError("TLS handshake timeout"))
        self.assertIsNotNone(hb)
        assert hb is not None
        self.assertIn("3 consecutive rounds", hb)
        # 错误原因变化时立即输出，不等下一次心跳周期
        changed = tracker.record_failure(RuntimeError("502 Bad Gateway"))
        self.assertEqual(changed, "poll failed: 502 Bad Gateway")
        recovered = tracker.record_success()
        self.assertEqual(recovered, "poll recovered after 4 consecutive failure(s)")
        self.assertEqual(tracker.consecutive_failures, 0)
        self.assertIsNone(tracker.record_success())


class FakeInboxJev:
    def __init__(self, go_prob: float = 0.0, flow: str = "standard", flow_prob: float = 0.92, fail: bool = False) -> None:
        self.go_prob = go_prob
        self.flow = flow
        self.flow_prob = flow_prob
        self.fail = fail
        self.calls = 0

    def decide(self, *, state: Any = None, questions: Any = None, **_: Any) -> Any:
        self.calls += 1
        if self.fail:
            raise INBOX.JC.JevUnavailableError("offline")
        other = round(1.0 - self.flow_prob, 4)
        probs = {"standard": self.flow_prob, "debug": other} if self.flow == "standard" else {"standard": other, "debug": self.flow_prob}
        return INBOX.JC.JevResult.from_payload({
            "model": "jev-1.14.0",
            "answers": {
                "go_authorized": {"type": "noul", "noul": self.go_prob},
                "flow": {"type": "choice", "choice": self.flow, "confidence": 0.90, "probabilities": probs},
            },
        })


class NaturalGoTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="hwf-inbox-jev-")
        patcher = patch.object(INBOX, "STATE_PATH", Path(tmp) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _chat_state(self) -> dict[str, Any]:
        return {
            "origins": {
                URL: {
                    "project": "frontend/web-v1",
                    "project_id": 10,
                    "iid": 7,
                    "repo": REPO,
                    "initiator": "pengjh",
                    "mode": INBOX.CHAT,
                    "last_note_id": 5,
                }
            }
        }

    def test_watched_scripts_includes_jev_client(self) -> None:
        names = {p.name for p in INBOX.watched_scripts()}
        self.assertIn("jev_client.py", names)
        self.assertIn("gitlab-inbox.py", names)

    def test_high_confidence_natural_go_switches_chat_to_deliver_and_preserves_full_text(self) -> None:
        state = self._chat_state()
        world = FakeWorld([], notes=[note(5), note(6, body="方案没问题，按这个开始写代码交付吧")])
        jev = FakeInboxJev(go_prob=0.95, flow="standard")
        INBOX.Inbox(CONFIG, state, run=world, jev=jev).poll()
        self.assertEqual(jev.calls, 1)
        self.assertEqual(state["origins"][URL]["mode"], INBOX.DELIVER)
        self.assertEqual(len(world.prompts), 1)
        self.assertIn("明确开工指令：从对话模式切换到交付流程", world.prompts[0])
        self.assertIn("按 ~/.hwf/protocol/planning.md 从 §0 复杂度判定开始", world.prompts[0])
        self.assertIn("用户补充：方案没问题，按这个开始写代码交付吧", world.prompts[0])

    def test_natural_debug_go_switches_to_debug_flow(self) -> None:
        state = self._chat_state()
        world = FakeWorld([], notes=[note(5), note(6, body="直接按 debug 模式把这个线上小 bug 修掉")])
        jev = FakeInboxJev(go_prob=0.94, flow="debug", flow_prob=0.91)
        INBOX.Inbox(CONFIG, state, run=world, jev=jev).poll()
        self.assertEqual(state["origins"][URL]["mode"], INBOX.DELIVER)
        self.assertIn("用户明确要求 debug 模式", world.prompts[0])
        self.assertIn("用户补充：直接按 debug 模式把这个线上小 bug 修掉", world.prompts[0])

    def test_discussion_or_jev_unavailable_stays_in_chat_mode(self) -> None:
        for name, jev in (
            ("below_threshold", FakeInboxJev(go_prob=0.65, flow="standard")),
            ("unavailable", FakeInboxJev(fail=True)),
        ):
            with self.subTest(name):
                state = self._chat_state()
                world = FakeWorld([], notes=[note(5), note(6, body="如果改成异步会不会丢消息？先评估下")])
                INBOX.Inbox(CONFIG, state, run=world, jev=jev).poll()
                self.assertEqual(state["origins"][URL]["mode"], INBOX.CHAT)
                self.assertEqual(len(world.prompts), 1)
                self.assertNotIn("从对话模式切换到交付流程", world.prompts[0])

    def test_deliver_mode_never_queries_jev(self) -> None:
        state = self._chat_state()
        state["origins"][URL]["mode"] = INBOX.DELIVER
        world = FakeWorld([], notes=[note(5), note(6, body="顺便补一条边界用例")])
        jev = FakeInboxJev(go_prob=0.99)
        INBOX.Inbox(CONFIG, state, run=world, jev=jev).poll()
        self.assertEqual(jev.calls, 0)


class FollowedPruneAndSessionRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hwf-inbox-prune-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        patcher = patch.object(INBOX, "STATE_PATH", self.tmp / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prunes_closed_followed_older_than_7d_and_suppresses_cross_url_transient_errors(self) -> None:
        now = 1_800_000_000.0
        old_url = "https://git.example/frontend/web-v1/-/issues/101"
        recent_url = "https://git.example/frontend/web-v1/-/issues/102"
        open_url = "https://git.example/frontend/web-v1/-/issues/103"
        state = {
            "follow_since": now - 60,
            "followed_pruned_at": now - 7200,
            "followed": {
                old_url: {"project": "frontend/web-v1", "iid": 101},
                recent_url: {"project": "frontend/web-v1", "iid": 102},
                open_url: {"project": "frontend/web-v1", "iid": 103},
            },
        }
        world = FakeWorld([])
        world.closed_followed = [
            {"iid": 101, "web_url": old_url, "state": "closed", "closed_at": "2020-01-01T00:00:00Z"},
            {"iid": 102, "web_url": recent_url, "state": "closed",
             "closed_at": INBOX.time.strftime("%Y-%m-%dT%H:%M:%SZ", INBOX.time.gmtime(now - 86400))},
        ]
        evicted = make(world, state).prune_closed_followed(now)
        self.assertEqual(evicted, [old_url])
        self.assertEqual(set(state["followed"].keys()), {recent_url, open_url})

        # 跨 URL 的同类 TLS/DNS 瞬时故障归一为同一签名，连败期间不因 URL 变化重复刷日志。
        tracker = INBOX.PollFailureTracker(heartbeat_every=0)
        first = tracker.record_failure(RuntimeError("GET projects/10/issues/7 failed: TLS handshake timeout"))
        second = tracker.record_failure(RuntimeError("GET projects/10/issues/8 failed: TLS handshake timeout"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(tracker.record_success(), "poll recovered after 2 consecutive failure(s)")

    def test_rotates_only_truly_idle_oversized_scheduler_sessions(self) -> None:
        repo_dir = self.tmp / "web-v1"
        (repo_dir / ".hwf/tmp").mkdir(parents=True)
        (repo_dir / ".hwf/tmp/INDEX.md").write_text(
            "| 任务标识 | 输入原文 | 调度者 | 状态 |\n"
            "| `frontend-web-v1-1` | done | `delivery-scheduler-w1-p1` | done |\n"
            "| `frontend-web-v1-2` | busy | `delivery-scheduler-w1-p2` | in-progress |\n",
            encoding="utf-8",
        )
        big_idle = self.tmp / "idle.jsonl"
        big_busy = self.tmp / "busy.jsonl"
        small_idle = self.tmp / "small.jsonl"
        with big_idle.open("wb") as fh:
            fh.seek(INBOX.MAX_IDLE_SCHEDULER_SESSION_BYTES + 1024)
            fh.write(b"\n")
        with big_busy.open("wb") as fh:
            fh.seek(INBOX.MAX_IDLE_SCHEDULER_SESSION_BYTES + 1024)
            fh.write(b"\n")
        small_idle.write_bytes(b"{}\n")

        cfg = {**CONFIG, "projects": {"frontend/web-v1": str(repo_dir)}}
        world = FakeWorld([])
        world.agents_list = [
            {"name": "delivery-scheduler-w1-p1", "pane_id": "w1:p1", "agent_status": "idle",
             "agent_session": {"value": str(big_idle)}},
            {"name": "delivery-scheduler-w1-p2", "pane_id": "w1:p2", "agent_status": "idle",
             "agent_session": {"value": str(big_busy)}},
            {"name": "delivery-scheduler-w1-p3", "pane_id": "w1:p3", "agent_status": "idle",
             "agent_session": {"value": str(small_idle)}},
        ]
        rotated = INBOX.Inbox(cfg, {}, run=world, jev=None).rotate_oversized_idle_schedulers()
        self.assertEqual(rotated, ["delivery-scheduler-w1-p1"])
        self.assertEqual(world.resets, ["w1:p1"])


class DynamicAllProjectAndAuthBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="hwf-dyn-")
        self.addCleanup(self.tmp_obj.cleanup)
        self.tmp = Path(self.tmp_obj.name)
        patcher = patch.object(INBOX, "STATE_PATH", self.tmp / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_colleague_assign_to_bot_launches_readonly_chat_and_blocks_colleague_go(self) -> None:
        cloned_dir = self.tmp / "code" / "api"
        url = "https://git.example/backend/api/-/issues/88"
        notes = [
            {
                "id": 201, "body": "@pjhagent /go 请马上改代码上线", "system": False,
                "author": {"username": "colleague"},
            }
        ]
        t_assign = {
            "id": 901,
            "action_name": "assigned",
            "target_type": "Issue",
            "target_url": url,
            "author": {"username": "colleague"},
            "project": {"id": 99, "path_with_namespace": "backend/api"},
            "target": {
                "id": 880, "iid": 88, "project_id": 99, "title": "接口超时",
                "state": "opened", "labels": [], "description": "请排查", "web_url": url,
            },
        }
        calls: list[list[str]] = []
        prompts: list[str] = []
        done_todos: list[int] = []
        comments: list[str] = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "clone"]:
                cloned_dir.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if any("find-scheduler.py" in str(x) for x in cmd):
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"task": "backend-api-88", "pane": "w9:p1"}), "")
            if cmd[0] == "herdr":
                if cmd[1:3] == ["agent", "list"]:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {"agents": []}}), "")
                if cmd[1:3] == ["agent", "get"]:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {"agent": {"agent_status": "idle"}}}), "")
                if cmd[1:3] == ["team", "start"]:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {"window_id": "w9", "pane_id": "w9:p1"}}), "")
                if cmd[1:3] == ["agent", "prompt"]:
                    prompts.append(cmd[cmd.index("--text") + 1] if "--text" in cmd else cmd[-1])
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {"ok": True}}), "")
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {}}), "")
            if cmd[0] == "glab":
                method = cmd[cmd.index("-X") + 1]
                path = unquote(cmd[cmd.index("-X") + 2])
                base = path.split("?")[0]
                if base == "user":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"username": "pjhagent"}), "")
                if base == "todos":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps([t_assign] if 901 not in done_todos else []), "")
                if base.endswith("/mark_as_done"):
                    done_todos.append(int(base.split("/")[1]))
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 901}), "")
                if base == "projects/backend/api":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 99, "path_with_namespace": "backend/api", "archived": False}), "")
                if base in ("projects/99/issues/88", "projects/backend/api/issues/88"):
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({
                        "iid": 88, "project_id": 99, "title": "接口超时", "state": "opened",
                        "labels": [], "description": "请排查", "web_url": url,
                    }), "")
                if base.endswith("/notes") and method == "GET":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(notes), "")
                if "/notes/" in base and method == "GET":
                    nid = int(base.rsplit("/notes/", 1)[1])
                    matched = next((n for n in notes if n["id"] == nid), notes[0])
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(matched), "")
                if base.endswith("/notes") and method == "POST":
                    fields = dict(cmd[i + 1].split("=", 1) for i, c in enumerate(cmd) if c == "-f")
                    body_val = fields.get("body", "")
                    if not body_val and "body=" in path:
                        body_val = unquote(path.split("body=", 1)[1])
                    comments.append(body_val)
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": 999}), "")
                if base.endswith("/resource_label_events"):
                    return subprocess.CompletedProcess(cmd, 0, "[]", "")
                if base == "projects/backend/api/issues":
                    return subprocess.CompletedProcess(cmd, 0, "[]", "")
                if base == "issues":
                    return subprocess.CompletedProcess(cmd, 0, "[]", "")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        cfg = {
            "host": "git.example",
            "agent_user": "pjhagent",
            "allowed_users": ["pengjh"],
            "label": "herdr-auto",
            "projects": {},
            "code_root": str(self.tmp / "code"),
            "local_config_path": str(self.tmp / "local.json"),
        }
        state: dict[str, Any] = {}
        inbox = INBOX.Inbox(cfg, state, run=fake_run, jev=None)
        inbox._user_token_loaded = True
        inbox._user_token_value = ""
        inbox.poll()
        # 同事指派给机器人：自动 clone + 启动只读 CHAT + 写入 watched_issues
        self.assertTrue(any(c[:2] == ["git", "clone"] for c in calls))
        self.assertIn(url, state["origins"])
        self.assertEqual(state["origins"][url]["mode"], "chat")
        self.assertEqual(state["watched_issues"][url]["status"], "active")
        self.assertIn(901, done_todos)

        # 同事随后在评论里发 /go：严禁切换到 deliver，仍保持 chat 且提示无权改代码
        t_mention = {
            "id": 902,
            "action_name": "mentioned",
            "target_type": "Issue",
            "target_url": f"{url}#note_201",
            "author": {"username": "colleague"},
            "project": {"id": 99, "path_with_namespace": "backend/api"},
            "target": {
                "id": 880, "iid": 88, "project_id": 99, "title": "接口超时",
                "state": "opened", "labels": [], "description": "请排查", "web_url": url,
            },
        }
        inbox.handle_mention(t_mention)
        self.assertEqual(state["origins"][url]["mode"], "chat")
        self.assertIn(902, done_todos)
        self.assertTrue(any("禁止视作 /go" in p for p in prompts) or any("无权" in c or "仅" in c or "授权" in c for c in comments))

    def test_poll_user_items_registers_standby_readonly_without_clone_or_mark_done(self) -> None:
        url = "https://git.example/ops/deploy/-/issues/15"
        user_writes: list[list[str]] = []
        git_clones: list[list[str]] = []
        user_todos: list[dict[str, Any]] = []

        def fake_run(cmd, env=None, **kw):
            if cmd[:2] == ["git", "clone"]:
                git_clones.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "glab":
                method = cmd[cmd.index("-X") + 1]
                path = unquote(cmd[cmd.index("-X") + 2])
                base = path.split("?")[0]
                as_user = bool(env and env.get("GITLAB_TOKEN") == "user-tok")
                if as_user and method != "GET":
                    user_writes.append(list(cmd))
                if base == "todos" and as_user:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(user_todos), "")
                if base == "issues" and as_user:
                    return subprocess.CompletedProcess(cmd, 0, "[]", "")
                if base == "projects/ops/deploy":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"path_with_namespace": "ops/deploy", "archived": False}), "")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        cfg = {
            "host": "git.example",
            "agent_user": "pjhagent",
            "allowed_users": ["pengjh"],
            "label": "herdr-auto",
            "projects": {},
            "code_root": str(self.tmp / "code"),
            "local_config_path": str(self.tmp / "local.json"),
        }
        state: dict[str, Any] = {}
        inbox = INBOX.Inbox(cfg, state, run=fake_run, jev=None)
        inbox._user_token_loaded = True
        inbox._user_token_value = "user-tok"
        # 第一轮先建立水位，防止旧待办洪泛
        inbox.poll_user_items()
        self.assertEqual(state.get("user_todo_watermark"), 0)
        # 第二轮产生新 todo（id=555 > 0）：只读登记为 standby，不 clone、不 mark_as_done
        user_todos.append({
            "id": 555,
            "action_name": "mentioned",
            "target_type": "Issue",
            "target_url": f"{url}#note_10",
            "author": {"username": "colleague"},
            "project": {"id": 55, "path_with_namespace": "ops/deploy"},
            "target": {"iid": 15, "title": "发版脚本异常", "state": "opened", "web_url": url},
        })
        inbox.poll_user_items()
        self.assertEqual(user_writes, [])
        self.assertEqual(git_clones, [])
        self.assertNotIn(url, state.get("origins", {}))
        self.assertIn(url, state["watched_issues"])
        self.assertEqual(state["watched_issues"][url]["status"], "standby")
        self.assertIn("gitlab_projects.py", [p.name for p in INBOX.watched_scripts()])

    def test_closed_issue_mention_todo_is_skipped_without_clone_comment_or_watch(self) -> None:
        url = "https://git.example/backend/user/-/issues/1"
        git_clones: list[list[str]] = []
        comments: list[str] = []
        done_todos: list[int] = []
        launched: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "git" and "clone" in cmd:
                git_clones.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "hwf":
                launched.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["glab", "api"]:
                endpoint = cmd[-1]
                if endpoint.endswith("/mark_as_done"):
                    done_todos.append(int(endpoint.split("/")[1]))
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
                if "/notes" in endpoint and "-X" in cmd and "POST" in cmd:
                    comments.append(endpoint)
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        cfg = {
            "host": "git.example",
            "agent_user": "pjhagent",
            "allowed_users": ["pengjh"],
            "label": "herdr-auto",
            "projects": {},
            "code_root": str(self.tmp / "code"),
            "local_config_path": str(self.tmp / "local.json"),
        }
        state: dict[str, Any] = {}
        inbox = INBOX.Inbox(cfg, state, run=fake_run, jev=None)
        inbox.me = "pjhagent"
        closed_todo = {
            "id": 3100,
            "action_name": "mentioned",
            "target_type": "Issue",
            "target_url": f"{url}#note_18884",
            "body": "/go 将工作区切换到 feat-workspace @pjhagent",
            "author": {"username": "pengjh"},
            "project": {"id": 42, "path_with_namespace": "backend/user"},
            "target": {
                "id": 100, "iid": 1, "project_id": 42, "title": "旧单",
                "state": "closed", "labels": [], "web_url": url,
            },
        }
        inbox.handle_mention(closed_todo)
        self.assertEqual(done_todos, [3100])
        self.assertEqual(git_clones, [])
        self.assertEqual(comments, [])
        self.assertEqual(launched, [])
        self.assertNotIn(url, state.get("followed", {}))
        self.assertNotIn(url, state.get("watched_issues", {}))

    def test_followup_and_feishu_originated_mentions_do_not_reannounce_watched_issue(self) -> None:
        url = "https://git.example/frontend/web-v1/-/issues/513"
        fresh_url = "https://git.example/frontend/web-v1/-/issues/514"
        repo_dir = self.tmp / "web-v1"
        repo_dir.mkdir(parents=True, exist_ok=True)
        notes = {
            "projects/10/issues/513/notes/3001": {
                "id": 3001,
                "author": {"username": "pengjh"},
                "body": "@pjhagent 顺手补充一个细节",
                "system": False,
            },
            "projects/10/issues/514/notes/3002": {
                "id": 3002,
                "author": {"username": "pengjh"},
                "body": "@pjhagent MR 已合并，close issue\n\n_（来自飞书秘书）_",
                "system": False,
            },
        }
        done_todos: list[int] = []

        def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["glab", "api"]:
                endpoint = cmd[-1]
                if endpoint in notes:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(notes[endpoint]), "")
                if endpoint.endswith("/mark_as_done"):
                    done_todos.append(int(endpoint.split("/")[1]))
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
                if "/notes" in endpoint and "-X" in cmd and "POST" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
            if cmd[:2] == ["herdr", "agent"] and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": {"agents": []}}), "")
            if cmd[0] == "hwf":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        cfg = {
            "host": "git.example",
            "agent_user": "pjhagent",
            "allowed_users": ["pengjh"],
            "label": "herdr-auto",
            "projects": {},
            "dynamic_projects": True,
            "code_root": str(self.tmp),
            "local_config_path": str(self.tmp / "local.json"),
        }
        (self.tmp / "local.json").write_text(
            json.dumps({"projects": {"frontend/web-v1": str(repo_dir)}}), encoding="utf-8"
        )
        state: dict[str, Any] = {
            "origins": {
                url: {"project": "frontend/web-v1", "project_id": 10, "iid": 513, "repo": str(repo_dir),
                      "initiator": "pengjh", "last_note_id": 2999, "mode": "deliver"},
            },
            "watched_issues": {
                url: {"url": url, "project": "frontend/web-v1", "project_id": 10, "iid": 513,
                      "title": "翻转水印", "kind": "mentioned_agent", "actor": "pengjh",
                      "event_key": "todo:4769", "status": "active"},
            },
        }
        inbox = INBOX.Inbox(cfg, state, run=fake_run, jev=None)
        inbox.me = "pjhagent"
        inbox.handle_note = lambda *_a, **_kw: True  # type: ignore[method-assign]

        # 1. 已在跟进的 #513 收到新的 @pjhagent 评论：不应覆写 event_key 重弹接活通知
        inbox.handle_mention({
            "id": 4999,
            "action_name": "mentioned",
            "target_type": "Issue",
            "target_url": f"{url}#note_3001",
            "body": "@pjhagent 顺手补充一个细节",
            "author": {"username": "pengjh"},
            "project": {"id": 10, "path_with_namespace": "frontend/web-v1"},
            "target": {"id": 5130, "iid": 513, "project_id": 10, "title": "翻转水印", "state": "opened", "web_url": url},
        })
        self.assertEqual(state["watched_issues"][url]["event_key"], "todo:4769")

        # 2. 来自飞书秘书的评论（带 _（来自飞书秘书）_）：即使首次出现也不写入 watched_issues 触发回弹
        inbox.handle_mention({
            "id": 5000,
            "action_name": "mentioned",
            "target_type": "Issue",
            "target_url": f"{fresh_url}#note_3002",
            "body": "@pjhagent MR 已合并，close issue\n\n_（来自飞书秘书）_",
            "author": {"username": "pengjh"},
            "project": {"id": 10, "path_with_namespace": "frontend/web-v1"},
            "target": {"id": 5140, "iid": 514, "project_id": 10, "title": "新单", "state": "opened", "web_url": fresh_url},
        })
        self.assertNotIn(fresh_url, state["watched_issues"])
        self.assertEqual(done_todos, [4999, 5000])




