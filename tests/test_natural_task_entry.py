"""自然语言任务入口：用真实桥路由，只替换模型进程和外部服务。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

SPEC = importlib.util.spec_from_file_location(
    "natural_bridge_shared", Path(__file__).resolve().parent / "test_feishu_bridge.py")
assert SPEC is not None and SPEC.loader is not None
TFB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TFB
SPEC.loader.exec_module(TFB)
FB, msg, ME, BOT = TFB.FB, TFB.msg, TFB.ME, TFB.BOT


class NaturalEntryTest(unittest.TestCase):
    setUp = TFB.BridgeTest.setUp
    fake_upload = TFB.BridgeTest.fake_upload

    def model(self, action="create", mode="analyze", reply="", issue=None, *, reference=None, evidence=None):
        previous = self.w
        upload_count = len(self.uploads)
        self.prompts = []
        secretary = self.b.secretary
        classify = type(secretary).classify_intent.__get__(secretary)

        def classify_current(text, context="", **kwargs):
            self.current_request = text
            self.current_linked = kwargs.get("linked_issue")
            return classify(text, context, **kwargs)

        self.b.secretary.classify_intent = classify_current

        def run(cmd, **kwargs):
            if cmd[0] == "omp":
                self.prompts.append(cmd)
                self.assertEqual(len(self.uploads), upload_count, "分类前不能上传附件")
                ref = reference or ("new" if action == "create" else
                                    "linked" if issue and self.current_linked == issue else
                                    "explicit" if issue else "none")
                proof = evidence if evidence is not None else (
                    str(issue) if ref == "explicit" else self.current_request if ref != "none" else "")
                result = {"action": action, "mode": mode, "reply": reply, "issue": issue,
                          "reference": ref, "evidence": proof}
                event = {"type": "turn_end", "message": {"content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(event, ensure_ascii=False), "")
            return previous(cmd, **kwargs)
        self.b.run = run

    def test_natural_bug_request_creates_issue_and_enters_debug_once(self):
        self.model(mode="debug")
        text = "帮我修这个bug：画布保存以后再次打开内容丢失"
        self.b.handle(msg("om_bug", text))
        self.b.handle(msg("om_bug", text))
        creates = [w for w in self.w.writes if w[1].endswith("/issues")]
        self.assertEqual(len(creates), 1)
        self.assertIn(text, creates[0][2]["description"])
        notes = [w[2]["body"] for w in self.w.writes if w[1].endswith("/notes")]
        self.assertTrue(any(body.startswith("/go debug") for body in notes), notes)
        self.assertTrue(all(w[3] for w in self.w.writes))
        self.assertIn("修复", self.w.replies[-1]["text"])


    def test_feature_request_defaults_to_analysis_in_mentioned_group(self):
        self.model()
        text = "我想在画布列表加一个按名称排序的功能"
        self.b.handle(msg("om_feature", text, chat="oc_g", chat_type="group", mentions=[{"id": BOT}]))
        self.assertEqual(len(self.w.writes), 1)
        self.assertEqual(self.w.writes[0][2]["title"], text)
        self.assertIn("只读分析", self.w.replies[-1]["text"])

    def test_natural_entry_keeps_allowlist_and_group_mention_gate(self):
        self.model(mode="debug")
        self.b.handle(msg("om_no_at", "帮我修bug", chat="oc_g", chat_type="group"))
        self.b.handle(msg("om_stranger", "帮我修bug", sender="ou_other"))
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.w.writes, [])

    def test_discussion_and_negation_only_reply_without_upload(self):
        self.model(action="answer", reply="好的，先讨论，不开工。")
        self.w.messages["om_quote"] = "帮我修这个 bug #7 [Image: img_quote]"
        self.b.handle(msg("om_discuss", "先别修，只讨论怎么做 [Image: img_current]", reply_to="om_quote"))
        self.assertEqual(self.w.writes, [])
        self.assertEqual(self.w.downloads, [])
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.w.replies[-1]["text"], "好的，先讨论，不开工。")
        self.assertIn("帮我修这个 bug #7", self.prompts[-1][-1])

    def quoted_message(self, mid, text, sender=BOT, sender_type="app"):
        previous = self.b.run

        def run(cmd, **kwargs):
            if cmd[:3] == ["lark-cli", "im", "+messages-mget"] \
                    and cmd[cmd.index("--message-ids") + 1] == mid:
                return TFB.ok({"ok": True, "data": {"messages": [{
                    "message_id": mid, "content": text,
                    "sender": {"id": sender, "sender_type": sender_type}}]}})
            return previous(cmd, **kwargs)

        self.b.run = run

    def test_unknown_bot_progress_reply_routes_pause_without_creating(self):
        self.w.issues[488] = {"iid": 488, "title": "进度任务", "state": "opened", "notes": []}
        self.model(action="relay", issue=488)
        self.quoted_message("om_progress", "#488 还在测试中。\n"
                            "https://git.example.com/frontend/web-v1/-/issues/488")
        text = "先暂停测试，测试环境在维护"
        self.b.handle(msg("om_pause", text, reply_to="om_progress"))
        self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))
        body = self.w.issues[488]["notes"][-1]["body"]
        self.assertIn(text, body)
        self.assertNotIn("/go", body)

    def test_known_progress_reply_preserves_pause_and_attachment(self):
        self.model(action="relay", issue=7)
        self.state["secretary"]["msgs"]["om_progress"] = self.b.issue_url(self.b.project, 7)
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护 [Image: img_pause]",
                          reply_to="om_progress"))
        self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))
        body = self.w.issues[7]["notes"][-1]["body"]
        self.assertIn("先暂停测试，测试环境在维护", body)
        self.assertIn("/uploads/x/img_pause.jpg", body)
        self.assertNotIn("/go", body)

    def test_quoted_task_without_trusted_unique_target_never_creates(self):
        url = self.b.issue_url(self.b.project, 7)
        cases = [
            ("user", url, ME, "user"),
            ("other_bot", url, "cli_other_bot", "app"),
            ("foreign_project", "https://git.example.com/other/project/-/issues/7", BOT, "app"),
            ("foreign_host", "https://elsewhere.example/frontend/web-v1/-/issues/7", BOT, "app"),
            ("multiple", url + " " + self.b.issue_url(self.b.project, 488), BOT, "app"),
            ("mixed_projects", url + " https://git.example.com/other/project/-/issues/7", BOT, "app"),
        ]
        self.model(mode="debug")
        for name, quoted, sender, sender_type in cases:
            with self.subTest(name=name):
                self.quoted_message("om_" + name, quoted, sender, sender_type)
                self.b.handle(msg("om_pause_" + name, "先暂停测试，测试环境在维护", reply_to="om_" + name))
                self.assertEqual(self.w.writes, [])
                self.assertIn("任务", self.w.replies[-1]["text"])

    def test_unreadable_task_reply_never_falls_back_to_create(self):
        self.model(mode="debug")
        self.w.unreadable_messages.add("om_missing")
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护", reply_to="om_missing"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("任务", self.w.replies[-1]["text"])

    def test_recovered_bot_reply_keeps_group_mention_gate(self):
        self.model(action="relay", issue=7)
        self.quoted_message("om_progress", self.b.issue_url(self.b.project, 7))
        self.b.handle(msg("om_no_at", "先暂停测试，测试环境在维护", chat="oc_g", chat_type="group",
                          reply_to="om_progress"))
        self.assertEqual(self.w.writes, [])
        self.b.handle(msg("om_at", "先暂停测试，测试环境在维护", chat="oc_g", chat_type="group",
                          mentions=[{"id": BOT}], reply_to="om_progress"))
        self.assertEqual(len(self.w.writes), 1)
        self.assertEqual(self.w.writes[0][1], "projects/frontend/web-v1/issues/7/notes")
        self.assertNotIn("/go", self.w.writes[0][2]["body"])

    def test_reply_recovers_raw_parent_and_verifies_current_cli_bot(self):
        self.b.cfg.pop("bot_open_id")
        self.model(action="relay", issue=7)
        self.quoted_message("om_progress", self.b.issue_url(self.b.project, 7), sender="cli_this_bot")
        previous = self.b.run
        identities = []

        def run(cmd, **kwargs):
            if cmd[:2] == ["lark-cli", "whoami"]:
                identities.append(cmd)
                return TFB.ok({"appId": "cli_this_bot", "identity": "bot", "available": True,
                               "tokenStatus": "ready"})
            return previous(cmd, **kwargs)

        self.b.run = run
        self.w.raw_messages["om_pause"] = {"message_id": "om_pause", "parent_id": "om_progress"}
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护"))
        self.b.handle(msg("om_again", "测试环境恢复之前不要继续", reply_to="om_progress"))
        self.assertEqual(len(identities), 1)
        self.assertEqual(len(self.w.writes), 2)
        self.assertTrue(all(w[1] == "projects/frontend/web-v1/issues/7/notes" for w in self.w.writes))
        self.assertTrue(all("/go" not in w[2]["body"] for w in self.w.writes))

    def test_missing_cli_identity_or_issue_cannot_create_from_reply(self):
        self.model(mode="debug")
        self.quoted_message("om_gone", self.b.issue_url(self.b.project, 999))
        self.b.handle(msg("om_pause_gone", "先暂停测试，测试环境在维护", reply_to="om_gone"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("尚未新建", self.w.replies[-1]["text"])
        self.b.cfg.pop("bot_open_id")
        self.quoted_message("om_unknown_bot", self.b.issue_url(self.b.project, 7), sender="cli_other")
        previous = self.b.run

        def run(cmd, **kwargs):
            if cmd[:2] == ["lark-cli", "whoami"]:
                return TFB.ok({"identity": "bot", "available": False})
            return previous(cmd, **kwargs)

        self.b.run = run
        self.b.handle(msg("om_pause_unknown", "先暂停测试，测试环境在维护", reply_to="om_unknown_bot"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("尚未新建", self.w.replies[-1]["text"])

    def test_natural_progress_reply_records_only_bot_reply_for_followup(self):
        url = self.b.issue_url(self.b.project, 488)
        self.w.issues[488] = {"iid": 488, "title": "进度任务", "state": "opened", "notes": []}
        self.state["secretary"]["issues"][url] = {"iid": 488, "project": self.b.project, "title": "进度任务"}
        self.model(action="answer", issue=488, reply="#488 仍在测试，尚未结束。")
        self.b.handle(msg("om_question_488", "488现在是什么进度？"))
        reply_id = f"om_r{len(self.w.replies)}"
        self.assertEqual(self.state["secretary"]["msgs"][reply_id], url)
        self.assertNotIn("om_question_488", self.state["secretary"]["msgs"])
        self.model(action="relay", issue=488)
        self.b.handle(msg("om_pause_488", "先暂停测试，测试环境在维护", reply_to=reply_id))
        self.assertEqual(len(self.w.writes), 1)
        self.assertEqual(self.w.writes[0][1], "projects/frontend/web-v1/issues/488/notes")
        self.assertIn("先暂停测试，测试环境在维护", self.w.writes[0][2]["body"])
        self.assertNotIn("/go", self.w.writes[0][2]["body"])

    def test_bare_quoted_task_number_is_not_permission_to_create(self):
        self.model(mode="debug")
        self.w.messages["om_bare"] = "#7 还在测试"
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护", reply_to="om_bare"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("尚未新建", self.w.replies[-1]["text"])

    def test_missing_reply_metadata_cannot_create(self):
        self.model(mode="debug")
        self.w.unreadable_messages.add("om_pause")
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("尚未新建", self.w.replies[-1]["text"])

    def test_recovered_reply_keeps_attachment_and_receipt_failure_truthful(self):
        self.quoted_message("om_progress", self.b.issue_url(self.b.project, 7) + "#note_70")
        self.w.reply_failures = 1
        self.b.handle(msg("om_pause", "先暂停测试，测试环境在维护 [Image: img_pause]",
                          reply_to="om_progress"))
        self.assertEqual(len(self.w.writes), 1)
        self.assertIn("/uploads/x/img_pause.jpg", self.w.writes[0][2]["body"])
        self.assertNotIn("/go", self.w.writes[0][2]["body"])
        self.assertIn("不用重发", self.w.replies[-1]["text"])

    def test_unavailable_model_never_writes_or_claims_started(self):
        self.b.handle(msg("om_failed", "帮我修这个bug [Image: img_no_upload]"))
        self.assertEqual(self.w.writes, [])
        self.assertEqual(self.uploads, [])
        self.assertIn("尚未新建或启动", self.w.replies[-1]["text"])

    def test_thread_and_quote_background_are_preserved_after_classification(self):
        self.model(mode="debug")
        self.w.messages["om_report"] = "保存后图片消失 [Image: img_report]"
        self.w.threads["omt_report"] = [{"message_id": "om_report", "content": self.w.messages["om_report"],
                                         "sender": {"id": ME, "sender_type": "user"}}]
        self.b.handle(msg("om_fix", "帮我修这个bug", thread="omt_report", root="om_report", reply_to="om_report"))
        description = self.w.writes[0][2]["description"]
        self.assertIn("保存后图片消失", description)
        self.assertEqual(self.w.downloads, [("om_report", "img_report")])
        self.assertIn("保存后图片消失", self.prompts[-1][-1])

    def test_natural_bind_keeps_original_text_and_starts_existing_debug(self):
        self.model(action="relay", mode="debug", issue=7)
        text = "请帮我修复 #7 的图片丢失问题"
        self.b.handle(msg("om_bind", text))
        self.assertFalse(any(w[1].endswith("/issues") for w in self.w.writes))
        body = self.w.issues[7]["notes"][-1]["body"]
        self.assertTrue(body.startswith("@pjhagent /go debug"))
        self.assertIn(text, body)

    def test_natural_bind_rejects_foreign_project(self):
        self.model(action="relay", issue=7)
        self.b.handle(msg("om_foreign", "关联 https://git.example.com/other/project/-/issues/7"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("不属于", self.w.replies[-1]["text"])

    def test_explicit_foreign_link_also_stays_read_only(self):
        self.b.handle(msg("om_foreign", "https://git.example.com/other/project/-/issues/7"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("不属于", self.w.replies[-1]["text"])

    def test_background_issue_number_cannot_authorize_binding(self):
        self.model(action="relay", issue=7)
        self.w.messages["om_quote"] = "这是 #7"
        self.b.handle(msg("om_bind", "关联这个", reply_to="om_quote"))
        self.assertEqual(self.w.writes, [])

    def test_linked_natural_start_enters_debug_without_new_issue(self):
        self.b.handle(msg("om_task", "需求：保存图片失败"))
        self.model(action="relay", mode="debug", issue=8)
        self.b.handle(msg("om_start", "开始修复", root="om_task"))
        self.assertEqual(sum(w[1].endswith("/issues") for w in self.w.writes), 1)
        self.assertTrue(self.w.issues[8]["notes"][-1]["body"].startswith("/go debug"))
        self.assertIn("开始修复", self.w.issues[8]["notes"][-1]["body"])

    def test_linked_negation_and_model_failure_keep_original_without_go(self):
        self.b.handle(msg("om_task", "需求：保存图片失败"))
        self.b.handle(msg("om_extra", "只在 Windows 出现", root="om_task"))
        self.model(action="relay", issue=8)
        self.b.handle(msg("om_stop", "先不要修复，补充一张图", root="om_task"))
        bodies = [n["body"] for n in self.w.issues[8]["notes"]]
        self.assertTrue(all(not body.startswith("/go") for body in bodies))
        self.assertIn("只在 Windows 出现", bodies[0])
        self.assertIn("先不要修复", bodies[1])

    def test_clarification_accepts_next_answer_and_preserves_original(self):
        self.model(action="clarify", reply="哪个页面需要排序？")
        self.b.handle(msg("om_question", "需要一个按名称排序功能"))
        self.assertEqual(self.w.writes, [])
        self.model()
        self.b.handle(msg("om_answer", "画布列表页"))
        description = self.w.writes[0][2]["description"]
        self.assertIn("需要一个按名称排序功能", description)
        self.assertIn("画布列表页", description)
        self.assertIn("哪个页面需要排序", self.prompts[-1][-1])
        self.assertEqual(self.state["pending_intents"], {})

    def test_clarification_accepts_direct_reply_after_other_discussion(self):
        self.model(action="clarify", reply="哪个页面？")
        self.b.handle(msg("om_question", "需要排序功能", thread="omt_a"))
        reply_id = f"om_r{len(self.w.replies)}"
        self.model(action="answer", reply="其他话题先讨论。")
        self.b.handle(msg("om_other", "先讨论权限", thread="omt_b"))
        self.model()
        self.b.handle(msg("om_answer", "画布列表", thread="omt_a", reply_to=reply_id))
        self.assertIn("需要排序功能", self.w.writes[0][2]["description"])

    def test_clarification_isolated_by_chat_thread_sender_and_reply(self):
        self.model(action="clarify", reply="哪个页面？")
        self.b.handle(msg("om_question", "保密排序需求", chat="oc_a", thread="omt_a"))
        self.b.users["ou_other"] = "other"
        cases = [{"chat": "oc_b", "thread": "omt_a"},
                 {"chat": "oc_a", "thread": "omt_b"},
                 {"chat": "oc_a", "thread": "omt_a", "sender": "ou_other"},
                 {"chat": "oc_a", "reply_to": "om_unrelated"}]
        for index, values in enumerate(cases):
            with self.subTest(values=values):
                self.model(action="answer", reply="先确认需求。")
                self.b.handle(msg(f"om_isolated_{index}", "列表页", **values))
                self.assertNotIn("保密排序需求", self.prompts[-1][-1])
        self.assertEqual(self.w.writes, [])

    def test_created_issue_receipt_failure_does_not_lose_debug_command(self):
        self.model(mode="debug")
        self.w.reply_failures = 1
        self.b.handle(msg("om_bug", "帮我修图片丢失bug"))
        self.assertEqual(sum(w[1].endswith("/issues") for w in self.w.writes), 1)
        self.assertTrue(self.w.issues[8]["notes"][-1]["body"].startswith("/go debug"))
        self.assertIn("不用重发", self.w.replies[-1]["text"])

    def test_debug_write_failure_reports_created_but_not_started(self):
        self.model(mode="debug")
        run = self.b.run

        def fail_note(cmd, **kwargs):
            if cmd[0] == "glab" and "POST" in cmd and cmd[cmd.index("-X") + 2].endswith("/notes"):
                return subprocess.CompletedProcess(cmd, 1, "", "temporarily unavailable")
            return run(cmd, **kwargs)

        self.b.run = fail_note
        self.b.handle(msg("om_bug", "帮我修图片丢失bug"))
        self.assertEqual(sum(w[1].endswith("/issues") for w in self.w.writes), 1)
        self.assertIn("已建单", self.w.replies[-1]["text"])
        self.assertIn("未发送成功", self.w.replies[-1]["text"])
        self.assertIn("#8", self.w.replies[-1]["text"])
        self.assertEqual(self.w.issues[8]["notes"], [])

    def test_explicit_delivery_request_uses_existing_go_protocol(self):
        self.model(mode="deliver")
        text = "按刚才讨论的方案直接实现列表排序功能"
        self.b.handle(msg("om_deliver", text))
        self.assertEqual(self.w.issues[8]["notes"][-1]["body"], "/go\n\n用户原话：" + text)

    def test_clarified_bug_request_keeps_execution_intent_and_attachment(self):
        self.model(action="clarify", reply="哪一步出错？")
        self.b.handle(msg("om_question", "帮我修这个bug [Image: img_bug]"))
        self.assertEqual(self.uploads, [])
        self.model(mode="debug")
        self.b.handle(msg("om_answer", "画布里点导出就报500"))
        description = self.w.writes[0][2]["description"]
        self.assertIn("帮我修这个bug", description)
        self.assertIn("画布里点导出就报500", description)
        self.assertEqual(self.w.downloads, [("om_question", "img_bug")])
        self.assertTrue(self.w.issues[8]["notes"][-1]["body"].startswith("/go debug"))
        self.assertIn("同一次待澄清请求的用户原话", self.prompts[-1][-1])

    def test_latest_clarification_cancellation_never_creates_task(self):
        self.model(action="clarify", reply="哪一步出错？")
        self.b.handle(msg("om_question", "帮我修这个bug"))
        self.model(action="answer", reply="好的，先不修改。")
        self.b.handle(msg("om_answer", "先别改，我再确认一下"))
        self.assertEqual(self.w.writes, [])
        self.assertEqual(self.state["pending_intents"], {})

    def test_quoted_clarification_accepts_next_unthreaded_answer(self):
        self.model(action="clarify", reply="在哪个页面？")
        self.w.messages["om_report"] = "列表排序有问题"
        self.b.handle(msg("om_question", "帮我修这个", reply_to="om_report"))
        self.model(mode="debug")
        self.b.handle(msg("om_answer", "画布列表页"))
        description = self.w.writes[0][2]["description"]
        self.assertIn("帮我修这个", description)
        self.assertIn("列表排序有问题", description)

    def test_clarification_survives_restart_and_unavailable_model(self):
        self.model(action="clarify", reply="哪个页面？")
        self.b.handle(msg("om_question", "需要列表排序功能"))
        restored = json.loads(json.dumps(self.state))
        self.b = FB.Bridge(self.b.cfg, restored, run=self.w, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(self.tmp.name) / "media")
        self.b._secretary = FB.secretary_module().Secretary(self.b, home=Path(self.tmp.name))
        self.b.handle(msg("om_unavailable", "画布页"))
        self.assertEqual(self.w.writes, [])
        self.model()
        self.b.handle(msg("om_answer", "画布列表页"))
        self.assertIn("需要列表排序功能", self.w.writes[0][2]["description"])

    def unified_decision(self, action, *, issue=None, reference="none", evidence="", reply="", mode="analyze"):
        self.decisions = []

        def answer(prompt, **kwargs):
            self.decisions.append(prompt)
            return json.dumps({"action": action, "issue": issue, "reference": reference,
                               "evidence": evidence, "reply": reply, "mode": mode}, ensure_ascii=False)

        self.b.secretary.ask_model = answer

    def test_mapped_private_progress_uses_one_decision_without_relay(self):
        url = self.b.issue_url(self.b.project, 7)
        self.state["secretary"]["msgs"]["om_progress"] = url
        self.unified_decision("answer", issue=7, reference="linked", evidence="进展", reply="还在测试。")
        self.b.handle(msg("om_query", "有什么进展？", reply_to="om_progress"))
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(self.w.writes, [])
        self.assertEqual(self.w.replies[-1]["text"], "还在测试。")
        self.assertEqual(self.state["secretary"]["msgs"]["om_r1"], url)

    def test_recovered_progress_question_does_not_relay(self):
        self.quoted_message("om_old", self.b.issue_url(self.b.project, 7))
        self.unified_decision("answer", issue=7, reference="linked", evidence="进展", reply="尚在测试。")
        self.b.handle(msg("om_query", "现在进展怎么样？", reply_to="om_old"))
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(self.w.writes, [])

    def test_task_name_pause_relays_without_opening_topic(self):
        url = self.b.issue_url(self.b.project, 7)
        self.state["secretary"]["issues"][url] = {"iid": 7, "project": self.b.project, "title": "旧单"}
        self.unified_decision("relay", issue=7, reference="name", evidence="旧单")
        self.b.handle(msg("om_pause_name", "旧单先暂停测试 [Image: img_pause]"))
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(len(self.w.writes), 1)
        self.assertIn("旧单先暂停测试", self.w.writes[0][2]["body"])
        self.assertIn("/uploads/x/img_pause.jpg", self.w.writes[0][2]["body"])
        self.assertNotIn("/go", self.w.writes[0][2]["body"])
        self.assertFalse(self.w.replies[-1]["in_thread"])

    def test_recent_target_survives_restart_but_not_another_user_scope(self):
        self.unified_decision("answer", issue=7, reference="explicit", evidence="#7", reply="还在测试。")
        self.b.handle(msg("om_query", "看看 #7 进度"))
        restored = json.loads(json.dumps(self.state))
        self.b = FB.Bridge(self.b.cfg, restored, run=self.w, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(self.tmp.name) / "media")
        self.b._secretary = FB.secretary_module().Secretary(self.b, home=Path(self.tmp.name))
        self.unified_decision("relay", issue=7, reference="recent", evidence="刚才那个")
        self.b.handle(msg("om_pause", "刚才那个先暂停测试"))
        self.assertEqual(len(self.w.writes), 1)
        self.b.users["ou_other"] = "other"
        self.b.handle(msg("om_other_pause", "刚才那个先暂停测试", sender="ou_other"))
        self.assertEqual(len(self.w.writes), 1)

    def test_group_task_name_cannot_select_other_groups_task(self):
        url = self.b.issue_url(self.b.project, 7)
        self.state["secretary"]["issues"][url] = {"iid": 7, "project": self.b.project, "title": "旧单"}
        self.state["threads"]["om_elsewhere"] = {"iid": 7, "project": self.b.project, "chat_id": "oc_other"}
        self.unified_decision("relay", issue=7, reference="name", evidence="旧单")
        self.b.handle(msg("om_group", "旧单暂停测试", chat="oc_group", chat_type="group", mentions=[{"id": BOT}]))
        self.assertEqual(self.w.writes, [])

    def test_recent_with_two_targets_does_not_choose_latest(self):
        for iid in (7, 9):
            self.w.issues.setdefault(iid, {"iid": iid, "title": f"任务{iid}", "state": "opened", "notes": []})
            self.unified_decision("answer", issue=iid, reference="explicit", evidence=f"#{iid}", reply="还在测试。")
            self.b.handle(msg(f"om_query_{iid}", f"看看 #{iid} 的进度"))
        self.unified_decision("relay", issue=9, reference="recent", evidence="刚才那个")
        self.b.handle(msg("om_ambiguous", "刚才那个暂停测试"))
        self.assertEqual(self.w.writes, [])

    def test_pending_thread_cannot_be_adopted_by_main_chat(self):
        self.unified_decision("clarify", reply="指哪张任务？")
        self.b.handle(msg("om_thread", "暂停那个", thread="omt_old"))
        self.unified_decision("answer", reply="请说明需求。")
        self.b.handle(msg("om_main", "列表任务"))
        self.assertNotIn("暂停那个", self.decisions[-1])

    def test_linked_independent_new_request_creates_new_task(self):
        self.b.handle(msg("om_root", "需求：修复图片"))
        self.unified_decision("create", reference="new", evidence="另外新建搜索功能")
        self.b.handle(msg("om_independent", "另外新建搜索功能，与这个任务无关", root="om_root"))
        self.assertEqual(sum(w[1].endswith("/issues") for w in self.w.writes), 2)

    def test_legacy_pending_reference_key_survives_restart(self):
        self.state["pending_intents"][json.dumps(["oc_1", ME, "om_old_quote"])] = {
            "chat": "oc_1", "sender": ME, "thread": "", "question": "哪个页面？",
            "messages": [msg("om_old_request", "需要名称排序功能")],
            "reply_ids": ["om_old_request", "om_old_question"]}
        restored = json.loads(json.dumps(self.state))
        self.b = FB.Bridge(self.b.cfg, restored, run=self.w, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(self.tmp.name) / "media")
        self.b._secretary = FB.secretary_module().Secretary(self.b, home=Path(self.tmp.name))
        self.unified_decision("create", reference="new", evidence="名称排序功能")
        self.b.handle(msg("om_new_answer", "画布列表页"))
        self.assertIn("需要名称排序功能", self.w.writes[0][2]["description"])
        self.assertIn("画布列表页", self.w.writes[0][2]["description"])
        self.assertEqual(restored["pending_intents"], {})

    def test_numeric_answer_to_clarification_keeps_original_control(self):
        self.unified_decision("clarify", reply="要暂停哪张任务？")
        self.b.handle(msg("om_pause", "先暂停测试 [Image: img_pending]"))
        self.unified_decision("relay", issue=7, reference="explicit", evidence="#7")
        self.b.handle(msg("om_choose", "#7"))
        self.assertEqual(len(self.w.writes), 1)
        body = self.w.writes[0][2]["body"]
        self.assertIn("先暂停测试", body)
        self.assertIn("/uploads/x/img_pending.jpg", body)
        self.assertNotIn("/go", body)
        self.assertEqual(self.state["pending_intents"], {})

if __name__ == "__main__":
    unittest.main()
