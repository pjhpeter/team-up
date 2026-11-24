"""验证 scripts/jev_gates.py 与关卡 4/5 动态规则库、30 天遥测统计、红线护栏及单测失败自动回滚。"""

from __future__ import annotations

from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


JG = _load("jev_gates", "jev_gates.py")
SL = _load("semantic_lint_dyn", "semantic-lint.py")
UM = _load("upload_media_dyn", "upload-media.py")
TR = _load("task_record_dyn", "task-record.py")


def ts_of(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, 0, 0).timestamp()


class DynamicFakeJev:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    def decide(self, *, questions: Any = None, **_: Any) -> Any:
        answers = {
            qid: {"type": "noul", "noul": self.scores.get(qid, 0.01)}
            for qid in (questions or {})
        }
        return SL.JC.JevResult.from_payload({
            "model": "jev-1.14.0",
            "answers": answers,
            "usage": {"input_tokens": 200, "output_tokens": 20},
        })


class JevGatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state_dir = self.home / "state"
        self.state_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "jev-gates.json", self.home / "jev-gates.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_gates_config_hot_reloads_on_mtime_change(self) -> None:
        self.assertAlmostEqual(JG.threshold_of("proactive_hit_min", 0.99, self.home), 0.20)
        cfg_path = self.home / "jev-gates.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        data["thresholds"]["proactive_hit_min"]["value"] = 0.26
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        st = cfg_path.stat()
        os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertAlmostEqual(JG.threshold_of("proactive_hit_min", 0.99, self.home), 0.26)

    def test_telemetry_summary_computes_30d_lifecycle_and_protects_redlines(self) -> None:
        cfg_path = self.home / "jev-gates.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        for r in data["gate4_semantic_lint"]:
            if r["rule_id"] in ("secret_exposed", "filler_comments"):
                r["created_at"] = "2026-10-01"
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        st = cfg_path.stat()
        os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))

        now_ts = ts_of(2026, 11, 24)
        since_ts = ts_of(2026, 11, 21)
        for i in range(32):
            JG.record_gate_event(
                "gate4_semantic_lint",
                "clean",
                rules_evaluated=["secret_exposed", "filler_comments", "weakened_tests"],
                near_misses={"weakened_tests": 0.72} if i < 3 else None,
                state_dir=self.state_dir,
                now_ts=now_ts - i * 3600,
            )

        summary = JG.summarize_gate_telemetry(self.state_dir, self.home, since_ts, now_ts)
        self.assertGreater(summary["window_by_gate"]["gate4_semantic_lint"]["total"], 0)
        cand_ids = {c["rule_id"] for c in summary["retirement_candidates"]}
        # 非保护规则满 30 天 + >=30 次评估 + 0 命中 -> 进入淘汰候选
        self.assertIn("filler_comments", cand_ids)
        # 红线保护规则即使 0 命中也绝不进入淘汰候选
        self.assertNotIn("secret_exposed", cand_ids)

        by_id = {r["rule_id"]: r for r in summary["rules_30d"]["gate4_semantic_lint"]}
        self.assertEqual(by_id["weakened_tests"]["near_misses_30d"], 3)

    def test_protected_redlines_block_retirement_and_threshold_loosening(self) -> None:
        now_ts = ts_of(2026, 11, 24)
        summary = JG.summarize_gate_telemetry(self.state_dir, self.home, now_ts - 86400, now_ts)
        res = JG.apply_gate_updates(
            [
                {"op": "retire_rule", "target": "gate4_semantic_lint", "rule_id": "feature_flag", "reason": "try retire"},
                {"op": "retire_rule", "target": "gate5_media_guard", "rule_id": "credential_exposed", "reason": "try retire"},
                {"op": "tune_threshold", "target": "thresholds", "rule_id": "inbox_natural_go_min", "new_threshold": 0.85, "reason": "loosen go"},
                {"op": "tune_threshold", "target": "gate4_semantic_lint", "rule_id": "secret_exposed", "new_threshold": 0.90, "reason": "loosen secret"},
            ],
            summary,
            hwf_home=self.home,
            state_dir=self.state_dir,
            now_ts=now_ts,
            verify_fn=lambda _: (True, "ok"),
        )
        self.assertEqual(res["applied"], [])
        self.assertEqual(len(res["rejected"]), 4)
        self.assertAlmostEqual(JG.threshold_of("inbox_natural_go_min", 0.0, self.home), 0.90)

    def test_add_rule_enforces_evidence_forces_advisory_and_takes_effect_immediately(self) -> None:
        now_ts = ts_of(2026, 11, 24)
        summary = JG.summarize_gate_telemetry(self.state_dir, self.home, now_ts - 86400, now_ts)

        res = JG.apply_gate_updates(
            [
                {
                    "op": "add_rule",
                    "target": "gate4_semantic_lint",
                    "rule_id": "too_few_samples",
                    "evidence_count": 1,
                    "title_zh": "样本不足",
                    "fix_hint_zh": "略",
                    "instructions_en": "Check x",
                    "true_en": "yes",
                    "false_en": "no",
                    "reason": "只出现 1 次",
                },
                {
                    "op": "add_rule",
                    "target": "gate4_semantic_lint",
                    "rule_id": "raw_markdown_link_in_reply",
                    "protected": True,
                    "severity": "critical",
                    "threshold": 0.82,
                    "evidence_count": 3,
                    "title_zh": "面向用户消息使用 Markdown 包装链接而非裸网址",
                    "fix_hint_zh": "改为直接输出 https://... 裸网址，便于终端直接点击。",
                    "instructions_en": "Does added user-facing message wrap URLs in `[text](https://...)`?",
                    "true_en": "Wraps https URL in markdown link syntax.",
                    "false_en": "Uses bare https URLs or internal non-user text.",
                    "reason": "本周期 #512、#515、#518 三次评审打回同一问题",
                },
            ],
            summary,
            hwf_home=self.home,
            state_dir=self.state_dir,
            now_ts=now_ts,
            verify_fn=lambda _: (True, "ok"),
        )
        self.assertEqual(len(res["applied"]), 1)
        self.assertEqual(len(res["rejected"]), 1)

        loaded = {r.rule_id: r for r in SL.load_rules(self.home)}
        added = loaded["raw_markdown_link_in_reply"]
        # 即使提议写了 critical/protected，引擎也强制降为非保护 advisory
        self.assertFalse(added.protected)
        self.assertEqual(added.severity, "advisory")

        diff = "diff --git a/src/msg.py b/src/msg.py\n+++ b/src/msg.py\n+msg = '[MR](https://example.com)'\n"
        report = SL.run_semantic_lint(
            diff,
            jev=DynamicFakeJev({"raw_markdown_link_in_reply": 0.91}),
            hwf_home=self.home,
            state_dir=self.state_dir,
        )
        self.assertEqual(report["status"], "advisory_flagged")
        self.assertEqual([x["rule_id"] for x in report["advisory"]], ["raw_markdown_link_in_reply"])

    def test_retire_eligible_rule_archives_and_blocks_cooldown_readd(self) -> None:
        cfg_path = self.home / "jev-gates.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        for r in data["gate5_media_guard"]:
            if r["rule_id"] == "blank_or_crash":
                r["created_at"] = "2026-10-10"
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        st = cfg_path.stat()
        os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000))

        now_ts = ts_of(2026, 11, 24)
        # 模拟近 30 天评估 12 次，其中 8 次都是空态豁免（豁免率 > 50%）
        for i in range(12):
            JG.record_gate_event(
                "gate5_media_guard",
                "exempted" if i < 8 else "clean",
                rules_evaluated=["credential_exposed", "blank_or_crash"],
                rules_triggered={"blank_or_crash": 0.92} if i < 8 else None,
                state_dir=self.state_dir,
                now_ts=now_ts - i * 3600,
            )

        summary = JG.summarize_gate_telemetry(self.state_dir, self.home, now_ts - 86400, now_ts)
        retire_res = JG.apply_gate_updates(
            [{"op": "retire_rule", "target": "gate5_media_guard", "rule_id": "blank_or_crash", "reason": "豁免率超 66%"}],
            summary,
            hwf_home=self.home,
            state_dir=self.state_dir,
            now_ts=now_ts,
            verify_fn=lambda _: (True, "ok"),
        )
        self.assertEqual(len(retire_res["applied"]), 1)
        remaining_ids = [r["rule_id"] for r in JG.active_rules("gate5_media_guard", self.home)]
        self.assertEqual(remaining_ids, ["credential_exposed"])

        # 14 天冷却期内禁止把刚淘汰的同 ID 加回来震荡
        readd_res = JG.apply_gate_updates(
            [{
                "op": "add_rule",
                "target": "gate5_media_guard",
                "rule_id": "blank_or_crash",
                "evidence_count": 3,
                "title_zh": "重加白屏",
                "error_zh": "白屏 {file_name}",
                "instructions_en": "blank?",
                "true_en": "yes",
                "false_en": "no",
                "reason": "立即重加",
            }],
            summary,
            hwf_home=self.home,
            state_dir=self.state_dir,
            now_ts=now_ts + 3600,
            verify_fn=lambda _: (True, "ok"),
        )
        self.assertEqual(readd_res["applied"], [])
        self.assertIn("retired within last 14 days", readd_res["rejected"][0]["reason"])

    def test_regression_test_failure_rolls_back_jev_gates_json_cleanly(self) -> None:
        cfg_path = self.home / "jev-gates.json"
        before_bytes = cfg_path.read_bytes()
        now_ts = ts_of(2026, 11, 24)
        summary = JG.summarize_gate_telemetry(self.state_dir, self.home, now_ts - 86400, now_ts)

        res = JG.apply_gate_updates(
            [{
                "op": "tune_threshold",
                "target": "thresholds",
                "rule_id": "proactive_hit_min",
                "new_threshold": 0.25,
                "reason": "测试单测失败回滚",
            }],
            summary,
            hwf_home=self.home,
            state_dir=self.state_dir,
            now_ts=now_ts,
            verify_fn=lambda _: (False, "AssertionError in test_semantic_lint"),
        )
        self.assertFalse(res["verified"])
        self.assertEqual(res["applied"], [])
        self.assertEqual(cfg_path.read_bytes(), before_bytes)
        self.assertAlmostEqual(JG.threshold_of("proactive_hit_min", 0.0, self.home), 0.20)

    def test_gate5_note_guard_reads_dynamic_rules_and_logs_telemetry(self) -> None:
        long_bad = "排查第一步看了路由配置，第二步查了日志，最后请确认是否合并分支。" * 14
        problems = TR.note_content_guard_problems(
            long_bad,
            kind="todo",
            jev=DynamicFakeJev({"secret_exposed": 0.02, "missing_top_conclusion": 0.93}),
            hwf_home=self.home,
            state_dir=self.state_dir,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("先说结论", problems[0])
        rows = [json.loads(ln) for ln in (self.state_dir / "jev-gates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["gate"], "gate5_note_guard")
        self.assertEqual(rows[-1]["outcome"], "blocked")
        self.assertIn("missing_top_conclusion", rows[-1]["trig"])


if __name__ == "__main__":
    unittest.main()
