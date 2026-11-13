"""聚焦验证秘书（私聊窗口）：只转 Agent 发言、对转达点回复回到对应 issue、巡检有变化才发、提问失败回总览。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

SPEC = importlib.util.spec_from_file_location(
    "test_feishu_bridge_shared", Path(__file__).resolve().parent / "test_feishu_bridge.py")
assert SPEC is not None and SPEC.loader is not None
TFB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TFB
SPEC.loader.exec_module(TFB)
FB, ok, msg, ME = TFB.FB, TFB.ok, TFB.msg, TFB.ME
SEC = FB.secretary_module()

URL7 = "https://git.example.com/frontend/web-v1/-/issues/7"


class World(TFB.World):
    def __init__(self) -> None:
        super().__init__()
        self.issues[7].update(labels=["状态:开发中"])
        self.owner = {"task": "frontend-web-v1-7", "live_pane": "w1:p1", "status": "in-progress"}
        self.agent_status = "working"
        self.agents: list[dict] = []
        self.send_failures = 0  # 接下来几次往私聊发消息返回失败
        self.omp_out = ""
        self.omp_cmds: list[list[str]] = []
        self.label_events: list[dict] = []

    def __call__(self, cmd, input=None, env=None, cwd=None, **kw):
        if cmd[0] == "glab" and "/resource_label_events" in cmd[cmd.index("-X") + 2]:
            return ok(self.label_events)
        if cmd[0] == sys.executable and cmd[1].endswith("find-scheduler.py"):
            return ok(self.owner)
        if cmd[0] == "lark-cli" and cmd[1:3] == ["im", "+messages-send"] and self.send_failures:
            self.send_failures -= 1
            return ok({"ok": False, "error": {"message": "TLS handshake timeout"}})
        if cmd[:3] == ["herdr", "agent", "list"]:
            return ok({"result": {"agents": self.agents}})
        if cmd[:3] == ["herdr", "agent", "get"]:
            return ok({"result": {"agent": {"agent_status": self.agent_status}}})
        if cmd[0] == "omp":
            self.omp_cmds.append(cmd)
        if cmd[0] == "omp" and self.omp_out:
            return subprocess.CompletedProcess(cmd, 0, self.omp_out, "")
        return super().__call__(cmd, input, env, cwd, **kw)

    def agent_note(self, iid: int, body: str, at: str = "") -> None:
        self.gitlab_comment(iid, body)
        self.issues[iid]["notes"][-1]["author"]["username"] = "pjhagent"
        if at:
            self.issues[iid]["notes"][-1]["created_at"] = at

    def ship(self, iid: int, at: str) -> None:
        self.issues[iid]["labels"] = ["状态:待上线"]
        self.label_events.append({"action": "remove", "label": {"name": "状态:开发中"}, "created_at": at})
        self.label_events.append({"action": "add", "label": {"name": "状态:待上线"}, "created_at": at})


class SecretaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
        self.state: dict = {}
        self.now = 1_000_000.0
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "state").mkdir()
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {URL7: {
            "project": "frontend/web-v1", "iid": 7, "repo": str(self.home / "repo")}}}))
        cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
               "users": {ME: "pengjh"}, "bot_open_id": TFB.BOT}
        self.b = FB.Bridge(cfg, self.state, run=self.w, user_token="tok", now=lambda: self.now,
                           upload=lambda p, f: "", media_dir=self.home / "media")
        self.b._secretary = SEC.Secretary(self.b, home=self.home)
        self.b.handle(msg("om_hi", "帮助"))  # 私聊里第一次说话就记下这个窗口
        self.sec = self.b.secretary
        self.sec.poll()

    def waiting(self) -> None:
        self.w.issues[7]["labels"] = ["状态:等你处理"]

    def forwarded(self) -> list[dict]:
        return self.w.sent[1:]

    def test_first_private_message_learns_chat_and_only_sets_watermark(self) -> None:
        self.assertEqual(self.state["secretary"]["chat_id"], "oc_1")
        self.assertEqual(len(self.w.sent), 1)  # 只有一段说明，旧评论不回放
        self.assertIn("秘书", self.w.sent[0]["text"])
        self.assertEqual(self.w.replies[-1]["text"], FB.HELP_TEXT)
        self.assertFalse(self.w.replies[-1]["in_thread"])  # 私聊里就地回，不开话题
        self.assertEqual(self.state["secretary"]["issues"][URL7]["last_note"], 70)
        self.b.handle(msg("om_hi2", "帮助"))
        self.assertEqual(len(self.w.sent), 1)  # 同一窗口不重复发说明

    def test_group_message_does_not_become_secretary_chat(self) -> None:
        self.b.handle(msg("om_g", "进度", chat="oc_g", chat_type="group", mentions=[{"id": TFB.BOT}]))
        self.assertEqual(self.state["secretary"]["chat_id"], "oc_1")
        self.assertEqual(self.w.replies[-1]["text"], FB.HELP_TEXT)

    def test_forwards_agent_notes_but_not_receipts_user_or_system(self) -> None:
        self.waiting()
        self.w.agent_note(7, "步骤 1 完成：列表改版\n\n<details>全文</details>")
        self.w.agent_note(7, "已收到，已转交目标 issue 的消息，等待调度者回复。")
        self.w.gitlab_comment(7, "我自己说的话")
        self.w.gitlab_comment(7, "改了标签", system=True)
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        sent = self.forwarded()[0]
        self.assertEqual(sent["chat"], "oc_1")
        self.assertIn("#7", sent["text"])
        self.assertIn("步骤 1 完成", sent["text"])
        self.assertNotIn("全文", sent["text"])
        self.sec.poll()  # 水位已推进，不重复
        self.assertEqual(len(self.forwarded()), 1)

    def test_reply_to_forwarded_message_becomes_issue_comment(self) -> None:
        self.waiting()
        self.w.agent_note(7, "方案要你确认：1. 选 A 2. 选 B")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.b.handle(msg("om_u1", "1", reply_to=forwarded))
        method, path, fields, as_user = self.w.writes[-1]
        self.assertEqual((method, path, as_user), ("POST", "projects/frontend/web-v1/issues/7/notes", True))
        self.assertTrue(fields["body"].startswith("@pjhagent 1"))
        self.assertIn("已转给", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_thread_under_forwarded_message_also_relays(self) -> None:
        self.waiting()
        self.w.agent_note(7, "要你看一下")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.b.handle(msg("om_u4", "看过了", root=forwarded, thread="omt_x"))
        self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
        self.assertIn("看过了", self.w.writes[-1][2]["body"])
        self.assertTrue(self.w.replies[-1]["in_thread"])  # 本来就在话题里，回执也留在话题里

    def test_command_reply_keeps_command_bare(self) -> None:
        self.waiting()
        self.w.agent_note(7, "可以开工吗")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.b.handle(msg("om_u2", "/go", reply_to=forwarded))
        self.assertEqual(self.w.writes[-1][2]["body"], "@pjhagent /go")

    def test_issue_number_prefix_still_binds_through_bridge(self) -> None:
        self.b.handle(msg("om_u3", "#7 先别合并"))
        self.assertIn("先别合并", self.w.writes[-1][2]["body"])
        self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")

    def test_patrol_hourly_only_when_changed_and_flags_problems(self) -> None:
        self.assertIsNone(self.sec.patrol())  # 刚开，没到一小时
        self.now += 3600
        first = self.sec.patrol()
        self.assertIn("调度者正在干活", first)
        self.assertEqual(self.w.sent[-1]["chat"], "oc_1")
        self.now += 3600
        self.assertIsNone(self.sec.patrol())  # 没变化不发
        self.w.agent_status = "blocked"
        self.w.issues[7]["labels"] = ["状态:等你处理"]
        self.now += 3600
        text = self.sec.patrol()
        self.assertIn("等你处理", text)
        self.assertIn("卡在确认界面", text)

    def test_waiting_label_after_user_reply_is_not_an_alert(self) -> None:
        self.w.issues[7]["labels"] = ["状态:等你处理"]
        self.w.agent_note(7, "要你在 A、B 里选一个")
        self.assertIn("⚠️ 等你处理", "\n".join(self.sec.overview()))
        self.w.gitlab_comment(7, "选 A")
        self.w.agent_note(7, "已收到，已转交目标 issue 的消息，等待调度者回复。")  # 回执不算
        line = "\n".join(self.sec.overview())
        self.assertIn("你已回复，等调度者接着处理", line)
        self.assertNotIn("⚠️", line)

    def test_quiet_holds_progress_and_patrol_sums_it_up(self) -> None:
        self.w.agent_note(7, "**国内冒烟首轮未通过**：站点可用，验收点未执行")
        self.w.agent_note(7, "**国内冒烟第2轮**：仍未进入验收")
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])  # 开发中的进展不马上推
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])
        self.now += 3600
        text = self.sec.patrol()
        self.assertIn("这一小时的进展", text)
        self.assertIn("2 条新进展", text)
        self.assertIn("国内冒烟第2轮", text)
        self.assertEqual(self.state["secretary"]["held"], {})
        self.now += 3600
        self.assertIsNone(self.sec.patrol())  # 发过就清空，不重复汇报

    def test_note_mentioning_user_forwards_without_waiting_for_patrol(self) -> None:
        self.w.agent_note(7, "@pengjh test 的采集结果：共 231 条")
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        self.assertIn("231 条", self.forwarded()[0]["text"])
        self.assertEqual(self.state["secretary"]["held"], {})

    def test_failed_send_is_kept_and_retried_next_round(self) -> None:
        self.waiting()
        self.w.agent_note(7, "第一条：要你选 A 或 B")
        self.w.agent_note(7, "第二条：补充说明")
        self.w.send_failures = 1
        with self.assertRaises(Exception):
            self.sec.poll()
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(len(self.state["secretary"]["held"][URL7]), 2)
        self.sec.poll()
        self.assertEqual([("第一条" in s["text"], "第二条" in s["text"]) for s in self.forwarded()], [(True, False), (False, True)])
        self.assertEqual(self.state["secretary"]["held"], {})

    def test_mention_of_other_name_prefix_is_not_a_mention(self) -> None:
        self.w.agent_note(7, "@pengjhx 进展：开发中")
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])

    def test_work_items_and_issues_links_are_one_issue(self) -> None:
        work_items = URL7.replace("/-/issues/", "/-/work_items/")
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {work_items: {
            "project": "frontend/web-v1", "iid": 7, "repo": str(self.home / "repo")}}}))
        self.state["threads"] = {"om_t": {"project": "frontend/web-v1", "iid": 7}}
        self.w.agent_note(7, "进展：开发中")
        self.sec.poll()
        self.assertEqual(list(self.state["secretary"]["issues"]), [URL7])
        self.assertEqual(len(self.state["secretary"]["held"][URL7]), 1)
        self.now += 3600
        self.assertIn("1 条新进展", self.sec.patrol())
        self.assertEqual(len(self.sec.overview()), 1)

    def test_old_state_with_both_link_forms_is_merged(self) -> None:
        work_items = URL7.replace("/-/issues/", "/-/work_items/")
        entry = {"id": 71, "body": "进展"}
        sec_state = self.state["secretary"]
        sec_state["issues"][work_items] = dict(sec_state["issues"][URL7])
        sec_state["held"] = {work_items: [entry], URL7: [entry]}
        sec_state["msgs"] = {"om_x": work_items}
        SEC.Secretary(self.b, home=self.home)
        self.assertEqual(list(sec_state["issues"]), [URL7])
        self.assertEqual(sec_state["held"], {URL7: [entry]})
        self.assertEqual(sec_state["msgs"], {"om_x": URL7})

    def test_overview_lists_busy_members_of_the_scheduler(self) -> None:
        self.w.agents = [{"pane_id": "w1:p1", "name": "delivery-scheduler-w1-p1", "agent_status": "working"},
                         {"pane_id": "w1:p2", "agent_status": "working"}, {"pane_id": "w1:p3", "agent_status": "idle"},
                         {"pane_id": "w1:p4", "agent_status": "working"}]
        (self.home / "state/members.json").write_text(json.dumps({
            "w1:p2": {"scheduler": "delivery-scheduler-w1-p1", "profile": "arch"},
            "w1:p3": {"scheduler": "delivery-scheduler-w1-p1", "profile": "test"},
            "w1:p4": {"scheduler": "delivery-scheduler-w9-p9", "profile": "dev-fe"}}))
        line = "\n".join(self.sec.overview())
        self.assertIn("调度者正在干活；架构师正在干活", line)
        self.assertNotIn("测试", line)
        self.assertNotIn("前端开发", line)

    def test_held_notes_flush_when_label_turns_waiting(self) -> None:
        self.w.agent_note(7, "需要你在 A、B 里选一个")
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])
        self.waiting()  # 调度者几秒后才改标签
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        self.assertIn("A、B 里选一个", self.forwarded()[0]["text"])
        self.assertEqual(self.state["secretary"]["held"], {})

    def test_ready_to_ship_or_closed_forwards_immediately(self) -> None:
        self.w.issues[7]["labels"] = ["状态:待上线"]
        self.w.agent_note(7, "整项完成，MR 等你合并")
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)

    def test_ship_label_without_asking_user_is_flagged(self) -> None:
        self.w.agent_note(7, "@pengjh 方案选 A 还是 B？", at="2026-11-12T08:00:00.000+08:00")  # 早先的 @ 不算
        self.w.agent_note(7, "全部完成，main MR 已建好", at="2026-11-12T10:00:00.000+08:00")
        self.w.ship(7, "2026-11-12T10:00:05.000+08:00")
        self.assertIn("已待上线，但调度者没 @你 请求授权合并", "\n".join(self.sec.overview()))

    def test_ship_label_with_request_shortly_before_is_fine(self) -> None:
        self.w.agent_note(7, "@pengjh 整项交付完成，请授权合并 MR", at="2026-11-12T01:50:00Z")
        self.w.ship(7, "2026-11-12T10:00:00.000+08:00")  # 评论先发、10 分钟后才改标签
        self.assertNotIn("没 @你", "\n".join(self.sec.overview()))

    def test_ship_label_after_user_reply_or_without_events_is_not_flagged(self) -> None:
        self.w.issues[7]["labels"] = ["状态:待上线"]  # 查不到标签记录：不报
        self.assertNotIn("没 @你", "\n".join(self.sec.overview()))
        self.w.ship(7, "2026-11-12T10:00:00.000+08:00")
        self.w.gitlab_comment(7, "可以合并了")
        self.assertNotIn("没 @你", "\n".join(self.sec.overview()))

    def test_verbose_issue_forwards_everything(self) -> None:
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {URL7: {
            "project": "frontend/web-v1", "iid": 7, "repo": str(self.home / "repo"), "notify": "verbose"}}}))
        self.w.agent_note(7, "代码评审通过")
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)

    def test_patrol_flags_exited_scheduler_and_silence(self) -> None:
        self.w.owner = {"task": "frontend-web-v1-7", "live_pane": None, "status": "in-progress"}
        self.now += 4 * 3600
        text = self.sec.patrol()
        self.assertIn("调度者已退出", text)
        self.assertIn("超过 3 小时没有动静", text)

    def test_status_word_replies_overview(self) -> None:
        self.b.handle(msg("om_q1", "进度"))
        self.assertIn("**总览**", self.w.replies[-1]["text"])
        self.assertIn("#7", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_question_uses_model_and_falls_back_to_overview(self) -> None:
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": "#7 在开发"}]}})
        self.b.handle(msg("om_q2", "现在哪个最急？"))
        self.assertEqual(self.w.replies[-1]["text"], f"[#7]({URL7}) 在开发")
        self.assertFalse(self.w.replies[-1]["in_thread"])
        self.w.omp_out = ""
        self.b.handle(msg("om_q3", "还有吗？"))
        self.assertIn("这会儿没法回答", self.w.replies[-1]["text"])

    def test_model_answer_links_known_issue_numbers(self) -> None:
        answer = "**#7 旧单**：开发中；[#7](x) 已有链接不重复；#70 不认识；见 note#7a"
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": answer}]}})
        self.b.handle(msg("om_q4", "现在有哪些需求？"))
        self.assertEqual(self.w.replies[-1]["text"],
                         f"**[#7]({URL7}) 旧单**：开发中；[#7](x) 已有链接不重复；#70 不认识；见 note#7a")

    def test_question_material_includes_bot_usage(self) -> None:
        self.b.handle(msg("om_q5", "我要新发起一个任务应该怎么做？"))
        material = self.w.omp_cmds[-1][-1]
        self.assertIn("## 机器人用法", material)
        self.assertIn("需求：<要做的事>", material)

    def test_question_material_includes_recent_issue_comments(self) -> None:
        self.w.agent_note(7, "方案要你在 A、B 里选一个，选好前先停着")
        self.b.handle(msg("om_q6", "7 为什么还在等我？"))
        material = self.w.omp_cmds[-1][-1]
        self.assertIn("## 各需求最近的评论", material)
        self.assertIn("#7 Agent", material)
        self.assertIn("A、B 里选一个", material)

    def test_ask_scheduler_line_becomes_user_comment_on_that_issue(self) -> None:
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": "转给 #7：用户想知道为什么状态还是等你处理，需要他做什么？"}]}})
        self.b.handle(msg("om_q7", "你帮我问7的调度，为什么状态还是等我处理？"))
        method, path, fields, as_user = self.w.writes[-1]
        self.assertEqual((method, path, as_user), ("POST", "projects/frontend/web-v1/issues/7/notes", True))
        self.assertTrue(fields["body"].startswith("@pjhagent 用户想知道为什么状态还是等你处理"))
        self.assertIn("用户原话：你帮我问7的调度", fields["body"])
        self.assertIn("已转给", self.w.replies[-1]["text"])
        self.assertIn("调度者回复后我会转到这里", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_closed_origin_drops_out_of_watch(self) -> None:
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))
        self.sec.poll()
        self.assertEqual(self.state["secretary"]["issues"], {})

    def test_notes_posted_right_before_close_are_forwarded_before_dropping(self) -> None:
        self.w.agent_note(7, "测试通过，攒着等巡检")
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])
        self.w.agent_note(7, "生产验证通过")
        self.w.agent_note(7, "交付收尾")
        self.w.issues[7]["state"] = "closed"
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))  # 关闭后同一轮就掉出名单
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 3)
        self.assertIn("攒着等巡检", texts[0])
        self.assertIn("交付收尾", texts[2])
        self.assertEqual(self.state["secretary"]["issues"], {})
        self.assertEqual(self.state["secretary"]["held"], {})
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 3)

    def test_dropped_issue_is_kept_when_notes_cannot_be_read(self) -> None:
        self.w.agent_note(7, "交付收尾")
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))
        with unittest.mock.patch.object(self.sec, "notes_of", side_effect=FB.BridgeError("timeout")):
            with self.assertRaises(FB.BridgeError):
                self.sec.poll()
        self.assertIn(URL7, self.state["secretary"]["issues"])
        self.sec.poll()
        self.assertIn("交付收尾", self.forwarded()[-1]["text"])
        self.assertEqual(self.state["secretary"]["issues"], {})


if __name__ == "__main__":
    unittest.main()
