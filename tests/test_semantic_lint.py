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
        self.calls: list[tuple[str, list[str]]] = []

    def decide(self, *, state: Any = None, questions: Any = None, **_: Any) -> Any:
        self.last_state = state
        self.last_questions = questions
        qids = sorted((questions or {}).keys())
        self.calls.append((str((state or {}).get("git_diff") or ""), qids))
        if self.fail:
            raise SL.JC.JevUnavailableError("no api key")
        answers = {
            rid: {"type": "noul", "noul": self.overrides.get(rid, 0.02)}
            for rid in (questions or {})
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
        self.assertEqual(report["critical"][0]["files"], ["src/service.py"])
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

    def test_scope_routing_excludes_tests_docs_and_unchanged_context_from_prod_rules(self) -> None:
        mixed_diff = """diff --git a/scripts/seo/deploy-web-ssh.sh b/scripts/seo/deploy-web-ssh.sh
index 111..222 100644
--- a/scripts/seo/deploy-web-ssh.sh
+++ b/scripts/seo/deploy-web-ssh.sh
@@ -46,8 +52,11 @@ remote_ssh "bash '$remote/publish-ssh-release.sh'"
 remote=$(remote_ssh "mktemp -d")
-old_step
+node scripts/seo/web-bundle-archive.mjs --retain "$bundle" "$mode"
diff --git a/tests/scripts/helpers/issue485WebFixture.ts b/tests/scripts/helpers/issue485WebFixture.ts
new file mode 100644
--- /dev/null
+++ b/tests/scripts/helpers/issue485WebFixture.ts
@@ -1,3 +1,5 @@
-expect(oldAssert).toBe(true)
+execFileSync("ssh", ["-i", fakeKey, "host", "cmd"])
diff --git a/docs/ops.md b/docs/ops.md
--- a/docs/ops.md
+++ b/docs/ops.md
@@ -1,1 +1,2 @@
+Do not invoke ssh user@host directly.
"""
        jev = FakeLintJev()
        report = SL.run_semantic_lint(mixed_diff, jev=jev)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["files_checked"], 3)

        remote_calls = [diff for diff, qids in jev.calls if "remote_exec" in qids]
        self.assertEqual(len(remote_calls), 1)
        remote_diff = remote_calls[0]
        self.assertIn("scripts/seo/deploy-web-ssh.sh", remote_diff)
        self.assertIn("web-bundle-archive.mjs --retain", remote_diff)
        # 未改动上下文行、删除行、hunk 尾部旧函数名、测试替身和文档均被剥离
        self.assertNotIn("remote_ssh", remote_diff)
        self.assertNotIn("old_step", remote_diff)
        self.assertNotIn("issue485WebFixture.ts", remote_diff)
        self.assertNotIn("docs/ops.md", remote_diff)

        weakened_calls = [diff for diff, qids in jev.calls if "weakened_tests" in qids]
        self.assertEqual(len(weakened_calls), 1)
        self.assertIn("issue485WebFixture.ts", weakened_calls[0])
        self.assertIn("-expect(oldAssert).toBe(true)", weakened_calls[0])
        self.assertNotIn("deploy-web-ssh.sh", weakened_calls[0])

        secret_calls = [diff for diff, qids in jev.calls if "secret_exposed" in qids]
        self.assertEqual(len(secret_calls), 1)
        self.assertIn("docs/ops.md", secret_calls[0])
        self.assertNotIn("issue485WebFixture.ts", secret_calls[0])

    def test_critical_hit_pinpoints_offending_file_in_multi_file_diff(self) -> None:
        multi_diff = """diff --git a/.gitlab-ci.yml b/.gitlab-ci.yml
--- a/.gitlab-ci.yml
+++ b/.gitlab-ci.yml
@@ -1,1 +1,4 @@
+rollback_web:
+  when: manual
+  script:
+    - sh scripts/seo/deploy-web-ssh.sh "$SSH_LOGIN" develop
diff --git a/scripts/ops/hotfix.sh b/scripts/ops/hotfix.sh
new file mode 100755
--- /dev/null
+++ b/scripts/ops/hotfix.sh
@@ -0,0 +1,2 @@
+#!/usr/bin/env bash
+ssh deploy@prod.example.com "systemctl restart app"
"""

        class FileAwareJev:
            def decide(self, *, state: Any = None, questions: Any = None, **_: Any) -> Any:
                diff = str((state or {}).get("git_diff") or "")
                answers = {}
                for rid in questions or {}:
                    prob = 0.96 if (rid == "remote_exec" and "scripts/ops/hotfix.sh" in diff) else 0.08
                    answers[rid] = {"type": "noul", "noul": prob}
                return SL.JC.JevResult.from_payload({
                    "model": "jev-1.14.0",
                    "answers": answers,
                    "usage": {"input_tokens": 120, "output_tokens": 12},
                })

        report = SL.run_semantic_lint(multi_diff, jev=FileAwareJev())
        self.assertEqual(report["status"], "critical_failed")
        self.assertEqual(len(report["critical"]), 1)
        self.assertEqual(report["critical"][0]["rule_id"], "remote_exec")
        self.assertEqual(report["critical"][0]["files"], ["scripts/ops/hotfix.sh"])
        human = SL.format_human(report)
        self.assertIn("[scripts/ops/hotfix.sh]", human)

    def test_large_diff_chunks_by_file_boundary_and_takes_max_score(self) -> None:
        two_files = """diff --git a/src/a.py b/src/a.py
+++ b/src/a.py
@@ -1,1 +1,2 @@
+print("ok a")
diff --git a/src/b.py b/src/b.py
+++ b/src/b.py
@@ -1,1 +1,2 @@
+ssh_bypass = True
"""

        class ChunkJev:
            def __init__(self) -> None:
                self.seen: list[str] = []

            def decide(self, *, state: Any = None, questions: Any = None, **_: Any) -> Any:
                diff = str((state or {}).get("git_diff") or "")
                self.seen.append(diff)
                answers = {
                    rid: {"type": "noul", "noul": 0.91 if (rid == "remote_exec" and "src/b.py" in diff) else 0.05}
                    for rid in (questions or {})
                }
                return SL.JC.JevResult.from_payload({
                    "model": "jev-1.14.0",
                    "answers": answers,
                    "usage": {"input_tokens": 80, "output_tokens": 8},
                })

        jev = ChunkJev()
        with patch.object(SL, "CHUNK_DIFF_CHARS", 80):
            report = SL.run_semantic_lint(two_files, jev=jev)
        self.assertEqual(report["status"], "critical_failed")
        self.assertEqual(report["critical"][0]["files"], ["src/b.py"])
        self.assertGreaterEqual(len(jev.seen), 2)


if __name__ == "__main__":
    unittest.main()
