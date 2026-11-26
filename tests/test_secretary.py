"""聚焦验证秘书（私聊窗口）：Agent 每条发言马上转、对转达点回复回到对应 issue、巡检有变化才发、提问失败回总览。"""

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

    def respond(self, decision: dict) -> None:
        self.omp_out = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": json.dumps(decision, ensure_ascii=False)}]}})

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

    def test_record_without_watermark_does_not_stop_forwarding(self) -> None:
        # 外部登记只带身份时按第一次看到处理：不回放旧评论，也不能让整轮巡检崩掉。
        self.state["secretary"]["issues"][URL7] = {"project": "frontend/web-v1", "iid": 7, "title": "x"}
        self.sec.poll()
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(self.state["secretary"]["issues"][URL7]["last_note"], 70)
        self.w.agent_note(7, "新的进展")
        self.sec.poll()
        self.assertIn("新的进展", self.forwarded()[-1]["text"])

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
        self.assertIn("尚未新建或启动", self.w.replies[-1]["text"])

    def test_reply_to_forwarded_agent_question_goes_to_scheduler(self) -> None:
        # 引用调度者的转发发言提问，是在和调度者说话；秘书不能抢答。
        self.w.agent_note(7, "尚未交付，后端回退演练还缺一个确认")
        self.sec.poll()
        forwarded = next(reversed(self.sec.state["msgs"]))
        replies = len(self.w.replies)
        self.w.respond({"action": "answer", "mode": "analyze", "reply": "回滚需要锁定版本。", "issue": 7,
                        "reference": "linked", "evidence": "为什么搞那么复杂"})
        self.b.handle(msg("om_why", "回滚不就是代码回滚重新部署吗？为什么搞那么复杂？", reply_to=forwarded))
        self.assertIn("为什么搞那么复杂", self.w.issues[7]["notes"][-1]["body"])
        self.assertTrue(all("回滚需要锁定版本" not in r["text"] for r in self.w.replies[replies:]))
        self.assertIn("已转给", self.w.replies[-1]["text"])

    def test_task_question_relays_even_if_model_answers(self) -> None:
        # 已确定任务的提问交给调度者回答；秘书不凭材料代答。
        self.w.respond({"action": "answer", "mode": "analyze", "reply": "#7 正在测试。", "issue": 7,
                        "reference": "explicit", "evidence": "7"})
        self.b.handle(msg("om_q1", "7进度怎么样？"))
        self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
        self.assertIn("7进度怎么样？", self.w.writes[-1][2]["body"])
        self.assertNotIn("/go", self.w.writes[-1][2]["body"])
        self.assertTrue(all("正在测试" not in r["text"] for r in self.w.replies))
        self.assertIn("已转给", self.w.replies[-1]["text"])

    def test_reply_to_any_task_message_relays_model_answer(self) -> None:
        self.sec.state["msgs"]["om_old_forward"] = URL7
        self.w.respond({"action": "answer", "mode": "analyze", "reply": "尚未确认。", "issue": None,
                        "reference": "none", "evidence": ""})
        self.b.handle(msg("om_now", "现在什么情况？", reply_to="om_old_forward"))
        self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
        self.assertIn("现在什么情况？", self.w.writes[-1][2]["body"])
        self.assertTrue(all("尚未确认" not in r["text"] for r in self.w.replies))

    def test_private_thread_and_secretary_send_each_note_once(self) -> None:
        self.b.handle(msg("om_bind", "#7"))
        self.b.handle(msg("om_bind_again", "#7"))
        before, replies = len(self.w.sent), len(self.w.replies)
        self.w.agent_note(7, "Windows 测试包已准备好，请测试")
        self.b.poll_gitlab()
        self.sec.poll()
        self.b.poll_gitlab()
        self.assertEqual(len(self.w.sent) - before, 1)
        self.assertEqual(len(self.w.replies), replies)
        self.assertIn("Windows 测试包已准备好", self.w.sent[-1]["text"])
        forwarded = next(reversed(self.sec.state["msgs"]))
        self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                        "reference": "linked", "evidence": "测试通过"})
        self.b.handle(msg("om_answer", "测试通过", reply_to=forwarded))
        self.assertIn("@pjhagent 测试通过", self.w.issues[7]["notes"][-1]["body"])

    def test_group_keeps_its_reply_while_private_uses_one_notification(self) -> None:
        self.b.handle(msg("om_group", "#7", chat="oc_group", chat_type="group", mentions=[{"id": TFB.BOT}]))
        before, replies = len(self.w.sent), len(self.w.replies)
        self.w.agent_note(7, "需要确认一个新风险")
        self.b.poll_gitlab()
        self.sec.poll()
        self.assertEqual(len(self.w.sent) - before, 1)
        self.assertEqual(len(self.w.replies) - replies, 1)
        self.assertEqual(self.w.replies[-1]["to"], "om_group")
        self.assertIn("需要确认一个新风险", self.w.sent[-1]["text"])

    def test_private_retry_survives_restart_without_a_second_thread_copy(self) -> None:
        self.b.handle(msg("om_bind", "#7"))
        before, replies = len(self.w.sent), len(self.w.replies)
        self.w.agent_note(7, "第一条实质答复")
        self.w.agent_note(7, "第二条实质答复")
        self.w.send_failures = 1
        with self.assertRaises(FB.BridgeError):
            self.b.poll_gitlab()
        self.assertEqual(len(self.w.sent), before)
        restored = json.loads(json.dumps(self.state))
        bridge = FB.Bridge(self.b.cfg, restored, run=self.w, user_token="tok", now=lambda: self.now)
        bridge._secretary = SEC.Secretary(bridge, home=self.home)
        bridge.secretary.poll()
        bridge.poll_gitlab()
        bridge.secretary.poll()
        self.assertEqual([m["text"].split("\n", 1)[1] for m in self.w.sent[before:]],
                         ["第一条实质答复", "第二条实质答复"])
        self.assertEqual(len(self.w.replies), replies)

    def test_private_receipt_is_skipped_but_received_prefix_answer_is_delivered(self) -> None:
        self.b.handle(msg("om_bind", "#7"))
        before, replies = len(self.w.sent), len(self.w.replies)
        self.w.agent_note(7, f"已收到，已转交目标 issue {URL7} 的消息，等待调度者回复。")
        self.w.agent_note(7, "已收到，新的检查结果是磁盘空间不足，请清理后继续。")
        self.b.poll_gitlab()
        self.sec.poll()
        self.assertEqual(len(self.w.sent) - before, 1)
        self.assertIn("磁盘空间不足", self.w.sent[-1]["text"])
        self.assertEqual(len(self.w.replies), replies)

    def test_private_route_keeps_other_peoples_substantive_comments(self) -> None:
        self.b.handle(msg("om_bind", "#7"))
        before = len(self.w.sent)
        self.w.gitlab_comment(7, "同事补充：这个问题也出现在 Windows", name="同事")
        self.b.poll_gitlab()
        self.sec.poll()
        self.assertEqual(len(self.w.sent) - before, 1)
        self.assertIn("同事补充", self.w.sent[-1]["text"])

    def test_private_close_does_not_add_a_second_completion_notice(self) -> None:
        self.b.handle(msg("om_bind", "#7"))
        before, replies = len(self.w.sent), len(self.w.replies)
        self.w.agent_note(7, "交付完成，已经验收")
        self.w.issues[7]["state"] = "closed"
        self.b.poll_gitlab()
        self.sec.poll()
        self.assertEqual(len(self.w.sent) - before, 1)
        self.assertIn("交付完成", self.w.sent[-1]["text"])
        self.assertEqual(len(self.w.replies), replies)

    def test_forwards_agent_notes_but_not_receipts_process_notes_user_or_system(self) -> None:
        self.waiting()
        self.w.agent_note(7, "需要你决定：是否保留旧入口\n\n<details>全文</details>\n\n<!-- hwf-kind:todo -->")
        self.w.agent_note(7, "步骤 1 完成：列表改版\n\n<details>全文</details>\n\n<!-- hwf-kind:step-done -->")
        self.w.agent_note(7, f"已收到，已转交目标 issue {URL7} 的消息，等待调度者回复。")
        self.w.agent_note(7, f"@pengjh 已收到目标 issue {URL7} 的消息。目前没有在线负责人，已转交调度入口，等待接手后回复。")
        self.w.gitlab_comment(7, "我自己说的话")
        self.w.gitlab_comment(7, "改了标签", system=True)
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        sent = self.forwarded()[0]
        self.assertEqual(sent["chat"], "oc_1")
        self.assertIn("#7", sent["text"])
        self.assertIn("需要你决定：是否保留旧入口", sent["text"])
        self.assertNotIn("步骤 1 完成", sent["text"])
        self.assertNotIn("全文", sent["text"])
        self.assertNotIn("hwf-kind", sent["text"])
        self.sec.poll()  # 水位已推进，不重复
        self.assertEqual(len(self.forwarded()), 1)

    def test_reply_to_forwarded_message_becomes_issue_comment(self) -> None:
        self.waiting()
        self.w.agent_note(7, "方案要你确认：1. 选 A 2. 选 B")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                        "reference": "linked", "evidence": "1"})
        self.b.handle(msg("om_u1", "1", reply_to=forwarded))
        method, path, fields, as_user = self.w.writes[-1]
        self.assertEqual((method, path, as_user), ("POST", "projects/frontend/web-v1/issues/7/notes", True))
        self.assertTrue(fields["body"].startswith("@pjhagent 1"))
        self.assertIn("已转给", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_relayed_but_receipt_failed_says_no_need_to_resend_without_opening_topic(self) -> None:
        self.waiting()
        self.w.agent_note(7, "要你看一下")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        notes = sum(1 for w in self.w.writes if w[1].endswith("/issues/7/notes"))
        self.w.reply_failures = 1  # 评论已写进 issue，只是「已转给」这条回执没发出去
        self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                        "reference": "linked", "evidence": "好的"})
        self.b.handle(msg("om_u1", "好的", root=forwarded, reply_to=forwarded))  # 引用回复带 root_id，但不在话题里
        self.assertEqual(sum(1 for w in self.w.writes if w[1].endswith("/issues/7/notes")), notes + 1)
        self.assertIn("#7", self.w.replies[-1]["text"])
        self.assertIn("不用重发", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_thread_under_forwarded_message_also_relays(self) -> None:
        self.waiting()
        self.w.agent_note(7, "要你看一下")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                        "reference": "linked", "evidence": "看过了"})
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
        self.w.agent_note(7, f"已收到，已转交目标 issue {URL7} 的消息，等待调度者回复。")  # 回执不算
        line = "\n".join(self.sec.overview())
        self.assertIn("你已回复，等调度者接着处理", line)
        self.assertNotIn("⚠️", line)

    def test_quiet_mode_filters_process_notes_and_receipts_and_patrol_is_overview_only(self) -> None:
        # 免打扰下：过程存档、步骤完成、中途冒烟播报、无残留收尾回执只留 GitLab，不刷私聊；异常残留与最终交卷仍马上推。
        self.w.agent_note(7, "484 测试用例（已评审）：https://git.example.com/-/snippets/1\n\n<!-- hwf-kind:record -->")
        self.w.agent_note(7, "步骤 1 完成：接口接入\n\n<!-- hwf-kind:step-done -->")
        self.w.agent_note(7, "**国内冒烟首轮未通过**：站点可用，验收点未执行")
        self.w.agent_note(7, "**国内冒烟第2轮**：仍未进入验收")
        self.w.agent_note(7, "## 待定清单\n- 低风险文案优化")
        self.w.agent_note(7, "**收尾回执**（wrapup.py 生成）\n- worktree 已清理")
        self.w.agent_note(7, "**收尾回执**（wrapup.py 生成）\n- **残留**：端口 3001 未释放")
        self.w.agent_note(7, "@pengjh 整项交付完成，上线 MR：https://git.example.com/frontend/web-v1/-/merge_requests/9\n\n<!-- hwf-kind:delivery-done -->")
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 2)
        self.assertIn("**残留**", texts[0])
        self.assertIn("整项交付完成", texts[1])
        self.assertTrue(all("hwf-kind" not in item for item in texts))
        self.assertEqual(self.state["secretary"]["held"], {})
        self.now += 3600
        text = self.sec.patrol()
        self.assertIn("每小时巡检", text)
        self.assertNotIn("这一小时的进展", text)

    def test_substantive_reply_starting_with_received_is_forwarded_while_command_acks_stay_quiet(self) -> None:
        self.w.agent_note(7, "已收到 note_25884 的重试要求，正在重新领取后台开发成员。")
        self.w.agent_note(7, f"@pengjh 开始跑交付流程。已收到，已转交目标 issue {URL7} 的消息，等待调度者回复。")
        self.w.agent_note(7, f"@pengjh 已切到免打扰模式，之后私聊只在需要你决定时发消息。已收到，已转交目标 issue {URL7} 的消息，等待调度者回复。")
        self.w.agent_note(7, "@pengjh 已清空调度者上下文（新会话 `sess-1`），它会按任务记录接着做。")
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 1)
        self.assertIn("重新领取后台开发成员", texts[0])

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

    def test_ship_request_to_other_name_prefix_is_not_a_request(self) -> None:
        self.w.agent_note(7, "@pengjhx 请授权合并 MR", at="2026-11-12T10:00:00.000+08:00")
        self.w.ship(7, "2026-11-12T10:00:05.000+08:00")
        self.assertIn("没 @你", "\n".join(self.sec.overview()))

    def test_work_items_and_issues_links_are_one_issue(self) -> None:
        work_items = URL7.replace("/-/issues/", "/-/work_items/")
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {work_items: {
            "project": "frontend/web-v1", "iid": 7, "repo": str(self.home / "repo")}}}))
        self.state["threads"] = {"om_t": {"project": "frontend/web-v1", "iid": 7}}
        self.w.agent_note(7, "进展：开发中")
        self.sec.poll()
        self.assertEqual(list(self.state["secretary"]["issues"]), [URL7])
        self.assertEqual(len(self.forwarded()), 1)
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
        self.w.agent_note(7, "步骤 1 完成：列表改版\n\n<!-- hwf-kind:step-done -->")
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all("hwf-kind" not in item for item in texts))

    def test_local_state_verbose_header_also_forwards_process_notes(self) -> None:
        state_file = self.home / "repo/.hwf/tmp/frontend-web-v1-7/STATE.md"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("# 调度记录\n\n<!-- hwf:top -->\n- 通知模式：详尽（会话切换）\n<!-- /hwf:top -->\n", encoding="utf-8")
        self.w.agent_note(7, "步骤 1 完成：列表改版\n\n<!-- hwf-kind:step-done -->")
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        self.assertIn("步骤 1 完成", self.forwarded()[0]["text"])

    def test_patrol_flags_exited_scheduler_and_silence(self) -> None:
        self.w.owner = {"task": "frontend-web-v1-7", "live_pane": None, "status": "in-progress"}
        self.now += 4 * 3600
        text = self.sec.patrol()
        self.assertIn("调度者已退出", text)
        self.assertIn("超过 3 小时没有动静", text)

    def test_natural_status_question_uses_unified_decision(self) -> None:
        self.w.respond({"action": "answer", "mode": "analyze", "reply": "#7 正在开发。", "issue": None,
                        "reference": "none", "evidence": ""})
        self.b.handle(msg("om_q1", "进度"))
        self.assertIn("#7", self.w.replies[-1]["text"])
        self.assertFalse(self.w.replies[-1]["in_thread"])
        self.assertEqual(len(self.w.omp_cmds), 1)
        self.assertEqual(self.w.writes, [])

    def test_question_uses_classified_reply_and_reports_unavailable_model(self) -> None:
        decision = {"action": "answer", "mode": "analyze", "reply": "#7 在开发", "issue": None,
                    "reference": "none", "evidence": ""}
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": json.dumps(decision)}]}})
        self.b.handle(msg("om_q2", "现在哪个最急？"))
        self.assertEqual(self.w.replies[-1]["text"], f"[#7]({URL7}) 在开发")
        self.assertFalse(self.w.replies[-1]["in_thread"])
        self.w.omp_out = ""
        self.b.handle(msg("om_q3", "还有吗？"))
        self.assertIn("尚未新建或启动", self.w.replies[-1]["text"])

    def test_model_answer_links_known_issue_numbers(self) -> None:
        answer = "**#7 旧单**：开发中；[#7](x) 已有链接不重复；#70 不认识；见 note#7a"
        decision = {"action": "answer", "mode": "analyze", "reply": answer, "issue": None,
                    "reference": "none", "evidence": ""}
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": json.dumps(decision)}]}})
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
        self.assertIn("#7 Agent", material)
        self.assertIn("A、B 里选一个", material)

    def test_recent_comments_respect_explicit_candidate_scope(self) -> None:
        self.w.agent_note(7, "只属于任务七的评论")
        self.assertEqual(self.sec.recent_notes(candidates=[]), [])
        notes = self.sec.recent_notes(candidates=[{"project": "frontend/web-v1", "iid": 7}])
        self.assertTrue(any("只属于任务七的评论" in note for note in notes))

    def test_natural_scheduler_request_binds_without_replacing_user_text(self) -> None:
        decision = {"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                    "reference": "explicit", "evidence": "7"}
        self.w.omp_out = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": json.dumps(decision)}]}})
        text = "你帮我问7的调度，为什么状态还是等我处理？"
        self.b.handle(msg("om_q7", text))
        method, path, fields, as_user = self.w.writes[-1]
        self.assertEqual((method, path, as_user), ("POST", "projects/frontend/web-v1/issues/7/notes", True))
        self.assertTrue(fields["body"].startswith("@pjhagent " + text))
        self.assertIn("#7", self.w.replies[-1]["text"])
        self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))

    def test_task_control_uses_one_decision_without_new_issue_or_go(self) -> None:
        for index, text in enumerate(("让7先停止测试，测试环境在维护", "让#7继续验证", "告诉7先别合并")):
            with self.subTest(text=text):
                self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                                "reference": "explicit", "evidence": "7"})
                before = len(self.w.omp_cmds)
                self.b.handle(msg(f"om_control_{index}", text))
                self.assertEqual(len(self.w.omp_cmds) - before, 1)
                self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))
                self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
                self.assertIn(text, self.w.writes[-1][2]["body"])
                self.assertNotIn("/go", self.w.writes[-1][2]["body"])

    def test_ordinary_quantity_does_not_bind_existing_task(self) -> None:
        self.w.respond({"action": "create", "mode": "analyze", "reply": "", "issue": None,
                        "reference": "new", "evidence": "7个测试样本"})
        self.b.handle(msg("om_quantity", "请增加7个测试样本"))
        self.assertEqual(len(self.w.omp_cmds), 1)
        self.assertEqual(len([w for w in self.w.writes if w[1].endswith("/issues")]), 1)
        self.assertFalse(any(w[1].endswith("/issues/7/notes") for w in self.w.writes))

    def test_answer_mapping_rejects_unknown_foreign_and_ambiguous_targets(self) -> None:
        self.sec.state["issues"][URL7[:-1] + "8"] = {"project": "frontend/web-v1", "iid": 8}
        answers = ("#7 与 #8 都在测试。", f"{URL7} 和 https://git.example.com/other/project/-/issues/7",
                   "https://git.example.com/frontend/web-v1/-/issues/99", "没有任务链接。")
        for index, answer in enumerate(answers):
            with self.subTest(answer=answer):
                mid = f"om_ambiguous_{index}"
                self.sec.remember_reply(mid, self.sec.linkify(answer))
                self.assertNotIn(mid, self.sec.state["msgs"])

    def test_closed_origin_drops_out_of_watch(self) -> None:
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))
        self.sec.poll()
        self.assertEqual(self.state["secretary"]["issues"], {})

    def test_notes_posted_right_before_close_are_forwarded_before_dropping(self) -> None:
        self.w.agent_note(7, "测试通过")
        self.sec.poll()
        self.assertEqual(len(self.forwarded()), 1)
        self.w.agent_note(7, "生产验证通过")
        self.w.agent_note(7, "交付收尾")
        self.w.issues[7]["state"] = "closed"
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))  # 关闭后同一轮就掉出名单
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 3)
        self.assertIn("测试通过", texts[0])
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

    def test_jev_quiet_fallback_suppresses_untagged_progress_noise_and_forwards_real_answers(self) -> None:
        answers = [0.06, 0.91]
        self.sec.jev = SEC._JC.JevClient(
            {"api_key": "ts-test"},
            transport=lambda *a: {
                "model": "jev-1.14.0",
                "answers": {"attention": {"type": "noul", "noul": answers.pop(0)}},
                "usage": {},
            },
            sleep_fn=lambda _: None,
        )
        self.w.agent_note(7, "已切换到独立分支跑完构建，准备进入下一阶段自检。")
        self.w.agent_note(7, "排查结果：白屏是因为 CDN 缓存未刷新，刷新后已恢复正常。")
        self.sec.poll()
        texts = [s["text"] for s in self.forwarded()]
        self.assertEqual(len(texts), 1)
        self.assertIn("白屏是因为 CDN 缓存未刷新", texts[0])

    def test_patrol_warns_when_pending_reviews_reach_wip_limit(self) -> None:
        # 2 个待处理：不触发限流提示；3 个待处理（含待上线/等你处理）：触发顶部审核限流提示
        lines_two = [
            "- [#1](https://git.example.com/p/-/issues/1) 需求一：状态:等你处理\n  ⚠️ 等你处理",
            "- [#2](https://git.example.com/p/-/issues/2) 需求二：状态:待上线",
            "- [#3](https://git.example.com/p/-/issues/3) 需求三：状态:等你处理；你已回复，等调度者接着处理",
        ]
        self.assertEqual(self.sec.pending_review_count(lines_two), 2)
        self.assertEqual(self.sec.review_load_banner(lines_two), "")

        for iid, label in ((7, "状态:等你处理"), (8, "状态:待上线"), (9, "状态:等你处理")):
            url = f"https://git.example.com/frontend/web-v1/-/issues/{iid}"
            self.w.issues[iid] = {
                "iid": iid, "title": f"任务{iid}", "state": "opened", "labels": [label],
                "notes": [], "events": [],
            }
            self.state["secretary"]["issues"][url] = {
                "project": "frontend/web-v1", "iid": iid, "title": f"任务{iid}",
                "repo": str(self.home / "repo"), "last_note": 0, "last_agent_at": self.now,
            }
        self.now += 3600
        text = self.sec.patrol()
        self.assertIsNotNone(text)
        self.assertIn("同时等你拍板或确认上线", text)
        self.assertIn("达到审核提醒阈值 3", text)

    def test_watched_standby_issue_announced_and_reply_routes_cross_project(self) -> None:
        cross_url = "https://git.example.com/backend/api/-/issues/42"
        self.w.projects["backend/api"] = {"path_with_namespace": "backend/api", "archived": False}
        self.w.extra_issues[("backend/api", 42)] = {
            "iid": 42, "title": "订单导出报错", "state": "opened", "labels": [], "notes": [], "events": [],
        }
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({
            "origins": {},
            "watched_issues": {
                cross_url: {
                    "project": "backend/api",
                    "iid": 42,
                    "title": "订单导出报错",
                    "status": "standby",
                    "kind": "assigned_user",
                    "actor": "colleague",
                    "event_key": "user_todo:777",
                }
            },
        }))
        self.sec.poll()
        self.assertTrue(any("backend/api#42" in s["text"] and "新登记待你处理" in s["text"] for s in self.w.sent))
        # 对应提醒消息已映射回跨项目 URL
        self.assertIn(cross_url, self.sec.state["msgs"].values())
        # 第二轮不重复推送同一条待命提醒
        sent_before = len(self.w.sent)
        self.sec.poll()
        self.assertEqual(len(self.w.sent), sent_before)

    def test_ambiguous_same_iid_across_projects_requires_disambiguation(self) -> None:
        cross_url = "https://git.example.com/backend/api/-/issues/7"
        self.w.projects["backend/api"] = {"path_with_namespace": "backend/api", "archived": False}
        self.w.extra_issues[("backend/api", 7)] = {
            "iid": 7, "title": "另一个七号", "state": "opened", "labels": [], "notes": [], "events": [],
        }
        self.sec.state["issues"][cross_url] = {
            "project": "backend/api", "iid": 7, "title": "另一个七号",
            "repo": "", "last_note": 0, "last_agent_at": self.now,
        }
        candidates = list(self.sec.known_tasks().values())
        self.assertIn(7, self.sec._ambiguous_iids(candidates))
        self.w.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 7,
                        "reference": "explicit", "evidence": "backend/api#7"})
        self.b.handle(msg("om_cross7", "帮我看下 backend/api#7 的进展"))
        self.assertEqual(self.w.writes[-1][1], "projects/backend/api/issues/7/notes")

    def test_closed_watched_issue_is_not_announced_tracked_or_shown_in_patrol(self) -> None:
        closed_url = "https://git.example.com/backend/user/-/issues/1"
        self.w.projects["backend/user"] = {"path_with_namespace": "backend/user", "archived": False}
        self.w.extra_issues[("backend/user", 1)] = {
            "iid": 1, "title": "旧邮箱绑定", "state": "closed", "labels": [], "notes": [], "events": [],
        }
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({
            "origins": {URL7: {"project": "frontend/web-v1", "iid": 7, "repo": str(self.home / "repo")}},
            "watched_issues": {
                closed_url: {
                    "project": "backend/user",
                    "iid": 1,
                    "title": "旧邮箱绑定",
                    "status": "active",
                    "kind": "mentioned_agent",
                    "actor": "pengjh",
                    "event_key": "todo:3100",
                }
            },
        }))
        sent_before = len(self.w.sent)
        self.sec.poll()
        # 1. 已关闭的跨项目 watched_issues 既不推送飞书接活提醒，也不纳入 secretary.issues
        self.assertEqual(len(self.w.sent), sent_before)
        self.assertNotIn(closed_url, self.sec.state["issues"])

        # 2. 先在 #7 打开时跑一次巡检建立基线，再将 #7 关闭（仍挂在 watched 窗口期内、甚至残留等你处理标签），
        #    巡检与总览应直接跳过已关闭单，不展示「已关闭」也不误算待审数。
        self.now += 3600
        first_patrol = self.sec.patrol()
        self.assertIsNotNone(first_patrol)
        self.assertIn("#7", first_patrol)

        self.w.issues[7]["labels"] = ["状态:等你处理"]
        self.w.issues[7]["state"] = "closed"
        self.w.agent_note(7, "最终关单说明")
        self.assertEqual(self.sec.overview(), [])
        self.assertEqual(self.sec.pending_review_count(self.sec.overview()), 0)
        self.now += 3600
        patrol_text = self.sec.patrol()
        self.assertIsNotNone(patrol_text)
        self.assertNotIn("已关闭", patrol_text)
        self.assertIn("现在没有在跑的需求", patrol_text)
        # 且当上游移除后，关闭前的最后一条评论仍会被补发再移出跟踪
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}, "watched_issues": {}}))
        self.sec.poll()
        self.assertTrue(any("最终关单说明" in s["text"] for s in self.forwarded()))
        self.assertNotIn(URL7, self.sec.state["issues"])




if __name__ == "__main__":
    unittest.main()
