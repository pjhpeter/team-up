#!/usr/bin/env python3
"""调度者改任务记录、给用户发评论的唯一入口：格式和通知规则由脚本保证，不靠调度者记。

    task-record.py index-add --repo <主仓库> --task <任务标识> --input <输入原文> [--status in-progress] [--queued]
    task-record.py index-set --repo <主仓库> --task <任务标识> --status <状态>
    task-record.py state-set --repo <主仓库> --task <任务标识> --field <字段> --value <内容>
    task-record.py note      --repo <主仓库> --task <任务标识> --issue <issue URL> --kind <类别> --body-file <文件> [--will …]
    task-record.py preflight --will <动作>...

为什么有它：
- 2026-11-15 #483：调度者手写 INDEX 行没加反引号，收件箱找不到负责人，用户的回复晚到约 7 分钟。
- 同一天 #483：免打扰模式下仍单独发了 4 条阶段进度评论，每条都转成一条飞书消息。
- 同一天 #483：先问用户「同意合并吗」，用户同意后才发现本账号没有 main 的合并权限，白跑一轮。

INDEX：index-add 追加本调度者的一行（接手别人的任务也是追加，最后一行是现在的负责人）；--queued 追加调度者列留空的
排队行。index-set 只改本调度者自己那行的状态；收尾状态（done/delivered/closed/cancelled）只能由 wrapup.py 写。

STATE 顶部：state-set 维护 STATE.md 开头 `<!-- hwf:top -->` 标记之间的固定字段（见 TOP_FIELDS），其余内容不动。

评论：note 按 STATE 顶部的通知模式决定发不发。免打扰下 stage（阶段进度）不发，退出码 3，内容只写 STATE。
todo（要用户处理或授权）必须用 --will 写明用户同意后本账号要做的动作，脚本先自查做不做得了：
    --will merge=<MR URL>   --will push=<项目 URL>@<分支>   --will tag=<项目 URL>@<tag 名>   --will none（只需要用户自己做）
有一项做不了就不发，改成请用户来做；查不出来的在评论末尾注明。正文和已发过的完全一样时不发。
发出后回读，并把链接写进 STATE 顶部「最近用户通知」和 <任务目录>/notes.jsonl。

退出码：0 成功；1 失败；2 参数错误；3 按规则没发（免打扰拦下、重复、自查不通过）。
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
LEGACY_MODE = re.compile(r"通知模式[:：]\s*(免打扰|详尽)")
# 免打扰下只有这几类发；stage 是阶段进度，只在详尽模式发（PROTOCOL「通知模式」）。
# record 是登记或存档（登记池评论、待定清单评论），不是发给用户看进度的。
KINDS = {"todo": "用户待办", "risk": "新风险", "answer": "实质答复", "step-done": "步骤完成",
         "delivery-done": "整项交付完成", "stage": "阶段进度", "record": "登记或存档"}
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


# ---------- 自查：同意之后本账号做不做得了 ----------

def api_get(host: str, path: str) -> Any:
    return _WR.glab_api(host, "GET", path)


def access_level(host: str, project: str) -> int:
    perms = api_get(host, f"projects/{quote(project, safe='')}").get("permissions") or {}
    return max(int((perms.get(key) or {}).get("access_level") or 0) for key in ("project_access", "group_access"))


def allowed(level: int, rules: Sequence[dict[str, Any]]) -> bool:
    return any(0 < int(rule.get("access_level") or 0) <= level for rule in rules)


def check(will: str) -> tuple[str, str]:
    """返回（ok / no / unknown，给人看的一句话）。"""
    action, _, target = will.partition("=")
    if action == "merge":
        match = MR_URL.match(target)
        if not match:
            raise RecordError(f"--will merge= 后面要跟 MR 链接：{target}")
        mr = api_get(match["host"], f"projects/{quote(match['project'], safe='')}/merge_requests/{match['iid']}")
        if mr.get("state") != "opened":
            return "no", f"{target} 现在是 {mr.get('state')}，不用再合并"
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


# ---------- 评论 ----------

def note(repo: str, task: str, issue_url: str, kind: str, body: str, wills: Sequence[str]) -> tuple[int, str]:
    body = body.strip()
    if not body:
        raise RecordError("评论正文是空的")
    mode = notify_mode(repo, task)
    if kind == "stage" and mode == QUIET:
        return EXIT_HELD, "没发：现在是免打扰模式，阶段进度只写 STATE，等步骤完成时一起发。"
    if kind == "todo" and not wills:
        raise RecordError("todo 必须带 --will：写明用户同意后你要做的动作，只需要用户自己做就写 --will none")
    results = preflight(wills)
    blocked = [text for verdict, text in results if verdict == "no"]
    if blocked:
        return EXIT_HELD, "没发：下面的动作本账号做不了，不要请用户同意，改成请用户来做（--will none）：\n- " + "\n- ".join(blocked)
    unknown = [text for verdict, text in results if verdict == "unknown"]
    if unknown:
        body += "\n\n自查没查出来的：\n" + "\n".join(f"- {text}" for text in unknown)
    log = state_path(repo, task).with_name("notes.jsonl")
    digest = hashlib.sha256(body.encode()).hexdigest()
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    same = next((item for item in sent if item["sha256"] == digest and item["issue"] == issue_url), None)
    if same:
        return EXIT_HELD, f"没发：同样的内容已经发过 {same['link']}"
    link = _WR.post_note(_WR.parse_issue(issue_url), body)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "issue": issue_url, "kind": kind,
                                 "link": link, "sha256": digest}, ensure_ascii=False) + "\n")
    if kind != "record":
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
        else:
            results = preflight(opts.will)
            for verdict, text in results:
                print(f"{ {'ok': '能做', 'no': '做不了', 'unknown': '无法确认'}[verdict] }：{text}")
            return EXIT_HELD if any(verdict != "ok" for verdict, _ in results) else EXIT_OK
    except (RecordError, _WR.WrapupError, OSError, ValueError, KeyError) as error:
        print(f"task-record failed: {error}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
