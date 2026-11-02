#!/usr/bin/env python3
"""清掉仓库 `.hwf/tmp/` 里超过 10 天没动过的已收尾任务目录。

    python3 ~/.hwf/scripts/clean-tmp.py [<repo>] [--days N] [--dry-run]

exit 0 = 清理完成（或 dry-run 列完）；exit 1 = 有目录删除失败（明细写 stderr）。

为什么需要：任务记录与截图证据只写不删，2026-10-30 web-v1 已堆到 2.6 GB；用户指示超过 10 天的清掉。
删除条件（全部满足）：
- 是 `.hwf/tmp/` 下的目录（INDEX.md 等文件不碰）；
- 目录内最新文件的修改时间早于 N 天前（默认 10），只看目录自身时间会漏掉仍在写的子文件；
- INDEX 里该任务已收尾（done/delivered/closed/cancelled）且发起 issue 已被用户关闭，或 INDEX 里根本没有它。
INDEX 行保留不改：恢复时发现目录不存在，按 GitLab issue 记录接续。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import time

_SPEC = importlib.util.spec_from_file_location("discovery", Path(__file__).resolve().parent / "find-scheduler.py")
assert _SPEC is not None and _SPEC.loader is not None
DISCOVERY = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = DISCOVERY  # dataclass 需要能通过 __module__ 找回模块。
_SPEC.loader.exec_module(DISCOVERY)

DEFAULT_DAYS = 10


def latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                latest = max(latest, (Path(root) / name).lstat().st_mtime)
            except FileNotFoundError:
                continue
    return latest


def expired_dirs(repo: str, days: int, now: float) -> list[Path]:
    tmp = Path(repo) / DISCOVERY.INDEX_RELATIVE.parent
    if not tmp.is_dir():
        return []
    keep = {row.task for row in DISCOVERY.active_rows(repo)}
    cutoff = now - days * 86400
    return sorted(
        entry for entry in tmp.iterdir()
        if entry.is_dir() and not entry.is_symlink()
        and entry.name not in keep and latest_mtime(entry) < cutoff
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", nargs="?", default=os.getcwd())
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    failed = False
    for path in expired_dirs(args.repo, args.days, time.time()):
        if args.dry_run:
            print(f"would remove {path}")
            continue
        try:
            shutil.rmtree(path)
            print(f"removed {path}")
        except OSError as error:
            failed = True
            print(f"failed {path}: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
