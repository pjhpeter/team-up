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

    def test_backtick_or_quoted_candidates_share_same_lock_and_migrate_legacy(self) -> None:
        # 2026-11-19 #484/#494：Markdown 反引号带入候选参数导致 "`bootingai-180c870`" 绕过互斥锁。
        self.assertEqual(self.claim("t1", "1", candidates=("`bootingai-180c870`",)), "bootingai-180c870")
        self.assertIsNone(self.claim("t2", "2", candidates=("bootingai-180c870", "'bootingai-180c870'")))
        # 历史文件中同时存在裸键和反引号键时，读取时合并并保留更早的在途占用方。
        TA.CLAIMS_PATH.write_text(json.dumps({
            "bootingai-180c870": {"holder": "t1#1", "task": "t1", "issue": "1", "repo": str(self.repo), "at": 100},
            "`bootingai-180c870`": {"holder": "t2#2", "task": "t2", "issue": "2", "repo": str(self.repo), "at": 200},
        }), encoding="utf-8")
        self.assertEqual(self.claim("t1", "1", candidates=("`bootingai-180c870`",)), "bootingai-180c870")
        saved = json.loads(TA.CLAIMS_PATH.read_text())
        self.assertEqual(list(saved.keys()), ["bootingai-180c870"])
        self.assertEqual(saved["bootingai-180c870"]["holder"], "t1#1")

    def test_release_single_account_keeps_active_account_and_rejects_foreign_holder(self) -> None:
        # 同一任务领了两个账号后，可只归还闲置的 b，保留正在用的 a；误传他人持有的账号时报错不误放。
        self.assertEqual(self.claim("t1", "1", candidates=("a",)), "a")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.claim("t1", "1", candidates=("b",)), "b")
        self.assertIn("同时还持有 a", err.getvalue())
        self.assertEqual(self.claim("t2", "2", candidates=("c",)), "c")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = TA.main(["release", "--task", "t1", "--issue", "1", "--account", "`b`"])
        self.assertEqual(code, TA.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue()), {"released": ["b"]})
        saved = json.loads(TA.CLAIMS_PATH.read_text())
        self.assertEqual(saved["a"]["holder"], "t1#1")
        self.assertNotIn("b", saved)

        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            bad = TA.main(["release", "--task", "t1", "--issue", "1", "--account", "c"])
        self.assertEqual(bad, TA.EXIT_ERROR)
        self.assertIn("t2#2", err2.getvalue())
        self.assertEqual(json.loads(TA.CLAIMS_PATH.read_text())["c"]["holder"], "t2#2")

    def test_list_cleans_finished_and_uses_last_index_row_on_handover(self) -> None:
        self.assertEqual(self.claim("t1", "1", candidates=("a",)), "a")
        self.assertEqual(self.claim("t2", "2", candidates=("b",)), "b")
        # t1 旧行 cancelled、新行接续 in-progress：以最后一行负责人状态为准，不能当已收尾误释放；t2 已 done 则在 list 时自动释放。
        self.write_index([("t1", "cancelled"), ("t1", "in-progress"), ("t2", "done")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(TA.main(["list"]), TA.EXIT_OK)
        listed = json.loads(out.getvalue())
        self.assertEqual(list(listed.keys()), ["a"])

    def test_issue_url_and_hash_normalize_to_same_holder(self) -> None:
        # 调度者有时传 --issue 494、有时传完整 issue URL 或 #494，必须归一为同一 holder。
        self.assertEqual(
            self.claim("t1", "https://git.bootingai.net/frontend/web-v1/-/issues/494", candidates=("a", "b")),
            "a",
        )
        self.assertEqual(self.claim("t1", "#494", candidates=("a", "b")), "a")
        self.assertEqual(TA.release("t1", "494", ["a"]), ["a"])


if __name__ == "__main__":
    unittest.main()
