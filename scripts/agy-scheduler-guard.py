#!/usr/bin/env python3
"""Antigravity (agy) 调度者守卫 Hook：把 omp 调度者的记录守卫、监听守卫以及禁弹框/禁子代理约束迁移到 agy。

挂载方式（由 install.sh 幂等写入 ~/.gemini/config/hooks.json 的 hwf-scheduler 分组）：
  - PreToolUse: python3 ~/.hwf/scripts/agy-scheduler-guard.py pre-tool-use
  - Stop:       python3 ~/.hwf/scripts/agy-scheduler-guard.py stop
  - 安装钩子:    python3 ~/.hwf/scripts/agy-scheduler-guard.py install-hooks

隔离边界：
  1. 仅当进程运行在 Herdr 中（HERDR_ENV=1 且有 HERDR_PANE_ID）且当前窗格登记名以
     `delivery-scheduler-` 开头时才执行拦截；arch / arch-lite 及日常终端 agy 一律毫秒放行。
  2. 任何解析/子进程异常一律 fail-open 放行，绝不卡死调度者会话。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEDULER_PREFIX = "delivery-scheduler-"
SCRIPT = "python3 ~/.hwf/scripts/task-record.py"
MAX_PROMPT_CHARS = 2500
MAX_STOP_BLOCKS = 2

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
STATE_DIR = Path(os.environ.get("HWF_STATE_DIR", HWF_HOME / "state"))
MEMBERS_FILE = STATE_DIR / "members.json"
STOP_STATE_FILE = STATE_DIR / "agy-watch-guard-state.json"
LOG_FILE = STATE_DIR / "agy-watch-guard.log"
HERDR = os.environ.get("HERDR_DISCOVERY_BIN") or os.environ.get("HERDR_BIN_PATH") or "herdr"

# PreToolUse 禁止在调度者内使用的交互弹框和内置子代理工具
FORBIDDEN_ASK_TOOLS = {"ask_question", "ask_user_question", "ask_user"}
FORBIDDEN_SUBAGENT_TOOLS = {
    "invoke_subagent",
    "define_subagent",
    "browser_subagent",
    "manage_subagents",
    "run_workflow",
}
SHELL_TOOLS = {"run_command", "bash", "execute_command"}
FILE_EDIT_TOOLS = {
    "write_to_file",
    "write_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "write",
    "edit",
}

INDEX_FILE_RE = re.compile(r"\.hwf/tmp/INDEX\.md\b")
INDEX_WRITE_RE = re.compile(
    r"(>>?\s*[\"']?[^\s\"'|;&]*\.hwf/tmp/INDEX\.md)"
    r"|(\b(sed\s+-i|tee|perl\s+-[a-z]*i)\b[^|;&]*\.hwf/tmp/INDEX\.md)"
)
ISSUE_NOTE_RE = re.compile(r"\bglab\s+issue\s+(note|comment)\b")
ISSUE_NOTES_API_RE = re.compile(r"\bglab\s+api\b[^|;&]*/issues/[^/\s]+/notes(?![/\w])")
API_WRITE_RE = re.compile(r"(-X|--method)[=\s]+[\"']?POST|\s(-f|-F|--field|--raw-field)[=\s]", re.IGNORECASE)
BARE_WAIT_RE = re.compile(r"\bherdr\s+agent\s+wait\s+(?!--help)\S")
AGENT_PROMPT_RE = re.compile(r"\bherdr\s+agent\s+prompt\s+(?!--help)\S")

TOP_OPEN = "<!-- hwf-state-top -->"
TOP_CLOSE = "<!-- /hwf-state-top -->"
NO_USER_TODO_RE = re.compile(r"^(?:无|暂无|没有|none|n/?a|[-—–]+)$", re.IGNORECASE)
WAITING_STAGE_RE = re.compile(r"等(?:待)?用户|待用户|待确认|待拍板|待授权|待上线|待收尾|待合并")


# ---------- PreToolUse 规则判定 ----------

def _index_reason() -> str:
    return (
        f"INDEX.md 不手改（手写漏反引号会导致收件箱找不到负责人）。"
        f"追加记录用：{SCRIPT} index-add --repo <主仓库> --task <任务标识> --input <输入原文>；"
        f"改状态用：{SCRIPT} index-set --repo <主仓库> --task <任务标识> --status <状态>；收尾状态只由 wrapup.py 写。"
    )


def _note_reason() -> str:
    return (
        "issue 评论不直接用 glab 发。请把正文写进文件后运行："
        f"{SCRIPT} note --repo <主仓库> --task <任务标识> --issue <issue URL> "
        "--kind <todo|risk|answer|step-done|delivery-done|stage|record> --body-file <文件>。"
        "请用户拍板或授权用 --kind todo 并写明 --will；delivery-done 会自动核验验收对账表与成员独立性（作者≠审者≠真机验者）。"
    )


def _wait_reason() -> str:
    return (
        "等成员不用裸 herdr agent wait（成员断网或卡住时会干等数小时）。"
        "改用 run_command（设 WaitMsBeforeAsync=500, IsDaemon=false）后台运行："
        "python3 ~/.hwf/scripts/member-wait.py <成员> [--until …] --repo <主仓库> --task <任务标识>。"
    )


def _prompt_len_reason(length: int) -> str:
    return (
        f"单条派活命令长达 {length} 字（上限 {MAX_PROMPT_CHARS} 字），会撑爆调度者与成员上下文。"
        "请把长背景、验收清单或代码位置写进 <主仓库>/.hwf/tmp/<任务标识>/dispatch-<角色>.md，"
        "herdr agent prompt 里只写本次目标、可写范围和该文件绝对路径。"
    )


def _extract_command(args: Mapping[str, Any]) -> str:
    for key in ("CommandLine", "commandLine", "command", "cmd"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _extract_target_paths(args: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("TargetFile", "targetFile", "AbsolutePath", "absolutePath", "path", "file_path", "filePath"):
        val = args.get(key)
        if isinstance(val, str) and val:
            paths.append(val)
    return paths


def pre_tool_violation(tool_name: str, args: Mapping[str, Any]) -> Optional[str]:
    """返回拦截理由；合法调用返回 None。"""
    name = (tool_name or "").strip()
    lower = name.lower()
    if lower in FORBIDDEN_ASK_TOOLS:
        return (
            "调度者禁止在终端弹交互提问框（用户常在飞书或 GitLab 回复，看不到终端弹框）。"
            "需要用户拍板或确认验收清单时，请用 task-record.py note --kind todo 发 GitLab 评论列编号选项，"
            "更新 STATE 待用户事项并发送 ntfy 后结束回合等待回复。"
        )
    if lower in FORBIDDEN_SUBAGENT_TOOLS:
        return (
            f"调度者禁止使用内置子代理/编排工具 `{name}` 代干活；"
            "分析、开发、代码评审、真机验收必须通过 member-start.py 派给独立 Herdr 成员（确保作者≠审者≠验者）。"
        )
    if lower in SHELL_TOOLS:
        cmd = _extract_command(args)
        if not cmd:
            return None
        if "task-record.py" in cmd or "wrapup.py" in cmd:
            return None
        if INDEX_WRITE_RE.search(cmd):
            return _index_reason()
        if ISSUE_NOTE_RE.search(cmd) or (ISSUE_NOTES_API_RE.search(cmd) and API_WRITE_RE.search(cmd)):
            return _note_reason()
        if BARE_WAIT_RE.search(cmd):
            return _wait_reason()
        if AGENT_PROMPT_RE.search(cmd) and len(cmd) > MAX_PROMPT_CHARS:
            return _prompt_len_reason(len(cmd))
        return None
    if lower in FILE_EDIT_TOOLS:
        for target in _extract_target_paths(args):
            if INDEX_FILE_RE.search(target):
                return _index_reason()
        return None
    return None


# ---------- Stop 规则判定（移植自 hwf-scheduler-watch-guard.ts） ----------

def parse_index_last_rows(text: str) -> list[dict[str, str]]:
    by_task: dict[str, dict[str, str]] = {}
    for line in (text or "").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        task = cells[1].strip("`").strip()
        if not task or task == "任务标识" or re.fullmatch(r"[-:\s]+", task):
            continue
        by_task[task] = {
            "task": task,
            "scheduler": cells[3].strip("`").strip(),
            "status": cells[4].strip("`").strip(),
        }
    return list(by_task.values())


def parse_state_top(text: str) -> dict[str, str]:
    start = (text or "").find(TOP_OPEN)
    if start < 0:
        return {}
    end = text.find(TOP_CLOSE, start)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for raw in text[start + len(TOP_OPEN) : end].splitlines():
        m = re.match(r"^-\s*([^：:]+)[：:](.*)$", raw.strip())
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def is_active_delivery_status(status: str) -> bool:
    clean = (status or "").strip().lower()
    return clean.startswith("in-progress") or clean.startswith("active")


def is_no_user_todo(value: str) -> bool:
    clean = (value or "").strip()
    return not clean or bool(NO_USER_TODO_RE.match(clean))


def is_waiting_stage(stage: str) -> bool:
    return bool(WAITING_STAGE_RE.search(stage or ""))


def _read_text_safe(file_path: Path) -> str:
    try:
        return file_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def find_unwatched_members(
    fully_idle: bool,
    agents: Sequence[Mapping[str, Any]],
    self_pane: str,
    registry: Mapping[str, Any],
    self_scheduler: str,
) -> list[dict[str, str]]:
    """当 agy 没有任何运行中的后台任务（fullyIdle=True）却仍有归属本调度者的 working/blocked 成员时返回清单。"""
    if not fully_idle:
        return []
    unwatched: list[dict[str, str]] = []
    for agent in agents or ():
        if not agent or agent.get("pane_id") == self_pane:
            continue
        status = str(agent.get("agent_status") or "")
        if status not in ("working", "blocked"):
            continue
        entry = registry.get(str(agent.get("pane_id") or ""))
        if not isinstance(entry, Mapping):
            continue
        if self_scheduler and entry.get("scheduler") != self_scheduler:
            continue
        name = str(agent.get("name") or agent.get("pane_id") or "")
        if name:
            unwatched.append({"name": name, "status": status})
    return unwatched


def find_silent_stall(
    fully_idle: bool,
    agents: Sequence[Mapping[str, Any]],
    self_pane: str,
    registry: Mapping[str, Any],
    self_scheduler: str,
    repo_dir: str,
    read_file: Callable[[Path], str] = _read_text_safe,
) -> Optional[dict[str, str]]:
    """拦截「进行中交付任务 + 待用户事项：无 + 0 活跃成员 + 0 后台等待」时的静默躺平。"""
    if not fully_idle or not self_scheduler or not repo_dir:
        return None
    for agent in agents or ():
        if not agent or agent.get("pane_id") == self_pane:
            continue
        if agent.get("agent_status") not in ("working", "blocked"):
            continue
        entry = registry.get(str(agent.get("pane_id") or ""))
        if isinstance(entry, Mapping) and entry.get("scheduler") == self_scheduler:
            return None

    index_text = read_file(Path(repo_dir) / ".hwf/tmp/INDEX.md")
    if not index_text:
        return None
    owned = [
        row
        for row in parse_index_last_rows(index_text)
        if row["scheduler"] == self_scheduler and is_active_delivery_status(row["status"])
    ]
    if not owned:
        return None

    any_waiting = False
    stalled: list[dict[str, str]] = []
    for row in owned:
        state_text = read_file(Path(repo_dir) / ".hwf/tmp" / row["task"] / "STATE.md")
        top = parse_state_top(state_text)
        if not top:
            continue
        if not is_no_user_todo(top.get("待用户事项", "")) or is_waiting_stage(top.get("当前阶段", "")):
            any_waiting = True
            continue
        stalled.append({"task": row["task"], "stage": top.get("当前阶段", ""), "nextAction": top.get("下一动作", "")})
    if any_waiting or not stalled:
        return None
    return stalled[-1]


def build_unwatched_reason(unwatched: Sequence[Mapping[str, str]]) -> str:
    items = "、".join(f"{u['name']}（{u['status']}）" for u in unwatched)
    return (
        f"[hwf 监听守卫] 仍有归属你的成员处于运行/卡住态且当前没有任何后台等待作业：{items}。\n"
        "请立刻用 run_command（设 WaitMsBeforeAsync=500, IsDaemon=false）为每个活跃成员挂上后台 "
        "`python3 ~/.hwf/scripts/member-wait.py <成员> --repo <主仓库> --task <任务标识>` 再结束回合；"
        "若成员卡住（blocked），请先读屏处理。"
    )


def build_stall_reason(stall: Mapping[str, str]) -> str:
    return (
        f"[hwf 监听守卫] 检测到进行中任务 `{stall['task']}` 准备静默停止"
        f"（当前阶段：{stall.get('stage') or '未写明'}；待用户事项：无），但既无在干活的成员也无后台等待。\n"
        "禁止只在终端或 STATE 写「暂停」就结束回合：\n"
        "1. 若在既有授权内还能推进，请当回合派发下一环成员并挂后台 `member-wait.py`；\n"
        "2. 若确需用户拍板或确认，请先用 `task-record.py note --kind todo` 发评论、更新 STATE「待用户事项」并发 ntfy。"
    )


def evaluate_stop(
    payload: Mapping[str, Any],
    *,
    agents: Sequence[Mapping[str, Any]],
    self_pane: str,
    self_scheduler: str,
    registry: Mapping[str, Any],
    repo_dir: str,
    read_file: Callable[[Path], str] = _read_text_safe,
) -> tuple[Optional[str], str]:
    """返回 (continue_reason, signature)；允许停止时返回 (None, '')。"""
    reason_code = str(payload.get("terminationReason") or "")
    if payload.get("continuationActive") or (reason_code and reason_code != "NO_TOOL_CALL"):
        return None, ""
    fully_idle = payload.get("fullyIdle") is not False
    unwatched = find_unwatched_members(fully_idle, agents, self_pane, registry, self_scheduler)
    if unwatched:
        sig = "unwatched:" + ",".join(sorted(f"{u['name']}:{u['status']}" for u in unwatched))
        return build_unwatched_reason(unwatched), sig
    stall = find_silent_stall(fully_idle, agents, self_pane, registry, self_scheduler, repo_dir, read_file=read_file)
    if stall:
        return build_stall_reason(stall), f"stall:{stall['task']}"
    return None, ""


# ---------- 运行时查询与防死循环计数 ----------

def _herdr_json(*args: str, timeout: int = 5) -> Optional[dict[str, Any]]:
    try:
        res = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout, check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        parsed = json.loads(res.stdout)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _self_scheduler_name(self_pane: str) -> str:
    data = _herdr_json("agent", "get", self_pane)
    agent = (data or {}).get("result", {}).get("agent")
    if isinstance(agent, Mapping):
        name = str(agent.get("name") or "")
        if name.startswith(SCHEDULER_PREFIX):
            return name
    return ""


def _list_agents() -> list[dict[str, Any]]:
    data = _herdr_json("agent", "list")
    agents = (data or {}).get("result", {}).get("agents")
    return [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []


def _read_registry() -> dict[str, Any]:
    try:
        data = json.loads(MEMBERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_repo_dir(payload: Mapping[str, Any], self_agent: Optional[Mapping[str, Any]]) -> str:
    candidates: list[str] = []
    for raw in (
        (self_agent or {}).get("cwd"),
        (self_agent or {}).get("foreground_cwd"),
        payload.get("cwd"),
        *(payload.get("workspacePaths") or []),
        os.getcwd(),
    ):
        if isinstance(raw, str) and raw and raw not in candidates:
            candidates.append(raw)
    for d in candidates:
        if (Path(d) / ".hwf/tmp/INDEX.md").is_file():
            return d
    return candidates[0] if candidates else ""


def _log(line: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def _bump_stop_block_count(pane_id: str, signature: str) -> int:
    try:
        state = json.loads(STOP_STATE_FILE.read_text(encoding="utf-8")) if STOP_STATE_FILE.is_file() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    prev = state.get(pane_id) if isinstance(state.get(pane_id), dict) else {}
    if not signature:
        if pane_id in state:
            state.pop(pane_id, None)
            _write_stop_state(state)
        return 0
    count = int(prev.get("count", 0)) + 1 if prev.get("sig") == signature else 1
    state[pane_id] = {"sig": signature, "count": count, "at": int(time.time())}
    _write_stop_state(state)
    return count


def _write_stop_state(state: Mapping[str, Any]) -> None:
    try:
        STOP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------- Hooks 安装器（供 install.sh 幂等调用） ----------

PRE_TOOL_MATCHER = (
    "run_command|write_to_file|write_file|edit_file|replace_file_content|"
    "multi_replace_file_content|sed_file|ask_question|ask_user_question|ask_user|"
    "invoke_subagent|define_subagent|browser_subagent|manage_subagents|run_workflow"
)


def build_hooks_group(script_path: Optional[Path] = None) -> dict[str, Any]:
    script = str(script_path or Path(__file__).resolve())
    return {
        "PreToolUse": [
            {
                "matcher": PRE_TOOL_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 '{script}' pre-tool-use",
                        "timeout": 10,
                    }
                ],
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": f"python3 '{script}' stop",
                "timeout": 10,
            }
        ],
    }


def install_hooks(
    hooks_file: Optional[Path] = None,
    *,
    script_path: Optional[Path] = None,
) -> Path:
    target = hooks_file or (Path.home() / ".gemini/config/hooks.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if target.is_file():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
    data["hwf-scheduler"] = build_hooks_group(script_path=script_path)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# ---------- CLI 入口 ----------

def handle_pre_tool_use(raw_stdin: str, env: Mapping[str, str] = os.environ) -> dict[str, Any]:
    self_pane = env.get("HERDR_PANE_ID") or ""
    if env.get("HERDR_ENV") != "1" or not self_pane:
        return {"decision": "allow"}
    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except ValueError:
        return {"decision": "allow"}
    if not isinstance(payload, Mapping):
        return {"decision": "allow"}
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), Mapping) else {}
    tool_name = str(tool_call.get("name") or payload.get("toolName") or payload.get("tool_name") or "")
    tool_args = tool_call.get("args") if isinstance(tool_call.get("args"), Mapping) else payload.get("toolInput")
    if not isinstance(tool_args, Mapping):
        tool_args = {}
    reason = pre_tool_violation(tool_name, tool_args)
    if not reason:
        return {"decision": "allow"}
    if not _self_scheduler_name(self_pane):
        return {"decision": "allow"}
    return {"decision": "deny", "reason": reason}


def handle_stop(raw_stdin: str, env: Mapping[str, str] = os.environ) -> dict[str, Any]:
    self_pane = env.get("HERDR_PANE_ID") or ""
    if env.get("HERDR_ENV") != "1" or not self_pane:
        return {"decision": "stop"}
    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except ValueError:
        return {"decision": "stop"}
    if not isinstance(payload, Mapping):
        return {"decision": "stop"}
    agents = _list_agents()
    self_agent = next((a for a in agents if a.get("pane_id") == self_pane), None)
    self_name = str((self_agent or {}).get("name") or "")
    if not self_name.startswith(SCHEDULER_PREFIX):
        return {"decision": "stop"}
    reason, sig = evaluate_stop(
        payload,
        agents=agents,
        self_pane=self_pane,
        self_scheduler=self_name,
        registry=_read_registry(),
        repo_dir=_resolve_repo_dir(payload, self_agent),
    )
    if not reason:
        _bump_stop_block_count(self_pane, "")
        return {"decision": "stop"}
    count = _bump_stop_block_count(self_pane, sig)
    if count > MAX_STOP_BLOCKS:
        _log(f"{self_name} allow stop after {MAX_STOP_BLOCKS} blocks: {sig}")
        return {"decision": "stop"}
    _log(f"{self_name} block stop #{count}: {sig}")
    return {"decision": "continue", "reason": reason}


def main(argv: Sequence[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "install-hooks":
            target = install_hooks()
            print(str(target))
            return 0
        raw = sys.stdin.read()
        if cmd == "pre-tool-use":
            out = handle_pre_tool_use(raw)
        elif cmd == "stop":
            out = handle_stop(raw)
        else:
            print("usage: agy-scheduler-guard.py <pre-tool-use|stop|install-hooks>", file=sys.stderr)
            return 2
        json.dump(out, sys.stdout, ensure_ascii=False)
        return 0
    except Exception:
        # fail-open：绝不因守卫脚本异常卡死调度者
        fallback = {"decision": "stop"} if cmd == "stop" else {"decision": "allow"}
        json.dump(fallback, sys.stdout, ensure_ascii=False)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
