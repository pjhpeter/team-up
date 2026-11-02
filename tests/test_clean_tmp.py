"""聚焦验证 tmp 清理：只删过期且已收尾/无主的任务目录，在途与发起 issue 未关闭的保留。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
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
        import shutil
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


if __name__ == "__main__":
    unittest.main()
