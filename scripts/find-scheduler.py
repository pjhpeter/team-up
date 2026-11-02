#!/usr/bin/env python3
"""只读发现本仓库可复用的 Herdr 调度者，供 team-up 入口分支。

stdout / 退出码契约（team-up.yaml 依赖本契约，修改须同步）：
  exit 0 + stdout = 一个 pane id → 复用该调度者。stdout 只含 pane id，
           不带换行、空白或任何其他文本（直接渲染为 agent target）。
  exit 0 + stdout 为空           → 没有空闲调度者，入口新建一个（允许多个调度者并行，2026-10-22 用户指示）。
           跳过的调度者及原因写到 stderr，仅供排查。
  其他退出码（1；argparse 参数错误为 2） → 发现失败，fail closed；stdout 一定为空。

`--owner-of <issue URL>`：供 GitLab 收件箱路由评论，stdout 输出 JSON
  {"task": 任务标识|null, "pane": 在线调度者 pane|null}，表示哪个未收尾交付负责该 issue。

空闲 = herdr 状态 idle/done，上报目录全部是本仓库，且名下没有占用中的任务。占用中 = `.hwf/tmp/INDEX.md`
里未收尾，或是 GitLab 收件箱记录的发起 issue 还没被用户关闭（发起 issue 只由用户关闭，关闭前调度者不接
下一个需求）。一个调度者同时只负责一个交付：2026-10-17 实测 omp 在工具调用和后台等待期间仍报
`idle`，只看 herdr 状态会把新需求塞给正在交付的调度者，导致排队不开工。

身份只看登记名前缀 `delivery-scheduler-`，不限定执行体 kind；herdr 名字随占用者存在，
agent 退出即被清除。herdr 把 omp always-ask 审批界面判为 `blocked`（2026-10-17 S11 实测），
状态过滤即可拦住审批界面。

测试覆盖：用 HERDR_DISCOVERY_BIN 指向桩替换 herdr 二进制。不要用 HERDR_BIN_PATH：
hwf 的 profile 加载器（herdr plugin config-dir）也读它，全局导出指向桩会让 profile
解析失败（unknown profile）。必须临时覆盖时，只把 HERDR_DISCOVERY_BIN 挂在发现步骤的 env: 上。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

NAME_PREFIX = "delivery-scheduler-"
REUSABLE_STATES = frozenset({"idle", "done"})
# INDEX 状态列以这些词开头即视为收尾；其余（in-progress/held/blocked/active/queued…）都算未收尾，
# 写错或漏写时宁可多建一个调度者，也不把新需求塞进在途交付。
TERMINAL_STATUS = ("done", "delivered", "closed", "cancelled")
INDEX_RELATIVE = Path(".hwf/tmp/INDEX.md")
# GitLab 收件箱记录仍打开的发起 issue；发起 issue 只由用户关闭，关闭前其调度者一直被占用（2026-10-22 用户指示）。
INBOX_STATE = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf")) / "state" / "gitlab-inbox.json"

EXIT_OK = 0
EXIT_ERROR = 1
HERDR_SYNTAX_EXIT = 2  # herdr CLI 用 2 表示参数/语法错误，通常是 CLI 版本与本脚本不一致。

LIST_TIMEOUT_S = 10
CELL_SPLIT = re.compile(r"(?<!\\)\|")  # INDEX 输入原文里的 | 写作 \|。
TICKED = re.compile(r"`([^`]+)`")
ISSUE_URL = re.compile(r"https?://[^/]+/(?P<project>.+?)/-/(?:issues|work_items)/(?P<iid>\d+)")


@dataclass
class IndexRow:
    task: str
    scheduler: str  # 登记名；行内没有登记名时为空
    status: str

    @property
    def open(self) -> bool:
        return not self.status.strip().lower().startswith(TERMINAL_STATUS)


def read_index(repo: str) -> list[IndexRow]:
    """解析 INDEX 表格行；文件不存在视为没有任务。表头和非表格行忽略。"""
    path = Path(repo) / INDEX_RELATIVE
    if not path.exists():
        return []
    rows: list[IndexRow] = []
    for line in path.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in CELL_SPLIT.split(line)]
        if len(cells) < 5:
            continue
        names = [n for n in TICKED.findall(cells[3]) if n.startswith(NAME_PREFIX)]
        rows.append(IndexRow(cells[1].strip("`"), names[0] if names else "", cells[4]))
    return rows


def open_origin_tasks(repo: str) -> frozenset[str]:
    """收件箱中属于本仓库、仍打开的发起 issue 对应的任务标识。文件不存在视为没有。"""
    if not INBOX_STATE.exists():
        return frozenset()
    origins = json.loads(INBOX_STATE.read_text()).get("origins", {})
    target = Path(repo).resolve()
    return frozenset(
        task for url, origin in origins.items()
        if Path(origin["repo"]).resolve() == target and (task := task_id_for(url))
    )


def active_rows(repo: str) -> list[IndexRow]:
    """仍占用调度者的任务：INDEX 未收尾，或发起 issue 还没被用户关闭（即使调度者已写 done）。"""
    bound = open_origin_tasks(repo)
    return [row for row in read_index(repo) if row.open or row.task in bound]


@dataclass
class Selection:
    """候选按普通字典序排列；skipped 记录本仓库调度者不可复用的原因。"""

    candidates: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _directories(agent: Mapping[str, Any]) -> list[Path]:
    """返回 agent 上报且非空的目录（cwd、foreground_cwd），已规范化。"""
    found: list[Path] = []
    for key in ("cwd", "foreground_cwd"):
        value = agent.get(key)
        if isinstance(value, str) and value:
            found.append(Path(value).resolve())
    return found


def select_scheduler(
    agents: Sequence[Mapping[str, Any]], cwd: str, owners: frozenset[str] = frozenset(),
) -> Selection:
    """owners：INDEX 中名下有未收尾任务的登记名。

    归属：`cwd` 或 `foreground_cwd` 任一等于仓库 → 本仓库调度者（2026-10-17 R-2：前台目录
    可能漂移，任一匹配避免漏认）。可复用：所有上报目录都等于仓库，否则不把需求派给它。
    """
    target = Path(cwd).resolve()
    selection = Selection()
    for agent in agents:
        name = agent.get("name")
        if not isinstance(name, str) or not name.startswith(NAME_PREFIX):
            continue
        directories = _directories(agent)
        if target not in directories:
            continue  # 其他仓库的调度者与本仓库无关。
        pane = agent.get("pane_id")
        status = agent.get("agent_status")
        label = f"{name}@{pane or '?'}"
        if not isinstance(pane, str) or not pane:
            selection.skipped.append(f"{label}: missing pane id")
        elif status not in REUSABLE_STATES:
            selection.skipped.append(f"{label}: status {status}")
        elif name in owners:
            selection.skipped.append(f"{label}: owns an open delivery in INDEX")
        elif any(directory != target for directory in directories):
            selection.skipped.append(
                f"{label}: directory mismatch "
                f"(cwd={agent.get('cwd')}, foreground_cwd={agent.get('foreground_cwd')})"
            )
        # 防御性判断：当前 herdr 的 agent list 不返回这两个字段（V3 实测），缺失即视为通过；
        # 若将来版本返回，未就绪的成员不能复用。
        elif agent.get("launch_pending") or agent.get("interactive_ready") is False:
            selection.skipped.append(f"{label}: not interactive-ready")
        else:
            selection.candidates.append(pane)
    # pane id 是不透明句柄：普通字典序，只保证相同输入得到相同选择。
    selection.candidates.sort()
    return selection


def load_inventory() -> list[dict[str, Any]]:
    binary = os.environ.get("HERDR_DISCOVERY_BIN") or os.environ.get("HERDR_BIN_PATH") or "herdr"
    result = subprocess.run(
        [binary, "agent", "list"],
        capture_output=True, text=True, timeout=LIST_TIMEOUT_S, check=False,
    )
    if result.returncode == HERDR_SYNTAX_EXIT:
        raise RuntimeError(
            f"herdr agent list: CLI syntax/version mismatch (exit 2): {result.stderr.strip()}"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"herdr agent list exited {result.returncode}: {result.stderr.strip()}"
        )
    data = json.loads(result.stdout)
    agents = data["result"]["agents"]  # 结构不符时抛 KeyError/TypeError → fail closed。
    if not isinstance(agents, list) or not all(isinstance(a, dict) for a in agents):
        raise ValueError("Invalid Herdr agent inventory")
    return agents


def task_id_for(url: str) -> str | None:
    """GitLab issue URL → INDEX 任务标识（`<project-path / 换成 ->-<iid>`），非 issue URL 返回 None。"""
    match = ISSUE_URL.match(url)
    if not match:
        return None
    return f"{match.group('project').replace('/', '-')}-{match.group('iid')}"


def state_mentions(repo: str, row: IndexRow, url: str) -> bool:
    """该任务的 STATE.md 是否提到这个 issue。完整 URL 的 issues/work_items 两种写法都认；`#<iid>` 简写只在
    同项目任务里认，避免跨项目同号误判。"""
    match = ISSUE_URL.match(url)
    task = task_id_for(url)
    state = Path(repo) / ".hwf/tmp" / row.task / "STATE.md"
    if not (match and task and state.exists()):
        return False
    project, iid = match.group("project"), match.group("iid")
    full = re.compile(rf"{re.escape(project)}/-/(?:issues|work_items)/{iid}(?!\d)")
    short = re.compile(rf"(?<![\w/&])#{iid}(?!\d)")
    text = state.read_text()
    return bool(full.search(text) or (row.task.startswith(task[: -len(iid)]) and short.search(text)))


def reused_elsewhere(repo: str, url: str, owner: IndexRow, index: Sequence[IndexRow], rows: Sequence[IndexRow]) -> bool:
    """已收尾任务的调度者是否被复用去做了无关任务（是则旧任务的消息不投给它）。

    调度者只有名下任务全部收尾才会被复用，所以它名下比 owner 更早登记、仍未收尾的任务说明它从那时起一直
    没空闲过，名下所有未收尾任务都是同一批交付（如 #408 拆出的 #417 与 #422，2026-10-27）。更晚登记的任务
    只有在 STATE 里提到 owner 的 issue（url）时才算兄弟，否则按复用处理。
    """
    others = [row for row in rows if row.scheduler == owner.scheduler and row.task != owner.task]
    if not others:
        return False
    position = index.index(owner)
    if any(index.index(row) < position for row in others):
        return False
    return not any(state_mentions(repo, row, url) for row in others)


def owner_of(url: str, repo: str, agents: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    """找出负责该 issue 的任务及其在线调度者 pane。

    1. 该 issue 本身就是 INDEX 里的任务（发起 issue）：只认这一行，不管是否收尾，评论总是回到原任务。
       已收尾就不去别的任务 STATE 里找；别的任务可能顺带提到这个号（2026-10-22 #380 重开后的评论
       就因此误投给了 #381 的调度者）。
    2. 否则在未收尾任务的 STATE.md 里找该 issue（协议要求 STATE 记录交付中创建的每个 issue）。
    原调度者已退出，或收尾后被复用去做无关任务时，pane 为 null：调用方走入口，由空闲调度者按 STATE 接续，
    不把消息塞进别的任务的会话。原调度者手上的未收尾任务若是同一批交付的兄弟任务，仍投给它（见 reused_elsewhere）。
    """
    task = task_id_for(url)
    rows = active_rows(repo)
    index = read_index(repo)
    owner = next((row for row in index if row.task == task), None)
    if owner is None:
        owner = next((row for row in rows if state_mentions(repo, row, url)), None)
    if owner is None:
        return {"task": None, "pane": None, "live_pane": None, "scheduler": None, "status": None}
    busy_elsewhere = owner not in rows and reused_elsewhere(repo, url, owner, index, rows)
    live = next((a for a in agents if owner.scheduler and a.get("name") == owner.scheduler), None)
    live_pane = live.get("pane_id") if live else None
    live_pane = live_pane if isinstance(live_pane, str) and live_pane else None
    # pane 用于投递（在忙别的任务就不投），live_pane 用于向用户报告它实际在不在线。
    return {"task": owner.task, "pane": None if busy_elsewhere else live_pane, "live_pane": live_pane,
            "scheduler": owner.scheduler or None, "status": owner.status}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cwd")
    parser.add_argument("--owner-of", metavar="ISSUE_URL",
                        help='print JSON {"task": id|null, "pane": pane|null} for the open delivery owning the issue')
    args = parser.parse_args(argv)
    if os.environ.get("HERDR_ENV") != "1":
        raise RuntimeError("Run this workflow inside a Herdr-managed pane")

    agents = load_inventory()
    if args.owner_of:
        sys.stdout.write(json.dumps(owner_of(args.owner_of, args.cwd, agents)))
        return EXIT_OK
    owners = frozenset(row.scheduler for row in active_rows(args.cwd) if row.scheduler)
    selection = select_scheduler(agents, args.cwd, owners)
    if selection.skipped:
        print("Skipped scheduler(s): " + "; ".join(selection.skipped), file=sys.stderr)
    if selection.candidates:
        sys.stdout.write(selection.candidates[0])  # 契约：只写 pane id，无换行。
    return EXIT_OK  # 没有空闲调度者时 stdout 为空 → 入口新建。


def cli(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except (
        RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
    ) as error:
        # json.JSONDecodeError ⊂ ValueError；TimeoutExpired ⊂ SubprocessError；找不到二进制 ⊂ OSError。
        print(f"Scheduler discovery failed: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())
