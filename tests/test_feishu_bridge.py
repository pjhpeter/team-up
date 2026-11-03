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

    def __call__(self, cmd, input=None, env=None, cwd=None, **_):
        if cmd[0] == "lark-cli":
            return self.lark(cmd, input, cwd)
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
            out = Path(cwd) / f"{cmd[cmd.index('--output') + 1]}.jpg"
            out.write_bytes(b"jpg")
            self.downloads.append((mid, key))
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
        if cmd[1:3] == ["im", "+chat-messages-list"]:
            chat = cmd[cmd.index("--chat-id") + 1]
            if chat in self.unreadable_chats:
                return ok({"ok": False, "error": {"code": 230027, "message": "access denied"}})
            return ok({"ok": True, "data": {"messages": self.history.get(chat, [])}})
        if cmd[1:3] == ["im", "+threads-messages-list"]:
            return ok({"ok": True, "data": {"messages": []}})
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
        self.b = FB.Bridge(cfg, self.state, run=self.w, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(tempfile.mkdtemp(prefix="fb-media-")))

    def fake_upload(self, project: str, path: Path) -> str:
        self.assertTrue(path.exists())
        self.uploads.append((project, path.name))
        return f"![{path.name}](/uploads/x/{path.name})"

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
        self.assertIn("没有使用权限", self.w.replies[0]["text"])

    def test_small_talk_gets_help_once_per_cooldown(self) -> None:
        self.b.handle(msg("om_1", "你叫什么名字？"))
        self.b.handle(msg("om_2", "在吗"))
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

    def test_history_normalization(self) -> None:
        item = {"message_id": "om_9", "chat_id": "oc_1", "content": "hi", "create_time": "2026-11-02 15:34",
                "sender": {"id": ME, "name": "彭嘉辉", "sender_type": "user"}}
        n = FB.normalize_history(item, default_root="om_1")
        self.assertEqual((n["sender_type"], n["root_id"], n["sender_name"]), ("user", "om_1", "彭嘉辉"))
        bot = FB.normalize_history({"message_id": "om_8", "sender": {"id": "cli_x", "id_type": "app_id"}})
        self.assertEqual(bot["sender_type"], "bot")


if __name__ == "__main__":
    unittest.main()
