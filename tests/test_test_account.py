"""聚焦验证测试账号分配：先到先得、同一占用方沿用、全被占不抢、释放、已收尾任务自动释放。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "test_account", Path(__file__).resolve().parents[1] / "scripts/test-account.py"
)
assert SPEC is not None and SPEC.loader is not None
TA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TA
SPEC.loader.exec_module(TA)

INDEX_HEAD = "# 任务索引\n\n| 任务标识 | 输入原文 | 调度者 | 状态 |\n|---|---|---|---|\n"


class AccountTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        (self.repo / ".hwf/tmp").mkdir(parents=True)
        patcher = patch.object(TA, "CLAIMS_PATH", root / "state/test-accounts.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_index([("t1", "in-progress"), ("t2", "in-progress"), ("t3", "in-progress")])

    def write_index(self, rows) -> None:
        body = "".join(f"| `{t}` | x | `delivery-scheduler-a` | {s} |\n" for t, s in rows)
        (self.repo / ".hwf/tmp/INDEX.md").write_text(INDEX_HEAD + body, encoding="utf-8")

    def claim(self, task, issue=None, candidates=("a", "b")):
        return TA.claim(str(self.repo), task, issue, list(candidates))

    def test_first_come_first_served_and_reuse(self) -> None:
        self.assertEqual(self.claim("t1", "1"), "a")
        self.assertEqual(self.claim("t2"), "b")
        self.assertEqual(self.claim("t1", "1"), "a")  # 复测沿用

    def test_parallel_issues_of_one_task_get_different_accounts(self) -> None:
        self.assertEqual(self.claim("t1", "1"), "a")
        self.assertEqual(self.claim("t1", "2"), "b")

    def test_all_busy_exit_3_without_stealing(self) -> None:
        self.claim("t1")
        self.claim("t2")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = TA.main(["claim", "--repo", str(self.repo), "--task", "t3", "--candidates", "a", "b"])
        self.assertEqual(code, TA.EXIT_ALL_BUSY)
        self.assertIn("t1", err.getvalue())
        self.assertEqual(json.loads(TA.CLAIMS_PATH.read_text())["a"]["holder"], "t1")

    def test_release_issue_then_task(self) -> None:
        self.claim("t1", "1")
        self.claim("t1", "2")
        self.assertEqual(TA.release("t1", "1"), ["a"])
        self.assertEqual(self.claim("t2"), "a")
        self.assertEqual(TA.release("t1", None), ["b"])

    def test_finished_task_freed_unknown_task_kept(self) -> None:
        self.claim("t1")
        self.claim("ghost")  # INDEX 没有这一行：保留
        self.write_index([("t1", "delivered（已上线）"), ("t3", "in-progress")])
        self.assertEqual(self.claim("t3"), "a")
        self.assertIsNone(self.claim("t4"))

    def test_unreadable_table_fails_closed(self) -> None:
        TA.CLAIMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TA.CLAIMS_PATH.write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = TA.main(["claim", "--repo", str(self.repo), "--task", "t1", "--candidates", "a"])
        self.assertEqual(code, TA.EXIT_ERROR)
        self.assertEqual(TA.CLAIMS_PATH.read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
