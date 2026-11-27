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
MAX_MTIME_SCAN_FILES = 2000
# 遍历任务证据与 worktree 修改时间时跳过重型依赖、构建缓存及解包大目录，并限制逐文件 stat 上限。
SKIP_WALK_DIRS = frozenset({
    "node_modules", ".git", ".nuxt", ".output", ".pnpm-store", "dist", ".cache", ".turbo", ".vite", ".next",
    "swift-cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "out", "coverage",
})
SKIP_WALK_SUFFIXES = (".app", ".asar.unpacked", ".framework", ".dSYM")
SKIP_WALK_PREFIXES = ("asar-", "unpacked-", "hwf-inspect-")
# 已过期且无任何已跟踪改动的 worktree 若仅剩可再生构建/测试缓存，允许先剥离缓存再走干净校验回收。
KNOWN_CACHE_DIRS = frozenset({
    ".cache", ".vite", ".nuxt", ".output", ".next", ".turbo",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", ".nyc_output",
})
KNOWN_CACHE_FILENAMES = frozenset({".eslintcache", ".stylelintcache", ".DS_Store", "Thumbs.db"})
KNOWN_CACHE_SUFFIXES = (".tsbuildinfo",)


def _is_known_cache_relpath(rel_path: str) -> bool:
    clean = rel_path.strip().strip("/")
    if not clean:
        return False
    parts = Path(clean).parts
    if any(part in KNOWN_CACHE_DIRS for part in parts):
        return True
    name = parts[-1]
    return name in KNOWN_CACHE_FILENAMES or name.endswith(KNOWN_CACHE_SUFFIXES)


def _untracked_relpaths(wt_p: str) -> Optional[list[str]]:
    res = _WR.git(wt_p, "ls-files", "--others", "--exclude-standard", "-z")
    if res.returncode != 0:
        return None
    return [item for item in res.stdout.split("\0") if item]


def _classify_untracked_caches(wt_p: str) -> tuple[bool, list[str], list[str]]:
    """返回 (是否全为已知可再生缓存, 缓存相对路径列表, 非缓存未跟踪路径示例)。"""
    untracked = _untracked_relpaths(wt_p)
    if not untracked:
        return False, [], []
    non_cache = [p for p in untracked if not _is_known_cache_relpath(p)]
    if non_cache:
        return False, [], non_cache[:5]
    return True, untracked, []


def _strip_known_cache_paths(wt_p: str, relpaths: list[str]) -> bool:
    """仅删除已过期 worktree 内确认属于构建/测试缓存的未跟踪文件与空缓存目录，绝不越出 worktree 根目录。"""
    root = Path(wt_p).resolve()
    removed_parents: set[Path] = set()
    for rel in relpaths:
        target = root / rel
        try:
            parent_real = target.parent.resolve()
            if parent_real != root and root not in parent_real.parents:
                return False
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)
            # 顺着嵌套子目录向上收集直到最外层已知缓存目录（例如 .cache/vite -> .cache），自底向上清掉空目录。
            chain: list[Path] = []
            cur = target.parent
            while cur != root and root in cur.parents:
                chain.append(cur)
                if cur.name in KNOWN_CACHE_DIRS:
                    removed_parents.update(chain)
                cur = cur.parent
        except OSError:
            return False
    for cache_dir in sorted(removed_parents, key=lambda p: len(p.parts), reverse=True):
        try:
            if cache_dir.is_dir() and not any(cache_dir.iterdir()):
                cache_dir.rmdir()
        except OSError:
            pass
    return _WR.unsaved_reason(wt_p) is None


def _should_skip_walk_dir(name: str) -> bool:
    return (
        name in SKIP_WALK_DIRS
        or name.endswith(SKIP_WALK_SUFFIXES)
        or name.startswith(SKIP_WALK_PREFIXES)
    )


def latest_mtime(path: Path, max_files: int = MAX_MTIME_SCAN_FILES) -> float:
    latest = path.stat().st_mtime
    scanned = 0
    for root, dirs, files in os.walk(path):
        kept_dirs: list[str] = []
        for d in dirs:
            if _should_skip_walk_dir(d):
                continue
            kept_dirs.append(d)
            try:
                latest = max(latest, (Path(root) / d).lstat().st_mtime)
            except FileNotFoundError:
                continue
        dirs[:] = kept_dirs
        if scanned >= max_files:
            continue
        for name in files:
            try:
                latest = max(latest, (Path(root) / name).lstat().st_mtime)
                scanned += 1
            except FileNotFoundError:
                continue
            if scanned >= max_files:
                break
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


def _active_sets_for_repo(repo_path: str) -> tuple[set[str], set[str]]:
    try:
        active = DISCOVERY.active_rows(repo_path)
    except Exception:
        active = []
    tasks = {row.task for row in active}
    iids = {m.group(1) for task in tasks if (m := re.search(r"-(\d+)$", task))}
    return tasks, iids


def inspect_worktrees(
    repo: str,
    days: int,
    now: float,
    *,
    strip_caches: bool = False,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str, str]]]:
    """跨 `~/.herdr/worktrees/` 筛选可回收 worktree，同时返回被保留目录的具体卡点明细。

    仅当 worktree 已过保留期、非在途、无窗格占用、无独有 `.hwf/tmp`、HEAD 已推远端，且仅被已知构建/测试缓存卡住时，
    按 `strip_caches` 决定是否先剥离这些可再生缓存再复验干净状态。
    """
    root = _WR.worktree_root()
    if not root.is_dir():
        return [], []
    real_repo = os.path.realpath(repo)
    if not (Path(real_repo) / ".git").exists():
        return [], []

    try:
        panes = _WR.herdr("pane", "list")["result"]["panes"]
    except Exception as exc:
        # 无法核验实时窗格占用时绝不盲删任何 worktree。
        return [], [(root, "pane_list_failed", str(exc))]

    primary_tasks, primary_iids = _active_sets_for_repo(real_repo)
    active_cache: dict[str, tuple[set[str], set[str]]] = {real_repo: (primary_tasks, primary_iids)}
    fetched_repos: dict[str, bool] = {}
    cutoff = now - days * 86400
    removable: dict[Path, str] = {}
    skipped: list[tuple[Path, str, str]] = []

    for repo_dir in sorted(root.iterdir()):
        if not repo_dir.is_dir() or repo_dir.is_symlink():
            continue
        for sub in sorted(repo_dir.iterdir()):
            if not sub.is_dir() or sub.is_symlink():
                continue
            if _matches_active_task(sub.name, primary_tasks, primary_iids):
                skipped.append((sub, "active_task", "INDEX 仍登记为在途任务"))
                continue
            if latest_mtime(sub) >= cutoff:
                skipped.append((sub, "recent", f"未满 {days} 天"))
                continue
            real_sub = os.path.realpath(sub)
            if real_sub == real_repo:
                continue
            try:
                entries = _WR.list_worktrees(real_sub)
            except Exception as exc:
                skipped.append((sub, "not_git_worktree", str(exc)[:120]))
                continue
            if not entries:
                skipped.append((sub, "not_git_worktree", "非有效 git worktree"))
                continue
            wt_main_repo = os.path.realpath(entries[0][0])
            # 独立 git clone 的首项是它自身，不属于 linked worktree，绝不清理。
            if real_sub == wt_main_repo or not (Path(wt_main_repo) / ".git").exists():
                skipped.append((sub, "standalone_clone", "独立克隆仓库"))
                continue
            if wt_main_repo not in active_cache:
                wt_tasks, wt_iids = _active_sets_for_repo(wt_main_repo)
                active_cache[wt_main_repo] = (wt_tasks | primary_tasks, wt_iids | primary_iids)
            repo_tasks, repo_iids = active_cache[wt_main_repo]
            if _matches_active_task(sub.name, repo_tasks, repo_iids):
                skipped.append((sub, "active_task", "所属仓库 INDEX 仍登记为在途任务"))
                continue
            # 检查该目录及其嵌套子 worktree，且永不把主工作树自身加入删除候选。
            sub_entries = [
                (p, bare)
                for p, _head, _branch, bare in entries
                if (p == real_sub or p.startswith(real_sub + os.sep)) and p != wt_main_repo
            ]
            if not sub_entries:
                skipped.append((sub, "not_git_worktree", "未登记在主仓库 worktree 列表"))
                continue
            if any(bare for _, bare in sub_entries):
                skipped.append((sub, "bare_worktree", "含 bare 工作树"))
                continue
            if wt_main_repo not in fetched_repos:
                fetched_repos[wt_main_repo] = _WR.git(wt_main_repo, "fetch", "--quiet", "origin").returncode == 0
            if not fetched_repos[wt_main_repo]:
                skipped.append((sub, "fetch_failed", f"fetch origin 失败 ({wt_main_repo})"))
                continue
            blocker: Optional[tuple[str, str]] = None
            cache_targets: list[tuple[str, list[str]]] = []
            for wt_p, _ in sub_entries:
                if _pane_uses_path(panes, wt_p):
                    blocker = ("pane_busy", "仍有窗格位于该目录")
                    break
                if has_unique_tmp_files(Path(wt_p), Path(wt_main_repo)):
                    blocker = ("unique_hwf_tmp", "目录内 .hwf/tmp 含主仓库不存在的独有文件")
                    break
                if _WR.saved_ref(wt_p, "") is None:
                    blocker = ("head_not_in_remote", "HEAD 尚未保存在 origin/*")
                    break
                reason = _WR.unsaved_reason(wt_p)
                if reason is not None:
                    if reason == "有未跟踪文件":
                        only_cache, cache_paths, non_cache_samples = _classify_untracked_caches(wt_p)
                        if only_cache:
                            cache_targets.append((wt_p, cache_paths))
                            continue
                        detail = (
                            f"{reason} ({', '.join(non_cache_samples)})"
                            if non_cache_samples
                            else reason
                        )
                        blocker = ("unsaved", detail)
                    else:
                        blocker = ("unsaved", reason)
                    break
            if blocker is None and strip_caches and cache_targets:
                for wt_p, cache_paths in cache_targets:
                    if not _strip_known_cache_paths(wt_p, cache_paths):
                        blocker = ("cache_strip_failed", "剥离可再生缓存后仍不干净")
                        break
            if blocker is None:
                for wt_p, _ in sub_entries:
                    removable[Path(wt_p)] = wt_main_repo
            else:
                skipped.append((sub, blocker[0], blocker[1]))

    ordered = sorted(removable.items(), key=lambda item: str(item[0]).count(os.sep), reverse=True)
    return ordered, skipped


def expired_worktrees_with_repos(repo: str, days: int, now: float) -> list[tuple[Path, str]]:
    """跨 `~/.herdr/worktrees/` 全部仓库子目录筛选超过 N 天未改动且满足安全门禁的干净 worktree。"""
    removable, _ = inspect_worktrees(repo, days, now, strip_caches=False)
    return removable


def expired_worktrees(repo: str, days: int, now: float) -> list[Path]:
    return [wt_path for wt_path, _ in expired_worktrees_with_repos(repo, days, now)]


def format_skipped_summary(skipped: list[tuple[Path, str, str]], *, max_details: int = 20) -> list[str]:
    """格式化保留 worktree 的卡点汇总，便于定位未能自动清理的根因。"""
    if not skipped:
        return []
    counts: dict[str, int] = {}
    for _, category, _ in skipped:
        counts[category] = counts.get(category, 0) + 1
    parts = ", ".join(f"{k}={counts[k]}" for k in sorted(counts))
    lines = [f"skipped worktrees (total={len(skipped)}): {parts}"]
    non_recent = [item for item in skipped if item[1] != "recent"]
    for path, category, detail in non_recent[:max_details]:
        lines.append(f"kept worktree {path}: {category} ({detail})")
    if len(non_recent) > max_details:
        lines.append(f"... and {len(non_recent) - max_details} more expired kept worktree(s)")
    return lines


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
        wts, skipped = inspect_worktrees(args.repo, args.days, now, strip_caches=not args.dry_run)
        pruned_repos: set[str] = set()
        for wt_path, wt_repo in wts:
            if args.dry_run:
                print(f"would remove worktree {wt_path}")
                continue
            res = _WR.git(wt_repo, "worktree", "remove", "--force", str(wt_path))
            if res.returncode == 0:
                pruned_repos.add(wt_repo)
                print(f"removed worktree {wt_path}")
            else:
                failed = True
                print(f"failed worktree {wt_path}: {res.stderr.strip()}", file=sys.stderr)
        for wt_repo in sorted(pruned_repos):
            _WR.git(wt_repo, "worktree", "prune")
        for line in format_skipped_summary(skipped):
            print(line)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
