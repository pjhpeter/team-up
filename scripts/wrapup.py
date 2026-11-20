#!/usr/bin/env python3
"""交付收尾一条命令版：按 ~/.hwf/protocol/delivery.md §9「成员收尾与收尾回执」的顺序做完收尾，全部通过才把 INDEX 改成收尾状态。

    python3 ~/.hwf/scripts/wrapup.py --repo <主仓库> --task <任务标识> --issue <发起 issue URL（没有则根 issue）> \\
        --release <release-<根编号>> --status "done（…）" [--port N]... [--dry-run]

为什么有它：2026-11-13 #478 调度者把 INDEX 写成 done，却没关成员、没删 worktree、没发收尾回执，10 个成员一直挂着。
它把「成员还在用 worktree」当成保留成员的理由，顺序弄反了。收尾几步全凭调度者读协议记着做，漏一步没人拦。
所以 INDEX 的收尾状态只能由本脚本写，调度者不再手改。

步骤（某一步失败也继续做完能做的，最后如实写回执、不改 INDEX、退出码 1）：
1. stop-test-services.py 关闭本任务 worktree 里的测试服务，回读端口空闲。
2. 关闭本调度者名下的成员 pane：登记簿 members.json 记在本调度者名下的，加上本调度者 tab 里未登记的 i<编号>-* 成员。
   调度者名下还有别的未收尾任务（同批兄弟任务）时，只关 i<编号>-*。所在 workspace 只剩这一个 pane 时保留
   （关掉会连带关闭 workspace）。只认还在运行、登记名符合成员格式的 agent，不碰调度者、用户 pane 与当前 pane。
   关完回读 pane list / agent list，再跑一次 stop-test-services，防止成员退出后服务成了孤儿进程。
3. 本任务 worktree（<worktree 根>/*/<任务标识>-*/，含后端等其他仓库，以及嵌在它们里面的）同时满足 没有未保存内容
   （工作区与 HEAD 一致、无未跟踪文件、暂存内容在历史里）、HEAD 已在任一远端分支（或按内容进了 release/main）、
   没有 pane 在用 时 git worktree remove --force，回读 git worktree list；不满足的保留并写明原因（保留不算失败）。
   只进了 develop 的集成目录也删：#488 收尾时它们因不在 release/main 被一直留着，磁盘越积越多。
4. test-account.py release --task 释放本任务的测试账号。
5. 在 issue 发「收尾回执」评论并回读，同时追加到 STATE.md。
6. 给了 --close-request（用户明确要求关闭的那条评论）且以上全部通过，才关闭 --issue 并回读；没给就不关。
   调度者不得用 glab/API 自己关闭发起 issue：#481 调度者上下文压缩后忘了规则，没人要求就关了，收尾也是手写的。
7. 以上全部通过，才把 INDEX 本任务行的状态改成 --status。

退出码：0 收尾完成、INDEX 已改；1 有残留或失败（INDEX 未改，调度者按 ~/.hwf/protocol/communication.md「需要用户确认」通知用户）；2 参数错误。
--dry-run 只打印将做什么：不关服务、不关 pane、不删 worktree、不释放账号、不发评论、不改 INDEX。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Optional, Sequence
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # find-scheduler 的 dataclass 需要能从 sys.modules 找到所属模块
    spec.loader.exec_module(module)
    return module


_FS = _load("find_scheduler", "find-scheduler.py")
_MS = _load("member_start", "member-start.py")

CLOSE_WAIT_S = 15
MEMBER_NAME = re.compile(r"(i\d+|new)-[a-z0-9_-]+|delivery-(?!scheduler-)[a-z0-9_-]+")


class WrapupError(RuntimeError):
    pass


def worktree_root() -> Path:
    return Path(os.environ.get("HWF_WORKTREE_ROOT") or Path.home() / ".herdr/worktrees").resolve()


def herdr(*args: str, timeout: int = 30) -> dict[str, Any]:
    return _MS.herdr(*args, timeout=timeout)


def run(argv: Sequence[str], cwd: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def git(path: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", path, *args])


def run_script(filename: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(SCRIPTS / filename), *args], timeout=300)


def glab_api(host: str, method: str, path: str, *fields: str) -> Any:
    argv = ["glab", "api", "--hostname", host, "-X", method, path]
    for item in fields:
        argv += ["--raw-field", item]
    result = run(argv, timeout=60)
    if result.returncode != 0:
        raise WrapupError(f"glab api {method} {path} 失败：{result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


# ---------- 前置核对：不通过就什么都不做 ----------

@dataclass
class Issue:
    url: str
    host: str
    project: str
    iid: str


def parse_issue(url: str) -> Issue:
    match = re.match(r"https?://(?P<host>[^/]+)/(?P<project>.+?)/-/(?:issues|work_items)/(?P<iid>\d+)", url)
    if not match:
        raise WrapupError(f"--issue 不是 GitLab issue 链接：{url}")
    return Issue(url.split("#", 1)[0], match["host"], match["project"], match["iid"])


def current_scheduler(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    pane = os.environ.get("HERDR_PANE_ID")
    me = next((a for a in herdr("agent", "list")["result"]["agents"] if a.get("pane_id") == pane), None)
    name = str((me or {}).get("name") or "")
    if not name.startswith(_FS.NAME_PREFIX):
        raise WrapupError(f"当前 pane {pane} 上不是已登记的调度者；从调度者 pane 运行，或用 --scheduler 指定登记名")
    return name


def owner_row(repo: str, task: str) -> Any:
    rows = [row for row in _FS.read_index(repo) if row.task == task]
    if not rows:
        raise WrapupError(f"{repo}/{_FS.INDEX_RELATIVE} 里没有任务 {task}")
    return rows[-1]  # 同一任务被接手时会追加新行，最后一行是现在的负责人


def check_close_request(issue: Issue, link: str) -> str:
    """--close-request 必须是本 issue 下由真人（不是本 Agent 账号）发的评论；返回发评论的人。"""
    match = re.fullmatch(r"(?P<issue>.+)#note_(?P<note>\d+)", link.strip())
    if not match:
        raise WrapupError(f"--close-request 要写用户要求关闭的那条评论链接（…#note_<编号>）：{link}")
    target = parse_issue(match["issue"])
    if (target.host, target.project, target.iid) != (issue.host, issue.project, issue.iid):
        raise WrapupError(f"--close-request 的评论不在 {issue.url} 下：{link}")
    me = str(glab_api(issue.host, "GET", "user").get("username") or "")
    note = glab_api(issue.host, "GET", f"projects/{quote(issue.project, safe='')}/issues/{issue.iid}/notes/{match['note']}")
    author = str((note.get("author") or {}).get("username") or "")
    if note.get("system") or not author or author == me:
        raise WrapupError(f"--close-request 必须是用户本人发的评论，这条是 {author or '系统'} 发的：{link}")
    return author


def close_issue(issue: Issue) -> None:
    path = f"projects/{quote(issue.project, safe='')}/issues/{issue.iid}"
    glab_api(issue.host, "PUT", path, "state_event=close")
    state = glab_api(issue.host, "GET", path).get("state")
    if state != "closed":
        raise WrapupError(f"关闭 {issue.url} 后回读状态是 {state}")


# ---------- 第 1 步：测试服务 ----------

@dataclass
class Services:
    ok: bool
    summary: str
    output: str


def stop_services(worktrees: Sequence[str], ports: Sequence[int], dry_run: bool) -> Services:
    if not worktrees:
        note = f"（未回读端口 {', '.join(map(str, ports))}）" if ports else ""
        return Services(True, f"本任务没有 worktree，无需关闭{note}", "")
    args = [*worktrees, *(f"--port={p}" for p in ports)] + (["--dry-run"] if dry_run else [])
    result = run_script("stop-test-services.py", *args)
    out = result.stdout + result.stderr
    stopped = re.findall(r"^stop: pid=(\d+) ports=\[([^\]]*)\]", out, re.M)
    free = re.findall(r"^port (\d+) free", out, re.M)
    residue = re.findall(r"^RESIDUE: (.+)$", out, re.M)
    if dry_run:
        return Services(True, f"将执行 stop-test-services.py（预演输出见上）", out)
    if result.returncode != 0:
        return Services(False, f"仍有残留：{'；'.join(residue) or out.strip()[-300:]}（stop-test-services.py 退出码 "
                               f"{result.returncode}）", out)
    closed = "、".join(f"pid {pid}（端口 {ports_text or '无'}）" for pid, ports_text in stopped) or "无在跑的服务"
    checked = f"端口回读 {'、'.join(free)} 全部空闲" if free else "worktree 内无监听进程"
    return Services(True, f"已关闭 {closed}；{checked}（stop-test-services.py 退出码 0）", out)


# ---------- 第 2 步：成员 pane ----------

@dataclass
class Members:
    closed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def team_tabs(scheduler: str, agents: Sequence[dict[str, Any]], registry: dict[str, Any]) -> set[str]:
    tabs = {a.get("tab_id") for a in agents if a.get("name") == scheduler}
    tabs |= {entry.get("tab_id") for entry in registry.values() if entry.get("scheduler") == scheduler}
    return {t for t in tabs if t}


def pick_members(scheduler: str, iid: str, only_issue: bool, agents: Sequence[dict[str, Any]],
                 registry: dict[str, Any], skip_panes: set[str],
                 panes: Optional[Sequence[dict[str, Any]]] = None,
                 problems: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """同时检查登记窗格和运行中的 Agent；身份不明的登记项必须阻止完成，不能静默漏掉。"""
    tabs = team_tabs(scheduler, agents, registry)
    live = {str(p["pane_id"]): dict(p) for p in (panes if panes is not None else agents)}
    for agent in agents:
        pane = str(agent.get("pane_id") or "")
        live[pane] = live.get(pane, {}) | agent
    picked = []
    for pane, member in live.items():
        name = str(member.get("name") or "")
        if not pane or pane in skip_panes:
            continue
        entry = registry.get(pane) or {}
        owner = entry.get("scheduler")
        named = bool(MEMBER_NAME.fullmatch(name))
        label = str(member.get("label") or "")
        label_issue = re.search(r"#(\d+)(?!\d)", label)
        this_issue = name.startswith(f"i{iid}-") if named else bool(label_issue and label_issue[1] == iid)
        if owner != scheduler and not (owner is None and named and this_issue and member.get("tab_id") in tabs):
            continue
        if only_issue and not this_issue and (named or label_issue):
            continue
        reason = ""
        if not named:
            if name or not label_issue or not label.startswith(str(entry.get("profile") or "") + "-") or member.get("tab_id") != entry.get("tab_id"):
                reason = "登记窗格的当前身份无法确认"
            elif member.get("agent") and member.get("agent_status") not in ("idle", "done"):
                reason = "无名 Agent 尚未结束或状态不明"
            elif only_issue and not this_issue:
                reason = "无法确定登记窗格属于本次 issue"
        if reason:
            if problems is None:
                raise WrapupError(f"{pane}：{reason}")
            problems.append(f"{pane}：{reason}，保留并核实后重跑")
            continue
        picked.append(member | {"name": name or label})
    return picked


def close_members(members: Sequence[dict[str, Any]], dry_run: bool) -> Members:
    result = Members()
    panes = herdr("pane", "list")["result"]["panes"]
    left: dict[str, int] = {}
    for pane in panes:
        left[pane.get("workspace_id")] = left.get(pane.get("workspace_id"), 0) + 1
    to_close = []
    for member in members:
        label = f"{member.get('name')} / {member['pane_id']}"
        workspace = member.get("workspace_id")
        if left.get(workspace, 0) <= 1:
            result.kept.append(f"{label}（是 workspace {workspace} 最后一个 pane，关掉会连带关闭 workspace）")
            continue
        left[workspace] -= 1
        to_close.append(member)
    if dry_run:
        result.closed = [f"{m.get('name')} / {m['pane_id']}" for m in to_close]
        return result
    for member in to_close:
        try:
            herdr("pane", "close", member["pane_id"])
        except RuntimeError as error:
            result.problems.append(f"{member.get('name')} / {member['pane_id']} 关闭失败：{error}")
    targets = {m["pane_id"] for m in to_close}
    deadline = time.time() + CLOSE_WAIT_S
    while True:
        alive_panes = {p.get("pane_id") for p in herdr("pane", "list")["result"]["panes"]}
        alive_agents = {a.get("pane_id") for a in herdr("agent", "list")["result"]["agents"]}
        still = targets & (alive_panes | alive_agents)
        if not still or time.time() >= deadline:
            break
        time.sleep(1)
    for member in to_close:
        label = f"{member.get('name')} / {member['pane_id']}"
        if member["pane_id"] in still:
            if not any(label in p for p in result.problems):
                result.problems.append(f"{label} 关闭后回读仍在")
        else:
            result.closed.append(label)
    forget(m["pane_id"] for m in to_close if m["pane_id"] not in still)
    return result


def forget(panes: Any) -> None:
    gone = set(panes)
    if not gone:
        return
    data = _MS.read_registry()
    kept = {pane: entry for pane, entry in data.items() if pane not in gone}
    if len(kept) == len(data):
        return
    path = _MS.registry_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------- 第 3 步：worktree ----------

@dataclass
class Worktree:
    repo: str
    path: str
    branch: Optional[str]
    head: str
    keep: Optional[str] = None  # 保留原因；None 表示可以删除
    branch_note: str = ""
    bare: bool = False


def list_worktrees(repo: str) -> list[tuple[str, str, Optional[str], bool]]:
    result = git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise WrapupError(f"git worktree list 失败（{repo}）：{result.stderr.strip()}")
    found, path, head, branch, bare = [], "", "", None, False
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path, head, branch, bare = line[len("worktree "):], "", None, False
        elif line == "bare":
            bare = True
        elif line.startswith("HEAD "):
            head = line[5:]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif not line and path:
            found.append((os.path.realpath(path), head, branch, bare))
            path = ""
    return found


def _belongs_to_repo_tree(wt_repo: str, main_repo: Optional[str]) -> bool:
    if not main_repo:
        return False
    real_wt = os.path.realpath(wt_repo)
    real_main = os.path.realpath(main_repo)
    return real_wt == real_main or real_wt.startswith(real_main + os.sep)


def task_worktrees(task: str, main_repo: Optional[str] = None, iid: Optional[str] = None) -> list[Worktree]:
    """查找本任务的工作树：
    1. `<worktree 根>/<仓库>/<任务标识>` 及 `<任务标识>-<用途>`（精确边界，不误匹配 proj-470）；
    2. 归属主仓库或其子目录后端仓库的 `issue-<编号>` / `issue-<编号>-<用途>`；
    3. 嵌套在上述目录内的子 worktree（如基线存证目录）。
    """
    root = worktree_root()
    task_candidates: list[tuple[str, bool]] = []
    if root.is_dir():
        for repo_dir in sorted(root.iterdir()):
            if not repo_dir.is_dir():
                continue
            for sub in sorted(repo_dir.iterdir()):
                if not sub.is_dir():
                    continue
                name = sub.name
                if name == task or name.startswith(f"{task}-"):
                    task_candidates.append((os.path.realpath(sub), False))
                elif iid and (name == f"issue-{iid}" or name.startswith(f"issue-{iid}-")):
                    task_candidates.append((os.path.realpath(sub), True))

    accepted_tops: list[str] = []
    entries_by_top: list[tuple[str, str, list[tuple[str, str, Optional[str], bool]]]] = []
    for top, require_repo_match in task_candidates:
        try:
            entries = list_worktrees(top)
        except WrapupError as error:
            print(f"跳过不是 git worktree 的目录 {top}：{error}")
            continue
        wt_repo = entries[0][0]  # git worktree list 第一项总是主工作树
        if require_repo_match and not _belongs_to_repo_tree(wt_repo, main_repo):
            continue
        accepted_tops.append(top)
        entries_by_top.append((top, wt_repo, entries))

    found: dict[str, Worktree] = {}
    for _, wt_repo, entries in entries_by_top:
        for path, head, branch, bare in entries:
            if path not in found and any(path == t or path.startswith(t + os.sep) for t in accepted_tops):
                found[path] = Worktree(wt_repo, path, branch, head, bare=bare)
    picked = list(found.values())
    # 先删里层：外层 worktree remove 会连带删掉目录，里层的 git 登记就成了悬空记录
    picked.sort(key=lambda w: w.path.count(os.sep), reverse=True)
    return picked


def describe_branch(wt: Worktree) -> None:
    if wt.bare:
        return
    if not wt.branch:
        wt.branch_note = f"{wt.path}：游离 HEAD {wt.head[:10]}，无分支"
        return
    upstream = git(wt.path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        # 没设上游不等于没推送：推送时没加 -u 也常见，看远端分支里有没有当前 HEAD
        remote = git(wt.path, "branch", "-r", "--contains", "HEAD", "--format=%(refname:short)").stdout.split()
        wt.branch_note = (f"{wt.branch}：未设上游，HEAD 已在 {'、'.join(remote[:3])}" if remote
                          else f"{wt.branch}：未推送（远端分支里都没有当前 HEAD）")
        return
    ahead = git(wt.path, "rev-list", "--count", "@{u}..HEAD").stdout.strip() or "?"
    wt.branch_note = (f"{wt.branch}：已推送 {upstream.stdout.strip()}" if ahead == "0"
                      else f"{wt.branch}：有 {ahead} 个提交未推送到 {upstream.stdout.strip()}")


def shipped(path: str, ref: str) -> bool:
    """HEAD 是 ref 的祖先，或 HEAD 独有的提交都能在 ref 里按内容（patch-id）找到——cherry-pick 进去的也算。"""
    if git(path, "merge-base", "--is-ancestor", "HEAD", ref).returncode == 0:
        return True
    cherry = git(path, "cherry", ref, "HEAD")
    return cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines())


def saved_ref(path: str, release: str) -> Optional[str]:
    """返回保存了 HEAD 的远端分支：先认 release/main（含按内容 cherry-pick 进去的），再认任意远端分支。"""
    for ref in (release, "main"):
        if shipped(path, f"origin/{ref}"):
            return f"origin/{ref}"
    remote = git(path, "branch", "-r", "--contains", "HEAD", "--format=%(refname:short)").stdout.split()
    # origin/HEAD 的 short 名是 "origin"，它只是指向别的分支，不单独算
    remote = [r for r in remote if r.startswith("origin/") and not r.endswith("/HEAD")]
    return remote[0] if remote else None


def unsaved_reason(path: str) -> Optional[str]:
    """工作区内容和 HEAD 一致、没有未跟踪文件、暂存区里的内容也都在历史里，才算没有未保存的东西。"""
    tracked = git(path, "diff", "--quiet", "HEAD")
    if tracked.returncode == 1:
        return "有未提交改动"
    if tracked.returncode != 0:
        return f"git diff 失败：{tracked.stderr.strip()}"
    if git(path, "ls-files", "--others", "--exclude-standard").stdout.strip():
        return "有未跟踪文件"
    # 只有暂存区和 HEAD 不同（例如把旧版本 checkout 进暂存区后工作区又改回）时，暂存的版本若在该文件历史里就不是独有内容
    for line in git(path, "diff", "--cached", "--raw", "--no-abbrev").stdout.splitlines():
        meta, _, name = line.partition("\t")
        fields = meta.split()
        blob = fields[3] if len(fields) > 3 else ""
        if not blob or set(blob) == {"0"}:
            continue
        if blob not in git(path, "log", "--raw", "--no-abbrev", "--format=", "--", name).stdout:
            return "暂存区有未提交的内容"
    return None

def judge_worktrees(worktrees: Sequence[Worktree], release: str, panes: Sequence[dict[str, Any]],
                    services_ok: bool) -> list[str]:
    problems = []
    fetched: dict[str, bool] = {}
    for wt in worktrees:
        if wt.repo not in fetched:
            fetched[wt.repo] = git(wt.repo, "fetch", "--quiet", "origin").returncode == 0
            if not fetched[wt.repo]:
                problems.append(f"git fetch origin 失败（{wt.repo}），无法判断 worktree 能否删除")
        describe_branch(wt)
        if wt.bare:
            wt.keep = "是裸仓库，不是 worktree，脚本不处理"
            continue
        users = [p.get("pane_id") for p in panes
                 if any(str(p.get(k) or "") and (os.path.realpath(str(p[k])) == wt.path
                                                 or os.path.realpath(str(p[k])).startswith(wt.path + os.sep))
                        for k in ("cwd", "foreground_cwd"))]
        if not services_ok:
            wt.keep = "测试服务没关干净，先不删"
        elif not fetched[wt.repo]:
            wt.keep = "取不到远端分支，无法核对代码是否已保存到远端"
        elif reason := unsaved_reason(wt.path):
            wt.keep = reason
        elif not saved_ref(wt.path, release):
            wt.keep = f"HEAD {wt.head[:10]} 不在任何远端分支里（按内容比对 origin/{release}、origin/main 也没有）"
        elif users:
            wt.keep = f"还有 pane 在用：{', '.join(map(str, users))}"
    return problems


def remove_worktrees(worktrees: Sequence[Worktree]) -> tuple[list[str], list[str]]:
    removed, problems = [], []
    for wt in worktrees:
        if wt.keep:
            continue
        result = git(wt.repo, "worktree", "remove", "--force", wt.path)
        if result.returncode != 0:
            problems.append(f"{wt.path} 删除失败：{result.stderr.strip()}")
            wt.keep = "删除失败"
    for repo in {wt.repo for wt in worktrees}:
        git(repo, "worktree", "prune")
        listed = {entry[0] for entry in list_worktrees(repo)}
        for wt in worktrees:
            if wt.repo != repo or wt.keep:
                continue
            if wt.path in listed or Path(wt.path).exists():
                problems.append(f"{wt.path} 删除后回读仍在")
                wt.keep = "删除后回读仍在"
            else:
                removed.append(wt.path)
    return removed, problems


# ---------- 第 5、6 步：回执、STATE、INDEX ----------

def receipt_text(services: Services, members: Members, removed: Sequence[str], worktrees: Sequence[Worktree],
                 accounts: str, dry_run: bool = False) -> str:
    closed = "将关闭 {}" if dry_run else "已关闭 {}；agent list 回读已无这些成员"
    member_line = closed.format("、".join(members.closed)) if members.closed else "本交付没有在运行的成员"
    if members.kept:
        member_line += f"；保留 {'、'.join(members.kept)}"
    if members.problems:
        member_line += f"；**残留**：{'；'.join(members.problems)}"
    kept = [f"{wt.path}（{wt.keep}）" for wt in worktrees if wt.keep]
    branches = [wt.branch_note for wt in worktrees if wt.branch_note]
    return "\n".join([
        "**收尾回执**（wrapup.py 生成）",
        f"- 测试服务：{services.summary}",
        f"- 成员：{member_line}",
        f"- worktree：{'将删除' if dry_run else '已删除'} {'、'.join(removed) or '无'}；保留 {'、'.join(kept) or '无'}",
        f"- 分支：{'；'.join(branches) or '本任务没有 worktree 分支'}",
        f"- 测试账号：{accounts}",
    ])


def post_note(issue: Issue, body: str) -> str:
    """发一条评论并回读，返回评论链接。"""
    base = f"projects/{quote(issue.project, safe='')}/issues/{issue.iid}/notes"
    note = glab_api(issue.host, "POST", base, f"body={body}")
    note_id = note.get("id")
    if not note_id:
        raise WrapupError(f"发评论后没拿到评论编号：{note}")
    back = glab_api(issue.host, "GET", f"{base}/{note_id}")
    if str(back.get("body") or "").strip() != body.strip():
        raise WrapupError(f"评论回读内容不一致：{issue.url}#note_{note_id}")
    return f"{issue.url}#note_{note_id}"


def append_state(repo: str, task: str, body: str, link: str) -> None:
    path = Path(repo) / ".hwf/tmp" / task / "STATE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## 收尾回执（{stamp}）\n\n{body}\n\n- 回执评论：{link}\n")


def write_index_status(repo: str, task: str, scheduler: str, status: str) -> None:
    path = Path(repo) / _FS.INDEX_RELATIVE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    cell = " " + status.replace("\n", " ").replace("|", "\\|") + " "
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        parts = _FS.index_cells(line)
        # 读的一侧（read_index）认 5 段，这里也得认：2026-11-15 #483 手写的行末尾少了「 |」，
        # 能找到负责人却改不了状态，收尾停在最后一步。
        if parts is None or len(parts) < 5 or _FS.task_of(parts) != task:
            continue
        if len(parts) == 5:
            parts.append("\n" if line.endswith("\n") else "")
        if scheduler not in _FS.scheduler_names(parts[3]):
            raise WrapupError(f"INDEX 里 {task} 的调度者不是 {scheduler}，不改")
        parts[4] = cell
        lines[i] = "|".join(parts)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(path)
        return
    raise WrapupError(f"INDEX 里找不到 {task} 的行")


# ---------- 主流程 ----------

def wrapup(opts: argparse.Namespace) -> int:
    repo = os.path.realpath(opts.repo)
    issue = parse_issue(opts.issue)
    if not opts.status.strip().lower().startswith(_FS.TERMINAL_STATUS):
        raise WrapupError(f"--status 必须以 {'/'.join(_FS.TERMINAL_STATUS)} 开头：{opts.status}")
    scheduler = current_scheduler(opts.scheduler)
    row = owner_row(repo, opts.task)
    if row.scheduler != scheduler:
        raise WrapupError(f"INDEX 里 {opts.task} 的调度者是 {row.scheduler or '（空）'}，不是 {scheduler}；只能收尾自己的任务")
    tag = "【预演】" if opts.dry_run else ""
    problems: list[str] = []
    requester = check_close_request(issue, opts.close_request) if opts.close_request else ""

    worktrees = task_worktrees(opts.task, repo, issue.iid)
    paths = [wt.path for wt in worktrees if not wt.bare]
    print(f"{tag}本任务 worktree：{', '.join(paths) or '无'}")

    services = stop_services(paths, opts.port, opts.dry_run)
    print(services.output.rstrip())
    print(f"{tag}1. 测试服务：{services.summary}")
    if not services.ok:
        problems.append(f"测试服务：{services.summary}")

    agents = herdr("agent", "list")["result"]["agents"]
    siblings = [r for r in _FS.read_index(repo) if r.scheduler == scheduler and r.task != opts.task and r.open]
    skip = {p for p in (os.environ.get("HERDR_PANE_ID"),) if p}
    skip |= {str(a.get("pane_id")) for a in agents if a.get("name") == scheduler}
    registry = _MS.read_registry()
    selection_problems: list[str] = []
    initial_panes = herdr("pane", "list")["result"]["panes"]
    picked = pick_members(scheduler, issue.iid, bool(siblings), agents, registry, skip,
                          initial_panes, selection_problems)
    members = close_members(picked, opts.dry_run)
    members.problems.extend(selection_problems)
    if not opts.dry_run:
        post_agents = herdr("agent", "list")["result"]["agents"]
        post_panes = herdr("pane", "list")["result"]["panes"]
        alive_ids = {str(p.get("pane_id")) for p in post_panes} | {str(a.get("pane_id")) for a in post_agents}
        dead_registered = [pid for pid in _MS.read_registry() if pid not in alive_ids]
        forget(dead_registered)
        remaining = pick_members(scheduler, issue.iid, bool(siblings),
                                 post_agents, _MS.read_registry(), skip,
                                 post_panes, members.problems)
        for member in remaining:
            if not any(member["pane_id"] in kept for kept in members.kept):
                members.problems.append(f"{member['pane_id']}：团队登记复核发现残留成员")
    print(f"{tag}2. 成员：{'将关闭' if opts.dry_run else '已关闭'} {'、'.join(members.closed) or '无'}"
          + (f"；保留 {'、'.join(members.kept)}" if members.kept else "")
          + (f"（调度者还有未收尾任务 {', '.join(r.task for r in siblings)}，只关 i{issue.iid}-*）" if siblings else ""))
    problems += [f"成员：{p}" for p in members.problems]
    if members.closed and not opts.dry_run and paths:
        again = stop_services(paths, opts.port, False)
        print(again.output.rstrip())
        if not again.ok:
            services = again
            problems.append(f"成员退出后测试服务：{again.summary}")

    panes = herdr("pane", "list")["result"]["panes"]
    if opts.dry_run:
        panes = [p for p in panes if p.get("pane_id") not in {m["pane_id"] for m in picked}]
    problems += judge_worktrees(worktrees, opts.release, panes, services.ok)
    if opts.dry_run:
        removed = [wt.path for wt in worktrees if not wt.keep]
    else:
        removed, failed = remove_worktrees(worktrees)
        problems += failed
    kept = [f"{wt.path}（{wt.keep}）" for wt in worktrees if wt.keep]
    print(f"{tag}3. worktree：{'将删除' if opts.dry_run else '已删除'} {'、'.join(removed) or '无'}；"
          f"保留 {'、'.join(kept) or '无'}")

    if opts.dry_run:
        accounts = "将执行 test-account.py release"
    else:
        released = run_script("test-account.py", "release", "--task", opts.task)
        try:
            count = len(json.loads(released.stdout).get("released") or [])
        except (ValueError, AttributeError):
            count = None
        # 回执发在 issue 里，只写个数，不写账号（手机号/邮箱）
        accounts = (f"已释放 {count} 个" if released.returncode == 0 and count is not None
                    else f"释放失败（test-account.py 退出码 {released.returncode}）")
        if released.returncode != 0 or count is None:
            problems.append(f"测试账号：{accounts}")
    print(f"{tag}4. 测试账号：{accounts}")

    body = receipt_text(services, members, removed, worktrees, accounts, opts.dry_run)
    if requester:
        body += (f"\n- issue：{'收尾未完成，先不关闭' if problems else '按 @' + requester + ' 的要求关闭'}"
                 f"（{opts.close_request.strip()}）")
    if problems:
        body += "\n- **收尾未完成**：" + "；".join(problems)
    print(f"{tag}5. 收尾回执：\n{body}")
    if opts.dry_run:
        if requester:
            print(f"{tag}6. 将按 @{requester} 的要求关闭 {issue.url}")
        print(f"{tag}7. 将把 INDEX 中 {opts.task} 的状态改为：{opts.status}")
        return 0
    try:
        link = post_note(issue, body)
        print(f"回执评论：{link}")
        append_state(repo, opts.task, body, link)
    except (WrapupError, ValueError) as error:
        problems.append(f"回执：{error}")
        print(f"回执未发出：{error}", file=sys.stderr)
    if requester and not problems:
        try:
            close_issue(issue)
            print(f"6. 已按 @{requester} 的要求关闭 {issue.url}，回读 state=closed")
        except (WrapupError, ValueError) as error:
            problems.append(f"关闭 issue：{error}")
    if problems:
        print("收尾未完成，INDEX 未改。按 ~/.hwf/protocol/communication.md「需要用户确认」把残留告诉用户：\n- " + "\n- ".join(problems))
        return 1
    write_index_status(repo, opts.task, scheduler, opts.status)
    print(f"7. INDEX 已改为：{opts.status}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="主仓库路径（INDEX 与 STATE 所在）")
    parser.add_argument("--task", required=True, help="INDEX 里的任务标识，如 frontend-web-v1-478")
    parser.add_argument("--issue", required=True, help="发回执的 issue：发起 issue，没有则根 issue")
    parser.add_argument("--release", required=True, help="根 issue 的汇总分支，如 release-470")
    parser.add_argument("--status", required=True, help="收尾后写进 INDEX 的状态，以 done/delivered/closed/cancelled 开头")
    parser.add_argument("--port", type=int, action="append", default=[], help="本交付用过、需要回读的端口，可重复")
    parser.add_argument("--scheduler", help="调度者登记名；默认取当前 pane 上的调度者")
    parser.add_argument("--close-request", help="用户明确要求关闭 issue 的那条评论链接；给了才在收尾全部通过后关闭 --issue，"
                                                "不给就不关")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么，不做任何改动")
    opts = parser.parse_args(argv)
    try:
        return wrapup(opts)
    except (WrapupError, RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"wrapup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
