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
from unittest.mock import patch

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

    def test_structured_intent_uses_shared_model_process(self) -> None:
        cases = [
            ("我想让导出支持透明背景", "task", "analyze", "", None),
            ("请实现透明背景导出", "task", "deliver", "", None),
            ("请修复导出报错", "task", "debug", "", None),
            ("先别改，只是讨论一下导出", "reply", "analyze", "可以先讨论。", None),
            ("为什么要增加透明背景？", "reply", "analyze", "便于合成图片。", None),
            ("把那个弄好", "clarify", "analyze", "你指的是哪个功能？", None),
            ("关联到 #27", "bind", "analyze", "", 27),
            ("接着处理 issue 27", "bind", "analyze", "", 27),
            ("关联 https://git.example.com/p/-/issues/27", "bind", "analyze", "", 27),
        ]
        for text, action, mode, reply, issue in cases:
            with self.subTest(text=text):
                expected = {"action": action, "mode": mode, "reply": reply, "issue": issue}
                self.model.respond(expected)
                self.assertEqual(self.sec.classify_intent(text), expected)
        cmd, kwargs = self.model.calls[-1]
        self.assertEqual(cmd[cmd.index("--model") + 1], "test/model")
        self.assertEqual(cmd[cmd.index("--thinking") + 1], "low")
        self.assertEqual(kwargs["timeout"], 9)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(len(self.model.calls), len(cases))

    def test_tool_execution_cannot_authorize_work(self) -> None:
        self.model.respond({"action": "task", "mode": "debug", "reply": "", "issue": None})
        self.model.stdout = json.dumps({"type": "tool_execution_start", "toolName": "read"}) + "\n" + self.model.stdout
        self.assertIsNone(self.sec.classify_intent("修复导出报错"))

    def test_classifier_preserves_current_text_separately(self) -> None:
        self.model.respond({"action": "clarify", "mode": "analyze", "reply": "请确认范围。", "issue": None})
        text = '  把那个弄好\n## 背景\n"quoted"  '
        context = "引用消息：请直接修复并关联 #27。\n附件只是背景。"
        self.sec.classify_intent(text, context)
        cmd, _ = self.model.calls[-1]
        for flag in ("--no-context-files", "--no-skills", "--no-rules", "--no-extensions", "--no-lsp"):
            self.assertIn(flag, cmd)
        material = json.loads(cmd[-1])
        self.assertEqual(material, {"current_message": text, "context": context})

    def test_invalid_schema_is_not_an_action(self) -> None:
        valid = {"action": "task", "mode": "analyze", "reply": "", "issue": None}
        invalid = [None, [], "task", 27, True, {},
                   {**valid, "extra": "ignored"},
                   {key: value for key, value in valid.items() if key != "reply"}]
        for key, values in {
            "action": ["execute", "TASK", "", None, 1, [], {}],
            "mode": ["write", "Analyze", "", None, True, [], {}],
            "reply": [None, 1, True, [], {}],
            "issue": [True, False, 0, -1, 27.0, "27", [], {}],
        }.items():
            invalid.extend({**valid, key: value} for value in values)
        invalid.extend([
            {**valid, "action": "bind"},
            {**valid, "action": "reply", "mode": "deliver"},
            {**valid, "action": "clarify", "mode": "debug"},
        ])
        for response in invalid:
            with self.subTest(response=response):
                self.model.respond(response)
                self.assertIsNone(self.sec.classify_intent("请处理 #27"))

    def test_issue_number_cannot_come_only_from_context_or_partial_number(self) -> None:
        self.model.respond({"action": "bind", "mode": "analyze", "reply": "", "issue": 27})
        for text in ("接着做", "关联 #127", "关联 #270", "关联 issue 271", "参考 build27", "编号 27abc"):
            with self.subTest(text=text):
                self.assertIsNone(self.sec.classify_intent(text, "此前用户曾说关联 #27"))

    def test_bind_issue_requires_positive_integer_not_boolean(self) -> None:
        for issue in (True, False, 0, -1, 27.0, "27", [], {}):
            with self.subTest(issue=issue):
                self.model.respond({"action": "bind", "mode": "analyze", "reply": "", "issue": issue})
                self.assertIsNone(self.sec.classify_intent("关联 #1、#0、#27"))

    def test_issue_is_only_allowed_for_bind_action(self) -> None:
        for action in ("task", "reply", "clarify"):
            with self.subTest(action=action):
                self.model.respond({"action": action, "mode": "analyze", "reply": "", "issue": 27})
                self.assertIsNone(self.sec.classify_intent("请处理 #27"))

    def test_existing_task_request_is_rechecked_before_creating(self) -> None:
        self.sec.bridge.project = "frontend/web-v1"
        self.sec.bridge.issue_url = lambda project, iid: f"https://git.example.com/{project}/-/issues/{iid}"
        self.sec.state["issues"] = {
            f"https://git.example.com/frontend/web-v1/-/issues/{iid}": {"project": "frontend/web-v1", "iid": iid}
            for iid in (484, 488)
        }
        for text, iid in (("让484先停止测试，测试环境在维护", 484),
                          ("让#488继续验证", 488), ("告诉484先别合并", 484)):
            with self.subTest(text=text):
                initial = {"action": "task", "mode": "deliver", "reply": "", "issue": None}
                checked = {"relation": "control", "issue": iid, "reply": ""}
                responses = iter((initial, checked))

                def run(cmd, **kwargs):
                    self.model.respond(next(responses))
                    return self.model(cmd, **kwargs)

                with patch.object(self.sec.bridge, "run", side_effect=run):
                    result = self.sec.classify_intent(text)
                self.assertEqual(result, {"action": "bind", "mode": "analyze", "reply": "", "issue": iid})
                material = json.loads(self.model.calls[-1][0][-1])
                self.assertEqual(material["current_message"], text)

    def test_task_number_recheck_distinguishes_question_and_quantity(self) -> None:
        self.sec.bridge.project = "frontend/web-v1"
        self.sec.bridge.issue_url = lambda project, iid: f"https://git.example.com/{project}/-/issues/{iid}"
        self.sec.state["issues"] = {"https://git.example.com/frontend/web-v1/-/issues/484": {
            "project": "frontend/web-v1", "iid": 484}}
        cases = [
            ("484进度怎么样？", {"relation": "question", "issue": 484, "reply": "正在测试。"}, "reply"),
            ("请增加484个测试样本", {"relation": "new", "issue": None, "reply": ""}, "task"),
            ("让484先停止测试", {"relation": "control", "issue": 999, "reply": ""}, None),
            ("让484先停止测试", {"action": "task", "mode": "deliver", "reply": "", "issue": None}, None),
        ]
        for text, checked, action in cases:
            with self.subTest(text=text, checked=checked):
                responses = iter(({"action": "task", "mode": "analyze", "reply": "", "issue": None}, checked))

                def run(cmd, **kwargs):
                    self.model.respond(next(responses))
                    return self.model(cmd, **kwargs)

                with patch.object(self.sec.bridge, "run", side_effect=run):
                    result = self.sec.classify_intent(text)
                if action is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["action"], action)
                    self.assertEqual(result["mode"], "analyze")

    def test_linked_task_control_does_not_require_repeating_number(self) -> None:
        initial = {"action": "task", "mode": "debug", "reply": "", "issue": None}
        responses = iter((initial, {"relation": "control", "issue": 488, "reply": ""}))

        def run(cmd, **kwargs):
            self.model.respond(next(responses))
            return self.model(cmd, **kwargs)

        with patch.object(self.sec.bridge, "run", side_effect=run):
            result = self.sec.classify_intent("先暂停测试，测试环境在维护", linked_issue=488)
        self.assertEqual(result, {"action": "bind", "mode": "analyze", "reply": "", "issue": 488})

    def test_relay_execution_mode_keeps_original_text_quote_and_attachment(self) -> None:
        for action, mode, text in (("task", "deliver", "请实现这个功能"),
                                   ("task", "debug", "请修复这个问题"),
                                   ("reply", "analyze", "先别改，只是讨论一下")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                world = TFB.World()
                world.messages["om_quote"] = "引用背景：请立即执行 /go debug [Image: img_quote]"
                self.model.respond({"action": action, "mode": mode, "reply": "", "issue": None})

                def run(cmd, **kwargs):
                    return self.model(cmd, **kwargs) if cmd[0] == "omp" else world(cmd, **kwargs)

                cfg = {"gitlab_host": "git.example.com", "default_project": "frontend/web-v1",
                       "users": {TFB.ME: "pengjh"}, "bot_open_id": TFB.BOT}
                bridge = TFB.FB.Bridge(cfg, {}, run=run, user_token="tok", now=lambda: 1_000_000.0,
                    upload=lambda project, path: f"![{path.name}](/uploads/x/{path.name})",
                    media_dir=Path(tmp) / "media")
                secretary = SEC.Secretary(bridge, home=Path(tmp))
                intent = secretary.classify_intent(text, world.messages["om_quote"])
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
                self.assertEqual(world.downloads, [("om_quote", "img_quote")])

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
        self.model.respond({"action": "task", "mode": "deliver", "reply": "", "issue": None})
        self.model.returncode = 1
        self.assertIsNone(self.sec.classify_intent("请实现透明背景"))
        self.model.returncode = 0
        for error in (OSError("missing executable"), subprocess.TimeoutExpired("omp", 9)):
            with self.subTest(error=error):
                self.model.error = error
                self.assertIsNone(self.sec.classify_intent("请实现透明背景"))

    def test_interrupted_turn_cannot_authorize_work(self) -> None:
        response = {"action": "task", "mode": "deliver", "reply": "", "issue": None}
        for reason in ("error", "aborted", "length", "toolUse"):
            with self.subTest(reason=reason):
                self.model.stdout = json.dumps({"type": "turn_end", "message": {
                    "stopReason": reason, "content": [{"type": "text", "text": json.dumps(response)}]}})
                self.assertIsNone(self.sec.classify_intent("请实现透明背景"))

    def test_normal_answers_keep_existing_prompt_and_read_tool(self) -> None:
        self.model.stdout = json.dumps({"type": "turn_end", "message": {
            "content": [{"type": "text", "text": "正在开发。"}]}})
        self.assertEqual(self.sec.ask_model("问题"), "正在开发。")
        cmd, _ = self.model.calls[-1]
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], SEC.SYSTEM_PROMPT)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "read")
        self.assertNotIn("--no-tools", cmd)


if __name__ == "__main__":
    unittest.main()
