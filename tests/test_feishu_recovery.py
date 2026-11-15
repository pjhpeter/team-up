"""消息补漏的回归测试：外部接口可分页/失败，真实桥负责转达、去重和恢复进度。"""
from __future__ import annotations

from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from test_feishu_bridge import FB, ME, World, ok

BASE = 1_800_000_000
CHAT = "oc_1"
URL = "https://git.example.com/frontend/web-v1/-/issues/7"


def history(mid: str, text: str, at: float, **fields) -> dict:
    return {"message_id": mid, "chat_id": CHAT, "content": text,
            "create_time": str(int(at * 1000)), "reply_to": "om_relay",
            "sender": {"id": ME, "sender_type": "user"}, **fields}


class HistoryWorld(World):
    """按真实接口的时间范围、顺序和分页返回消息；只替换外部 CLI。"""

    def __init__(self):
        super().__init__()
        self.reads = []
        self.fail_page = None
        self.page_size = 50
        self.invalid_token = False
        self.on_read = None

    def lark(self, cmd, stdin, cwd=None):
        if cmd[1:3] not in (["im", "+chat-messages-list"], ["im", "+threads-messages-list"]):
            return super().lark(cmd, stdin, cwd)
        thread = cmd[2] == "+threads-messages-list"
        source = cmd[cmd.index("--thread" if thread else "--chat-id") + 1]
        token = cmd[cmd.index("--page-token") + 1] if "--page-token" in cmd else "0"
        offset = int(token)
        self.reads.append(cmd)
        if self.on_read:
            callback, self.on_read = self.on_read, None
            callback()
        if self.fail_page == (source, offset):
            return ok({"ok": False, "error": {"message": "network timeout"}})
        rows = list((self.threads if thread else self.history).get(source, []))
        if not thread:
            start = datetime.fromisoformat(cmd[cmd.index("--start") + 1]).timestamp() * 1000
            end = datetime.fromisoformat(cmd[cmd.index("--end") + 1]).timestamp() * 1000
            rows = [r for r in rows if start <= int(r["create_time"]) < end]
        rows.sort(key=lambda r: int(r["create_time"]), reverse=cmd[cmd.index("--order") + 1] == "desc")
        page = rows[offset:offset + self.page_size]
        more = offset + len(page) < len(rows)
        return ok({"ok": True, "data": {"messages": page, "has_more": more,
                   "page_token": "" if self.invalid_token else str(offset + len(page))}})


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.w = HistoryWorld()
        self.now = float(BASE)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = {"chats": {CHAT: (BASE - 600) * 1000},
                      "secretary": {"chat_id": CHAT, "msgs": {"om_relay": URL}}}
        self.cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
                    "users": {ME: "pengjh"}, "bot_open_id": "ou_bot", "agent_account": "pjhagent"}
        self.b = self.make_bridge(self.state)

    def make_bridge(self, state):
        bridge = FB.Bridge(self.cfg, state, run=self.w, user_token="tok", now=lambda: self.now,
                           media_dir=Path(self.tmp.name) / "media")
        bridge._secretary = FB.secretary_module().Secretary(bridge, home=Path(self.tmp.name))
        return bridge

    def comments(self):
        return [fields["body"] for _, path, fields, _ in self.w.writes if path.endswith("/notes")]

    def connected(self):
        FB.read_event_status(io.StringIO("[source] feishu-websocket: connected\n"), self.b.history_requested)

    def test_timer_recovers_private_reply_without_reconnect_and_deduplicates_event(self):
        self.b.poll_history()
        self.w.history[CHAT] = [history("om_lost", "可以继续工作", BASE + 1)]
        self.now += 59
        self.b.poll_history()
        self.assertEqual(self.comments(), [])
        self.now += 1
        self.b.poll_history()
        self.b.handle(FB.normalize_history(self.w.history[CHAT][0]))
        self.now += 60
        self.b.poll_history()
        self.assertEqual(self.comments(), ["@pjhagent 可以继续工作\n\n_（来自飞书秘书）_"])
        self.assertEqual(len(self.w.replies), 1)

    def test_connected_and_consumer_ready_recover_before_next_timer(self):
        self.b.poll_history()
        for index, line in enumerate(("[source] feishu-websocket: connected",
                                      "[event] ready event_key=im.message.receive_v1")):
            self.now += 5
            self.w.history.setdefault(CHAT, []).append(history(f"om_{index}", f"回复{index}", self.now - 1))
            FB.read_event_status(io.StringIO(line + "\n"), self.b.history_requested)
            self.b.poll_history()
        self.assertEqual(self.comments(), [f"@pjhagent 回复{i}\n\n_（来自飞书秘书）_" for i in range(2)])

    def test_new_event_and_restart_do_not_skip_earlier_lost_reply(self):
        self.w.history[CHAT] = [history("om_old", "漏掉的回复", BASE - 300)]
        self.b.handle(FB.normalize_history(history("om_new", "较新的回复", BASE - 1)))
        # 重建桥模拟实际状态落盘后重启；历史进度不能变成最新事件的时间。
        self.b = self.make_bridge(json.loads(json.dumps(self.state)))
        self.b.poll_history()
        self.assertEqual(self.comments(), ["@pjhagent 较新的回复\n\n_（来自飞书秘书）_",
                                           "@pjhagent 漏掉的回复\n\n_（来自飞书秘书）_"])

    def test_failed_later_page_keeps_progress_and_does_not_block_other_chat(self):
        self.w.page_size = 1
        self.w.history[CHAT] = [history("om_a", "第一条", BASE - 10), history("om_b", "第二条", BASE - 9)]
        self.state["chats"]["oc_other"] = (BASE - 600) * 1000
        self.w.history["oc_other"] = [history("om_other", "#7 另一会话", BASE - 8, chat_id="oc_other", reply_to="")]
        self.w.fail_page = (CHAT, 1)
        self.b.poll_history()
        self.assertEqual(self.state["history_cursors"][f"chat:{CHAT}"], (BASE - 600) * 1000)
        self.assertEqual(len(self.comments()), 2)
        self.w.fail_page = None
        self.now += 60
        self.b.poll_history()
        self.assertEqual(self.comments().count("@pjhagent 第一条\n\n_（来自飞书秘书）_"), 1)
        self.assertIn("@pjhagent 第二条\n\n_（来自飞书秘书）_", self.comments())
        self.assertEqual(len(self.comments()), 3)

    def test_more_than_five_pages_are_not_silently_skipped(self):
        self.w.history[CHAT] = [history(f"om_{i}", f"回复{i}", BASE - 300 + i) for i in range(251)]
        self.b.poll_history()
        self.assertEqual(self.comments(), [f"@pjhagent 回复{i}\n\n_（来自飞书秘书）_" for i in range(251)])
        self.assertEqual(self.state["history_cursors"][f"chat:{CHAT}"], BASE * 1000)

    def test_missing_pagination_token_does_not_advance_checkpoint(self):
        self.w.page_size = 1
        self.w.invalid_token = True
        self.w.history[CHAT] = [history("om_a", "第一条", BASE - 10), history("om_b", "第二条", BASE - 9)]
        self.b.poll_history()
        self.w.invalid_token = False
        self.now += 60
        self.b.poll_history()
        self.assertEqual(self.comments(), [f"@pjhagent {text}\n\n_（来自飞书秘书）_" for text in ("第一条", "第二条")])

    def test_periodic_thread_recovery_reads_newest_pages_and_preserves_command_order(self):
        self.state["threads"]["om_root"] = {"project": "frontend/web-v1", "iid": 7, "chat_id": CHAT,
                                              "closed": False, "last_note": 70}
        self.state["history_cursors"]["thread:om_root"] = (BASE - 60) * 1000
        self.w.page_size = 2
        self.w.threads["om_root"] = [history(f"om_old{i}", "很早的内容", BASE - 1000 + i) for i in range(10)]
        self.w.threads["om_root"] += [history("om_first", "/go", BASE - 10, reply_to=""),
                                      history("om_second", "/status", BASE - 9, reply_to="")]
        self.b.poll_history()
        self.assertEqual(self.comments(), ["/go", "/status"])
        thread_reads = [r for r in self.w.reads if "--thread" in r]
        self.assertEqual(len(thread_reads), 2)  # 到时间边界就停，不反复读完整话题
        self.now += 60
        self.b.poll_history()
        self.assertEqual(self.comments(), ["/go", "/status"])

    def test_reconnect_during_history_read_is_not_lost(self):
        self.w.on_read = self.connected
        self.b.poll_history()
        self.w.history[CHAT] = [history("om_late", "读取期间漏到的回复", BASE + 1)]
        self.now += 2
        self.b.poll_history()
        self.assertEqual(self.comments(), ["@pjhagent 读取期间漏到的回复\n\n_（来自飞书秘书）_"])

    def test_replay_window_dedup_survives_recent_id_capacity(self):
        first = history("om_first", "不要重复转达", BASE - 10)
        self.b.handle(FB.normalize_history(first))
        # 同一聊天的大量机器人消息也会消耗旧的固定长度去重表。
        for i in range(FB.SEEN_CAP):
            self.b.handle(FB.normalize_history(history(f"om_bot{i}", "", BASE - 9,
                                                       sender={"id": "cli_bot", "sender_type": "app"})))
        self.w.history[CHAT] = [first]
        self.b.poll_history()
        self.assertEqual(self.comments(), ["@pjhagent 不要重复转达\n\n_（来自飞书秘书）_"])


if __name__ == "__main__":
    unittest.main()
