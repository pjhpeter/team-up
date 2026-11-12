"""显式拆分话题：只模拟 GitLab/飞书边界，不调用真实账号或运行状态。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SPEC = importlib.util.spec_from_file_location("bridge_test_helpers", Path(__file__).with_name("test_feishu_bridge.py"))
HELPERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPERS)
FB, World, msg, ok = HELPERS.FB, HELPERS.World, HELPERS.msg, HELPERS.ok
TOPICS = FB.topics_module()


class TopicWorld(World):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.dedup = {}
        self.timeout_after = ""
        self.missing_link = False
        self.thread_history = {}
        for iid in (470, 471, 472):
            self.issues[iid] = {"iid": iid, "title": f"任务{iid}", "state": "opened", "notes": []}

    def __call__(self, cmd, input=None, env=None, cwd=None, **kwargs):
        result = super().__call__(cmd, input=input, env=env, cwd=cwd, **kwargs)
        if cmd[0] == "glab":
            path = cmd[cmd.index("-X") + 2]
            query = parse_qs(urlsplit(path).query)
            if "/notes?" in path and "page" in query:
                notes = json.loads(result.stdout)
                size, page = int(query["per_page"][0]), int(query["page"][0])
                return ok(notes[(page - 1) * size:page * size])
        return result

    def lark(self, cmd, stdin, cwd=None):
        action = cmd[2]
        key = cmd[cmd.index("--idempotency-key") + 1] if "--idempotency-key" in cmd else ""
        if action in ("+messages-send", "+messages-reply") and key in self.dedup:
            return self.dedup[key]
        if action == "+messages-send":
            mid = f"om_topic{len(self.sent) + 1}"
            self.sent.append({"message_id": mid, "chat": cmd[cmd.index("--chat-id") + 1], "text": stdin, "key": key})
            result = ok({"ok": True, "data": {"message_id": mid}})
        elif action == "+threads-messages-list":
            root = cmd[cmd.index("--thread") + 1]
            return ok({"ok": True, "data": {"messages": self.thread_history.get(root, [])}})
        else:
            result = super().lark(cmd, stdin, cwd)
        if action == "+messages-mget":
            data = json.loads(result.stdout)
            if not self.missing_link:
                for message in data["data"]["messages"]:
                    message["message_app_link"] = f"https://applink.feishu.cn/client/message/open?messageId={message['message_id']}"
            return ok(data)
        if action in ("+messages-send", "+messages-reply") and json.loads(result.stdout).get("ok"):
            if key:
                self.dedup[key] = result
            if self.timeout_after == action:
                self.timeout_after = ""
                raise subprocess.TimeoutExpired(cmd, 60)
        return result


class TopicTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / "feishu-topics"
        self.state_path = self.home / "bridge.json"
        self.world = TopicWorld()
        self.clock = 1_000_000.0
        self.cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
                    "users": {HELPERS.ME: "pengjh"}, "bot_open_id": HELPERS.BOT, "agent_account": "pjhagent"}
        self.bridge = FB.Bridge(self.cfg, {}, run=self.world, user_token="tok", now=lambda: self.clock)
        self.bridge.state["threads"]["om_source"] = {"project": self.bridge.project, "iid": 470,
                                                    "chat_id": "oc_1", "last_note": 0, "closed": False, "bound": True}
        self.bridge.state["thread_ids"]["omt_source"] = "om_source"
        self.queue = TOPICS.Topics(self.bridge, self.directory, self.persist)

    def persist(self):
        FB.save_json(self.state_path, self.bridge.state)

    def url(self, iid):
        return f"https://git.example.com/frontend/web-v1/-/issues/{iid}"

    def register(self, source=470, target=471):
        return TOPICS.register(self.cfg, self.directory, self.url(source), self.url(target), self.bridge.api, lambda: self.clock)

    def status(self, iid=471):
        return TOPICS.status(self.cfg, self.directory, self.url(iid))

    def restart(self):
        state = FB.load_json(self.state_path, {})
        self.bridge = FB.Bridge(self.cfg, state, run=self.world, user_token="tok", now=lambda: self.clock)
        self.queue = TOPICS.Topics(self.bridge, self.directory, self.persist)

    def test_split_470_471_independent_both_directions_and_registration_watermark(self):
        self.world.gitlab_comment(471, "不回放旧评论")
        request = self.register()
        self.world.gitlab_comment(471, "登记后待确认")
        self.queue.consume()
        result = self.status()
        self.assertEqual(result["status"], "ready")
        root, tid = result["root"], result["thread_id"]
        self.assertEqual(self.bridge.state["threads"][root]["last_note"], request["last_note"])
        self.assertTrue(self.bridge.state["threads"][root]["bound"])
        self.assertEqual(self.bridge.state["threads"]["om_source"]["iid"], 470)
        for value in ("#471", "任务471", self.url(471), "#470"):
            self.assertIn(value, self.world.sent[0]["text"])
        self.assertIn(result["message_app_link"], self.world.replies[-1]["text"])
        self.world.gitlab_comment(470, "来源结果")
        self.world.gitlab_comment(471, "目标结果")
        self.bridge.poll_gitlab()
        target = "\n".join(r["text"] for r in self.world.replies if r["to"] == root)
        source = "\n".join(r["text"] for r in self.world.replies if r["to"] == "om_source")
        self.assertIn("登记后待确认", target)
        self.assertIn("目标结果", target)
        self.assertNotIn("不回放旧评论", target)
        self.assertNotIn("来源结果", target)
        self.assertIn("来源结果", source)
        self.assertNotIn("目标结果", source)
        self.bridge.handle(msg("om_user_target", "目标补充", thread=tid))
        self.bridge.handle(msg("om_user_source", "来源补充", thread="omt_source"))
        self.assertIn("@pjhagent 目标补充", self.world.issues[471]["notes"][-1]["body"])
        self.assertIn("来源补充", self.world.issues[470]["notes"][-1]["body"])

    def test_cross_issue_explicit_commands_redirect_without_changing_binding(self):
        self.register()
        self.queue.consume()
        before = len(self.world.writes)
        for index, text in enumerate(("#471 开工", f"需求：{self.url(471)} 处理", "[Image: img_x]\n#471")):
            self.bridge.handle(msg(f"om_redirect{index}", text, thread="omt_source"))
            self.assertIn(self.status()["message_app_link"], self.world.replies[-1]["text"])
        self.assertEqual(len(self.world.writes), before)
        self.assertFalse(self.world.downloads)
        self.assertEqual(self.bridge.state["threads"]["om_source"]["iid"], 470)
        self.bridge.handle(msg("om_missing", "#472", thread="omt_source"))
        self.assertIn("回主聊天发「#472」", self.world.replies[-1]["text"])
        self.assertEqual(len(self.world.writes), before)

    def test_mentions_and_commands_keep_current_issue(self):
        self.bridge.handle(msg("om_mention", "请参考 #471 的做法", thread="omt_source"))
        self.bridge.handle(msg("om_url_mention", f"补充：参考 {self.url(471)}", thread="omt_source"))
        self.bridge.handle(msg("om_status", "/status", thread="omt_source"))
        bodies = [n["body"] for n in self.world.issues[470]["notes"]]
        self.assertIn("请参考 #471", bodies[0])
        self.assertIn(self.url(471), bodies[1])
        self.assertEqual(bodies[2], "@pjhagent /status")
        self.assertFalse(self.world.issues[471]["notes"])

    def test_foreign_url_same_iid_is_not_forwarded(self):
        self.bridge.handle(msg("om_foreign", "https://other.example/frontend/web-v1/-/issues/470", thread="omt_source"))
        self.bridge.handle(msg("om_foreign_project", "需求：https://git.example.com/other/project/-/issues/470", thread="omt_source"))
        self.assertFalse(self.world.writes)
        self.assertIn("不属于", self.world.replies[-1]["text"])

    def test_duplicate_registration_keeps_result_watermark_and_source(self):
        request = self.register()
        self.queue.consume()
        first = self.status()
        self.world.gitlab_comment(471, "后来的评论")
        self.assertEqual(self.register(), first)
        self.assertEqual(self.status()["last_note"], request["last_note"])
        with self.assertRaises(TOPICS.TopicError):
            self.register(source=472)
        self.queue.consume()
        self.assertEqual(len(self.world.sent), 1)
        self.assertEqual(len(self.world.replies), 2)

    def test_registration_rejects_foreign_or_non_url_sources(self):
        for source in ("#470", "正文提到 #470", "https://other.example/frontend/web-v1/-/issues/470",
                       "https://git.example.com/other/project/-/issues/470", self.url(470) + "#note_1"):
            with self.subTest(source=source), self.assertRaises(TOPICS.TopicError):
                TOPICS.register(self.cfg, self.directory, source, self.url(471), self.bridge.api)
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.world.sent)

    def test_source_unbound_and_ambiguous_do_not_send(self):
        self.register()
        original = self.bridge.state["threads"].pop("om_source")
        self.queue.consume()
        self.assertEqual(self.status()["status"], "failed")
        self.assertIn("没有飞书绑定", self.status()["error"])
        self.bridge.state["threads"]["om_source"] = original
        self.bridge.state["threads"]["om_other_chat"] = {**original, "chat_id": "oc_other"}
        self.queue.consume()
        self.assertIn("多个话题或聊天", self.status()["error"])
        self.assertFalse(self.world.sent)
        self.assertFalse(self.world.replies)

    def test_reuse_target_only_in_source_chat(self):
        self.bridge.state["threads"]["om_existing"] = {"project": self.bridge.project, "iid": 471,
                                                      "chat_id": "oc_1", "last_note": 99, "closed": False}
        self.register()
        self.queue.consume()
        self.assertEqual(self.status()["root"], "om_existing")
        self.assertFalse(self.world.sent)
        self.assertEqual(self.bridge.state["threads"]["om_existing"]["last_note"], 99)
        self.assertTrue(self.bridge.state["threads"]["om_existing"]["bound"])

    def test_timeout_after_root_sent_recovers_same_message_and_binding(self):
        self.persist()
        self.register()
        self.world.timeout_after = "+messages-send"
        self.queue.consume()
        self.assertEqual(self.status()["status"], "failed")
        self.restart()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "ready")
        self.assertEqual(len(self.world.sent), 1)
        self.assertEqual(self.bridge.state["thread_ids"][self.status()["thread_id"]], self.status()["root"])

    def test_intro_failure_restores_from_disk_without_duplicate_root(self):
        self.register()
        self.world.reply_failures = 1
        self.queue.consume()
        self.assertEqual(self.status()["status"], "failed")
        root = self.status()["root"]
        self.assertEqual(FB.load_json(self.state_path, {})["threads"][root]["iid"], 471)
        self.restart()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "ready")
        self.assertEqual(len(self.world.sent), 1)
        self.assertEqual(len(self.world.replies), 2)

    def test_timeout_after_reply_recovers_without_duplicate_intro(self):
        self.register()
        self.world.timeout_after = "+messages-reply"
        self.queue.consume()
        self.restart()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "ready")
        self.assertEqual(len(self.world.replies), 2)

    def test_uncertain_send_expired_dedup_window_requires_manual_attention(self):
        self.persist()
        self.register()
        self.world.timeout_after = "+messages-send"
        self.queue.consume()
        self.clock += 3601
        self.restart()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "needs_attention")
        self.assertIn("一小时", self.status()["error"])
        self.queue.consume()
        self.assertEqual(len(self.world.sent), 1)
        self.assertFalse(self.world.replies)

    def test_missing_applink_retries_read_without_duplicate_messages(self):
        self.register()
        self.world.missing_link = True
        self.queue.consume()
        self.assertEqual(self.status()["status"], "failed")
        self.assertNotIn("message_app_link", self.status())
        self.assertEqual(len(self.world.replies), 1)
        self.world.missing_link = False
        self.restart()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "ready")
        self.assertEqual(len(self.world.sent), 1)
        self.assertEqual(len(self.world.replies), 2)

    def test_more_than_one_page_since_registration_is_not_lost(self):
        self.register()
        for index in range(65):
            self.world.gitlab_comment(471, f"新评论 {index}")
        self.queue.consume()
        root = self.status()["root"]
        self.bridge.poll_gitlab()
        forwarded = [body for r in self.world.replies if r["to"] == root
                     for body in re.findall(r"\n(新评论 \d+)$", r["text"])]
        self.assertEqual(forwarded, [f"新评论 {index}" for index in range(65)])

    def test_closed_target_creates_archive_without_reopen_or_old_history(self):
        self.world.issues[471]["state"] = "closed"
        self.world.gitlab_comment(471, "旧关闭结论")
        self.register()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "archived")
        self.bridge.poll_gitlab()
        self.bridge.handle(msg("om_closed", "继续", thread=self.status()["thread_id"]))
        self.assertFalse(self.world.writes)
        self.assertIn("归档", self.world.replies[-1]["text"])
        self.assertNotIn("旧关闭结论", "\n".join(r["text"] for r in self.world.replies))

    def test_group_and_whitelist_constraints_apply_to_new_topic_and_catchup(self):
        self.bridge.state["group_chats"] = ["oc_1"]
        self.register()
        self.queue.consume()
        tid, root = self.status()["thread_id"], self.status()["root"]
        self.bridge.handle(msg("om_no_at", "不处理", thread=tid, chat_type="group"))
        self.bridge.handle(msg("om_stranger", "不处理", thread=tid, sender="ou_stranger", chat_type="group",
                               mentions=[{"id": HELPERS.BOT}]))
        self.world.thread_history[root] = [{"message_id": "om_history_no_at", "chat_id": "oc_1",
                                            "content": "历史没at", "thread_id": tid,
                                            "sender": {"id": HELPERS.ME, "sender_type": "user"}}]
        self.bridge.catch_up()
        self.assertFalse(self.world.writes)
        self.bridge.handle(msg("om_at", "可以处理", thread=tid, chat_type="group", mentions=[{"id": HELPERS.BOT}]))
        self.assertEqual(len(self.world.writes), 1)
        self.assertIn("@pjhagent 可以处理", self.world.issues[471]["notes"][-1]["body"])

    def test_cli_status_is_read_only_without_bridge_keychain_or_api(self):
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps(self.cfg))
        with patch.object(FB, "CONFIG_PATH", config_path), patch.object(FB, "STATE_PATH", self.state_path), \
                patch.object(FB, "Bridge", side_effect=AssertionError("must not initialize bridge")), \
                patch.object(FB, "keychain_token", side_effect=AssertionError("must not read keychain")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(FB.main(["--topic-status", self.url(471)]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_registered")
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.directory.exists())

    def test_register_only_writes_queue_not_running_state_or_lark(self):
        self.state_path.write_text("do not read or replace")
        self.register()
        self.assertEqual(self.state_path.read_text(), "do not read or replace")
        self.assertEqual(self.status()["status"], "queued")
        self.assertFalse(self.world.sent)
        self.assertFalse(self.world.replies)

    def test_only_one_bridge_consumer_can_hold_lock(self):
        first = TOPICS.lock_bridge(self.state_path)
        self.addCleanup(first.close)
        with self.assertRaises(TOPICS.TopicError):
            TOPICS.lock_bridge(self.state_path)

    def test_registered_lookup_is_local_and_accepts_work_item_alias(self):
        self.directory = self.home / "state" / "feishu-topics"
        self.assertFalse(TOPICS.is_registered_topic(self.home, self.url(471)))
        self.assertFalse(self.directory.exists())
        self.register()
        self.assertTrue(TOPICS.is_registered_topic(self.home, self.url(471)))
        self.assertTrue(TOPICS.is_registered_topic(self.home, self.url(471).replace("/issues/", "/work_items/")))
        self.assertFalse(TOPICS.is_registered_topic(self.home, self.url(470)))
        self.assertFalse(TOPICS.is_registered_topic(self.home, self.url(471).replace("git.example.com", "other.example")))
        self.assertFalse(self.world.sent)

    def test_existing_target_in_other_chat_is_not_reused(self):
        self.bridge.state["threads"]["om_elsewhere"] = {"project": self.bridge.project, "iid": 471,
                                                       "chat_id": "oc_other", "last_note": 0, "closed": False}
        self.register()
        self.queue.consume()
        self.assertEqual(self.status()["status"], "ready")
        self.assertNotEqual(self.status()["root"], "om_elsewhere")
        self.assertEqual(self.world.sent[0]["chat"], "oc_1")

    def test_new_topic_reply_keeps_attachments_on_target_only(self):
        self.register()
        self.queue.consume()
        self.bridge.media_dir = self.home / "media"
        self.bridge.upload = lambda project, path: f"![{path.name}](/uploads/{path.name})"
        self.bridge.handle(msg("om_target_image", "补图 [Image: img_target]", thread=self.status()["thread_id"]))
        self.assertIn("![img_target.jpg](/uploads/img_target.jpg)", self.world.issues[471]["notes"][-1]["body"])
        self.assertIn("@pjhagent", self.world.issues[471]["notes"][-1]["body"])
        self.assertFalse(self.world.issues[470]["notes"])


if __name__ == "__main__":
    unittest.main()
