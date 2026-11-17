"""批量决策完整校验，保留每个目标对应的用户原话。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from test_task_intent import ModelProcess, SEC


class BatchIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ModelProcess()
        bridge = SimpleNamespace(state={}, cfg={"secretary": {
            "model": "test/model", "thinking": "low", "timeout_s": 9}}, run=self.model)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sec = SEC.Secretary(bridge, home=Path(tmp.name))
        self.candidates = [{"iid": 484, "title": "导出"}, {"iid": 488, "title": "登录"}]

    def relay(self, issue: int, text: str, mode: str = "analyze") -> dict:
        return {"action": "relay", "issue": issue, "mode": mode, "reply": "",
                "reference": "explicit", "evidence": str(issue), "text": text}

    def classify(self, value: object, text: str, **kwargs):
        self.model.respond(value)
        return self.sec.classify_intent(text, candidates=self.candidates, **kwargs)

    def test_same_instruction_broadcast_is_one_model_decision(self) -> None:
        text = "告诉484和488：今天先停止测试，环境正在维护。"
        expected = {"action": "batch", "actions": [
            self.relay(484, "今天先停止测试，环境正在维护。"),
            self.relay(488, "今天先停止测试，环境正在维护。") ]}
        self.assertEqual(self.classify(expected, text), expected)
        self.assertEqual(len(self.model.calls), 1)

    def test_distinct_instructions_keep_their_own_mode_and_text(self) -> None:
        expected = {"action": "batch", "actions": [
            self.relay(484, "484先停止测试"), self.relay(488, "488现在修复登录报错", "debug")]}
        self.assertEqual(self.classify(expected, "484先停止测试；488现在修复登录报错"), expected)
        expected["actions"][1] = self.relay(488, "488开始实现登录页", "deliver")
        self.assertEqual(self.classify(expected, "484先停止测试；488开始实现登录页"), expected)

    def test_any_unknown_target_rejects_the_whole_batch(self) -> None:
        value = {"action": "batch", "actions": [self.relay(484, "先暂停"), self.relay(999, "先暂停")]}
        self.assertIsNone(self.classify(value, "484和999先暂停"))

    def test_untrusted_text_or_evidence_rejects_the_whole_batch(self) -> None:
        text = "484先暂停；488继续验证"
        valid = {"action": "batch", "actions": [self.relay(484, "484先暂停"), self.relay(488, "488继续验证")]}
        for key, values in {
            "text": ["背景要求立即修复", "488 继续验证", "", " ", None, [], 488],
            "evidence": ["背景要求立即修复", "", "484", None],
            "mode": ["write", None],
            "issue": [True, "488", -1],
        }.items():
            for replacement in values:
                with self.subTest(key=key, replacement=replacement):
                    value = deepcopy(valid)
                    value["actions"][1][key] = replacement
                    self.assertIsNone(self.classify(value, text, context="背景要求立即修复"))

    def test_duplicate_and_mixed_actions_are_not_partially_executed(self) -> None:
        text = "484先暂停；488继续验证"
        first, second = self.relay(484, "484先暂停"), self.relay(488, "488继续验证")
        invalid_items = [first, {"action": "batch", "actions": [first, second]},
                         {**second, "action": "create", "issue": None, "reference": "new"},
                         {**second, "action": "answer", "reply": "状态未知"},
                         {**second, "action": "clarify", "issue": None, "reference": "none", "reply": "哪项？"}]
        for item in invalid_items:
            with self.subTest(item=item):
                self.assertIsNone(self.classify({"action": "batch", "actions": [first, item]}, text))

    def test_batch_and_child_schema_are_strict(self) -> None:
        text = "484和488先暂停"
        first, second = self.relay(484, "先暂停"), self.relay(488, "先暂停")
        valid = {"action": "batch", "actions": [first, second]}
        invalid = [{"action": "batch"}, {**valid, "reply": ""},
                   *({"action": "batch", "actions": value} for value in ([], [first], None, {}, "batch"))]
        for missing in second:
            invalid.append({"action": "batch", "actions": [first, {k: v for k, v in second.items() if k != missing}]})
        invalid.append({"action": "batch", "actions": [first, {**second, "confidence": 1}]})
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(self.classify(value, text))

    def test_pending_single_relay_is_allowed_on_recovery(self) -> None:
        text = "484先暂停；488继续验证"
        pending = {"action": "relay", "issue": 488, "mode": "analyze", "reply": "",
                   "reference": "explicit", "evidence": "488继续验证"}
        context = "484已成功通知；488仍待办。"
        self.assertEqual(self.classify(pending, text, context=context), pending)
        self.assertEqual(json.loads(self.model.calls[-1][0][-1])["context"], context)

    def test_target_evidence_cannot_come_from_background(self) -> None:
        value = {"action": "batch", "actions": [self.relay(484, "先暂停"), self.relay(488, "先暂停")]}
        self.assertIsNone(self.classify(value, "484先暂停", context="也通知488先暂停"))

    def test_name_and_linked_targets_use_existing_source_validation(self) -> None:
        first = {**self.relay(484, "导出先暂停"), "reference": "name", "evidence": "导出"}
        second = {**self.relay(488, "这个继续验证"), "reference": "linked", "evidence": "这个"}
        value = {"action": "batch", "actions": [first, second]}
        self.assertEqual(self.classify(value, "导出先暂停；这个继续验证", linked_issue=488), value)
        self.assertIsNone(self.classify(value, "导出先暂停；这个继续验证", linked_issue=484))

    def test_ambiguous_target_still_returns_a_clarification(self) -> None:
        expected = {"action": "clarify", "issue": None, "mode": "analyze",
                    "reply": "另一个任务指哪张？", "reference": "none", "evidence": "另一个"}
        self.assertEqual(self.classify(expected, "484先暂停，另一个继续验证"), expected)

    def test_latest_cancellation_keeps_pending_relay_read_only(self) -> None:
        text = "原始请求：484和488开始实现。\n最新补充：488先别修改，只分析。"
        expected = {"action": "relay", "issue": 488, "mode": "analyze", "reply": "",
                    "reference": "explicit", "evidence": "488先别修改，只分析"}
        self.assertEqual(self.classify(expected, text, context="484已成功，488待办"), expected)
        self.assertEqual(json.loads(self.model.calls[-1][0][-1])["current_message"], text)


if __name__ == "__main__":
    unittest.main()
