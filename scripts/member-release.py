#!/usr/bin/env python3
"""阶段中释放已交接成员，不执行整项收尾（不改 INDEX、不删 worktree、不发收尾回执）。

支持两种用法：
1. 精确释放单个成员：
   python3 ~/.hwf/scripts/member-release.py --issue <编号> --pane <窗格> --session <会话标识>
2. 进入「状态:待上线」或长时外部阻塞前，批量释放本单名下全部空闲就绪成员：
   python3 ~/.hwf/scripts/member-release.py --issue <编号> --all-idle [--dry-run] [--scheduler <登记名>]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys
from typing import Any, Optional, Sequence

_SPEC = importlib.util.spec_from_file_location("hwf_wrapup_for_release", Path(__file__).with_name("wrapup.py"))
assert _SPEC is not None and _SPEC.loader is not None
_WR = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _WR
_SPEC.loader.exec_module(_WR)


def _verified_member_for_issue(
    owner: str,
    iid: int,
    pane: str,
    registry: dict[str, Any],
    expected_session: Optional[str] = None,
) -> dict[str, Any]:
    info = _WR.herdr("agent", "get", pane)["result"]["agent"]
    entry = registry.get(pane, {})
    profile = entry.get("profile")
    if not (
        profile
        and entry.get("scheduler") == owner
        and info.get("pane_id") == pane
        and entry.get("tab_id")
        and entry["tab_id"] == info.get("tab_id")
        and re.fullmatch(rf"i{iid}-{re.escape(str(profile))}(?:-\d+)?", str(info.get("name") or ""))
    ):
        raise ValueError("当前窗格与本任务的成员登记不一致，保留")
    if expected_session is not None:
        if not expected_session or (info.get("agent_session") or {}).get("value") != expected_session:
            raise ValueError("成员会话已变化或无法确认，保留")
    if info.get("agent_status") not in ("idle", "done") or not info.get("interactive_ready"):
        raise ValueError("成员未空闲或尚未就绪，保留")
    return info


def release(iid: int, pane: str, session: str, scheduler: Optional[str] = None, dry_run: bool = False) -> int:
    owner = _WR.current_scheduler(scheduler)
    registry = _WR._MS.read_registry()
    info = _verified_member_for_issue(owner, iid, pane, registry, expected_session=session)
    result = _WR.close_members([info], dry_run)
    print("\n".join(result.closed + result.kept + result.problems))
    return 0 if result.closed and not result.problems else 1


def release_all_idle(iid: int, scheduler: Optional[str] = None, dry_run: bool = False) -> int:
    """核对当前调度者名下归属 i<iid>-* 且 idle/done + interactive_ready 的全部成员并安全释放。"""
    owner = _WR.current_scheduler(scheduler)
    registry = _WR._MS.read_registry()
    self_pane = os.environ.get("HERDR_PANE_ID") or ""
    all_agents = _WR.herdr("agent", "list")["result"]["agents"]
    live_panes = {str(p.get("pane_id") or "") for p in _WR.herdr("pane", "list")["result"]["panes"]}
    live_agents = {str(a.get("pane_id") or "") for a in all_agents}
    dead_registered = sorted(
        pane for pane, entry in registry.items()
        if isinstance(entry, dict)
        and entry.get("scheduler") == owner
        and pane not in (live_panes | live_agents)
    )
    if dead_registered and not dry_run:
        _WR.forget(dead_registered)

    targets: list[dict[str, Any]] = []
    skipped: list[str] = []
    for item in all_agents:
        pane = str(item.get("pane_id") or "")
        name = str(item.get("name") or "")
        if not pane or pane == self_pane or name == owner:
            continue
        entry = registry.get(pane) or {}
        profile = str(entry.get("profile") or "")
        if entry.get("scheduler") != owner or not profile:
            continue
        if not re.fullmatch(rf"i{iid}-{re.escape(profile)}(?:-\d+)?", name):
            continue
        if item.get("agent_status") not in ("idle", "done"):
            skipped.append(f"{name} / {pane}（状态 {item.get('agent_status')}，保留）")
            continue
        try:
            verified = _verified_member_for_issue(owner, iid, pane, registry, expected_session=None)
        except (RuntimeError, KeyError, ValueError) as exc:
            skipped.append(f"{name} / {pane}（{exc}）")
            continue
        targets.append(verified)

    if not targets:
        lines = ["没有空闲可释放的成员"] + skipped
        print("\n".join(lines))
        return 0

    result = _WR.close_members(targets, dry_run)
    lines = list(result.closed) + list(result.kept) + skipped + list(result.problems)
    print("\n".join(lines))
    return 0 if not result.problems else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--issue", type=int, required=True, help="本任务 issue 数字编号")
    parser.add_argument("--pane", help="单窗格释放时的成员 pane_id")
    parser.add_argument("--session", help="单窗格释放时已交接的会话标识")
    parser.add_argument("--all-idle", action="store_true", help="批量释放本调度者名下属于该 issue 的全部空闲就绪成员")
    parser.add_argument("--scheduler", help="调度者登记名；默认取当前 pane 上的调度者")
    parser.add_argument("--dry-run", action="store_true", help="只打印将关闭/保留哪些窗格，不实际关闭")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.all_idle:
        if args.pane or args.session:
            parser.error("--all-idle 不能与 --pane / --session 同时使用")
        return release_all_idle(args.issue, scheduler=args.scheduler, dry_run=args.dry_run)
    if not args.pane or not args.session:
        parser.error("单窗格释放必须同时提供 --pane 与 --session（或改用 --all-idle）")
    return release(args.issue, args.pane, args.session, scheduler=args.scheduler, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, KeyError, ValueError, OSError) as error:
        sys.exit(str(error))
