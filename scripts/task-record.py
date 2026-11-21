#!/usr/bin/env python3
"""调度者改任务记录、给用户发评论的唯一入口：格式和通知规则由脚本保证，不靠调度者记。

    task-record.py index-add --repo <主仓库> --task <任务标识> --input <输入原文> [--status in-progress] [--queued]
    task-record.py index-set --repo <主仓库> --task <任务标识> --status <状态>
    task-record.py state-set --repo <主仓库> --task <任务标识> --field <字段> --value <内容>
    task-record.py note      --repo <主仓库> --task <任务标识> --issue <issue URL> --kind <类别> --body-file <文件> [--will …]
    task-record.py preflight --will <动作>...
    task-record.py card-check --file <arch 判定落盘文件>
    task-record.py release-check --repo <仓库路径> --release <release-根编号> [--base origin/main] [--develop origin/develop] --commit <步骤源SHA>... [--no-fetch]
    task-record.py progress  --repo <主仓库> --task <任务标识> [--issue <根 issue URL> --steps-file <步骤 JSON>]

为什么有它：
- 2026-11-15 #483：调度者手写 INDEX 行没加反引号，收件箱找不到负责人，用户的回复晚到约 7 分钟。
- 同一天 #483：免打扰模式下仍单独发了 4 条阶段进度评论，每条都转成一条飞书消息。
- 同一天 #483：先问用户「同意合并吗」，用户同意后才发现本账号没有 main 的合并权限，白跑一轮。
- 2026-11-21 #484：前端 main MR 标题写成 `fix: #484 …` 命中 GitLab 自动关单规则，用户点合并瞬间把仍有后续步骤的发起 issue 误关了。

INDEX：index-add 追加本调度者的一行（接手别人的任务也是追加，最后一行是现在的负责人）；--queued 追加调度者列留空的
排队行。index-set 只改本调度者自己那行的状态；收尾状态（done/delivered/closed/cancelled）只能由 wrapup.py 写。

STATE 顶部：state-set 维护 STATE.md 开头 `<!-- hwf:top -->` 标记之间的固定字段（见 TOP_FIELDS），其余内容不动。

评论：note 按 STATE 顶部的通知模式决定发不发，并在评论末尾附隐藏标记 `<!-- hwf-kind:<类别> -->` 供飞书秘书识别。
免打扰下 stage（阶段进度）不发，退出码 3，内容只写 STATE；step-done（步骤汇总）与 record（登记或存档）只写 GitLab 留档备查，
不推送飞书私聊、也不覆盖 STATE「最近用户通知」；只有 todo、risk、answer、delivery-done 会推送飞书私聊并更新「最近用户通知」（详尽模式全推）。
todo（要用户处理或授权）必须用 --will 写明用户同意后本账号要做的动作，脚本先自查做不做得了：
    --will merge=<MR URL>   --will push=<项目 URL>@<分支>   --will tag=<项目 URL>@<tag 名>   --will none（只需要用户自己做）
有一项做不了就不发，改成请用户来做；查不出来的在评论末尾注明。正文和已发过的完全一样时不发。
发出后回读，并把记录写进 <任务目录>/notes.jsonl。

退出码：0 成功；1 失败；2 参数错误；3 按规则没发（免打扰拦下、重复、自查不通过）。

card-check：按 ~/.hwf/protocol/planning.md §0 核对 arch 判定里的「需求理解卡」（现状带 file:line、历史与坑不为空、验收条件每条写了证据、验收前提不为空）。
2026-11-15 用户同意：编码前对项目了解不够、验收太粗；只写进协议的要求聊几句就忘，所以由脚本拦。缺项时退出码 3。

progress：根 issue 描述顶部的「进度」维护区只由它写。2026-11-19 #484：进度区手写了 7 小时没更新，代码还没进
release-484 就打了勾，单项里塞满分支、流水线和验收细节，用户看不出现在做到哪、在等谁。所以：
- 「现在 / 下一步 / 等你处理」三行直接取 STATE 顶部；state-set 改这三项时自动刷新 issue 描述，不用另记。
- 勾不勾由脚本核对：步骤的 commits 全部是对应项目 release-<根编号> 的祖先才打勾，调度者不能手勾。
- 步骤标题限 80 字、三行各限 150 字；细节、证据写评论。用户原文和「上线清单」原样保留，写完回读。
步骤 JSON：{"steps": [{"title": "接口接入", "blocked_by": [1], "child": 12,
            "commits": [{"project": "frontend/web-v1", "sha": "<提交>"}]}]}
第一次带 --issue 与 --steps-file，存到 <任务目录>/progress.json；步骤变了（新增、拆分、汇总了新提交）再带 --steps-file。
"""

from __future__ import annotations

import argparse
import fcntl
from fnmatch import fnmatch
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Optional, Sequence
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_WR = _load("wrapup", "wrapup.py")
_FS = _WR._FS

EXIT_OK, EXIT_ERROR, EXIT_HELD = 0, 1, 3
QUIET, VERBOSE = "免打扰", "详尽"
TOP_OPEN = "<!-- hwf:top 本段只由 task-record.py 改 -->"
TOP_CLOSE = "<!-- /hwf:top -->"
TOP_FIELDS = ("通知模式", "当前阶段", "下一动作", "待用户事项", "最近用户通知")
# 这三项会原样显示在根 issue 进度区，必须短到一眼看完（2026-11-19 #484）。
PROGRESS_FIELDS = {"当前阶段": "现在", "下一动作": "下一步", "待用户事项": "等你处理"}
NOW_LIMIT, TITLE_LIMIT = 150, 80
AREA_OPEN = "<!-- hwf:start 以下由交付流程维护 -->"
AREA_CLOSE = "<!-- hwf:end -->"
AREA = re.compile(r"<!-- hwf:start[^>]*-->.*?<!-- hwf:end -->\n*", re.S)
EMPTY_RELEASE = "## 上线清单\n| 仓库 | release 分支 | 改了什么 | 有没有数据库改动 |\n| --- | --- | --- | --- |"
LEGACY_MODE = re.compile(r"通知模式[:：]\s*(免打扰|详尽)")
# 免打扰下 stage 只记 STATE 不发 GitLab；step-done 与 record 仅在 GitLab 留档备查，只有 QUIET_NOTIFY_KINDS 会推飞书私聊。
KINDS = {"todo": "用户待办", "risk": "新风险", "answer": "实质答复", "step-done": "步骤完成",
         "delivery-done": "整项交付完成", "stage": "阶段进度", "record": "登记或存档"}
QUIET_NOTIFY_KINDS = frozenset({"todo", "risk", "answer", "delivery-done"})
KIND_TAG = re.compile(r"\s*<!--\s*hwf-kind:([a-z-]+)\s*-->\s*")
DEVELOPER = 30
MR_URL = re.compile(r"https?://(?P<host>[^/]+)/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)")
PROJECT_REF = re.compile(r"https?://(?P<host>[^/]+)/(?P<project>[^@]+?)/?@(?P<ref>.+)")


class RecordError(RuntimeError):
    pass


# ---------- INDEX ----------

def scheduler_of(explicit: Optional[str]) -> tuple[str, str]:
    """本调度者的（登记名，pane）。"""
    name = _WR.current_scheduler(explicit)
    agents = _WR.herdr("agent", "list")["result"]["agents"]
    pane = next((str(a.get("pane_id") or "") for a in agents if a.get("name") == name), "")
    if not pane:
        raise RecordError(f"herdr 里找不到调度者 {name} 的 pane")
    return name, pane


def cell(text: str) -> str:
    return text.replace("\r", "").replace("\n", "\\n").replace("|", "\\|").strip()


def index_add(repo: str, task: str, text: str, status: str, queued: bool, scheduler: Optional[str]) -> str:
    if not _FS.TASK_ID.fullmatch(task):
        raise RecordError(f"任务标识不合规：{task}")
    if _FS.IndexRow(task, "", status).open is False:
        raise RecordError("收尾状态只能由 wrapup.py 写")
    owner = "" if queued else "`{}` / `{}`".format(*scheduler_of(scheduler))
    path = Path(repo) / _FS.INDEX_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "INDEX.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = [row for row in _FS.read_index(repo) if row.task == task]
        if rows and not queued and owner.split("`")[1] == rows[-1].scheduler:
            raise RecordError(f"INDEX 里 {task} 已经记在你名下，改状态用 index-set")
        body = path.read_text(encoding="utf-8") if path.exists() else \
            "# 任务索引\n\n| 任务标识 | 输入原文 | 调度者 | 状态 |\n|---|---|---|---|\n"
        line = f"| `{task}` | {cell(text)} | {owner} | {cell(status)} |\n"
        path.write_text(body + ("" if body.endswith("\n") else "\n") + line, encoding="utf-8")
    return line.strip()


def index_set(repo: str, task: str, status: str, scheduler: Optional[str]) -> None:
    if not _FS.IndexRow(task, "", status).open:
        raise RecordError("收尾状态只能由 wrapup.py 写")
    name = _WR.current_scheduler(scheduler)
    with (Path(repo) / _FS.INDEX_RELATIVE.parent / "INDEX.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _WR.write_index_status(repo, task, name, status)


# ---------- STATE 顶部 ----------

def state_path(repo: str, task: str) -> Path:
    return Path(repo) / ".hwf/tmp" / task / "STATE.md"


def read_top(text: str) -> dict[str, str]:
    start, end = text.find(TOP_OPEN), text.find(TOP_CLOSE)
    if start < 0 or end < start:
        return {}
    fields = {}
    for line in text[start + len(TOP_OPEN):end].splitlines():
        match = re.match(r"- ([^：]+)：(.*)", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def notify_mode(repo: str, task: str) -> str:
    """STATE 顶部的通知模式；没写按免打扰。脚本接管前的旧 STATE 认正文里第一处「通知模式：…」。"""
    path = state_path(repo, task)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    mode = read_top(text).get("通知模式", "")
    if not mode:
        legacy = LEGACY_MODE.search(text)
        mode = legacy.group(1) if legacy else QUIET
    return VERBOSE if mode.startswith(VERBOSE) else QUIET


def state_set(repo: str, task: str, name: str, value: str) -> None:
    if name not in TOP_FIELDS:
        raise RecordError(f"STATE 顶部没有「{name}」这一项，可用：{'、'.join(TOP_FIELDS)}")
    value = " ".join(value.split())
    if name in PROGRESS_FIELDS and len(value) > NOW_LIMIT:
        raise RecordError(f"「{name}」会显示在 issue 进度区，限 {NOW_LIMIT} 字（现在 {len(value)} 字）：只写一句结论，细节写评论或 STATE 正文")
    if name == "通知模式" and not value.startswith((QUIET, VERBOSE)):
        raise RecordError(f"通知模式只能以「{QUIET}」或「{VERBOSE}」开头")
    path = state_path(repo, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else f"# {task} 调度记录\n"
    fields = read_top(text)
    if not fields:
        fields = {"通知模式": notify_mode(repo, task)}
    fields[name] = value
    block = "\n".join([TOP_OPEN, *(f"- {key}：{fields.get(key, '')}" for key in TOP_FIELDS), TOP_CLOSE])
    start, end = text.find(TOP_OPEN), text.find(TOP_CLOSE)
    if start >= 0 and end > start:
        text = text[:start] + block + text[end + len(TOP_CLOSE):]
    else:
        head, _, rest = text.partition("\n") if text.startswith("# ") else ("", "", text)
        text = (head + "\n\n" if head else "") + block + "\n\n" + rest.lstrip("\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    if name in PROGRESS_FIELDS and progress_path(repo, task).exists():
        progress(repo, task)


# ---------- 自查：同意之后本账号做不做得了 ----------

def api_get(host: str, path: str) -> Any:
    return _WR.glab_api(host, "GET", path)


def access_level(host: str, project: str) -> int:
    perms = api_get(host, f"projects/{quote(project, safe='')}").get("permissions") or {}
    return max(int((perms.get(key) or {}).get("access_level") or 0) for key in ("project_access", "group_access"))


def allowed(level: int, rules: Sequence[dict[str, Any]]) -> bool:
    return any(0 < int(rule.get("access_level") or 0) <= level for rule in rules)


def mr_closing_issues(host: str, project: str, iid: str) -> list[str]:
    """查询该 MR 合并进默认分支后会被 GitLab 自动关闭的未关 issue 列表。"""
    items = api_get(host, f"projects/{quote(project, safe='')}/merge_requests/{iid}/closes_issues") or []
    return [f"#{item['iid']}" for item in items if item.get("iid") is not None and item.get("state", "opened") != "closed"]


def autoclose_message(target: str, closing: Sequence[str]) -> str:
    return (f"{target} 合并后会自动关闭 {', '.join(closing)}（标题、描述或提交含 fix: #N / closes #N 等自动关单关键词）；"
            "请先用 glab mr update 改掉标题/描述（如写成「fix: 说明（关联 #N）」）并确认 closes_issues 为空后再发")


def body_autoclose_problems(body: str, checked_urls: set[str]) -> list[str]:
    """扫描评论正文里贴出的打开态 MR，拦截夹带自动关单毒丸的合并链接（含 --will none 与完成汇报）。"""
    seen = set(checked_urls)
    problems: list[str] = []
    for match in MR_URL.finditer(body):
        url = match.group(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            mr = api_get(match["host"], f"projects/{quote(match['project'], safe='')}/merge_requests/{match['iid']}")
            if mr.get("state") != "opened":
                continue
            closing = mr_closing_issues(match["host"], match["project"], match["iid"])
        except _WR.WrapupError:
            continue
        if closing:
            problems.append(autoclose_message(url, closing))
    return problems


def check(will: str) -> tuple[str, str]:
    """返回（ok / no / autoclose / unknown，给人看的一句话）。"""
    action, _, target = will.partition("=")
    if action == "merge":
        match = MR_URL.match(target)
        if not match:
            raise RecordError(f"--will merge= 后面要跟 MR 链接：{target}")
        mr = api_get(match["host"], f"projects/{quote(match['project'], safe='')}/merge_requests/{match['iid']}")
        if mr.get("state") != "opened":
            return "no", f"{target} 现在是 {mr.get('state')}，不用再合并"
        closing = mr_closing_issues(match["host"], match["project"], match["iid"])
        if closing:
            return "autoclose", autoclose_message(target, closing)
        if mr.get("has_conflicts"):
            return "no", f"{target} 有冲突，合并不了"
        if not (mr.get("user") or {}).get("can_merge"):
            return "no", f"本账号没有把 {target} 合并进 {mr.get('target_branch')} 的权限"
        return "ok", f"本账号可以合并 {target}"
    if action in ("push", "tag"):
        match = PROJECT_REF.match(target)
        if not match:
            raise RecordError(f"--will {action}= 后面要跟 <项目 URL>@<名字>：{target}")
        host, project, ref = match["host"], match["project"], match["ref"]
        base = f"projects/{quote(project, safe='')}"
        level = access_level(host, project)
        if level < DEVELOPER:
            return "no", f"本账号在 {project} 的权限不到 Developer，不能{'推送' if action == 'push' else '打 tag'}"
        try:
            if action == "push":
                rules = [api_get(host, f"{base}/protected_branches/{quote(ref, safe='')}")]
                key = "push_access_levels"
            else:
                rules = [r for r in api_get(host, f"{base}/protected_tags?per_page=100") if fnmatch(ref, r["name"])]
                key = "create_access_levels"
        except _WR.WrapupError as error:
            if action == "push" and "404" in str(error):
                return "ok", f"{project} 的 {ref} 分支没有保护，本账号可以推送"
            what = "分支" if action == "push" else "tag"
            return "unknown", f"读不到 {project} 的{what}保护规则，无法确认本账号能不能{'推送' if action == 'push' else '打'} {ref}"
        if rules and not all(allowed(level, rule.get(key) or []) for rule in rules):
            return "no", f"{project} 的 {ref} 受保护，本账号没有{'推送' if action == 'push' else '创建'}权限"
        return "ok", f"本账号可以在 {project} {'推送' if action == 'push' else '创建 tag'} {ref}"
    raise RecordError(f"--will 不认识的动作：{will}（可用 merge=、push=、tag=、none）")


def preflight(wills: Sequence[str]) -> list[tuple[str, str]]:
    return [check(will) for will in wills if will != "none"]


# ---------- 需求理解卡 ----------

CARD_FIELDS = ("复杂度判定", "先出方案", "防风险", "理由", "依据", "现状", "历史与坑", "验收条件",
               "验收前提", "关键决定", "接口约定", "待澄清问题")
FIELD_LINE = re.compile(rf"^[#>*\s]*({'|'.join(CARD_FIELDS)})\**\s*(?:[:：]\s*\**\s*(.*))?$")
FILE_LINE = re.compile(r"[\w./-]+\.\w+:\d+")
ITEM = re.compile(r"^(?:[-*]|\d+[.、)])\s+")
EVIDENCE = re.compile(r"证据\**\s*[:：]\s*\**\s*\S")


def card_sections(text: str) -> dict[str, str]:
    """按字段名切开 arch 的判定；字段名可以是「现状：…」一行，也可以是小标题或加粗。"""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = FIELD_LINE.match(line)
        if match:
            current = match.group(1)
            sections[current] = [match.group(2) or ""]
        elif line.lstrip().startswith("#"):
            current = None
        elif current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def card_check(text: str) -> list[str]:
    """返回缺项清单；空列表表示需求理解卡写全了。"""
    sections = card_sections(text)
    problems = []
    if not FILE_LINE.search(sections.get("现状", "")):
        problems.append("「现状」缺失或没有 file:line 依据")
    if not sections.get("历史与坑"):
        problems.append("「历史与坑」缺失或是空的（没有相关记录也要写明查了什么）")
    items: list[str] = []
    for line in sections.get("验收条件", "").splitlines():
        if ITEM.match(line):
            items.append(line)
        elif items and line.strip():
            items[-1] += "\n" + line
    if not items:
        problems.append("「验收条件」缺失或不是编号列表")
    for number, item in enumerate(items, 1):
        if not EVIDENCE.search(item):
            problems.append(f"验收条件第 {number} 条没写「证据：<怎么证明>」：{item.splitlines()[0][:60]}")
    if not sections.get("验收前提"):
        problems.append("「验收前提」缺失或是空的（都不需要也要写「无特殊前提」）")
    return problems


# ---------- 汇总分支完整性核对 ----------

def _rev_parse(repo: str, ref: str, kind: str = "commit") -> str:
    res = _WR.git(repo, "rev-parse", "--verify", f"{ref}^{{{kind}}}")
    if res.returncode != 0:
        raise RecordError(f"找不到 Git 引用 {ref}：{res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def _resolve_release_ref(repo: str, release: str) -> str:
    if release.startswith(("origin/", "refs/")):
        return release
    if _WR.git(repo, "rev-parse", "--verify", f"origin/{release}^{{commit}}").returncode == 0:
        return f"origin/{release}"
    return release


def release_check(repo: str, release: str, base: str, commits: Sequence[str],
                  develop: Optional[str] = None, fetch: bool = True) -> dict[str, Any]:
    """对照基准分支（origin/main）、已批准步骤提交与测试分支，核对汇总分支完整性。"""
    if not commits:
        raise RecordError("release-check 至少要给一个 --commit")
    if fetch:
        fetched = _WR.git(repo, "fetch", "--quiet", "origin")
        if fetched.returncode != 0:
            raise RecordError(f"git fetch origin 失败：{fetched.stderr.strip() or fetched.stdout.strip()}")
    release_ref = _resolve_release_ref(repo, release)
    base_sha = _rev_parse(repo, base)
    release_sha = _rev_parse(repo, release_ref)
    approved = [_rev_parse(repo, item) for item in commits]
    approved_set = set(approved)

    missing = [
        raw for raw, sha in zip(commits, approved)
        if _WR.git(repo, "merge-base", "--is-ancestor", sha, release_sha).returncode != 0
    ]
    rev_res = _WR.git(repo, "rev-list", "--no-merges", f"{base_sha}..{release_sha}", *(f"^{sha}" for sha in approved))
    if rev_res.returncode != 0:
        raise RecordError(f"git rev-list 失败：{rev_res.stderr.strip()}")
    foreign_commits = [line.strip() for line in rev_res.stdout.splitlines() if line.strip()]

    log_res = _WR.git(repo, "log", "--format=%H\t%P\t%s", f"{base_sha}..{release_sha}")
    if log_res.returncode != 0:
        raise RecordError(f"git log 失败：{log_res.stderr.strip()}")
    commits_in_release: list[str] = []
    foreign_merges: list[str] = []
    for raw_line in log_res.stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t", 2)
        sha = parts[0]
        parents = parts[1].split() if len(parts) > 1 and parts[1] else []
        subject = parts[2] if len(parts) > 2 else ""
        commits_in_release.append(f"{sha[:12]} {subject}".strip())
        # 拦截将 develop 或其他未批准分支合入开发分支/汇总分支的合并提交。
        for extra_parent in parents[1:]:
            if extra_parent not in approved_set and \
                    _WR.git(repo, "merge-base", "--is-ancestor", extra_parent, base_sha).returncode != 0:
                foreign_merges.append(f"{sha[:12]}<-{extra_parent[:12]} ({subject})")

    unique_approved = list(dict.fromkeys(approved))
    release_tree = _rev_parse(repo, release_sha, "tree")
    same_tree = len(unique_approved) == 1 and _rev_parse(repo, unique_approved[0], "tree") == release_tree

    diff_base = _WR.git(repo, "diff", "--name-only", f"{base_sha}..{release_sha}")
    if diff_base.returncode != 0:
        raise RecordError(f"git diff 失败：{diff_base.stderr.strip()}")
    changed_files = [line.strip() for line in diff_base.stdout.splitlines() if line.strip()]

    develop_sha: Optional[str] = None
    develop_diff_files: list[str] = []
    if develop:
        develop_sha = _rev_parse(repo, develop)
        if changed_files:
            diff_dev = _WR.git(repo, "diff", "--name-only", release_sha, develop_sha, "--", *changed_files)
            if diff_dev.returncode != 0:
                raise RecordError(f"git diff {develop} 失败：{diff_dev.stderr.strip()}")
            develop_diff_files = [line.strip() for line in diff_dev.stdout.splitlines() if line.strip()]

    problems: list[str] = []
    if missing:
        problems.append("未进入汇总分支的步骤提交：" + ", ".join(missing))
    if foreign_commits:
        problems.append("汇总分支含非本需求提交：" + ", ".join(s[:12] for s in foreign_commits))
    if foreign_merges:
        problems.append("汇总分支含未批准合并提交：" + "; ".join(foreign_merges))
    if not changed_files:
        problems.append(f"{release_ref} 相对 {base} 没有任何文件改动")
    if develop_diff_files:
        problems.append(f"改动文件与 {develop} 内容不同（需 code-review 逐个核对）：" + ", ".join(develop_diff_files))

    return {
        "release_ref": release_ref,
        "release_sha": release_sha,
        "base_ref": base,
        "base_sha": base_sha,
        "approved_commits": unique_approved,
        "commits_in_release": commits_in_release,
        "same_tree_as_single_commit": same_tree,
        "changed_files": changed_files,
        "develop_ref": develop,
        "develop_sha": develop_sha,
        "develop_diff_files": develop_diff_files,
        "problems": problems,
    }


def format_release_check(report: dict[str, Any]) -> str:
    lines = [
        f"release: {report['release_ref']} ({report['release_sha']})",
        f"base: {report['base_ref']} ({report['base_sha']})",
        f"approved_commits: {', '.join(report['approved_commits'])}",
        f"commits_in_release ({len(report['commits_in_release'])}): "
        + ("; ".join(report["commits_in_release"]) if report["commits_in_release"] else "none"),
        f"same_tree_as_single_commit: {'yes' if report['same_tree_as_single_commit'] else 'no'}",
        f"changed_files ({len(report['changed_files'])}): "
        + (", ".join(report["changed_files"]) if report["changed_files"] else "none"),
    ]
    if report["develop_ref"]:
        dev_status = "matched" if not report["develop_diff_files"] else "diff: " + ", ".join(report["develop_diff_files"])
        lines.append(f"develop: {report['develop_ref']} ({report['develop_sha']}) | {dev_status}")
    if report["problems"]:
        lines.append("status: held")
        lines.extend(f"- {item}" for item in report["problems"])
    else:
        lines.append("status: ok")
    return "\n".join(lines)


# ---------- 根 issue 进度区 ----------

def progress_path(repo: str, task: str) -> Path:
    return state_path(repo, task).with_name("progress.json")


def in_release(host: str, project: str, sha: str, release: str) -> bool:
    """sha 是 release 的祖先（两者的共同祖先就是 sha 本身）才算已汇总；release 分支还没建也算没汇总。"""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RecordError(f"commits 里的 sha 要写完整 40 位：{sha}")
    path = f"projects/{quote(project, safe='')}/repository/merge_base?refs[]={sha}&refs[]={quote(release)}"
    try:
        base = _WR.glab_api(host, "GET", path)
    except _WR.WrapupError as error:
        if f"Could not find ref: {release}" in str(error):
            return False
        raise
    return str(base.get("id") or "") == sha


def render_steps(host: str, release: str, steps: Sequence[dict[str, Any]]) -> list[str]:
    lines = []
    for number, step in enumerate(steps, 1):
        title = " ".join(str(step.get("title") or "").split())
        if not title or len(title) > TITLE_LIMIT:
            raise RecordError(f"第 {number} 步标题要有、且不超过 {TITLE_LIMIT} 字（现在 {len(title)} 字）：分支、流水线、验收细节写评论")
        commits = step.get("commits") or []
        done = bool(commits) and all(in_release(host, c["project"], c["sha"], release) for c in commits)
        line = f"- [{'x' if done else ' '}] {number}. {title}"
        if done:
            line += f"（已在 {release}）"
        else:
            status = step.get("status", "待核实")
            if status not in ("待核实", "未开始", "进行中", "修复中", "待验证", "待汇总", "受阻"):
                raise RecordError(f"第 {number} 步的状态不支持：{status}")
            remaining = step.get("remaining", "")
            if not isinstance(remaining, str) or len(remaining) > 80 or len(remaining.splitlines()) > 1:
                raise RecordError(f"第 {number} 步的剩余事项限一行、80 字")
            if status == "受阻" and not remaining.strip():
                raise RecordError(f"第 {number} 步受阻，请说明在等什么")
            line = f"- [ ] {number}. **{status}**｜{title}"
            if remaining.strip():
                line += f"。{remaining.strip()}"
        if step.get("child"):
            line += f" → #{int(step['child'])}（子 issue）"
        if step.get("blocked_by") and not done:
            line += "（依赖步骤 " + "、".join(str(int(n)) for n in step["blocked_by"]) + "）"
        lines.append(line)
    return lines


def progress(repo: str, task: str, issue_url: Optional[str] = None, steps: Optional[dict[str, Any]] = None) -> str:
    """按 STATE 顶部和步骤重写根 issue 描述顶部的维护区，回读确认；返回 issue 链接。"""
    saved = progress_path(repo, task)
    record = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    if issue_url:
        record["issue"] = issue_url.split("#", 1)[0]
    if steps is not None:
        record["steps"] = steps.get("steps") or []
    if not record.get("issue") or not record.get("steps"):
        raise RecordError("第一次要带 --issue 和 --steps-file（steps 不能为空）")
    issue = _WR.parse_issue(record["issue"])
    release = f"release-{issue.iid}"
    top = read_top(state_path(repo, task).read_text(encoding="utf-8")) if state_path(repo, task).exists() else {}
    head = [f"**{label}**：{top.get(field) or '无'}" for field, label in PROGRESS_FIELDS.items()]
    body = render_steps(issue.host, release, record["steps"])
    api = f"projects/{quote(issue.project, safe='')}/issues/{issue.iid}"
    old = str(_WR.glab_api(issue.host, "GET", api).get("description") or "")
    found = AREA.search(old)
    release_table = EMPTY_RELEASE
    if found:
        table = re.search(r"## 上线清单\n.*?(?=\n+---\n|\n*<!-- hwf:end)", found.group(0), re.S)
        release_table = table.group(0).strip() if table else EMPTY_RELEASE
    original = AREA.sub("", old, count=1).strip("\n")
    area = "\n".join([AREA_OPEN, "## 进度", "", "  \n".join(head), "",
                      f"_更新于 {time.strftime('%Y-%m-%d %H:%M')}；勾选表示代码已进 `{release}`，细节见评论_", "",
                      *body, "", release_table, "", "---", AREA_CLOSE])
    new = area + ("\n\n" + original if original else "") + "\n"
    if new.strip() != old.strip():
        _WR.glab_api(issue.host, "PUT", api, f"description={new}")
        back = str(_WR.glab_api(issue.host, "GET", api).get("description") or "")
        if back.strip() != new.strip():
            raise RecordError(f"进度区回读不一致：{issue.url}")
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return issue.url


# ---------- 评论 ----------

def format_kind_tag(kind: str) -> str:
    """在 GitLab 评论末尾附隐藏类别标记，供秘书按免打扰/详尽模式精确分流；飞书渲染前会剥离该标记。"""
    return f"<!-- hwf-kind:{kind} -->"


def note(repo: str, task: str, issue_url: str, kind: str, body: str, wills: Sequence[str]) -> tuple[int, str]:
    body = KIND_TAG.sub("", body).strip()
    if not body:
        raise RecordError("评论正文是空的")
    mode = notify_mode(repo, task)
    if kind == "stage" and mode == QUIET:
        return EXIT_HELD, "没发：现在是免打扰模式，阶段进度只写 STATE，等步骤完成时一起发。"
    if kind == "todo" and not wills:
        raise RecordError("todo 必须带 --will：写明用户同意后你要做的动作，只需要用户自己做就写 --will none")
    results = preflight(wills)
    checked_mrs = {w.partition("=")[2] for w in wills if w.startswith("merge=")}
    body_problems = [] if kind == "record" else body_autoclose_problems(body, checked_mrs)
    autoclose = [text for verdict, text in results if verdict == "autoclose"] + body_problems
    if autoclose:
        return EXIT_HELD, "没发：发现 MR 夹带自动关单关键词，点合并会擅自关掉 issue：\n- " + "\n- ".join(autoclose)
    blocked = [text for verdict, text in results if verdict == "no"]
    if blocked:
        return EXIT_HELD, "没发：下面的动作本账号做不了，不要请用户同意，改成请用户来做（--will none）：\n- " + "\n- ".join(blocked)
    unknown = [text for verdict, text in results if verdict == "unknown"]
    if unknown:
        body += "\n\n自查没查出来的：\n" + "\n".join(f"- {text}" for text in unknown)
    log = state_path(repo, task).with_name("notes.jsonl")
    # 摘要仍对不含隐藏标签的正文计算，兼容历史 notes.jsonl 去重记录。
    digest = hashlib.sha256(body.encode()).hexdigest()
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    same = next((item for item in sent if item["sha256"] == digest and item["issue"] == issue_url), None)
    if same:
        return EXIT_HELD, f"没发：同样的内容已经发过 {same['link']}"
    link = _WR.post_note(_WR.parse_issue(issue_url), f"{body}\n\n{format_kind_tag(kind)}")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "issue": issue_url, "kind": kind,
                                 "link": link, "sha256": digest}, ensure_ascii=False) + "\n")
    # 免打扰下 step-done 与 record 仅留 GitLab 存档，不更新「最近用户通知」，以免冲掉上一条真正发给用户的待办或答复记录。
    if kind != "record" and (mode == VERBOSE or kind in QUIET_NOTIFY_KINDS):
        state_set(repo, task, "最近用户通知", f"{link} 类别={KINDS[kind]}")
    return EXIT_OK, link


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True, help="主仓库路径（INDEX 与 STATE 所在）")
    common.add_argument("--task", required=True, help="任务标识，如 frontend-web-v1-478")
    add = sub.add_parser("index-add", parents=[common])
    add.add_argument("--input", required=True, help="输入原文")
    add.add_argument("--status", default="in-progress")
    add.add_argument("--queued", action="store_true", help="排队行：调度者列留空，等下一个入口领取")
    add.add_argument("--scheduler", help="调度者登记名；默认取当前 pane 上的调度者")
    change = sub.add_parser("index-set", parents=[common])
    change.add_argument("--status", required=True)
    change.add_argument("--scheduler")
    top = sub.add_parser("state-set", parents=[common])
    top.add_argument("--field", required=True, choices=TOP_FIELDS)
    top.add_argument("--value", required=True)
    post = sub.add_parser("note", parents=[common])
    post.add_argument("--issue", required=True)
    post.add_argument("--kind", required=True, choices=sorted(KINDS))
    post.add_argument("--body-file", required=True)
    post.add_argument("--will", action="append", default=[])
    pre = sub.add_parser("preflight")
    pre.add_argument("--will", action="append", required=True)
    card = sub.add_parser("card-check")
    card.add_argument("--file", required=True, help="arch §0 判定的落盘文件")
    rel = sub.add_parser("release-check")
    rel.add_argument("--repo", required=True, help="仓库或工作树路径")
    rel.add_argument("--release", required=True, help="汇总分支名或引用，如 release-47")
    rel.add_argument("--base", default="origin/main", help="上线基准引用，默认 origin/main")
    rel.add_argument("--develop", help="测试分支引用，如 origin/develop")
    rel.add_argument("--commit", action="append", required=True, help="已批准的步骤源 SHA（可多次传入）")
    rel.add_argument("--no-fetch", action="store_true", help="跳过 git fetch origin")
    prog = sub.add_parser("progress", parents=[common])
    prog.add_argument("--issue", help="根 issue URL；第一次必填")
    prog.add_argument("--steps-file", help="步骤 JSON；第一次或步骤变化时给")
    opts = parser.parse_args(argv)
    try:
        if opts.command == "index-add":
            print(index_add(opts.repo, opts.task, opts.input, opts.status, opts.queued, opts.scheduler))
        elif opts.command == "index-set":
            index_set(opts.repo, opts.task, opts.status, opts.scheduler)
            print(f"INDEX 已改为：{opts.status}")
        elif opts.command == "state-set":
            state_set(opts.repo, opts.task, opts.field, opts.value)
            print(f"STATE 顶部「{opts.field}」已更新")
        elif opts.command == "note":
            code, text = note(opts.repo, opts.task, opts.issue, opts.kind,
                              Path(opts.body_file).read_text(encoding="utf-8"), opts.will)
            print(text)
            return code
        elif opts.command == "progress":
            steps = json.loads(Path(opts.steps_file).read_text(encoding="utf-8")) if opts.steps_file else None
            print(f"进度区已更新并回读：{progress(opts.repo, opts.task, opts.issue, steps)}")
        elif opts.command == "card-check":
            problems = card_check(Path(opts.file).read_text(encoding="utf-8"))
            for problem in problems:
                print(f"缺：{problem}")
            if problems:
                return EXIT_HELD
            print("需求理解卡齐全，验收条件：")
            sections = card_sections(Path(opts.file).read_text(encoding="utf-8"))
            print(sections["验收条件"])
            print("验收前提：")
            print(sections["验收前提"])
        elif opts.command == "release-check":
            report = release_check(opts.repo, opts.release, opts.base, opts.commit,
                                   develop=opts.develop, fetch=not opts.no_fetch)
            print(format_release_check(report))
            return EXIT_HELD if report["problems"] else EXIT_OK
        else:
            results = preflight(opts.will)
            labels = {"ok": "能做", "no": "做不了", "autoclose": "禁止（会自动关单）", "unknown": "无法确认"}
            for verdict, text in results:
                print(f"{labels[verdict]}：{text}")
            return EXIT_HELD if any(verdict != "ok" for verdict, _ in results) else EXIT_OK
    except (RecordError, _WR.WrapupError, OSError, ValueError, KeyError) as error:
        print(f"task-record failed: {error}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
