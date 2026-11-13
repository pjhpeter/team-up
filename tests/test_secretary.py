"""聚焦验证秘书（私聊窗口）：只转 Agent 发言、对转达点回复回到对应 issue、巡检有变化才发、提问失败回总览。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
        self.omp_out = ""
        self.omp_cmds: list[list[str]] = []

    def __call__(self, cmd, input=None, env=None, cwd=None, **kw):
        if cmd[0] == sys.executable and cmd[1].endswith("find-scheduler.py"):
            return ok(self.owner)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return ok({"result": {"agent": {"agent_status": self.agent_status}}})
        if cmd[0] == "omp":
            self.omp_cmds.append(cmd)
        if cmd[0] == "omp" and self.omp_out:
            return subprocess.CompletedProcess(cmd, 0, self.omp_out, "")
        return super().__call__(cmd, input, env, cwd, **kw)

    def agent_note(self, iid: int, body: str) -> None:
        self.gitlab_comment(iid, body)
        self.issues[iid]["notes"][-1]["author"]["username"] = "pjhagent"


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
        self.w.agent_note(7, "要你看一下")
        self.sec.poll()
        forwarded = list(self.state["secretary"]["msgs"])[0]
        self.b.handle(msg("om_u4", "看过了", root=forwarded, thread="omt_x"))
        self.assertEqual(self.w.writes[-1][1], "projects/frontend/web-v1/issues/7/notes")
        self.assertIn("看过了", self.w.writes[-1][2]["body"])
        self.assertTrue(self.w.replies[-1]["in_thread"])  # 本来就在话题里，回执也留在话题里

    def test_command_reply_keeps_command_bare(self) -> None:
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

    def test_closed_origin_drops_out_of_watch(self) -> None:
        (self.home / "state/gitlab-inbox.json").write_text(json.dumps({"origins": {}}))
        self.sec.poll()
        self.assertEqual(self.state["secretary"]["issues"], {})


if __name__ == "__main__":
    unittest.main()
