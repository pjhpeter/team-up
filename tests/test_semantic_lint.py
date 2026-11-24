"""验证 scripts/semantic-lint.py：12 条铁律分级门禁、锁文件过滤、红线退出码 2 与离线优雅跳过。"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("semantic_lint", ROOT / "scripts/semantic-lint.py")
assert SPEC is not None and SPEC.loader is not None
SL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SL
SPEC.loader.exec_module(SL)


SAMPLE_DIFF = """diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
index 111..222 100644
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1,3 +1,50 @@
+lockfileVersion: '9.0'
diff --git a/src/service.py b/src/service.py
index 333..444 100644
--- a/src/service.py
+++ b/src/service.py
@@ -10,2 +10,5 @@
+if os.getenv("ENABLE_NEW_CHECKOUT_GRAY") == "1":
+    return run_v2()
"""


class FakeLintJev:
    def __init__(self, overrides: dict[str, float] | None = None, fail: bool = False) -> None:
        self.overrides = overrides or {}
        self.fail = fail
        self.last_state: Any = None
        self.last_questions: Any = None

    def decide(self, *, state: Any = None, questions: Any = None, **_: Any) -> Any:
        self.last_state = state
        self.last_questions = questions
        if self.fail:
            raise SL.JC.JevUnavailableError("no api key")
        answers = {
            rule.rule_id: {"type": "noul", "noul": self.overrides.get(rule.rule_id, 0.02)}
            for rule in SL.RULES
        }
        return SL.JC.JevResult.from_payload({
            "model": "jev-1.14.0",
            "answers": answers,
            "usage": {"input_tokens": 320, "output_tokens": 24},
        })


class SemanticLintTests(unittest.TestCase):
    def test_empty_or_lockfile_only_diff_returns_clean_without_calling_jev(self) -> None:
        jev = FakeLintJev(fail=True)
        lock_only = "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+++ b/pnpm-lock.yaml\n+foo: bar\n"
        self.assertEqual(SL.run_semantic_lint("", jev=jev)["status"], "clean")
        res = SL.run_semantic_lint(lock_only, jev=jev)
        self.assertEqual(res["status"], "clean")
        self.assertEqual(res["files_checked"], 0)
        self.assertIsNone(jev.last_state)

    def test_critical_hit_sets_critical_failed_and_main_exits_two(self) -> None:
        jev = FakeLintJev({"feature_flag": 0.94, "non_english_runtime_log": 0.85})
        report = SL.run_semantic_lint(SAMPLE_DIFF, jev=jev)
        self.assertEqual(report["status"], "critical_failed")
        self.assertEqual(report["files_checked"], 1)
        self.assertIn("[omitted lockfile diff: pnpm-lock.yaml]", jev.last_state["git_diff"])
        self.assertEqual([x["rule_id"] for x in report["critical"]], ["feature_flag"])
        self.assertEqual([x["rule_id"] for x in report["advisory"]], ["non_english_runtime_log"])

        with tempfile.NamedTemporaryFile("w", suffix=".diff", encoding="utf-8", delete=False) as fh:
            fh.write(SAMPLE_DIFF)
            diff_path = fh.name
        try:
            buf = io.StringIO()
            with patch.object(SL.JC, "default_client", return_value=jev), patch("sys.stdout", buf):
                rc = SL.main(["--diff-file", diff_path, "--json"])
            self.assertEqual(rc, SL.EXIT_CRITICAL)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["status"], "critical_failed")
        finally:
            Path(diff_path).unlink(missing_ok=True)

    def test_advisory_only_exits_zero_with_structured_findings(self) -> None:
        jev = FakeLintJev({"non_english_runtime_log": 0.88})
        with tempfile.NamedTemporaryFile("w", suffix=".diff", encoding="utf-8", delete=False) as fh:
            fh.write(SAMPLE_DIFF)
            diff_path = fh.name
        try:
            buf = io.StringIO()
            with patch.object(SL.JC, "default_client", return_value=jev), patch("sys.stdout", buf):
                rc = SL.main(["--diff-file", diff_path])
            self.assertEqual(rc, SL.EXIT_OK)
            self.assertIn("advisory_flagged", buf.getvalue())
            self.assertIn("non_english_runtime_log", buf.getvalue())
        finally:
            Path(diff_path).unlink(missing_ok=True)

    def test_jev_unavailable_skips_gracefully_with_exit_zero(self) -> None:
        jev = FakeLintJev(fail=True)
        report = SL.run_semantic_lint(SAMPLE_DIFF, jev=jev)
        self.assertEqual(report["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
