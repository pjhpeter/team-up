"""聚焦验证 GitLab 收件箱：发起门槛、按归属转交评论、转交失败不丢不重。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
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
        self.opened_assigned: list[dict[str, Any]] = []  # 指派给本账号、带开关标签、仍打开的 issue
        self.authored: list[dict[str, Any]] = []  # 本账号建的、近期有更新的 issue
        self.by_iid: list[dict[str, Any]] = []  # 按 iids[] 查到的、近期有更新的 issue
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

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        out, rc = "", 0
        if argv[:2] == ["glab", "api"]:
            method, path = argv[5], argv[6]
            if method == "POST" and "/notes?body=" in path:
                self.comments.append(unquote(path.split("body=", 1)[1]))
            elif method == "POST":
                self.done.append(path)
            elif path == "user":
                out = json.dumps({"username": "pjhagent"})
            elif path.startswith("todos?"):
                out = json.dumps(self.todos)
            elif "/issues?state=opened" in path:
                out = json.dumps(self.opened_assigned)
            elif "/issues?scope=all" in path:
                self.issue_queries.append(path)
                out = json.dumps(self.authored if "author_username=" in path else self.by_iid)
            elif "/notes/" in path:
                out = json.dumps(next(n for n in self.notes if path.endswith(f"/{n['id']}")))
            elif "/notes?" in path:
                out = json.dumps(sorted(self.notes, key=lambda n: -n["id"]))
            else:
                out = json.dumps({"state": self.issue_state})
        elif argv[1].endswith("reset-session.py"):
            rc = self.reset_rc
            self.resets.append(argv[2])
            out = "/sessions/new-session.jsonl" if rc == 0 else "busy"
        elif argv[1].endswith("find-scheduler.py"):
            assert argv[3] == "--owner-of", argv  # 收件箱只用归属查询，入口发现交给 team-up。
            task, pane = self.owners.get(argv[4], (None, None))
            out = json.dumps({"task": task, "pane": pane, "live_pane": pane, "scheduler": "delivery-scheduler-w1-p1" if task else None,
                              "status": "in-progress（§0 判定中）" if task else None})
        elif argv[:3] == ["herdr", "agent", "get"]:
            out = json.dumps({"result": {"agent": {"agent_status": self.agent_status,
                                                   "terminal_title_stripped": "π > 派 arch 判定复杂度"}}})
        elif argv[0] == "hwf":
            values = [argv[i + 1] for i, a in enumerate(argv) if a == "--input"]
            inputs = dict(v.split("=", 1) for v in values)
            self.launched.append(inputs["task"])
            self.params.append((inputs["model"], inputs["thinking"]))
        elif argv[:3] == ["omp", "models", "find"]:
            out = json.dumps({"models": [m for m in MODELS if argv[3] in m["selector"]]})
        elif argv[:3] == ["herdr", "agent", "prompt"]:
            rc = self.prompt_rc
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
        self.assertIn("已关闭", world.prompts[0])

    def test_reset_command_tolerates_mentioning_the_agent(self) -> None:
        world = FakeWorld([], notes=[note(6) | {"body": "/new @pjhagent"}])
        make(world, self.origin_state()).poll()
        self.assertEqual(world.resets, ["w1:p1"])

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
        self.assertIn("已关闭", world.prompts[-1])
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


    def test_forwarded_comment_gets_an_immediate_receipt(self) -> None:
        # 调度者在干活时要回合结束才回复；收件箱立即回执，用户在远程知道消息已到、它在干什么。
        world = FakeWorld([], notes=[note(6)])
        make(world, self.origin_state()).poll()
        self.assertEqual(len(world.comments), 1)
        self.assertIn("正在干活", world.comments[0])
        self.assertIn("派 arch 判定复杂度", world.comments[0])
        self.assertIn("§0 判定中", world.comments[0])

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

    def test_go_minimal_skips_complexity_and_keeps_rest_as_extra(self) -> None:
        for body, extra in (("@pjhagent /go 极简 先只改文案", "先只改文案"), ("/go minimal", ""), ("/go 极简模式", "")):
            state = self.origin_state()
            state["origins"][URL]["mode"] = INBOX.CHAT
            world = FakeWorld([], notes=[note(6) | {"body": body}])
            make(world, state).poll()
            self.assertIn("极简模式", world.prompts[0])
            self.assertNotIn("§0 复杂度判定开始", world.prompts[0])
            if extra:
                self.assertIn(f"用户补充：{extra}", world.prompts[0])
            self.assertIn("极简模式", world.comments[0])

    def test_go_with_other_extra_is_not_minimal(self) -> None:
        state = self.origin_state()
        state["origins"][URL]["mode"] = INBOX.CHAT
        world = FakeWorld([], notes=[note(6) | {"body": "/go 这个不要走极简"}])
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

