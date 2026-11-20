#!/usr/bin/env python3
"""跨调度者分配自动化测试登录账号：同一账号同一时间只给一个任务（issue）用。

问题：多个调度者同时交付时，各自在自己的 STATE 里记账号占用，彼此看不见；两个测试用同一账号登录，
一个会把另一个挤下线，两边都白跑。办法：所有调度者都到本机同一份占用表（state/test-accounts.json）
里领账号，领取在文件锁里进行，先到先得。

用法：
    test-account.py claim --repo <主仓库> --task <任务标识> [--issue <编号>] --candidates <账号> [<账号>…]
        从候选里领一个没人占的账号，stdout 只输出账号。同一占用方再领时原样交回已领的账号（复测沿用）。
        候选全被占时退出码 3，stderr 列出占用方，不抢占。
    test-account.py release --task <任务标识> [--issue <编号>] [--account <账号> [<账号>…]]
        传 --account 时只释放指定闲置账号（保留本任务仍在用的账号；误指他人账号时报错退出 1）；
        不传 --account 时释放该 issue（或整个任务）的全部账号。
    test-account.py list
        清理已收尾任务的残留占用后，打印当前占用表（JSON）。

过期清理：每次 claim / list 时，占用方任务在其仓库 `.hwf/tmp/INDEX.md` 最后一行已收尾（done/delivered/closed/cancelled）
的占用自动释放，防止忘记 release 的任务一直占着账号。INDEX 里找不到该任务时保留占用，宁可少一个可用账号，
也不把在用的账号分给别人。

占用表只记账号标识（手机号/邮箱）与占用方，不记密码。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator, Sequence

_FS_SPEC = importlib.util.spec_from_file_location("find_scheduler", Path(__file__).with_name("find-scheduler.py"))
assert _FS_SPEC is not None and _FS_SPEC.loader is not None
_FS = importlib.util.module_from_spec(_FS_SPEC)
sys.modules[_FS_SPEC.name] = _FS  # find-scheduler 里的 dataclass 需要能从 sys.modules 找到所属模块
_FS_SPEC.loader.exec_module(_FS)

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
CLAIMS_PATH = HWF_HOME / "state" / "test-accounts.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALL_BUSY = 3


_ISSUE_URL_OR_HASH = re.compile(r"(?:^#?|/(?:issues|work_items)/)(\d+)/?(?:[#?].*)?$")


def normalize_issue(raw: str | None) -> str | None:
    """将 '#494' 或完整 issue/work_items URL 归一为纯编号 '494'，避免同单因写法不同拆成两份占用。"""
    if raw is None:
        return None
    text = raw.strip().strip("`'\"")
    if not text:
        return None
    m = _ISSUE_URL_OR_HASH.search(text)
    return m.group(1) if m else text


def holder_of(task: str, issue: str | None) -> str:
    norm_issue = normalize_issue(issue)
    return f"{task}#{norm_issue}" if norm_issue else task


def normalize_account(raw: str) -> str:
    """去掉从 Markdown 表格或命令行复制时带入的反引号、引号和首尾空白，防止同名账号绕过互斥锁。"""
    acc = raw.strip().strip("`'\"“”‘’").strip()
    if not acc:
        raise RuntimeError(f"无效的测试账号标识：{raw!r}")
    return acc


def canonicalize_claims(raw_claims: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """迁移占用表中历史遗留的带引号键与 URL 形式的 issue；同一账号多条记录时优先保留未收尾且最早的记录。"""
    cleaned: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in raw_claims.items():
        acc = normalize_account(raw_key)
        entry = dict(raw_entry)
        norm_issue = normalize_issue(entry.get("issue"))
        if "task" in entry:
            entry["issue"] = norm_issue
            entry["holder"] = holder_of(entry["task"], norm_issue)
        if acc not in cleaned:
            cleaned[acc] = entry
            continue
        prev = cleaned[acc]
        prev_closed = bool(prev.get("repo") and task_closed(prev["repo"], prev["task"]))
        curr_closed = bool(entry.get("repo") and task_closed(entry["repo"], entry["task"]))
        if prev_closed and not curr_closed:
            cleaned[acc] = entry
        elif prev_closed == curr_closed and int(entry.get("at", 0)) < int(prev.get("at", 0)):
            cleaned[acc] = entry
    return cleaned


@contextmanager
def claims_locked() -> Iterator[dict[str, dict[str, Any]]]:
    """独占读写占用表（账号 → 占用信息），退出时原子写回。"""
    CLAIMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CLAIMS_PATH.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            loaded = json.loads(CLAIMS_PATH.read_text()) if CLAIMS_PATH.exists() else {}
        except ValueError:
            # 读不懂就停下，不当成空表：当成空表会把在用账号重新分出去。
            raise RuntimeError(f"占用表无法解析，请人工检查：{CLAIMS_PATH}")
        claims = canonicalize_claims(loaded)
        yield claims
        tmp = CLAIMS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(claims, ensure_ascii=False, indent=2))
        tmp.replace(CLAIMS_PATH)


def task_closed(repo: str, task: str) -> bool:
    matched = [row for row in _FS.read_index(repo) if row.task == task]
    return bool(matched) and not matched[-1].open


def drop_finished(claims: dict[str, dict[str, Any]]) -> list[str]:
    freed = [acc for acc, c in claims.items() if c.get("repo") and task_closed(c["repo"], c["task"])]
    for acc in freed:
        del claims[acc]
    return freed


def claim(repo: str, task: str, issue: str | None, candidates: Sequence[str]) -> str | None:
    norm_issue = normalize_issue(issue)
    holder = holder_of(task, norm_issue)
    normalized = list(dict.fromkeys(normalize_account(c) for c in candidates))
    with claims_locked() as claims:
        for acc in drop_finished(claims):
            print(f"已收尾任务的账号自动释放：{acc}", file=sys.stderr)
        for acc in normalized:
            if claims.get(acc, {}).get("holder") == holder:
                return acc
        for acc in normalized:
            if acc not in claims:
                others = [a for a, c in claims.items() if c.get("holder") == holder]
                claims[acc] = {"holder": holder, "task": task, "issue": norm_issue,
                               "repo": str(Path(repo).resolve()), "at": int(time.time())}
                if others:
                    print(
                        f"提示：{holder} 同时还持有 {', '.join(others)}；已不再并行使用的账号请立即运行 "
                        f"test-account.py release --task {task}"
                        + (f" --issue {norm_issue}" if norm_issue else "")
                        + " --account <账号> 归还",
                        file=sys.stderr,
                    )
                return acc
        for acc in normalized:
            print(f"{acc} 被占用：{claims[acc]['holder']}", file=sys.stderr)
        return None


def release(task: str, issue: str | None, accounts: Sequence[str] | None = None) -> list[str]:
    norm_issue = normalize_issue(issue)
    targets = list(dict.fromkeys(normalize_account(a) for a in accounts)) if accounts else None
    with claims_locked() as claims:
        if targets is not None:
            conflicts = [
                f"{acc}（当前归属 {claims[acc]['holder']}）"
                for acc in targets
                if acc in claims
                and (
                    claims[acc].get("task") != task
                    or (norm_issue is not None and claims[acc].get("issue") != norm_issue)
                )
            ]
            if conflicts:
                raise RuntimeError(f"不能释放非本任务持有的账号：{', '.join(conflicts)}")
            freed = [acc for acc in targets if acc in claims]
        else:
            freed = [acc for acc, c in claims.items()
                     if c.get("task") == task and (norm_issue is None or c.get("issue") == norm_issue)]
        for acc in freed:
            del claims[acc]
    return freed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨调度者领取/释放自动化测试账号")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--repo", required=True, help="主仓库路径（读它的 .hwf/tmp/INDEX.md 判断任务是否收尾）")
    p_claim.add_argument("--task", required=True)
    p_claim.add_argument("--issue")
    p_claim.add_argument("--candidates", nargs="+", required=True)
    p_release = sub.add_parser("release")
    p_release.add_argument("--task", required=True)
    p_release.add_argument("--issue")
    p_release.add_argument(
        "--account", "--accounts", dest="accounts", action="extend", nargs="+",
        help="只释放指定闲置账号，保留本任务仍在用的其他账号",
    )
    sub.add_parser("list")
    opts = parser.parse_args(argv)
    try:
        if opts.cmd == "claim":
            account = claim(opts.repo, opts.task, opts.issue, opts.candidates)
            if account is None:
                print("候选账号全部被占用，等其他任务释放后再领，不要抢占", file=sys.stderr)
                return EXIT_ALL_BUSY
            print(account)
        elif opts.cmd == "release":
            freed = release(opts.task, opts.issue, opts.accounts)
            print(json.dumps({"released": freed}, ensure_ascii=False))
        else:
            with claims_locked() as claims:
                drop_finished(claims)
                print(json.dumps(claims, ensure_ascii=False, indent=2))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
