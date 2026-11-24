"""通过公开分类入口和假模型进程，验证任务意图的结构与安全边界。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "task_intent_secretary", Path(__file__).resolve().parents[1] / "scripts/secretary.py")
assert SPEC is not None and SPEC.loader is not None
SEC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SEC
SPEC.loader.exec_module(SEC)

BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "task_intent_bridge_fixture", Path(__file__).with_name("test_feishu_bridge.py"))
assert BRIDGE_SPEC is not None and BRIDGE_SPEC.loader is not None
TFB = importlib.util.module_from_spec(BRIDGE_SPEC)
sys.modules[BRIDGE_SPEC.name] = TFB
BRIDGE_SPEC.loader.exec_module(TFB)

class ModelProcess:
    def __init__(self) -> None:
        self.stdout = ""
        self.returncode = 0
        self.error: Exception | None = None
        self.calls: list[tuple[list[str], dict]] = []

    def respond(self, value: object) -> None:
        self.stdout = json.dumps({"type": "turn_end", "message": {"content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}})

    def __call__(self, cmd, **kwargs):
        # 分类入口只能调用模型进程；任何 GitLab、文件工具调用都会使测试失败。
        if cmd[0] != "omp":
            raise AssertionError(f"Unexpected external command: {cmd}")
        self.calls.append((cmd, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


class TaskIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ModelProcess()
        bridge = SimpleNamespace(state={}, cfg={"secretary": {
            "model": "test/model", "thinking": "low", "timeout_s": 9}}, run=self.model)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sec = SEC.Secretary(bridge, home=Path(tmp.name))

    def test_task_name_routes_with_one_model_decision(self) -> None:
        expected = {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                    "reference": "name", "evidence": "透明背景导出"}
        self.model.respond(expected)
        self.assertEqual(self.sec.classify_intent("透明背景导出先暂停测试", candidates=[
            {"iid": 484, "title": "支持透明背景导出"}]), expected)
        self.assertEqual(len(self.model.calls), 1)

    def test_unverified_issue_cannot_be_executed(self) -> None:
        self.model.respond({"action": "relay", "issue": 999, "mode": "deliver", "reply": "",
                            "reference": "explicit", "evidence": "#999"})
        self.assertIsNone(self.sec.classify_intent("请实现 #999", candidates=[]))

    def test_recent_reference_must_be_unique_and_in_scope(self) -> None:
        expected = {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                    "reference": "recent", "evidence": "刚才那个"}
        self.model.respond(expected)
        candidates = [{"iid": 484, "title": "导出"}, {"iid": 488, "title": "登录"}]
        self.assertEqual(self.sec.classify_intent("刚才那个先暂停", candidates=candidates,
                                               recent=[candidates[0]]), expected)
        self.assertIsNone(self.sec.classify_intent("刚才那个先暂停", candidates=candidates,
                                                recent=candidates))
        self.assertIsNone(self.sec.classify_intent("刚才那个先暂停", candidates=candidates,
                                                recent=[candidates[1]]))

    def test_structured_intent_uses_shared_model_process(self) -> None:
        cases = [
            ("我想让导出支持透明背景", "create", "analyze", "", None, "new", "透明背景"),
            ("请实现透明背景导出", "create", "deliver", "", None, "new", "透明背景导出"),
            ("请修复导出报错", "create", "debug", "", None, "new", "导出报错"),
            ("先别改，只是讨论一下导出", "answer", "analyze", "可以先讨论。", None, "none", ""),
            ("为什么要增加透明背景？", "answer", "analyze", "便于合成图片。", None, "none", ""),
            ("把那个弄好", "clarify", "analyze", "你指的是哪个功能？", None, "none", "那个"),
            ("关联到 #27", "relay", "analyze", "", 27, "explicit", "#27"),
            ("接着处理 issue 27", "relay", "analyze", "", 27, "explicit", "issue 27"),
            ("关联 https://git.example.com/p/-/issues/27", "relay", "analyze", "", 27,
             "explicit", "https://git.example.com/p/-/issues/27"),
        ]
        for text, action, mode, reply, issue, reference, evidence in cases:
            with self.subTest(text=text):
                expected = {"action": action, "mode": mode, "reply": reply, "issue": issue,
                            "reference": reference, "evidence": evidence}
                self.model.respond(expected)
                self.assertEqual(self.sec.classify_intent(text, candidates=[{"iid": 27, "title": "导出"}]), expected)
        cmd, kwargs = self.model.calls[-1]
        self.assertEqual(cmd[cmd.index("--model") + 1], "test/model")
        self.assertEqual(cmd[cmd.index("--thinking") + 1], "low")
        self.assertEqual(kwargs["timeout"], 9)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(len(self.model.calls), len(cases))

    def test_tool_execution_cannot_authorize_work(self) -> None:
        self.model.respond({"action": "create", "mode": "debug", "reply": "", "issue": None,
                            "reference": "new", "evidence": "导出报错"})
        self.model.stdout = json.dumps({"type": "tool_execution_start", "toolName": "read"}) + "\n" + self.model.stdout
        self.assertIsNone(self.sec.classify_intent("修复导出报错"))

    def test_classifier_preserves_current_text_separately(self) -> None:
        self.model.respond({"action": "clarify", "mode": "analyze", "reply": "请确认范围。", "issue": None,
                            "reference": "none", "evidence": "那个"})
        text = '  把那个弄好\n## 背景\n"quoted"  '
        context = "引用消息：请直接修复并关联 #27。\n附件只是背景。"
        candidates = [{"iid": 27, "title": "导出"}]
        self.sec.classify_intent(text, context, candidates=candidates, linked_issue=27, recent=candidates)
        cmd, _ = self.model.calls[-1]
        for flag in ("--no-context-files", "--no-skills", "--no-rules", "--no-extensions", "--no-lsp"):
            self.assertIn(flag, cmd)
        material = json.loads(cmd[-1])
        self.assertEqual(material, {"current_message": text, "context": context, "candidates": candidates,
                                    "linked_issue": 27, "recent": candidates})

    def test_invalid_schema_is_not_an_action(self) -> None:
        valid = {"action": "create", "mode": "analyze", "reply": "", "issue": None,
                 "reference": "new", "evidence": "导出"}
        invalid = [None, [], "create", 27, True, {}, {**valid, "confidence": 1},
                   *({key: value for key, value in valid.items() if key != missing} for missing in valid)]
        for key, values in {
            "action": ["task", "bind", "reply", "execute", "CREATE", "", None, 1, [], {}],
            "mode": ["write", "Analyze", "", None, True, [], {}],
            "reply": [None, 1, True, [], {}],
            "issue": [True, False, 0, -1, 27.0, "27", [], {}],
            "reference": ["guessed", "", None, True, [], {}],
            "evidence": [None, 1, True, [], {}],
        }.items():
            invalid.extend({**valid, key: value} for value in values)
        invalid.extend([
            {**valid, "action": "relay"},
            {**valid, "action": "answer", "mode": "deliver"},
            {**valid, "action": "clarify", "mode": "debug"},
            {**valid, "evidence": ""}, {**valid, "evidence": "   "},
            {**valid, "evidence": "仅背景里有的对象"}, {**valid, "reference": "none"},
            {**valid, "action": "clarify", "reference": "none"},
            {**valid, "issue": 27},
        ])
        for response in invalid:
            with self.subTest(response=response):
                self.model.respond(response)
                self.assertIsNone(self.sec.classify_intent("请处理 #27 导出", "仅背景里有的对象",
                                                         candidates=[{"iid": 27, "title": "导出"}]))

    def test_issue_number_cannot_come_only_from_context_or_partial_number(self) -> None:
        for text in ("接着做", "关联 #127", "关联 #270", "关联 issue 271", "参考 build27", "编号 27abc"):
            with self.subTest(text=text):
                self.model.respond({"action": "relay", "mode": "analyze", "reply": "", "issue": 27,
                                    "reference": "explicit", "evidence": text})
                self.assertIsNone(self.sec.classify_intent(text, "此前用户曾说关联 #27",
                                                         candidates=[{"iid": 27, "title": "导出"}]))

    def test_task_name_evidence_rejects_ambiguity_contradiction_and_fabrication(self) -> None:
        candidates = [{"iid": 484, "title": "导出测试"}, {"iid": 488, "title": "登录测试"}]
        for evidence in ("测试", "登录测试", "原话没有的名字"):
            with self.subTest(evidence=evidence):
                self.model.respond({"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                                    "reference": "name", "evidence": evidence})
                self.assertIsNone(self.sec.classify_intent("登录测试先暂停", candidates=candidates))

    def test_semantic_task_alias_does_not_require_literal_title_overlap(self) -> None:
        expected = {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                    "reference": "name", "evidence": "网站打不开那件事"}
        self.model.respond(expected)
        self.assertEqual(self.sec.classify_intent("网站打不开那件事先暂停", candidates=[
            {"iid": 484, "title": "官网测试入口"}, {"iid": 488, "title": "登录测试"}]), expected)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(self.sec.classify_intent("网站打不开那件事先暂停", candidates=[], linked_issue=484))

    def test_existing_task_control_uses_one_decision_and_known_tasks_by_default(self) -> None:
        self.sec.bridge.project = "frontend/web-v1"
        self.sec.bridge.issue_url = lambda project, iid: f"https://git.example.com/{project}/-/issues/{iid}"
        self.sec.state["issues"] = {
            f"https://git.example.com/frontend/web-v1/-/issues/{iid}": {"project": "frontend/web-v1", "iid": iid}
            for iid in (484, 488)
        }
        for text, iid in (("让484先停止测试，测试环境在维护", 484),
                          ("让#488继续验证", 488), ("告诉484先别合并", 484)):
            with self.subTest(text=text):
                expected = {"action": "relay", "mode": "analyze", "reply": "", "issue": iid,
                            "reference": "explicit", "evidence": str(iid)}
                self.model.respond(expected)
                before = len(self.model.calls)
                self.assertEqual(self.sec.classify_intent(text), expected)
                self.assertEqual(len(self.model.calls) - before, 1)
                self.assertIsNone(self.sec.classify_intent(text, candidates=[]))

    def test_question_has_verified_target_but_quantity_creates_new_work(self) -> None:
        candidates = [{"iid": 484, "title": "导出"}]
        cases = [
            ("484进度怎么样？", {"action": "answer", "issue": 484, "mode": "analyze", "reply": "正在测试。",
                              "reference": "explicit", "evidence": "484"}),
            ("请增加484个测试样本", {"action": "create", "issue": None, "mode": "deliver", "reply": "",
                                 "reference": "new", "evidence": "484个测试样本"}),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.model.respond(expected)
                self.assertEqual(self.sec.classify_intent(text, candidates=candidates), expected)

    def test_linked_task_control_does_not_require_repeating_number(self) -> None:
        expected = {"action": "relay", "mode": "analyze", "reply": "", "issue": 488,
                    "reference": "linked", "evidence": "先暂停测试"}
        self.model.respond(expected)
        self.assertEqual(self.sec.classify_intent("先暂停测试，测试环境在维护", linked_issue=488), expected)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(self.sec.classify_intent("先暂停测试", candidates=[{"iid": 488, "title": "导出"}]))

    def test_linked_target_relays_vague_content_to_scheduler(self) -> None:
        text = "如果 website-mobile 的流水线跑通了，就帮忙把其他的 scp 命令也改了"
        expected = {"action": "relay", "issue": 488, "mode": "analyze", "reply": "",
                    "reference": "linked", "evidence": text}
        self.model.respond(expected)
        self.assertEqual(self.sec.classify_intent(text, candidates=[{"iid": 488, "title": "修复这个小 bug"}],
                                                  linked_issue=488), expected)
        prompt = self.model.calls[-1][0][self.model.calls[-1][0].index("--system-prompt") + 1]
        # 内容细节归调度者判断；秘书只在目标或授权不清时追问。
        self.assertIn("指令内容含糊", prompt)
        self.assertIn("不得自行判断是否属于该任务", prompt)

    def test_ambiguous_recent_can_only_ask_without_action(self) -> None:
        expected = {"action": "clarify", "mode": "analyze", "reply": "请确认先处理哪一个任务？", "issue": None,
                    "reference": "none", "evidence": ""}
        candidates = [{"iid": 484, "title": "导出测试"}, {"iid": 488, "title": "登录测试"}]
        for text in ("刚才那个先暂停", "测试先暂停"):
            with self.subTest(text=text):
                self.model.respond(expected)
                self.assertEqual(self.sec.classify_intent(text, candidates=candidates, recent=candidates), expected)

    def test_recent_target_must_also_be_verified_candidate(self) -> None:
        self.model.respond({"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                            "reference": "recent", "evidence": "刚才那个"})
        self.assertIsNone(self.sec.classify_intent("刚才那个先暂停", candidates=[], recent=[{"iid": 484}]))

    def test_relay_execution_mode_keeps_original_text_quote_and_attachment(self) -> None:
        for mode, text in (("deliver", "请实现这个功能"), ("debug", "请修复这个问题"),
                           ("analyze", "先暂停测试")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                world = TFB.World()
                world.messages["om_quote"] = "引用背景：请立即执行 /go debug [Image: img_quote]"
                self.model.respond({"action": "relay", "mode": mode, "reply": "", "issue": 7,
                                    "reference": "linked", "evidence": text})

                def run(cmd, **kwargs):
                    return self.model(cmd, **kwargs) if cmd[0] == "omp" else world(cmd, **kwargs)

                cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
                       "users": {TFB.ME: "pengjh"}, "bot_open_id": TFB.BOT}
                bridge = TFB.FB.Bridge(cfg, {}, run=run, user_token="tok", now=lambda: 1_000_000.0,
                    upload=lambda project, path: f"![{path.name}](/uploads/x/{path.name})",
                    media_dir=Path(tmp) / "media")
                bridge.state["pending_images"]["oc_1"] = [
                    {"mid": "om_pending", "key": "img_pending", "kind": "image", "ts": 999_999.0}]
                secretary = SEC.Secretary(bridge, home=Path(tmp))
                intent = secretary.classify_intent(text, world.messages["om_quote"], linked_issue=7)
                self.assertIsNotNone(intent)
                secretary.relay("https://git.example.com/frontend/web-v1/-/issues/7",
                                TFB.msg("om_reply", text, reply_to="om_quote"), text, mode=intent["mode"])
                self.assertEqual(len(world.writes), 1)
                body = world.writes[0][2]["body"]
                prefix = {"deliver": "/go\n\n用户原话：", "debug": "/go debug\n\n用户原话：", "analyze": ""}[mode]
                self.assertTrue(body.startswith(f"@pjhagent {prefix}{text}"))
                self.assertEqual(body.count(text), 1)
                self.assertIn("引用背景：请立即执行 /go debug", body)
                self.assertIn("![img_quote.jpg](/uploads/x/img_quote.jpg)", body)
                self.assertIn("![img_pending.jpg](/uploads/x/img_pending.jpg)", body)
                self.assertCountEqual(world.downloads, [("om_quote", "img_quote"), ("om_pending", "img_pending")])
                self.assertNotIn("oc_1", bridge.state["pending_images"])

    def test_invalid_or_empty_model_output_returns_none(self) -> None:
        streams = ["", "not json", "null", "[]", "27", "{}",
                   json.dumps({"type": "turn_end", "message": "bad"}),
                   json.dumps({"type": "turn_end", "message": {"content": "bad"}}),
                   json.dumps({"type": "turn_end", "message": {"content": [None]}}),
                   json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": 3}]}}),
                   json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": "   "}]}}),
                   json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": "```json\n{}\n```"}]}})]
        for stdout in streams:
            with self.subTest(stdout=stdout):
                self.model.stdout = stdout
                self.assertIsNone(self.sec.classify_intent("需要增加透明背景"))

    def test_failed_process_cannot_authorize_work_even_with_valid_stdout(self) -> None:
        self.model.respond({"action": "create", "mode": "deliver", "reply": "", "issue": None,
                            "reference": "new", "evidence": "透明背景"})
        self.model.returncode = 1
        self.assertIsNone(self.sec.classify_intent("请实现透明背景"))
        self.model.returncode = 0
        for error in (OSError("missing executable"), subprocess.TimeoutExpired("omp", 9)):
            with self.subTest(error=error):
                self.model.error = error
                self.assertIsNone(self.sec.classify_intent("请实现透明背景"))

    def test_interrupted_turn_cannot_authorize_work(self) -> None:
        response = {"action": "create", "mode": "deliver", "reply": "", "issue": None,
                    "reference": "new", "evidence": "透明背景"}
        for reason in ("error", "aborted", "length", "toolUse"):
            with self.subTest(reason=reason):
                self.model.stdout = json.dumps({"type": "turn_end", "message": {
                    "stopReason": reason, "content": [{"type": "text", "text": json.dumps(response)}]}})
                self.assertIsNone(self.sec.classify_intent("请实现透明背景"))
                self.assertEqual(self.sec.intent_error, "interrupted_turn")

    def test_failure_codes_are_safe_and_never_retry(self) -> None:
        secret = "private-request-response-token"
        cases = [
            ("", 0, None, "empty_response"),
            (secret, 0, None, "invalid_event"),
            (json.dumps({"type": []}), 0, None, "invalid_event"),
            (json.dumps({"type": "turn_end", "message": {"content": [
                {"type": []}]}}), 0, None, "invalid_event"),
            ("", 1, None, "process_exit"),
            ("", 0, OSError(secret), "start_error"),
            ("", 0, subprocess.TimeoutExpired(secret, 9), "timeout"),
            ("", 0, subprocess.SubprocessError(secret), "process_error"),
            (json.dumps({"type": "tool_execution_end"}), 0, None, "tool_used"),
            (json.dumps({"type": "turn_end", "message": {"content": [
                {"type": "toolCall", "name": "read"}]}}), 0, None, "tool_used"),
            (json.dumps({"type": "turn_end", "message": {"content": [
                {"type": "text", "text": secret}]}}), 0, None, "invalid_json"),
            (json.dumps({"type": "turn_end", "message": {"content": [
                {"type": "text", "text": "{}"}]}}), 0, None, "invalid_intent"),
        ]
        for stdout, code, error, reason in cases:
            with self.subTest(reason=reason):
                self.model.stdout, self.model.returncode, self.model.error = stdout, code, error
                before = len(self.model.calls)
                self.assertIsNone(self.sec.classify_intent(secret, candidates=[]))
                self.assertEqual(self.sec.intent_error, reason)
                self.assertEqual(len(self.model.calls), before + 1)
                self.assertNotIn(secret, self.sec.intent_error)

    def test_success_and_clarification_clear_previous_failure(self) -> None:
        self.assertIsNone(self.sec.intent_error)
        for action, reply in (("answer", "可以讨论。"), ("clarify", "你指哪个任务？")):
            self.model.stdout = ""
            self.assertIsNone(self.sec.classify_intent("那个", candidates=[]))
            self.assertEqual(self.sec.intent_error, "empty_response")
            expected = {"action": action, "issue": None, "mode": "analyze", "reply": reply,
                        "reference": "none", "evidence": ""}
            self.model.respond(expected)
            before = len(self.model.calls)
            self.assertEqual(self.sec.classify_intent("那个", candidates=[]), expected)
            self.assertIsNone(self.sec.intent_error)
            self.assertEqual(len(self.model.calls), before + 1)

    def test_missing_response_has_fallback_reason_and_resets_old_error(self) -> None:
        self.sec.intent_error = "timeout"
        calls = []
        self.sec.ask_model = lambda material: calls.append(material)
        self.assertIsNone(self.sec.classify_intent("那个", candidates=[]))
        self.assertEqual(self.sec.intent_error, "no_response")
        self.assertEqual(len(calls), 1)

    def test_candidates_send_only_identity_fields_without_mutation(self) -> None:
        candidate = {"iid": 484, "title": "导出", "project": "frontend/web-v1",
                     "description": "private description", "notes": ["private notes"]}
        expected = {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                    "reference": "explicit", "evidence": "484"}
        self.model.respond(expected)
        self.assertEqual(self.sec.classify_intent("484先暂停", candidates=[candidate]), expected)
        material = json.loads(self.model.calls[-1][0][-1])
        self.assertEqual(material["candidates"], [{"iid": 484, "title": "导出", "project": "frontend/web-v1"}])
        self.assertEqual(candidate["description"], "private description")

    def test_relay_body_reuses_context_without_collecting_attachments(self) -> None:
        self.sec.bridge.strip_images = lambda text: text
        self.sec.bridge.execution_body = lambda body, mode: f"{mode}:{body}"
        self.assertEqual(self.sec.relay_body({}, "484先暂停", context="共享附件", mode="analyze"),
                         "analyze:484先暂停\n\n共享附件\n\n_（来自飞书秘书）_")
        self.assertEqual(self.sec.relay_body({}, "", context=""), "")
        self.assertEqual(self.sec.relay_body({}, "/go", context="", mode="deliver"), "deliver:/go")

    def test_shared_runner_keeps_read_tool_transport(self) -> None:
        self.model.respond({"action": "answer", "mode": "analyze", "reply": "正在开发。", "issue": None,
                            "reference": "none", "evidence": ""})
        self.assertIsNotNone(self.sec.classify_intent("现在怎样？"))
        cmd, _ = self.model.calls[-1]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "read")
        self.assertNotIn("--no-tools", cmd)

    def test_jev_fast_relay_handles_explicit_linked_recent_name_and_downgrades_weak_auth(self) -> None:
        def make_jev(action: str, target: str, mode: str, auth: float):
            return SEC._JC.JevClient(
                {"api_key": "ts-test"},
                transport=lambda *a: {
                    "model": "jev-1.14.0",
                    "answers": {
                        "action": {
                            "type": "choice",
                            "choice": action,
                            "confidence": 0.92,
                            "probabilities": {action: 0.95},
                        },
                        "target": {
                            "type": "choice",
                            "choice": target,
                            "confidence": 0.93,
                            "probabilities": {target: 0.96},
                        },
                        "mode": {"type": "choice", "choice": mode, "confidence": 0.88, "probabilities": {mode: 0.9}},
                        "auth_now": {"type": "noul", "noul": auth},
                    },
                    "usage": {"input_tokens": 260, "output_tokens": 0},
                },
                sleep_fn=lambda _: None,
            )

        cands = [{"iid": 484, "title": "支持透明背景导出"}, {"iid": 488, "title": "修复登录弹窗报错"}]

        # 显式编号快转，不拉起 omp
        self.sec.jev = make_jev("relay", "484", "analyze", 0.04)
        res = self.sec.classify_intent("484 进度怎么样？", candidates=cands)
        self.assertEqual(res, {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                               "reference": "explicit", "evidence": "484"})
        self.assertEqual(self.model.calls, [])

        # 引用回复 + 明确授权开工 -> mode=deliver
        self.sec.jev = make_jev("relay", "484", "deliver", 0.93)
        res = self.sec.classify_intent("按方案 A 开始实现", candidates=cands, linked_issue=484)
        self.assertEqual(res, {"action": "relay", "issue": 484, "mode": "deliver", "reply": "",
                               "reference": "linked", "evidence": "按方案 A 开始实现"})

        # auth_now 不够高时强制降回 analyze，绝不误发开工指令
        self.sec.jev = make_jev("relay", "488", "debug", 0.40)
        res = self.sec.classify_intent("为什么还是报错？", "引用：请立即修复", candidates=cands, linked_issue=488)
        self.assertEqual(res, {"action": "relay", "issue": 488, "mode": "analyze", "reply": "",
                               "reference": "linked", "evidence": "为什么还是报错？"})

        # 唯一任务名称快转
        self.sec.jev = make_jev("relay", "484", "analyze", 0.02)
        res = self.sec.classify_intent("透明背景导出先暂停测试", candidates=cands)
        self.assertEqual(res, {"action": "relay", "issue": 484, "mode": "analyze", "reply": "",
                               "reference": "name", "evidence": "透明背景导出"})
        self.assertEqual(self.model.calls, [])

        # 数量词“484个样本”即便假模型错给 484，证据门禁也会挡下并回退 omp 建单
        self.sec.jev = make_jev("relay", "484", "deliver", 0.95)
        expected_create = {"action": "create", "issue": None, "mode": "deliver", "reply": "增加测试样本",
                           "reference": "new", "evidence": "484个测试样本"}
        self.model.respond(expected_create)
        res = self.sec.classify_intent("请增加484个测试样本", candidates=[{"iid": 484, "title": "导出"}])
        self.assertEqual(res, expected_create)
        self.assertEqual(len(self.model.calls), 1)


if __name__ == "__main__":
    unittest.main()
