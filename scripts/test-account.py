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
META_PATH = HWF_HOME / "state" / "test-account-meta.json"
DEFAULT_POOL_PATH = Path(os.environ.get("HWF_TEST_ACCOUNTS_FILE", Path.home() / ".agents/secrets/look-ai-test-accounts.md"))

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALL_BUSY = 3


def mask_account(account: str) -> str:
    clean = account.strip()
    if not clean:
        return "***"
    if "@" in clean:
        local, _, domain = clean.partition("@")
        head = local[:2] if len(local) >= 2 else local[:1]
        return f"{head}***@{domain}"
    if len(clean) <= 4:
        return f"{clean[:1]}***"
    return f"{clean[:2]}***{clean[-2:]}"


def _normalize_tag(raw: str) -> str:
    return raw.strip().lower().replace("_", "-")


def _infer_tags_from_text(text: str) -> set[str]:
    lower = text.lower()
    tags: set[str] = set()
    for bracket in re.findall(r"\[([^\[\]]+)\]", text):
        for piece in re.split(r"[,，/\s]+", bracket):
            norm = _normalize_tag(piece)
            if norm and re.fullmatch(r"[a-z0-9:+-]+", norm):
                tags.add(norm)
    if any(k in lower for k in ("plus试用", "plus-trial", "plus trial")) or "试用" in text or "trial" in lower:
        tags.add("plus-trial")
    if "免费" in text or re.search(r"\bfree\b", lower):
        tags.add("free")
    if any(k in text for k in ("已付费", "付费", "年费", "月费")) or re.search(r"\b(?:paid|pro|enterprise)\b", lower):
        tags.add("paid")
    if re.search(r"\bplus\b", lower) and "plus-trial" not in tags:
        tags.update({"paid", "plus"})
    if any(k in text for k in ("国内", "中国")) or any(k in lower for k in ("region:cn", "bootingai", "look-ai.cn")):
        tags.add("region:cn")
    if any(k in text for k in ("国际", "海外")) or any(k in lower for k in ("region:intl", "lookai.app", "lookfashion")):
        tags.add("region:intl")
    return tags


def load_account_meta(meta_path: Path | None = None) -> dict[str, set[str]]:
    path = meta_path if meta_path is not None else META_PATH
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, set[str]] = {}
    for key, val in raw.items():
        try:
            acc = normalize_account(str(key))
        except RuntimeError:
            continue
        raw_tags = val.get("tags", []) if isinstance(val, dict) else (val if isinstance(val, list) else [])
        out[acc] = {_normalize_tag(str(t)) for t in raw_tags if str(t).strip()}
    return out


_PLAN_PAID_TAGS = frozenset({"paid", "plus", "pro"})


def _reconcile_plan_tags(base_tags: set[str], override_tags: set[str], removed_tags: set[str] = frozenset()) -> set[str]:
    """套餐状态互斥收敛：本机实测登记 paid/plus/pro 时自动覆盖静态表里的 free，反之亦然。"""
    merged = set(base_tags) - set(removed_tags)
    if override_tags & _PLAN_PAID_TAGS:
        merged.discard("free")
    elif "free" in override_tags:
        merged -= _PLAN_PAID_TAGS
    merged.update(override_tags)
    merged -= set(removed_tags)
    return merged


def load_account_meta_full(meta_path: Path | None = None) -> dict[str, tuple[set[str], set[str]]]:
    path = meta_path if meta_path is not None else META_PATH
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, tuple[set[str], set[str]]] = {}
    for key, val in raw.items():
        try:
            acc = normalize_account(str(key))
        except RuntimeError:
            continue
        raw_tags = val.get("tags", []) if isinstance(val, dict) else (val if isinstance(val, list) else [])
        raw_removed = val.get("removed_tags", []) if isinstance(val, dict) else []
        out[acc] = (
            {_normalize_tag(str(t)) for t in raw_tags if str(t).strip()},
            {_normalize_tag(str(t)) for t in raw_removed if str(t).strip()},
        )
    return out


def update_account_tags(
    account: str,
    *,
    add_tags: Sequence[str] = (),
    remove_tags: Sequence[str] = (),
    note: str = "",
    meta_path: Path | None = None,
) -> list[str]:
    """在本机状态目录记录账号的非敏感套餐/站点标签（如 free、plus-trial、paid、region:intl），绝不存密码。"""
    acc = normalize_account(account)
    path = meta_path if meta_path is not None else META_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    added = {_normalize_tag(t) for t in add_tags if _normalize_tag(t)}
    removed = {_normalize_tag(t) for t in remove_tags if _normalize_tag(t)}
    if added & _PLAN_PAID_TAGS:
        removed.add("free")
    elif "free" in added:
        removed |= _PLAN_PAID_TAGS - added
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(loaded, dict):
            loaded = {}
        entry = loaded.get(acc) if isinstance(loaded.get(acc), dict) else {}
        prev_tags = {_normalize_tag(str(t)) for t in (entry.get("tags") or []) if str(t).strip()}
        prev_removed = {_normalize_tag(str(t)) for t in (entry.get("removed_tags") or []) if str(t).strip()}
        all_removed = (prev_removed | removed) - added
        tags = _reconcile_plan_tags(prev_tags, added, all_removed)
        sorted_tags = sorted(tags)
        record: dict[str, Any] = {"tags": sorted_tags, "removed_tags": sorted(all_removed), "updated_at": int(time.time())}
        if note.strip():
            record["note"] = note.strip()[:200]
        elif entry.get("note"):
            record["note"] = entry["note"]
        loaded[acc] = record
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(loaded, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return sorted_tags


def parse_accounts_pool(pool_path: Path | None = None, meta_path: Path | None = None) -> list[dict[str, Any]]:
    """解析测试账号池的账号标识与套餐/站点标签，跳过密码列且永不返回明文密码。"""
    path = pool_path if pool_path is not None else DEFAULT_POOL_PATH
    meta_full = load_account_meta_full(meta_path)
    ordered: dict[str, set[str]] = {}

    if path.is_file():
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("accounts", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for item in items:
                if not isinstance(item, dict) or not item.get("account"):
                    continue
                acc = normalize_account(str(item["account"]))
                tags = {_normalize_tag(str(t)) for t in (item.get("tags") or []) if str(t).strip()}
                if item.get("plan"):
                    tags.add(_normalize_tag(str(item["plan"])))
                ordered.setdefault(acc, set()).update(tags)
        else:
            section_tags: set[str] = set()
            acc_col: int | None = None
            pwd_cols: set[int] = set()
            tag_cols: set[int] = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    section_tags = _infer_tags_from_text(stripped)
                    acc_col = None
                    continue
                if not stripped.startswith("|") or not stripped.endswith("|"):
                    continue
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if all(not c or set(c) <= {"-", ":", " "} for c in cells):
                    continue
                lowered = [c.strip("`'\"").lower() for c in cells]
                # 仅在尚未确定表头且单元格本身是列名（不含 @ 且非手机号）时识别表头，避免把 user@xxx 数据行误当表头。
                is_header = acc_col is None and not any("@" in h for h in lowered) and any(
                    h in ("账号", "测试账号", "邮箱", "手机号", "手机", "用户名", "account", "email", "phone", "username", "user")
                    for h in lowered
                )
                if is_header:
                    acc_col = next(
                        (
                            i for i, h in enumerate(lowered)
                            if h in ("账号", "测试账号", "邮箱", "手机号", "手机", "用户名", "account", "email", "phone", "username", "user")
                        ),
                        0,
                    )
                    pwd_cols = {
                        i for i, h in enumerate(lowered) if any(k in h for k in ("密码", "password", "口令", "pwd", "secret", "token"))
                    }
                    tag_cols = {i for i in range(len(cells)) if i != acc_col and i not in pwd_cols}
                    continue
                if acc_col is None:
                    first_clean = cells[0].strip("`'\"")
                    if "@" in first_clean or re.fullmatch(r"\+?\d{6,15}", first_clean):
                        acc_col = 0
                        pwd_cols = {1} if len(cells) >= 2 else set()
                        tag_cols = set(range(2, len(cells)))
                    else:
                        continue
                if acc_col >= len(cells):
                    continue
                raw_acc = cells[acc_col].strip()
                if not raw_acc or raw_acc in ("-", "—", "无"):
                    continue
                try:
                    acc = normalize_account(raw_acc)
                except RuntimeError:
                    continue
                row_tags = set(section_tags)
                for idx in tag_cols:
                    if idx < len(cells):
                        row_tags.update(_infer_tags_from_text(cells[idx]))
                        for tok in re.split(r"[,，/\s]+", cells[idx]):
                            norm_tok = _normalize_tag(tok.strip("`'\""))
                            if norm_tok in ("free", "paid", "plus", "pro", "plus-trial", "trial", "cn", "intl"):
                                row_tags.add("region:cn" if norm_tok == "cn" else "region:intl" if norm_tok == "intl" else norm_tok)
                ordered.setdefault(acc, set()).update(row_tags)

    for acc, (extra, removed) in meta_full.items():
        ordered[acc] = _reconcile_plan_tags(ordered.get(acc, set()), extra, removed)

    return [
        {"account": acc, "masked": mask_account(acc), "tags": sorted(tags)}
        for acc, tags in ordered.items()
    ]


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


def filter_candidates_by_tags(
    candidates: Sequence[str] | None,
    *,
    pool_path: Path | None = None,
    meta_path: Path | None = None,
    require_tags: Sequence[str] = (),
    exclude_tags: Sequence[str] = (),
) -> list[str]:
    req = {_normalize_tag(t) for t in require_tags if _normalize_tag(t)}
    exc = {_normalize_tag(t) for t in exclude_tags if _normalize_tag(t)}
    pool_entries = parse_accounts_pool(pool_path, meta_path)
    tags_by_acc = {item["account"]: set(item["tags"]) for item in pool_entries}
    base = (
        [normalize_account(c) for c in candidates]
        if candidates
        else [item["account"] for item in pool_entries]
    )
    if not req and not exc:
        return list(dict.fromkeys(base))
    matched = [
        acc for acc in dict.fromkeys(base)
        if req.issubset(tags_by_acc.get(acc, set())) and not (exc & tags_by_acc.get(acc, set()))
    ]
    return matched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨调度者领取/释放自动化测试账号（支持套餐/站点标签筛选）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--repo", required=True, help="主仓库路径（读它的 .hwf/tmp/INDEX.md 判断任务是否收尾）")
    p_claim.add_argument("--task", required=True)
    p_claim.add_argument("--issue")
    p_claim.add_argument("--candidates", nargs="+", help="候选账号列表；省略时从 --pool-file 加载")
    p_claim.add_argument("--pool-file", type=Path, help="测试账号池 Markdown/JSON 路径（默认 ~/.agents/secrets/look-ai-test-accounts.md）")
    p_claim.add_argument("--require-tag", action="append", default=[], help="必须具备的套餐/站点标签（如 free、paid、region:intl）")
    p_claim.add_argument("--exclude-tag", action="append", default=[], help="排除的套餐标签（如 plus-trial）")
    p_release = sub.add_parser("release")
    p_release.add_argument("--task", required=True)
    p_release.add_argument("--issue")
    p_release.add_argument(
        "--account", "--accounts", dest="accounts", action="extend", nargs="+",
        help="只释放指定闲置账号，保留本任务仍在用的其他账号",
    )
    p_tag = sub.add_parser("tag", help="在本机 state/test-account-meta.json 标注账号套餐状态标签（不存密码）")
    p_tag.add_argument("--account", required=True)
    p_tag.add_argument("--add", action="append", default=[])
    p_tag.add_argument("--remove", action="append", default=[])
    p_tag.add_argument("--note", default="", help="记录套餐状态变更背景（严禁写密码）")
    p_pool = sub.add_parser("pool", help="查看脱敏账号池及其套餐/站点标签与当前占用状态")
    p_pool.add_argument("--pool-file", type=Path)
    p_pool.add_argument("--require-tag", action="append", default=[])
    p_pool.add_argument("--exclude-tag", action="append", default=[])
    sub.add_parser("list")
    opts = parser.parse_args(argv)
    try:
        if opts.cmd == "claim":
            if not opts.candidates and not opts.pool_file and not opts.require_tag and not opts.exclude_tag:
                raise RuntimeError("claim 需要指定 --candidates，或通过 --pool-file / --require-tag 从账号池筛选")
            filtered = filter_candidates_by_tags(
                opts.candidates,
                pool_path=opts.pool_file,
                require_tags=opts.require_tag,
                exclude_tags=opts.exclude_tag,
            )
            if not filtered:
                print("没有空闲且符合套餐/站点标签条件的候选测试账号，请用 test-account.py pool 核对标签状态", file=sys.stderr)
                return EXIT_ALL_BUSY
            account = claim(opts.repo, opts.task, opts.issue, filtered)
            if account is None:
                print("候选账号全部被占用，等其他任务释放后再领，不要抢占", file=sys.stderr)
                return EXIT_ALL_BUSY
            print(account)
        elif opts.cmd == "release":
            freed = release(opts.task, opts.issue, opts.accounts)
            print(json.dumps({"released": freed}, ensure_ascii=False))
        elif opts.cmd == "tag":
            updated = update_account_tags(opts.account, add_tags=opts.add, remove_tags=opts.remove, note=opts.note)
            print(json.dumps({"account": mask_account(normalize_account(opts.account)), "tags": updated}, ensure_ascii=False))
        elif opts.cmd == "pool":
            with claims_locked() as claims:
                drop_finished(claims)
                snapshot = dict(claims)
            allowed = set(filter_candidates_by_tags(
                None,
                pool_path=opts.pool_file,
                require_tags=opts.require_tag,
                exclude_tags=opts.exclude_tag,
            ))
            out_items = [
                {
                    "account": item["masked"],
                    "tags": item["tags"],
                    "busy": item["account"] in snapshot,
                    "holder": snapshot.get(item["account"], {}).get("holder"),
                }
                for item in parse_accounts_pool(opts.pool_file)
                if item["account"] in allowed
            ]
            print(json.dumps(out_items, ensure_ascii=False, indent=2))
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
