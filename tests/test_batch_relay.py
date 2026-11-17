"""批量转告通过真实消息入口验证；只替换模型和外部服务。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

SPEC = importlib.util.spec_from_file_location("batch_bridge_fixture", Path(__file__).with_name("test_feishu_bridge.py"))
assert SPEC and SPEC.loader
TFB = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TFB
SPEC.loader.exec_module(TFB)
FB, msg = TFB.FB, TFB.msg


class BatchRelayTest(unittest.TestCase):
    setUp = TFB.BridgeTest.setUp
    fake_upload = TFB.BridgeTest.fake_upload

    def prepare(self, directives=None):
        directives = directives or [(484, "测试环境已经可以使用了，继续工作"), (488, "测试环境已经可以使用了，继续工作")]
        for iid, _ in directives:
            self.w.issues[iid] = {"iid": iid, "title": f"任务{iid}", "state": "opened", "notes": []}
        self.response = {"action": "batch", "actions": [
            {"action": "relay", "issue": iid, "mode": "analyze", "reply": "",
             "reference": "explicit", "evidence": str(iid), "text": text} for iid, text in directives]}
        self.materials = []
        self.snapshots = []
        self.b.persist = lambda: self.snapshots.append(json.loads(json.dumps(self.state)))
        previous = self.b.run

        def run(cmd, **kwargs):
            if cmd[0] == "omp":
                self.materials.append(json.loads(cmd[-1]))
                if self.response is None:
                    return subprocess.CompletedProcess(cmd, 1, "", "unavailable")
                return TFB.ok({"type": "turn_end", "message": {"content": [
                    {"type": "text", "text": json.dumps(self.response, ensure_ascii=False)}]}})
            return previous(cmd, **kwargs)

        self.b.run = run

    def test_broadcast_delivers_both_once_and_one_receipt(self):
        self.prepare()
        text = "告诉 484 和 488 的调度，测试环境已经可以使用了，继续工作"
        self.b.handle(msg("om_batch", text))
        self.b.handle(msg("om_batch", text))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertTrue(all("测试环境已经可以使用了，继续工作" in self.w.issues[i]["notes"][0]["body"] for i in (484, 488)))
        self.assertTrue(all("/go" not in w[2]["body"] for w in self.w.writes))
        self.assertEqual(len(self.materials), 1)
        self.assertEqual(len(self.w.replies), 1)
        self.assertIn("#484", self.w.replies[0]["text"])
        self.assertIn("#488", self.w.replies[0]["text"])
        self.assertEqual(self.state["pending_intents"], {})

    def test_batch_relay_keeps_secretary_watermark(self):
        self.prepare()
        self.b.handle(msg("om_batch", "告诉 484 和 488 的调度，测试环境已经可以使用了，继续工作"))
        for iid in (484, 488):
            known = self.state["secretary"]["issues"][self.b.issue_url(self.b.project, iid)]
            self.assertEqual(known["last_note"], self.w.issues[iid]["notes"][-1]["id"])
            self.assertNotIn("description", known)
        self.b.secretary.poll()

    def test_opposite_instructions_only_reach_their_recipient(self):
        self.prepare([(484, "先暂停测试"), (488, "继续工作")])
        self.b.handle(msg("om_split", "告诉484先暂停测试，告诉488继续工作"))
        self.assertIn("先暂停测试", self.w.issues[484]["notes"][0]["body"])
        self.assertNotIn("继续工作", self.w.issues[484]["notes"][0]["body"])
        self.assertIn("继续工作", self.w.issues[488]["notes"][0]["body"])
        self.assertNotIn("先暂停测试", self.w.issues[488]["notes"][0]["body"])

    def test_invalid_second_target_prevents_every_write(self):
        self.prepare()
        self.response["actions"][1]["issue"] = 999
        self.response["actions"][1]["evidence"] = "999"
        self.b.handle(msg("om_invalid", "告诉484和999测试环境已经可以使用了，继续工作"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("处理失败", self.w.replies[-1]["text"])
        self.assertNotIn("补充任务编号", self.w.replies[-1]["text"])

    def test_model_failure_never_relays_to_quoted_other_task(self):
        self.prepare()
        self.response = None
        self.state["secretary"]["msgs"]["om_old"] = self.b.issue_url(self.b.project, 484)
        self.b.handle(msg("om_failed", "转告 #488 测试环境可以使用了，继续工作", reply_to="om_old"))
        self.assertEqual(self.w.writes, [])
        self.assertIn("处理失败", self.w.replies[-1]["text"])
        self.assertNotIn("补充任务编号", self.w.replies[-1]["text"])
        pending = next(iter(self.state["pending_intents"].values()))
        self.assertEqual(pending["status"], "failed")
        self.assertIn("#488", pending["messages"][0]["content"])

    def test_retry_after_model_recovery_keeps_original_instruction(self):
        self.prepare()
        response = self.response
        self.response = None
        self.b.handle(msg("om_failed", "告诉 484 和 488 的调度，测试环境已经可以使用了，继续工作"))
        self.response = response
        self.b.handle(msg("om_retry", "再试一次"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertEqual(self.state["pending_intents"], {})

    def test_partial_failure_survives_restart_and_skips_delivered_target(self):
        self.prepare()
        self.w.issues[488]["state"] = "closed"
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 0])
        self.assertIn("未转交", self.w.replies[-1]["text"])
        self.assertTrue(self.snapshots)
        # 重启只恢复已保存状态，不能依赖上次调用的内存标记。
        restored = json.loads(json.dumps(self.state))
        run = self.b.run
        self.b = FB.Bridge(self.b.cfg, restored, run=run, user_token="tok", now=lambda: 1_000_000.0,
                           upload=self.fake_upload, media_dir=Path(self.tmp.name) / "media")
        self.b._secretary = FB.secretary_module().Secretary(self.b, home=Path(self.tmp.name))
        self.w.issues[488]["state"] = "opened"
        self.b.handle(msg("om_retry", "继续转告未完成的任务"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertEqual(restored["pending_intents"], {})

    def test_receipt_failure_does_not_repeat_comments_or_lose_summary(self):
        self.prepare()
        self.w.reply_failures = 1
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertIn("#484", self.w.replies[-1]["text"])
        self.assertIn("#488", self.w.replies[-1]["text"])
        self.assertEqual(self.state["pending_intents"], {})

    def test_batch_keeps_group_and_sender_boundaries(self):
        self.prepare()
        text = "告诉484和488测试环境已经可以使用了，继续工作"
        self.b.handle(msg("om_no_at", text, chat="oc_group", chat_type="group"))
        self.b.handle(msg("om_stranger", text, sender="ou_other"))
        self.assertEqual(self.w.writes, [])
        self.assertEqual(self.materials, [])

    def test_single_remaining_target_keeps_original_directive(self):
        self.prepare()
        self.w.issues[488]["state"] = "closed"
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作"))
        self.w.issues[488]["state"] = "opened"
        self.response = {"action": "relay", "issue": 488, "mode": "analyze", "reply": "",
                         "reference": "explicit", "evidence": "488"}
        self.b.handle(msg("om_retry", "然后告诉488"))
        self.assertEqual(len(self.w.issues[484]["notes"]), 1)
        self.assertIn("测试环境已经可以使用了，继续工作", self.w.issues[488]["notes"][0]["body"])
        self.assertEqual(self.state["pending_intents"], {})

    def test_written_but_timed_out_comment_is_reconciled_without_resending(self):
        self.prepare()
        previous = self.b.run

        def timeout_after_write(cmd, **kwargs):
            result = previous(cmd, **kwargs)
            if cmd[0] == "glab" and "POST" in cmd and cmd[cmd.index("-X") + 2].endswith("/488/notes"):
                raise subprocess.TimeoutExpired(cmd, 1)
            return result

        self.b.run = timeout_after_write
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作"))
        self.assertIn("结果待核实", self.w.replies[-1]["text"])
        self.b.run = previous
        self.b.handle(msg("om_check", "核对未完成项"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertEqual(self.state["pending_intents"], {})

    def test_unknown_write_without_receipt_is_never_blindly_retried(self):
        self.prepare()
        previous = self.b.run

        def timeout_before_receipt(cmd, **kwargs):
            if cmd[0] == "glab" and "POST" in cmd and cmd[cmd.index("-X") + 2].endswith("/488/notes"):
                raise subprocess.TimeoutExpired(cmd, 1)
            return previous(cmd, **kwargs)

        self.b.run = timeout_before_receipt
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作"))
        self.b.run = previous
        self.b.handle(msg("om_check", "重试未完成项"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 0])
        self.assertIn("结果待核实", self.w.replies[-1]["text"])

    def test_batch_attachment_is_uploaded_once_and_reaches_both_targets(self):
        self.prepare()
        self.b.handle(msg("om_batch", "告诉484和488测试环境已经可以使用了，继续工作 [Image: img_env]"))
        self.assertEqual(self.w.downloads, [("om_batch", "img_env")])
        self.assertEqual(len(self.uploads), 1)
        self.assertTrue(all("/uploads/x/img_env.jpg" in self.w.issues[i]["notes"][0]["body"] for i in (484, 488)))

    def test_number_prefixed_multi_target_message_is_not_silently_bound_to_first(self):
        self.prepare()
        self.b.handle(msg("om_batch", "#484 和 #488 测试环境已经可以使用了，继续工作"))
        self.assertEqual([len(self.w.issues[i]["notes"]) for i in (484, 488)], [1, 1])
        self.assertEqual(self.state["threads"], {})


if __name__ == "__main__":
    unittest.main()
