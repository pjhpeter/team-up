#!/usr/bin/env python3
"""清掉仓库 `.hwf/tmp/` 里超过 10 天没动过的已收尾任务目录，并回收同仓库过期已收尾且完全保存在远端的干净 worktree。

    python3 ~/.hwf/scripts/clean-tmp.py [<repo>] [--days N] [--no-worktrees] [--dry-run]

exit 0 = 清理完成（或 dry-run 列完）；exit 1 = 有目录删除失败（明细写 stderr）。

为什么需要：
1. 任务记录与截图证据只写不删，2026-10-30 web-v1 已堆到 2.6 GB；用户指示超过 10 天的清掉。
2. 早期未走新版 `wrapup.py` 的历史任务在 `~/.herdr/worktrees/` 留下数十个已合并但无人用的干净 worktree；
   满足「非在途、超过 N 天未改动、无任何窗格占用、无未提交/未跟踪改动、worktree 内 `.hwf/tmp` 无独有文件、HEAD 已保存在 `origin/*`」
   全部安全条件时一并自动回收，不满足任一条即原样保留。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Optional

_SPEC = importlib.util.spec_from_file_location("discovery", Path(__file__).resolve().parent / "find-scheduler.py")
assert _SPEC is not None and _SPEC.loader is not None
DISCOVERY = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = DISCOVERY  # dataclass 需要能通过 __module__ 找回模块。
_SPEC.loader.exec_module(DISCOVERY)

_WR_SPEC = importlib.util.spec_from_file_location("hwf_wrapup_clean", Path(__file__).resolve().parent / "wrapup.py")
assert _WR_SPEC is not None and _WR_SPEC.loader is not None
_WR = importlib.util.module_from_spec(_WR_SPEC)
sys.modules[_WR_SPEC.name] = _WR
_WR_SPEC.loader.exec_module(_WR)

DEFAULT_DAYS = 10
# 遍历 worktree 修改时间时跳过重型依赖/构建缓存目录，避免扫描数十万个 node_modules 文件阻塞几十秒。
SKIP_WALK_DIRS = frozenset({
    "node_modules", ".git", ".nuxt", ".output", ".pnpm-store", "dist", ".cache", ".turbo", ".vite", ".next",
})


def latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_WALK_DIRS]
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


def has_unique_tmp_files(wt_path: Path, main_repo: Path) -> bool:
    """检查 worktree 内被忽略的 `.hwf/tmp` 是否含有主仓库不存在或字节不一致的独有文件。"""
    wt_tmp = wt_path / ".hwf/tmp"
    if not wt_tmp.is_dir():
        return False
    main_tmp = main_repo / ".hwf/tmp"
    for root, _dirs, files in os.walk(wt_tmp):
        for fname in files:
            src = Path(root) / fname
            rel = src.relative_to(wt_tmp)
            dst = main_tmp / rel
            try:
                if not dst.is_file() or dst.stat().st_size != src.stat().st_size or dst.read_bytes() != src.read_bytes():
                    return True
            except OSError:
                return True
    return False


def _matches_active_task(dir_name: str, active_tasks: set[str], active_iids: set[str]) -> bool:
    for task in active_tasks:
        if dir_name == task or dir_name.startswith(f"{task}-"):
            return True
    m = re.match(r"^issue-(\d+)(?:$|-)", dir_name)
    if m and m.group(1) in active_iids:
        return True
    return False


def _pane_uses_path(panes: list[dict], target_real: str) -> bool:
    for p in panes:
        for key in ("cwd", "foreground_cwd"):
            raw = p.get(key)
            if not raw:
                continue
            real = os.path.realpath(str(raw))
            if real == target_real or real.startswith(target_real + os.sep):
                return True
    return False


def expired_worktrees(repo: str, days: int, now: float) -> list[Path]:
    """筛选同仓库下超过 N 天未改动、任务已收尾、无窗格占用、无未保存改动且 HEAD 已在远端分支的干净 worktree。"""
    root = _WR.worktree_root()
    if not root.is_dir():
        return []
    real_repo = os.path.realpath(repo)
    if not (Path(real_repo) / ".git").exists():
        return []

    try:
        panes = _WR.herdr("pane", "list")["result"]["panes"]
    except Exception:
        # 无法核验实时窗格占用时绝不盲删任何 worktree。
        return []

    active = DISCOVERY.active_rows(real_repo)
    active_tasks = {row.task for row in active}
    active_iids = {
        m.group(1)
        for task in active_tasks
        if (m := re.search(r"-(\d+)$", task))
    }
    cutoff = now - days * 86400
    fetched: Optional[bool] = None
    removable: list[Path] = []

    for repo_dir in sorted(root.iterdir()):
        if not repo_dir.is_dir() or repo_dir.is_symlink():
            continue
        for sub in sorted(repo_dir.iterdir()):
            if not sub.is_dir() or sub.is_symlink():
                continue
            if _matches_active_task(sub.name, active_tasks, active_iids):
                continue
            if latest_mtime(sub) >= cutoff:
                continue
            real_sub = os.path.realpath(sub)
            if real_sub == real_repo:
                continue
            try:
                entries = _WR.list_worktrees(real_sub)
            except Exception:
                continue
            if not entries or entries[0][0] != real_repo:
                continue
            # 检查该目录及其嵌套子 worktree，且永不把主工作树自身加入删除候选
            sub_entries = [(p, bare) for p, _head, _branch, bare in entries
                           if (p == real_sub or p.startswith(real_sub + os.sep)) and p != real_repo]
            if not sub_entries or any(bare for _, bare in sub_entries):
                continue
            if fetched is None:
                fetched = _WR.git(real_repo, "fetch", "--quiet", "origin").returncode == 0
            if not fetched:
                return []
            all_safe = True
            for wt_p, _ in sub_entries:
                if (
                    _pane_uses_path(panes, wt_p)
                    or has_unique_tmp_files(Path(wt_p), Path(real_repo))
                    or _WR.unsaved_reason(wt_p) is not None
                    or _WR.saved_ref(wt_p, "") is None
                ):
                    all_safe = False
                    break
            if all_safe:
                for wt_p, _ in sub_entries:
                    removable.append(Path(wt_p))

    unique = list(dict.fromkeys(removable))
    unique.sort(key=lambda p: str(p).count(os.sep), reverse=True)
    return unique


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", nargs="?", default=os.getcwd())
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--no-worktrees", action="store_true", help="只清理 .hwf/tmp/，不检查过期 worktree")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    now = time.time()
    failed = False
    for path in expired_dirs(args.repo, args.days, now):
        if args.dry_run:
            print(f"would remove {path}")
            continue
        try:
            shutil.rmtree(path)
            print(f"removed {path}")
        except OSError as error:
            failed = True
            print(f"failed {path}: {error}", file=sys.stderr)

    if not args.no_worktrees:
        real_repo = os.path.realpath(args.repo)
        wts = expired_worktrees(args.repo, args.days, now)
        removed_any = False
        for wt_path in wts:
            if args.dry_run:
                print(f"would remove worktree {wt_path}")
                continue
            res = _WR.git(real_repo, "worktree", "remove", "--force", str(wt_path))
            if res.returncode == 0:
                removed_any = True
                print(f"removed worktree {wt_path}")
            else:
                failed = True
                print(f"failed worktree {wt_path}: {res.stderr.strip()}", file=sys.stderr)
        if removed_any:
            _WR.git(real_repo, "worktree", "prune")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
