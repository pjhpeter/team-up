"""聚焦验证飞书桥：建单/关联/话题回复同步到 GitLab，GitLab 新评论回帖到飞书，防刷屏与白名单。"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

SPEC = importlib.util.spec_from_file_location(
    "feishu_bridge", Path(__file__).resolve().parents[1] / "scripts/feishu-bridge.py")
assert SPEC is not None and SPEC.loader is not None
FB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FB
SPEC.loader.exec_module(FB)

ME = "ou_me"
BOT = "ou_bot"


class World:
    """假 GitLab + 假 lark-cli，记录所有写操作。"""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {7: {"iid": 7, "title": "旧单", "state": "opened", "notes": [
            {"id": 70, "body": "很早的评论", "system": False, "author": {"name": "某人"}}]}}
        self.next_note = 100
        self.replies: list[dict] = []   # 飞书回帖
        self.writes: list[tuple] = []   # GitLab 写操作 (method, path, fields, as_user)
        self.history: dict[str, list] = {}  # chat_id → 聊天记录（补漏用）
        self.unreadable_chats: set[str] = set()
        self.messages: dict[str, str] = {}  # message_id → 渲染后的 content（给 mget 用）
        self.broken_images: set[str] = set()
        self.downloads: list[tuple] = []
        self.thread_of: dict[str, str] = {}  # 被机器人在话题里回复过的消息 → thread_id
        self.reply_failures = 0  # 接下来几次飞书回帖返回失败
        self.threads: dict[str, list] = {}  # thread_id → 话题消息（历史格式）
        self.unreadable_threads: set[str] = set()
        self.sent: list[dict] = []  # 机器人主动发的顶层消息（秘书）

    def __call__(self, cmd, input=None, env=None, cwd=None, **_):
        if cmd[0] == "lark-cli":
            return self.lark(cmd, input, cwd)
        if cmd[0] == "omp":  # 秘书回答提问用的模型，默认不可用
            return subprocess.CompletedProcess(cmd, 1, "", "")
        method, path = cmd[cmd.index("-X") + 1], unquote(cmd[cmd.index("-X") + 2])
        fields = dict(cmd[i + 1].split("=", 1) for i, c in enumerate(cmd) if c == "-f")
        as_user = bool(env and env.get("GITLAB_TOKEN") == "tok")
        base = path.split("?")[0]
        if method != "GET":
            self.writes.append((method, base, fields, as_user))
        if base == "user":
            return ok({"username": "pengjh" if as_user else "pjhagent"})
        if base == "users":
            return ok([{"id": 42, "username": "pjhagent"}])
        if base == "projects/frontend/web-v1/issues" and method == "POST":
            iid = max(self.issues) + 1
            self.issues[iid] = {"iid": iid, "title": fields["title"], "state": "opened", "notes": []}
            return ok({"iid": iid})
        parts = base.split("/")
        if len(parts) >= 5 and parts[3] == "issues":
            iid = int(parts[4])
            if iid not in self.issues:
                return subprocess.CompletedProcess([], 1, "", "404 Not Found")
            issue = self.issues[iid]
            if base.endswith("/notes") and method == "POST":
                self.next_note += 1
                note = {"id": self.next_note, "body": fields["body"], "system": False,
                        "author": {"name": "彭嘉辉"}}
                issue["notes"].append(note)
                return ok(note)
            if base.endswith("/notes"):
                notes = sorted(issue["notes"], key=lambda n: n["id"], reverse="sort=desc" in path)
                return ok(notes[:1] if re.search(r"per_page=1(&|$)", path) else notes)
            return ok({k: v for k, v in issue.items() if k != "notes"})
        raise AssertionError(f"unexpected {method} {path}")

    def lark(self, cmd, stdin, cwd=None):
        if cmd[1:3] == ["im", "+messages-mget"]:
            mid = cmd[cmd.index("--message-ids") + 1]
            return ok({"ok": True, "data": {"messages": [{"message_id": mid, "content": self.messages.get(mid, ""),
                                                          "thread_id": self.thread_of.get(mid, "")}]}})
        if cmd[1:3] == ["im", "+messages-resources-download"]:
            mid, key = cmd[cmd.index("--message-id") + 1], cmd[cmd.index("--file-key") + 1]
            if key in self.broken_images:
                return ok({"ok": False, "error": {"message": "resource gone"}})
            name = cmd[cmd.index('--output') + 1]
            kind = cmd[cmd.index("--type") + 1]
            out = Path(cwd) / (f"{name}.jpg" if kind == "image" else name)  # 图片由 lark-cli 补扩展名
            out.write_bytes(b"data")
            self.downloads.append((mid, key) if kind == "image" else (mid, key, kind, name))
            return ok({"ok": True, "data": {"saved_path": str(out)}})
        if cmd[1:3] == ["im", "+messages-reply"]:
            to = cmd[cmd.index("--message-id") + 1]
            if self.reply_failures:
                self.reply_failures -= 1
                return ok({"ok": False, "error": {"message": "temporary failure"}})
            if "--reply-in-thread" in cmd:
                self.thread_of.setdefault(to, f"omt_{to}")
            self.replies.append({"to": to, "text": stdin, "in_thread": "--reply-in-thread" in cmd,
                                 "key": cmd[cmd.index("--idempotency-key") + 1] if "--idempotency-key" in cmd else None})
            return ok({"ok": True, "data": {"message_id": f"om_r{len(self.replies)}"}})
        if cmd[1:3] == ["im", "+messages-send"]:
            self.sent.append({"chat": cmd[cmd.index("--chat-id") + 1], "text": stdin,
                              "key": cmd[cmd.index("--idempotency-key") + 1]})
            return ok({"ok": True, "data": {"message_id": f"om_s{len(self.sent)}"}})
        if cmd[1:3] == ["im", "+chat-messages-list"]:
            chat = cmd[cmd.index("--chat-id") + 1]
            if chat in self.unreadable_chats:
                return ok({"ok": False, "error": {"code": 230027, "message": "access denied"}})
            return ok({"ok": True, "data": {"messages": self.history.get(chat, [])}})
        if cmd[1:3] == ["im", "+threads-messages-list"]:
            thread = cmd[cmd.index("--thread") + 1]
            if thread in self.unreadable_threads:
                return ok({"ok": False, "error": {"message": "access denied"}})
            return ok({"ok": True, "data": {"messages": self.threads.get(thread, [])}})
        raise AssertionError(f"unexpected lark {cmd}")

    def gitlab_comment(self, iid: int, body: str, name: str = "交付调度者", system: bool = False) -> None:
        self.next_note += 1
        self.issues[iid]["notes"].append({"id": self.next_note, "body": body, "system": system,
                                          "author": {"name": name}})


def ok(data):
    return subprocess.CompletedProcess([], 0, json.dumps(data, ensure_ascii=False), "")


def msg(mid: str, text: str, root: str = "", sender: str = ME, sender_type: str = "user", mentions=None,
        chat: str = "oc_1", chat_type: str = "p2p", thread: str = "", reply_to: str = ""):
    return FB.normalize_event({"message_id": mid, "chat_id": chat, "chat_type": chat_type, "sender_id": sender,
                               "sender_type": sender_type, "content": text, "root_id": root,
                               "create_time": "1793604428990", "mentions": mentions or [],
                               "thread_id": thread, "reply_to": reply_to})


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
        self.state: dict = {}
        cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
               "users": {ME: "pengjh"}, "bot_open_id": BOT}
        self.uploads: list[tuple] = []
        self.tmp = tempfile.TemporaryDirectory()  # 观察队列写到这里，不碰本机 ~/.hwf/state
        self.addCleanup(self.tmp.cleanup)
        proactive = FB.proactive_module()
        self.addCleanup(setattr, proactive, "STATE_DIR", proactive.STATE_DIR)
        proactive.STATE_DIR = Path(self.tmp.name)
        self.b = FB.Bridge(cfg, self.state, run=self.w, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(tempfile.mkdtemp(prefix="fb-media-")))

    def fake_upload(self, project: str, path: Path) -> str:
        self.assertTrue(path.exists())
        self.uploads.append((project, path.name))
        return f"![{path.name}](/uploads/x/{path.name})"

    def test_folded_full_text_becomes_link_in_feishu(self) -> None:
        body = "能做，选 A。\n\n<details><summary>完整内容</summary>\n\n很长的全文\n\n</details>\n"
        text = self.b.render_note({"project": "frontend/web-v1", "iid": 7},
                                  {"id": 9, "body": body, "author": {"name": "agent"}})
        self.assertIn("能做，选 A。", text)
        self.assertNotIn("很长的全文", text)
        self.assertNotIn("<details>", text)
        self.assertIn("[查看完整内容](", text)
        self.assertIn("#note_9", text)

    def test_create_issue_as_user_assigned_to_agent_and_reply_link(self) -> None:
        self.b.handle(msg("om_1", "需求：画布最近列表缺旧画布\n详细描述"))
        method, path, fields, as_user = self.w.writes[0]
        self.assertEqual((method, path, as_user), ("POST", "projects/frontend/web-v1/issues", True))
        self.assertEqual(fields["title"], "画布最近列表缺旧画布")
        self.assertEqual(fields["labels"], "herdr-auto")
        self.assertEqual(fields["assignee_id"], "42")
        self.assertIn("来自飞书", fields["description"])
        self.assertEqual(self.state["threads"]["om_1"]["iid"], 8)
        self.assertEqual(self.w.replies[0]["to"], "om_1")
        self.assertIn("#8", self.w.replies[0]["text"])

    def test_image_before_requirement_creates_issue_with_attachment(self) -> None:
        # 实时事件和历史补漏使用不同图片标记，两者都不能挡住后面的需求。
        for index, marker in enumerate(("[Image: img_first]", "![Image](img_first)")):
            with self.subTest(marker=marker):
                mid = f"om_image_first_{index}"
                self.b.handle(msg(mid, f"{marker}\n需求：排查为什么会出现这个页面？"))
                creates = [w for w in self.w.writes if w[1].endswith("/issues")]
                self.assertEqual(len(creates), index + 1)
                fields = creates[-1][2]
                self.assertEqual(fields["title"], "排查为什么会出现这个页面？")
                self.assertIn("![img_first.jpg](/uploads/x/img_first.jpg)", fields["description"])
                self.assertEqual(self.w.downloads[-1], (mid, "img_first"))
                self.assertIn(f"#{8 + index}", self.w.replies[-1]["text"])

    def test_file_before_issue_command_binds_without_creating(self) -> None:
        for index, command in enumerate(("#7", "需求：https://git.example.com/frontend/web-v1/-/issues/7")):
            with self.subTest(command=command):
                self.b.handle(msg(f"om_bind_file_{index}",
                                  f'<file key="file_log" name="日志.zip"/>\n{command} 排查附件'))
                self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))
                self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
                self.assertEqual(self.w.writes[-1][2]["body"],
                                 "@pjhagent 排查附件\n\n![日志.zip](/uploads/x/日志.zip)\n\n_（来自飞书）_")

    def test_requirement_with_only_attachment_keeps_creating(self) -> None:
        self.b.handle(msg("om_attachment_only", "需求：[Image: img_only]"))
        creates = [w for w in self.w.writes if w[1].endswith("/issues")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2]["title"], "（见附件）")
        self.assertIn("![img_only.jpg](/uploads/x/img_only.jpg)", creates[0][2]["description"])

    def test_attachment_does_not_turn_prose_or_empty_requirement_into_command(self) -> None:
        # 私聊里不是命令的话交给秘书回答（模型不可用时回总览），空的「需求：」回用法
        cases = (("[Image: img_prose]\n这里写着需求：不是发起命令", "这会儿没法回答"), ("需求：", FB.HELP_TEXT))
        for index, (text, expected) in enumerate(cases):
            with self.subTest(text=text):
                self.b.handle(msg(f"om_not_command_{index}", text, chat=f"oc_not_command_{index}"))
                self.assertEqual(self.w.writes, [])
                self.assertEqual(self.w.downloads, [])
                self.assertIn(expected, self.w.replies[-1]["text"])

    def test_thread_reply_becomes_user_comment_and_command_stays_bare(self) -> None:
        self.b.handle(msg("om_1", "/需求 修个按钮"))
        self.b.handle(msg("om_2", "按钮在右上角", root="om_1"))
        self.b.handle(msg("om_3", "/go", root="om_1"))
        notes = [w for w in self.w.writes if w[1].endswith("/notes")]
        self.assertEqual([n[2]["body"] for n in notes], ["按钮在右上角\n\n_（来自飞书）_", "/go"])
        self.assertTrue(all(n[3] for n in notes))

    def test_bind_existing_issue_mentions_agent_and_skips_old_notes(self) -> None:
        self.b.handle(msg("om_1", "#7 帮忙看下进度"))
        t = self.state["threads"]["om_1"]
        self.assertEqual((t["iid"], t["last_note"], t["bound"]), (7, 70, True))
        self.assertEqual(self.w.writes[-1][2]["body"], "@pjhagent 帮忙看下进度\n\n_（来自飞书）_")
        self.b.handle(msg("om_2", "/status", root="om_1"))
        self.assertEqual(self.w.writes[-1][2]["body"], "@pjhagent /status")
        self.b.poll_gitlab()
        self.assertFalse(any("很早的评论" in r["text"] for r in self.w.replies))

    def test_demand_prefix_with_existing_link_binds_instead_of_creating(self) -> None:
        self.b.handle(msg("om_1", "需求：https://git.example.com/frontend/web-v1/-/work_items/7，处理这个 issue，只做前端"))
        self.assertFalse(any(w[1] == "projects/frontend/web-v1/issues" for w in self.w.writes))
        self.assertEqual(self.state["threads"]["om_1"]["iid"], 7)
        self.assertEqual(self.w.writes[-1][2]["body"], "@pjhagent 处理这个 issue，只做前端\n\n_（来自飞书）_")

    def test_gitlab_comments_flow_back_once_without_echo(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.b.handle(msg("om_2", "补充一句", root="om_1"))  # 桥自己发的评论
        self.w.gitlab_comment(8, "已收到，已转给调度者")
        self.w.gitlab_comment(8, "added label", system=True)
        before = len(self.w.replies)
        self.b.poll_gitlab()
        self.b.poll_gitlab()  # 第二轮不重复
        new = self.w.replies[before:]
        self.assertEqual(len(new), 1)
        self.assertIn("已收到，已转给调度者", new[0]["text"])
        self.assertEqual(new[0]["to"], "om_1")

    def test_failed_forward_is_retried_next_round_in_order(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.w.gitlab_comment(8, "第一条")
        self.w.gitlab_comment(8, "第二条")
        before = len(self.w.replies)
        self.w.reply_failures = 1
        self.b.poll_gitlab()
        self.assertEqual(len(self.w.replies), before)  # 第一条发失败，第二条也不越过它
        self.b.poll_gitlab()
        self.b.poll_gitlab()
        texts = [r["text"] for r in self.w.replies[before:]]
        self.assertEqual(len(texts), 2)
        self.assertIn("第一条", texts[0])
        self.assertIn("第二条", texts[1])

    def test_stuck_or_missing_command_is_a_bridge_error_and_next_round_recovers(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.w.gitlab_comment(8, "卡住时写的评论")
        before = len(self.w.replies)
        real_run = self.b.run
        for failure in (subprocess.TimeoutExpired(["glab"], FB.CMD_TIMEOUT_S), FileNotFoundError("glab")):
            def broken(*_a, _e=failure, **_k):
                raise _e
            self.b.run = broken
            with self.assertRaises(FB.BridgeError):  # 主循环只接 BridgeError 这类错误，不能让桥退出
                self.b.poll_gitlab()
        self.b.run = real_run
        self.b.poll_gitlab()
        self.assertIn("卡住时写的评论", self.w.replies[before]["text"])

    def break_gitlab_writes(self):
        """GitLab 写请求一律卡住；返回恢复函数。"""
        real_run = self.b.run

        def broken(cmd, **kwargs):
            if cmd[0] == "glab" and "POST" in cmd:
                raise subprocess.TimeoutExpired(cmd, FB.CMD_TIMEOUT_S)
            return real_run(cmd, **kwargs)
        self.b.run = broken
        return lambda: setattr(self.b, "run", real_run)

    def test_failed_create_asks_user_to_resend_and_keeps_pending_attachment(self) -> None:
        self.b.handle(msg("om_img", "[Image: img_v3_abc-1]"))
        before = len(self.w.replies)
        restore = self.break_gitlab_writes()
        self.b.handle(msg("om_req", "需求：做个东西"))
        self.assertEqual(self.w.replies[before:], [{"to": "om_req", "text": FB.FAILED_TEXT, "in_thread": False,
                                                    "key": "failed-om_req"}])
        self.assertNotIn("om_req", self.state["threads"])
        restore()
        self.b.handle(msg("om_req2", "需求：做个东西"))  # 重发是新消息；之前单独发的图还在
        self.assertEqual(self.state["threads"]["om_req2"]["iid"], 8)
        self.assertEqual(self.uploads[-1][1], "img_v3_abc-1.jpg")

    def test_failed_reply_after_issue_created_says_no_need_to_resend(self) -> None:
        self.w.reply_failures = 1  # 单已建好，只是「已建」这条回帖没发出去
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.assertEqual(sum(1 for w in self.w.writes if w[1].endswith("/issues")), 1)
        self.assertEqual(len(self.w.replies), 1)
        self.assertIn("#8", self.w.replies[0]["text"])
        self.assertIn("不用重发", self.w.replies[0]["text"])
        self.assertNotIn(FB.FAILED_TEXT, self.w.replies[0]["text"])

    def test_failed_bind_note_does_not_link_and_asks_to_resend(self) -> None:
        restore = self.break_gitlab_writes()
        self.b.handle(msg("om_1", "#7 帮忙看下"))
        self.assertNotIn("om_1", self.state["threads"])
        self.assertEqual(self.w.replies[-1]["text"], FB.FAILED_TEXT)
        restore()
        self.b.handle(msg("om_2", "#7 帮忙看下"))
        self.assertEqual(self.state["threads"]["om_2"]["iid"], 7)
        self.assertEqual(self.w.writes[-1][0:2], ("POST", "projects/frontend/web-v1/issues/7/notes"))

    def test_failed_thread_reply_asks_to_resend_inside_the_thread(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        restore = self.break_gitlab_writes()
        self.b.handle(msg("om_2", "补充一句", root="om_1"))
        restore()
        self.assertEqual(self.w.replies[-1], {"to": "om_2", "text": FB.FAILED_TEXT, "in_thread": True,
                                              "key": "failed-om_2"})

    def test_failure_notice_failing_too_does_not_raise(self) -> None:
        self.w.reply_failures = 2  # 飞书也发不出去
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.assertEqual(self.w.replies, [])
        self.assertEqual(self.state["threads"]["om_1"]["iid"], 8)

    def test_stranger_gets_no_resend_prompt(self) -> None:
        self.w.reply_failures = 1
        self.b.handle(msg("om_1", "需求：做个东西", sender="ou_stranger"))
        self.assertEqual(self.w.replies, [])

    def test_one_failed_message_does_not_stop_catch_up(self) -> None:
        self.state["chats"]["oc_1"] = 1
        self.w.history["oc_1"] = [
            {"message_id": "om_a", "sender": {"id": ME, "sender_type": "user"}, "content": "需求：第一条", "create_time": "1"},
            {"message_id": "om_b", "sender": {"id": ME, "sender_type": "user"}, "content": "需求：第二条", "create_time": "2"}]
        real_run = self.b.run
        calls = {"n": 0}

        def first_post_stuck(cmd, **kwargs):
            if cmd[0] == "glab" and "POST" in cmd:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise subprocess.TimeoutExpired(cmd, FB.CMD_TIMEOUT_S)
            return real_run(cmd, **kwargs)
        self.b.run = first_post_stuck
        self.b.catch_up()
        self.assertNotIn("om_a", self.state["threads"])
        self.assertIn("om_b", self.state["threads"])

    def test_note_that_keeps_failing_becomes_link_and_does_not_block(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.w.gitlab_comment(8, "飞书不收的内容")
        self.w.gitlab_comment(8, "后面的评论")
        before = len(self.w.replies)
        self.w.reply_failures = FB.NOTE_RETRIES
        for _ in range(FB.NOTE_RETRIES + 1):
            self.b.poll_gitlab()
        texts = [r["text"] for r in self.w.replies[before:]]
        self.assertEqual(len(texts), 2)
        self.assertIn("没能搬过来", texts[0])
        self.assertIn("后面的评论", texts[1])

    def test_long_comment_is_truncated_with_link(self) -> None:
        self.b.handle(msg("om_1", "需求：长内容"))
        self.w.gitlab_comment(8, "字" * 5000)
        self.b.poll_gitlab()
        text = self.w.replies[-1]["text"]
        self.assertLess(len(text), 2700)
        self.assertIn("全文见", text)

    def test_closed_issue_notifies_once_and_rejects_replies(self) -> None:
        self.b.handle(msg("om_1", "需求：会被关掉"))
        self.w.issues[8]["state"] = "closed"
        self.b.poll_gitlab()
        self.b.poll_gitlab()
        self.assertEqual(sum("已关闭" in r["text"] for r in self.w.replies), 1)
        self.b.handle(msg("om_2", "还有个问题", root="om_1"))
        self.assertIn("已关闭", self.w.replies[-1]["text"])
        self.assertFalse([w for w in self.w.writes if w[1].endswith("/notes")])

    def test_bot_and_duplicate_messages_are_ignored(self) -> None:
        self.b.handle(msg("om_1", "需求：一次", sender=BOT, sender_type="bot"))
        self.b.handle(msg("om_2", "需求：一次"))
        self.b.handle(msg("om_2", "需求：一次"))  # 补漏重复拿到
        creates = [w for w in self.w.writes if w[1].endswith("/issues")]
        self.assertEqual(len(creates), 1)

    def test_stranger_is_refused_and_nothing_written(self) -> None:
        self.b.handle(msg("om_1", "需求：我也要", sender="ou_other"))
        self.assertEqual(self.w.writes, [])
        self.assertIn(self.w.replies[0]["text"], FB.DENY_TEXTS)

    def test_stranger_replies_are_random_from_ten(self) -> None:
        self.assertEqual(len(set(FB.DENY_TEXTS)), 10)
        for i in range(60):
            self.b.handle(msg(f"om_s{i}", "在吗", sender="ou_other"))
        texts = {r["text"] for r in self.w.replies}
        self.assertTrue(texts <= set(FB.DENY_TEXTS))
        self.assertGreater(len(texts), 1)  # 不是固定一句
        self.assertEqual(self.w.writes, [])

    def test_group_small_talk_gets_help_once_per_cooldown(self) -> None:
        bot = [{"id": BOT}]
        self.b.handle(msg("om_1", "你叫什么名字？", chat="oc_g", chat_type="group", mentions=bot))
        self.b.handle(msg("om_2", "在吗", chat="oc_g", chat_type="group", mentions=bot))
        self.assertEqual(len(self.w.replies), 1)
        self.assertIn("用法", self.w.replies[0]["text"])
        self.assertEqual(self.w.writes, [])

    def test_group_mention_of_bot_is_stripped(self) -> None:
        # 原始接口格式：占位符 + 机器人 open_id
        self.b.handle(msg("om_1", "@_user_1 需求：群里提的", mentions=[{"key": "@_user_1", "id": BOT, "name": "交付助手"}]))
        # lark-cli 事件实际格式（2026-11-02 实测）：content 已渲染成名字，机器人 id 是应用 ID
        self.b.handle(msg("om_2", "@交付助手-试点 需求：事件格式",
                          mentions=[{"key": "@_user_1", "id": "cli_abc", "name": "交付助手-试点"}]))
        titles = [w[2]["title"] for w in self.w.writes if w[1].endswith("/issues")]
        self.assertEqual(titles, ["群里提的", "事件格式"])

    def test_group_message_without_bot_mention_gets_no_reply(self) -> None:
        # 开了群消息权限后，群里所有话都会推过来：同事闲聊、本人没 @ 的话都不能回复或写 GitLab
        self.b.handle(msg("om_1", "导出又挂了", sender="ou_other", chat="oc_g", chat_type="group"))
        self.b.handle(msg("om_2", "需求：顺手提一个", chat="oc_g", chat_type="group"))
        self.b.handle(msg("om_3", "@小王 你看下", chat="oc_g", chat_type="group",
                          mentions=[{"key": "@_user_2", "id": "ou_x", "name": "小王"}]))
        self.assertEqual(self.w.replies, [])
        self.assertEqual(self.w.writes, [])
        self.b.handle(msg("om_4", "@交付助手-试点 需求：这次 @ 了", chat="oc_g", chat_type="group",
                          mentions=[{"key": "@_user_1", "id": "cli_abc", "name": "交付助手-试点"}]))
        self.assertEqual([w[2]["title"] for w in self.w.writes if w[1].endswith("/issues")], ["这次 @ 了"])

    def observed(self) -> list:
        path = Path(self.tmp.name) / "proactive-feishu-queue.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_observe_chat_messages_go_to_queue_silently(self) -> None:
        self.b.observe_chats = {"oc_g": "研发小分队"}
        self.b.handle(msg("om_1", "导出又挂了 [Image: img_1]", sender="ou_other", chat="oc_g", chat_type="group",
                          thread="omt_9"))
        self.b.handle(msg("om_2", "我顺口说一句", chat="oc_g", chat_type="group"))  # 本人没 @ 也只观察
        self.b.handle(msg("om_3", "随便聊", sender="ou_other", chat="oc_x", chat_type="group"))  # 非观察群：丢掉
        self.assertEqual(self.w.replies, [])
        self.assertEqual(self.w.writes, [])
        rows = self.observed()
        self.assertEqual([r["mid"] for r in rows], ["om_1", "om_2"])
        self.assertEqual(rows[0]["text"], "导出又挂了 [图片]")
        self.assertEqual(len(rows[0]["images"]), 1)
        self.assertTrue(Path(rows[0]["images"][0]).is_file())
        self.assertTrue(rows[0]["images"][0].startswith(str(Path(self.tmp.name) / "proactive-media" / "om_1")))
        self.assertEqual((rows[0]["chat_name"], rows[0]["thread"], rows[0]["sender"]), ("研发小分队", "omt_9", "ou_other"))

    def test_observe_chat_stranger_mention_is_observed_and_gets_fixed_reply(self) -> None:
        self.b.observe_chats = {"oc_g": "研发小分队"}
        bot = [{"key": "@_user_1", "id": "cli_abc", "name": "交付助手-试点"}]
        self.b.handle(msg("om_1", "@交付助手-试点 导出挂了帮看下", sender="ou_other", chat="oc_g",
                          chat_type="group", mentions=bot))
        self.b.handle(msg("om_2", "@交付助手-试点 需求：本人明确指令", chat="oc_g", chat_type="group", mentions=bot))
        self.assertEqual([r["mid"] for r in self.observed()], ["om_1"])  # 本人 @ 是指令，不进观察
        self.assertIn(self.w.replies[0]["text"], FB.DENY_TEXTS)
        self.assertEqual([w[2]["title"] for w in self.w.writes if w[1].endswith("/issues")], ["本人明确指令"])

    def test_observe_files_are_named_not_downloaded_and_broken_image_is_skipped(self) -> None:
        self.b.observe_chats = {"oc_g": "研发小分队"}
        self.w.broken_images.add("img_bad")
        self.b.handle(msg("om_1", '日志在这 <file key="file_1" name="日志.zip"/> [Image: img_bad]', sender="ou_other",
                          chat="oc_g", chat_type="group"))
        row = self.observed()[0]
        self.assertEqual(row["text"], "日志在这 [文件 日志.zip] [图片]")
        self.assertEqual(row["images"], [])
        self.assertEqual(self.w.downloads, [])  # 文件不下载；坏图下载失败不影响入队
        self.assertEqual(self.w.replies, [])

    def test_observe_quote_reply_carries_quoted_text(self) -> None:
        self.b.observe_chats = {"oc_g": "研发小分队"}
        self.w.messages["om_old"] = "导出 PDF 点了没反应 [Image: img_9]"
        self.b.handle(msg("om_1", "我这也是", sender="ou_other", chat="oc_g", chat_type="group",
                          root="om_old", reply_to="om_old"))
        self.b.handle(msg("om_2", "话题里接着说", sender="ou_other", chat="oc_g", chat_type="group",
                          root="om_old", reply_to="om_old", thread="omt_1"))
        rows = self.observed()
        self.assertEqual(rows[0]["quote"], "导出 PDF 点了没反应 [附件]")
        self.assertEqual(rows[1]["quote"], "")  # 话题里不额外取
        self.assertEqual(self.w.replies, [])

    def test_catch_up_observes_group_history_but_skips_other_groups(self) -> None:
        self.b.observe_chats = {"oc_g": "研发小分队"}
        self.state["chats"] = {"oc_g": 1, "oc_x": 1}
        self.state["group_chats"] = ["oc_g", "oc_x"]
        item = lambda mid, chat: {"message_id": mid, "chat_id": chat, "content": "聊天", "create_time": "2026-11-08 17:00",
                                  "sender": {"id": "ou_other", "name": "小王", "sender_type": "user"}}
        self.w.history = {"oc_g": [item("om_1", "oc_g")], "oc_x": [item("om_2", "oc_x")]}
        self.b.catch_up()
        rows = self.observed()
        self.assertEqual([(r["mid"], r["sender_name"]) for r in rows], [("om_1", "小王")])
        self.assertEqual(self.w.replies, [])

    def test_other_people_mentioned_keep_their_name(self) -> None:
        self.b.handle(msg("om_1", "需求：让 @_user_2 看下", mentions=[{"key": "@_user_2", "id": "ou_x", "name": "小王"}]))
        self.assertEqual(self.w.writes[0][2]["title"], "让 @小王 看下")

    def test_catch_up_skips_unreadable_group_and_unmentioned_group_messages(self) -> None:
        self.b.handle(msg("om_g1", "@交付助手-试点 #7 看下", chat="oc_g", chat_type="group",
                          mentions=[{"key": "@_user_1", "id": "cli_abc", "name": "交付助手-试点"}]))
        self.state["chats"]["oc_g2"] = 1
        self.state["group_chats"].append("oc_g2")
        self.w.unreadable_chats.add("oc_g2")
        sender = {"id": ME, "name": "彭嘉辉", "sender_type": "user"}
        self.w.history["oc_g"] = [
            {"message_id": "om_g2", "chat_id": "oc_g", "content": "群里闲聊", "sender": sender},
            {"message_id": "om_g3", "chat_id": "oc_g", "content": "@交付助手-试点 需求：补漏到的", "sender": sender,
             "mentions": [{"key": "@_user_1", "id": "cli_abc", "name": "交付助手-试点"}]}]
        self.b.catch_up()  # oc_g2 读不到不应中断
        titles = [w[2]["title"] for w in self.w.writes if w[1].endswith("/issues")]
        self.assertEqual(titles, ["补漏到的"])
        self.assertFalse(any("用法" in r["text"] for r in self.w.replies))

    def test_reply_in_unknown_thread_asks_to_bind(self) -> None:
        self.b.handle(msg("om_2", "这是啥", root="om_unknown", thread="omt_unknown"))
        self.assertIn("还没关联 issue", self.w.replies[0]["text"])

    def test_quote_reply_to_image_makes_one_thread_with_image(self) -> None:
        # 2026-11-02 实测：先单独发图，再对图「回复」（主对话里的引用回复）写「需求：…」
        self.w.messages["om_img"] = "[Image: img_v3_abc-1]"
        self.b.handle(msg("om_img", "[Image: img_v3_abc-1]"))
        hint = self.w.replies[-1]
        self.assertIn("接着发「需求", hint["text"])
        self.assertFalse(hint["in_thread"])  # 提示不开话题
        self.b.handle(msg("om_req", "需求：测试环境报这个错，帮忙排查", root="om_img", reply_to="om_img"))
        create = [w for w in self.w.writes if w[1].endswith("/issues")][0]
        self.assertEqual(create[2]["title"], "测试环境报这个错，帮忙排查")
        self.assertIn("![img_v3_abc-1.jpg](/uploads/x/img_v3_abc-1.jpg)", create[2]["description"])
        self.assertEqual(len(self.w.downloads), 1)  # 待用图和被回复的图是同一张，只搬一次
        self.assertEqual(self.state["thread_ids"], {"omt_om_req": "om_req"})
        # 话题里的后续消息（root 可能指向图片）按 thread_id 找回 #8
        self.b.handle(msg("om_more", "补充：只在测试环境", root="om_img", thread="omt_om_req"))
        self.assertEqual(self.w.writes[-1][2]["body"], "补充：只在测试环境\n\n_（来自飞书）_")
        self.w.gitlab_comment(8, "看了，是权限问题")
        self.b.poll_gitlab()
        self.assertEqual(self.w.replies[-1]["to"], "om_req")  # Agent 回复和 issue 链接在同一个话题

    def test_lone_image_is_attached_to_next_requirement_within_ten_minutes(self) -> None:
        clock = [1_000_000.0]
        self.b.now = lambda: clock[0]
        self.b.handle(msg("om_img", "[Image: img_v3_a]"))
        self.b.handle(msg("om_req", "需求：看下这个报错"))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][-1][2]["description"]
        self.assertIn("img_v3_a.jpg", desc)
        self.b.handle(msg("om_img2", "[Image: img_v3_b]"))
        clock[0] += 601
        self.b.handle(msg("om_req2", "需求：另一件事"))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][-1][2]["description"]
        self.assertNotIn("img_v3_b", desc)

    def test_image_reply_in_linked_thread_becomes_comment_with_image(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        self.b.handle(msg("om_2", "[Image: img_v3_zz]", root="om_1"))
        body = self.w.writes[-1][2]["body"]
        self.assertTrue(body.startswith("![img_v3_zz.jpg]"))
        self.assertNotIn("[Image:", body)

    def test_image_download_failure_still_creates_issue(self) -> None:
        self.w.broken_images.add("img_v3_bad")
        self.b.handle(msg("om_1", "需求：看图 [Image: img_v3_bad]"))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][0][2]["description"]
        self.assertIn("没搬过来", desc)
        self.assertEqual(self.uploads, [])

    def test_file_root_then_demand_in_its_thread_carries_the_file(self) -> None:
        # 2026-11-04 实测（#459）：先单独发 zip，再在这条文件下面「回复话题」写「需求：…」，zip 曾被丢掉
        zip_tag = '<file key="file_v3_00174_zip" name="历史面板 · 可交互-html.zip"/>'
        self.w.messages["om_zip"] = zip_tag
        self.b.handle(msg("om_zip", zip_tag))
        hint = self.w.replies[-1]
        self.assertIn("收到附件", hint["text"])
        self.assertFalse(hint["in_thread"])
        self.b.handle(msg("om_req", "需求：按照交互设计优化画布快照交互效果", root="om_zip", thread="omt_zip"))
        create = [w for w in self.w.writes if w[1].endswith("/issues")][0]
        self.assertEqual(create[2]["title"], "按照交互设计优化画布快照交互效果")
        self.assertIn("历史面板 · 可交互-html.zip", create[2]["description"])
        self.assertEqual(self.w.downloads, [("om_zip", "file_v3_00174_zip", "file", "历史面板 · 可交互-html.zip")])
        self.assertEqual(self.uploads, [("frontend/web-v1", "历史面板 · 可交互-html.zip")])
        self.assertEqual(list(self.b.media_dir.iterdir()), [])  # 中转文件搬完就删

    def test_demand_in_someone_elses_thread_carries_earlier_text(self) -> None:
        # 2026-11-12 实测（#479）：同事在话题里描述问题，我回「需求：处理这个问题」，问题描述曾被丢掉
        def item(mid, text, name, sender_type="user", sender="ou_qi", mentions=None):
            return {"message_id": mid, "chat_id": "oc_g", "content": text, "create_time": "2026-11-12 19:20",
                    "mentions": mentions or [], "thread_id": "omt_q",
                    "sender": {"id": sender, "name": name, "sender_type": sender_type}}
        bot = [{"id": BOT, "key": "@_user_1", "name": "嘉辉的Agent"}]
        self.w.messages["om_q"] = "@嘉辉的Agent \n![Image](img_v3_q)\n组织管理员看不到所有团队"
        self.w.threads["omt_q"] = [
            item("om_q", self.w.messages["om_q"], "祁子德", mentions=bot),
            item("om_deny", "你 @ 我也没用", "嘉辉的Agent", sender_type="app", sender="cli_x"),
            item("om_s", "第二行补充\n附图 [Image: img_v3_s]", "施楚宇"),
            item("om_req", "@嘉辉的Agent 需求：处理这个问题", "彭嘉辉", sender=ME, mentions=bot),
            item("om_later", "这条在需求之后", "施楚宇"),
        ]
        self.b.handle(msg("om_req", "需求：处理这个问题", root="om_q", thread="omt_q", chat="oc_g",
                          chat_type="group", mentions=bot))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][0][2]["description"]
        self.assertTrue(desc.startswith("处理这个问题\n\n**话题里的前文**"))
        self.assertIn("> **祁子德**（2026-11-12 19:20）：组织管理员看不到所有团队 [图片]", desc)
        self.assertIn("> **施楚宇**（2026-11-12 19:20）：第二行补充\n> 附图 [图片]", desc)
        self.assertNotIn("嘉辉的Agent", desc)
        self.assertNotIn("你 @ 我也没用", desc)
        self.assertNotIn("这条在需求之后", desc)
        self.assertEqual(desc.count("处理这个问题"), 1)
        self.assertEqual(sorted(self.w.downloads), [("om_q", "img_v3_q"), ("om_s", "img_v3_s")])
        self.assertLess(desc.index("**话题里的前文**"), desc.index("![img_v3_q.jpg]"))

    def test_unreadable_thread_still_creates_issue(self) -> None:
        self.w.unreadable_threads.add("omt_x")
        self.b.handle(msg("om_req", "需求：看下这个", root="om_root", thread="omt_x"))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][0][2]["description"]
        self.assertTrue(desc.startswith("看下这个\n\n---"))
        self.assertIn("#8", self.w.replies[-1]["text"])

    def test_any_file_kind_in_thread_reply_becomes_comment(self) -> None:
        self.b.handle(msg("om_1", "需求：做个东西"))
        text = ('设计稿 <file key="file_a" name="a&amp;b.fig"/> 录屏 <video key="file_v" name="demo.mp4"/> '
                '<audio key="file_s" duration="3s"/>')
        self.b.handle(msg("om_2", text, root="om_1"))
        body = self.w.writes[-1][2]["body"]
        self.assertTrue(body.startswith("设计稿  录屏"))
        self.assertNotIn("<file", body)
        self.assertEqual([u[1] for u in self.uploads], ["a&b.fig", "demo.mp4", "file_s"])
        self.assertEqual({d[2] for d in self.w.downloads}, {"file"})

    def test_file_download_failure_names_the_file(self) -> None:
        self.w.broken_images.add("file_bad")
        self.b.handle(msg("om_1", '需求：看附件 <file key="file_bad" name="稿子.zip"/>'))
        desc = [w for w in self.w.writes if w[1].endswith("/issues")][0][2]["description"]
        self.assertIn("有一个文件「稿子.zip」没搬过来", desc)
        self.assertTrue(desc.startswith("看附件"))

    def test_attachment_parsing(self) -> None:
        text = ('![Image](img_v3_h) <folder key="file_d" name="assets" child_count="1">'
                '<file key="file_c" name="../x/.env"/></folder> [Image: img_v3_e]')
        self.assertEqual([(a["key"], a["kind"]) for a in FB.find_attachments(text)],
                         [("img_v3_h", "image"), ("file_c", "file"), ("img_v3_e", "image")])
        self.assertEqual(FB.strip_attachments(text), "")
        self.assertEqual(FB.safe_filename("../x/.env", "k"), "env")
        self.assertEqual(FB.safe_filename("", "k"), "k")

    def test_history_normalization(self) -> None:
        item = {"message_id": "om_9", "chat_id": "oc_1", "content": "hi", "create_time": "2026-11-02 15:34",
                "sender": {"id": ME, "name": "彭嘉辉", "sender_type": "user"}}
        n = FB.normalize_history(item, default_root="om_1")
        self.assertEqual((n["sender_type"], n["root_id"], n["sender_name"]), ("user", "om_1", "彭嘉辉"))
        bot = FB.normalize_history({"message_id": "om_8", "sender": {"id": "cli_x", "id_type": "app_id"}})
        self.assertEqual(bot["sender_type"], "bot")


if __name__ == "__main__":
    unittest.main()
