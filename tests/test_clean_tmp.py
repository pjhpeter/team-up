"""聚焦验证 tmp 与陈旧 worktree 清理：只删过期且已收尾、干净已推远端且无独有证据的目录。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "clean_tmp", Path(__file__).resolve().parents[1] / "scripts/clean-tmp.py"
)
assert SPEC is not None and SPEC.loader is not None
CLEAN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLEAN
SPEC.loader.exec_module(CLEAN)

DAY = 86400


class ExpiredDirsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp(prefix="hwf-clean-")
        self.tmp = Path(self.repo) / ".hwf/tmp"
        self.tmp.mkdir(parents=True)
        self.now = time.time()
        (self.tmp / "INDEX.md").write_text(
            "| 任务标识 | 输入原文 | 调度者 | 状态 |\n"
            "| `closed-old` | x | `delivery-scheduler-a` | done |\n"
            "| `open-old` | x | `delivery-scheduler-b` | in-progress |\n"
            "| `origin-open` | x | `delivery-scheduler-c` | done（待用户关闭发起 issue） |\n"
            "| `closed-recent-file` | x | `delivery-scheduler-d` | closed |\n"
        )
        for name in ("closed-old", "open-old", "origin-open", "closed-recent-file", "orphan-old"):
            self.make(name, age_days=20)
        os.utime(self.tmp / "closed-recent-file" / "STATE.md", (self.now - DAY, self.now - DAY))

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def make(self, name: str, age_days: int) -> None:
        path = self.tmp / name
        path.mkdir()
        (path / "STATE.md").write_text("x")
        stamp = self.now - age_days * DAY
        os.utime(path / "STATE.md", (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def test_only_expired_closed_or_orphan_dirs(self) -> None:
        with patch.object(CLEAN.DISCOVERY, "open_origin_tasks", return_value=frozenset({"origin-open"})):
            names = [p.name for p in CLEAN.expired_dirs(self.repo, 10, self.now)]
        self.assertEqual(names, ["closed-old", "orphan-old"])

    def test_missing_tmp_is_empty(self) -> None:
        self.assertEqual(CLEAN.expired_dirs(tempfile.gettempdir() + "/hwf-none", 10, self.now), [])


class ExpiredWorktreesTest(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(os.path.realpath(tempfile.mkdtemp(prefix="hwf-clean-wt-")))
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        self.repo = base / "web-v1"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / ".hwf/tmp").mkdir(parents=True)
        (self.repo / ".hwf/tmp/INDEX.md").write_text(
            "| 任务标识 | 输入原文 | 调度者 | 状态 |\n"
            "| `frontend-web-v1-100` | old done | `delivery-scheduler-a` | done |\n"
            "| `frontend-web-v1-494` | active | `delivery-scheduler-b` | in-progress |\n",
            encoding="utf-8",
        )
        self.wt_root = base / "worktrees"
        self.wt_dir = self.wt_root / "web-v1"
        self.wt_dir.mkdir(parents=True)
        self.now = time.time()
        env = patch.dict(os.environ, {"HWF_WORKTREE_ROOT": str(self.wt_root)})
        env.start()
        self.addCleanup(env.stop)

    def _make_wt(self, name: str, age_days: int = 20) -> Path:
        p = self.wt_dir / name
        p.mkdir(parents=True)
        f = p / "README.md"
        f.write_text("ok", encoding="utf-8")
        stamp = self.now - age_days * DAY
        os.utime(f, (stamp, stamp))
        os.utime(p, (stamp, stamp))
        return p

    def test_reclaims_only_safe_expired_worktrees(self) -> None:
        clean_old = self._make_wt("frontend-web-v1-100-dev", age_days=20)
        active_old = self._make_wt("frontend-web-v1-494-dev", age_days=20)
        active_issue_wt = self._make_wt("issue-494-be", age_days=20)
        dirty_old = self._make_wt("frontend-web-v1-101-dirty", age_days=20)
        unpushed_old = self._make_wt("frontend-web-v1-102-unpushed", age_days=20)
        pane_busy = self._make_wt("frontend-web-v1-103-busy", age_days=20)
        unique_tmp = self._make_wt("frontend-web-v1-104-evidence", age_days=20)
        (unique_tmp / ".hwf/tmp/rec").mkdir(parents=True)
        ev = unique_tmp / ".hwf/tmp/rec/only-here.mp4"
        ev.write_bytes(b"video")
        old_stamp = self.now - 20 * DAY
        os.utime(ev, (old_stamp, old_stamp))
        os.utime(unique_tmp / ".hwf/tmp/rec", (old_stamp, old_stamp))
        os.utime(unique_tmp / ".hwf/tmp", (old_stamp, old_stamp))
        os.utime(unique_tmp, (old_stamp, old_stamp))

        standalone_clone = self._make_wt("frontend-web-v1-465-standalone", age_days=20)
        all_wts = [clean_old, active_old, active_issue_wt, dirty_old, unpushed_old, pane_busy, unique_tmp]

        def fake_list_wts(path: str):
            if path == str(standalone_clone):
                # 独立 git clone 的 entries[0][0] 是它自己，不是 self.repo 的 linked worktree
                return [(str(standalone_clone), "h", "main", False)]
            return [(str(self.repo), "h", "main", False)] + [(str(p), "h", "feat", False) for p in all_wts]

        def fake_herdr(*args: str, timeout: int = 30):
            assert args[:2] == ("pane", "list")
            return {"result": {"panes": [{"pane_id": "w1:p1", "cwd": str(pane_busy)}]}}

        def fake_git(_repo: str, *args: str):
            return subprocess.CompletedProcess([], 0, "", "")

        def fake_unsaved(path: str):
            return "有未提交改动" if path == str(dirty_old) else None

        def fake_saved(path: str, _release: str):
            return None if path == str(unpushed_old) else "origin/develop"

        with (
            patch.object(CLEAN.DISCOVERY, "open_origin_tasks", return_value=frozenset()),
            patch.object(CLEAN._WR, "list_worktrees", side_effect=fake_list_wts),
            patch.object(CLEAN._WR, "herdr", side_effect=fake_herdr),
            patch.object(CLEAN._WR, "git", side_effect=fake_git),
            patch.object(CLEAN._WR, "unsaved_reason", side_effect=fake_unsaved),
            patch.object(CLEAN._WR, "saved_ref", side_effect=fake_saved),
        ):
            picked = CLEAN.expired_worktrees(str(self.repo), 10, self.now)

        self.assertEqual(picked, [clean_old])

    def test_latest_mtime_skips_unpacked_bundles_and_caps_file_scan(self) -> None:
        task_dir = Path(self.repo) / ".hwf/tmp/heavy-task"
        task_dir.mkdir(parents=True)
        old_stamp = self.now - 15 * DAY
        fresh_stamp = self.now - DAY
        state_file = task_dir / "STATE.md"
        state_file.write_text("done", encoding="utf-8")
        os.utime(state_file, (old_stamp, old_stamp))

        # 解包目录里放了较新 mtime 的文件，latest_mtime 应跳过重型解包/缓存目录且受 max_files 上限保护。
        for skipped in ("unpacked-dmg/LookAI.app/Contents", "swift-cache/mod", "pkg.asar.unpacked/dist"):
            d = task_dir / skipped
            d.mkdir(parents=True)
            f = d / "fresh.bin"
            f.write_bytes(b"x")
            os.utime(f, (fresh_stamp, fresh_stamp))
            os.utime(d, (old_stamp, old_stamp))
        os.utime(task_dir / "unpacked-dmg/LookAI.app", (old_stamp, old_stamp))
        os.utime(task_dir / "unpacked-dmg", (old_stamp, old_stamp))
        os.utime(task_dir / "swift-cache", (old_stamp, old_stamp))
        os.utime(task_dir / "pkg.asar.unpacked", (old_stamp, old_stamp))
        os.utime(task_dir, (old_stamp, old_stamp))

        self.assertAlmostEqual(CLEAN.latest_mtime(task_dir, max_files=5), old_stamp, delta=1.0)

    def test_reclaims_cross_repo_sibling_worktrees_and_respects_sibling_index(self) -> None:
        be_repo = self.repo.parent / "member-be"
        (be_repo / ".git").mkdir(parents=True)
        (be_repo / ".hwf/tmp").mkdir(parents=True)
        (be_repo / ".hwf/tmp/INDEX.md").write_text(
            "| 任务标识 | 输入原文 | 调度者 | 状态 |\n"
            "| `backend-member-be-200` | old done | `delivery-scheduler-a` | done |\n"
            "| `backend-member-be-501` | active | `delivery-scheduler-b` | in-progress |\n",
            encoding="utf-8",
        )
        be_wt_dir = self.wt_root / "member-be"
        be_wt_dir.mkdir(parents=True)
        old_stamp = self.now - 20 * DAY

        def make_be_wt(name: str) -> Path:
            p = be_wt_dir / name
            p.mkdir(parents=True)
            f = p / "main.go"
            f.write_text("package main\n", encoding="utf-8")
            os.utime(f, (old_stamp, old_stamp))
            os.utime(p, (old_stamp, old_stamp))
            return p

        expired_be = make_be_wt("issue-200-api")
        active_be = make_be_wt("issue-501-api")
        active_from_primary = make_be_wt("issue-494-api")

        def fake_list_wts(path: str):
            if "member-be" in path:
                return [(str(be_repo), "h", "main", False),
                        (str(expired_be), "h", "feat/200", False),
                        (str(active_be), "h", "feat/501", False),
                        (str(active_from_primary), "h", "feat/494", False)]
            return [(str(self.repo), "h", "main", False)]

        with (
            patch.object(CLEAN.DISCOVERY, "open_origin_tasks", return_value=frozenset()),
            patch.object(CLEAN._WR, "list_worktrees", side_effect=fake_list_wts),
            patch.object(CLEAN._WR, "herdr", return_value={"result": {"panes": []}}),
            patch.object(CLEAN._WR, "git", return_value=subprocess.CompletedProcess([], 0, "", "")),
            patch.object(CLEAN._WR, "unsaved_reason", return_value=None),
            patch.object(CLEAN._WR, "saved_ref", return_value="origin/develop"),
        ):
            pairs = CLEAN.expired_worktrees_with_repos(str(self.repo), 10, self.now)

        self.assertEqual(pairs, [(expired_be, str(be_repo))])


if __name__ == "__main__":
    unittest.main()
