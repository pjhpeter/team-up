#!/usr/bin/env python3
"""按角色领取 Herdr 成员：本调度者有同角色空闲成员就复用，没有才新建；启动参数一律取角色配置。

    python3 ~/.hwf/scripts/member-start.py <profile> --task <任务> [--cwd DIR] [--exclude NAME ...] [--keep-session]
        [--model-override MODEL --authorized-by <用户授权的 issue 评论链接>]

为什么有它：2026-11-07 #453 调度者手写 `herdr agent start` 参数，把开发者的 gpt-6.1-sol 参数抄给了架构师；
又只按会被执行体改掉的窗口标题找架构师，没认出已在的 delivery-arch-w18-p1t，另建了第二个架构师。
调度者领取成员只能调用本脚本，不再手写 `pane split` / `agent start` 参数。

步骤：
1. 读角色配置：全局插件配置 → <仓库>/.hwf/config.yaml → <仓库>/.hwf/config.local.yaml，后者按整条 profile 覆盖。
   成员 profile 的参数里必须带模型（--model / -m；droid 写在 --settings 文件的 sessionDefaultSettings.model），
   否则拒绝启动（协议：成员不得依赖执行体默认模型）。
2. 认调度者：当前 pane（HERDR_PANE_ID）上的登记名必须是 delivery-scheduler-*，且所在 tab 已用 name-tab.py
   命名为 `#<编号> 调度 …`（或 `待建issue 调度 …`），否则拒绝。
3. 找本团队同角色成员：登记名为 `i<编号>-<profile>[-n]`（无 issue 时 `new-<profile>[-n]`，旧名
   `delivery-<profile>-<handle>` 也认），pane 在本调度者的 tab 里或登记簿记在本调度者名下。
   成员登记名 = `i<编号>-<profile>`（2026-11-08 用户指示「issue 编号-角色」；herdr 要求字母开头，所以加 i），
   同名已被占用时依次加 -2、-3。
   idle/done、不在 --exclude（调度者自知还有在途任务的成员）里，而且同时满足下面两条才算空闲：
   - 属于本 issue：登记名前缀是本 issue 的 `i<编号>`，或建 issue 前领取的 `new`（领取后改名为 `i<编号>-…`）。
     别的 issue 的成员（`i464-dev-fe`）在那个 issue 收尾前还没释放，旧名 `delivery-…` 看不出归属，都不复用。
   - 优先选 pane 当前工作目录就是 --cwd 的空闲成员（可 --keep-session 沿用会话）；若本 issue 同角色空闲窗格在旧目录，
     则退出旧会话后先在 shell 执行 `cd -- <cwd>` 并回读 `pane get` 确认目录已切换，再按配置启动新会话，避免多开副本。
   2026-11-09 #453：脚本把 #464 未释放的开发复用过来、在 #464 的 worktree 里重启，调度者只好再建一个（别的 issue 仍不复用）。
4. 有空闲成员：同目录、--keep-session 且正在跑的命令行与配置一致 → 原样交回；否则在同一 pane 退出旧会话（跨目录先切 cwd）、按配置重启。
   没有：在本调度者 tab 里挑最宽敞的 pane 切一块（太挤就在同一 workspace 开新 tab），按配置启动。
5. 设持久 pane 标题 `角色-模型-任务`，登记簿记下归属，stdout 输出一行 JSON，调度者照抄进 STATE。

退出码：0 成功；1 失败（原因写 stderr）。失败时调用方报告阻塞，不得手写命令兜底。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

import yaml

_SPEC = importlib.util.spec_from_file_location("hwf_start_scheduler", Path(__file__).with_name("start-scheduler.py"))
assert _SPEC is not None and _SPEC.loader is not None
_START = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_START)
handle = _START.handle  # 登记名规则只在一处实现
_TAB_SPEC = importlib.util.spec_from_file_location("hwf_name_tab", Path(__file__).with_name("name-tab.py"))
assert _TAB_SPEC is not None and _TAB_SPEC.loader is not None
_TAB = importlib.util.module_from_spec(_TAB_SPEC)
_TAB_SPEC.loader.exec_module(_TAB)
_RC_SPEC = importlib.util.spec_from_file_location("hwf_role_config", Path(__file__).with_name("role_config.py"))
assert _RC_SPEC is not None and _RC_SPEC.loader is not None
_RC = importlib.util.module_from_spec(_RC_SPEC)
sys.modules.setdefault("hwf_role_config", _RC)
_RC_SPEC.loader.exec_module(_RC)

START_TIMEOUT_MS = 90000
SHELL_RETRIES = 10
EXIT_WAIT_S = 30
CWD_SWITCH_WAIT_S = 10
MIN_WIDTH, MIN_HEIGHT = 60, 16  # 切完每块至少这么大，否则换方向或开新 tab
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
EXIT_COMMAND = {"codex": "/quit"}  # 其余执行体用 /exit


class ClaimError(RuntimeError):
    pass


def herdr(*args: str, timeout: int = 30) -> dict[str, Any]:
    return _START.herdr(*args, timeout=timeout)


# ---------- 角色配置 ----------

def global_config_path() -> Path:
    if os.environ.get("HWF_GLOBAL_CONFIG"):
        return Path(os.environ["HWF_GLOBAL_CONFIG"])
    roles = _RC.roles_path()
    if roles.is_file():
        return roles
    result = subprocess.run([_START.HERDR, "plugin", "config-dir", "herdr-workflows"], capture_output=True,
                            text=True, timeout=10, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()) / "config.yaml"
    return Path.home() / ".hwf/herdr/config.yaml"


def main_repo(cwd: str) -> Optional[Path]:
    """worktree 里也要读主仓库的 .hwf/config.local.yaml（不入库），所以取 git common dir 的上一级。"""
    result = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, timeout=10, check=False)
    return Path(result.stdout.strip()).parent if result.returncode == 0 and result.stdout.strip() else None


def _profiles_from_layer(path: Path) -> dict[str, Any]:
    """同时兼容统一格式 roles.yaml（team 段）与传统 Herdr 格式（profiles 段）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    raw_team = data.get("team")
    if isinstance(raw_team, dict):
        for role_name, spec in raw_team.items():
            if not isinstance(spec, dict) or not spec.get("tool"):
                continue
            try:
                out[str(role_name)] = _RC.compile_team_profile(
                    str(role_name), spec, hwf_home=path.parent, write_settings=True
                )
            except _RC.RoleConfigError as exc:
                raise ClaimError(str(exc)) from exc
    raw_profiles = data.get("profiles")
    if isinstance(raw_profiles, dict):
        out.update(raw_profiles)
    return out


def load_profile(name: str, layers: Sequence[Path]) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for path in layers:
        if path.is_file():
            profiles.update(_profiles_from_layer(path))  # 整条 entry 覆盖，不逐字段合并
    profile = profiles.get(name)
    if not isinstance(profile, dict) or not profile.get("kind"):
        raise ClaimError(f"配置里没有角色 {name}（查过：{', '.join(str(p) for p in layers)}）")
    if name == "scheduler":
        raise ClaimError("调度者由 team-up 入口启动，不用本脚本领取")
    args = [str(a) for a in profile.get("args") or []]
    if not model_of(args):
        raise ClaimError(f"角色 {name} 的配置没写模型（--model / -m 或 --settings 文件），成员不得用执行体默认模型")
    return {"kind": str(profile["kind"]), "args": args}


def model_of(args: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        if arg in ("--model", "-m") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    path = settings_path(args)
    return settings_model(path) if path else None


def settings_path(args: Sequence[str]) -> Optional[Path]:
    """交互式 droid 没有 --model 参数，模型与思考档只能写在 `--settings` 运行时设置文件里。"""
    for i, arg in enumerate(args):
        if arg == "--settings" and i + 1 < len(args):
            return Path(args[i + 1]).expanduser()
        if arg.startswith("--settings="):
            return Path(arg.split("=", 1)[1]).expanduser()
    return None


def settings_model(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session = data.get("sessionDefaultSettings") if isinstance(data, dict) else None
    model = session.get("model") if isinstance(session, dict) else None
    return str(model) if model else None


def override_model(args: Sequence[str], model: str) -> list[str]:
    """用户授权换模型时只替换模型值，其余参数仍按配置。"""
    out = list(args)
    for i, arg in enumerate(out):
        if arg in ("--model", "-m") and i + 1 < len(out):
            out[i + 1] = model
            return out
        if arg.startswith("--model="):
            out[i] = f"--model={model}"
            return out
    if settings_path(out):
        raise ClaimError("这个角色的模型写在 --settings 文件里，不能临时替换；按用户授权改领其他角色")
    raise ClaimError("配置里找不到模型参数，无法替换")

def prepare_launch_args(kind: str, args: Sequence[str], cwd: str, repo: Optional[Path] = None) -> list[str]:
    """按执行体补齐运行时启动参数。agy 对工作目录做精确信任匹配，用会话级 --settings 信任当前 cwd/主仓避免卡在启动弹窗。"""
    out = list(args)
    if kind != "agy":
        return out
    if any(a == "--settings" or a.startswith("--settings=") for a in out):
        return out
    candidates: list[str] = []
    for raw in (cwd, os.path.realpath(cwd), str(repo) if repo else "", os.path.realpath(str(repo)) if repo else ""):
        if raw and raw not in candidates:
            candidates.append(raw)
    if candidates:
        out.extend(["--settings", json.dumps({"trustedWorkspaces": candidates}, ensure_ascii=False, separators=(",", ":"))])
    return out


def pane_label(profile: str, args: Sequence[str], task: str, issue: str = "") -> str:
    model = (model_of(args) or "").rsplit("/", 1)[-1]
    # 补齐 issue 编号，避免 Agent 退出后空壳窗格丢失归属标识
    if issue and issue.startswith("#") and not re.search(rf"{re.escape(issue)}(?!\d)", task):
        task = f"{issue} {task}"
    return f"{profile}-{model}-{task}"


# ---------- 团队与空闲成员 ----------

def registry_path() -> Path:
    return Path(os.environ.get("HWF_STATE_DIR") or Path.home() / ".hwf/state") / "members.json"


def read_registry() -> dict[str, Any]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(pane: dict[str, str], scheduler: str, profile: str,
           drop_panes: Sequence[str] = (), live_panes: Optional[set[str]] = None,
           issue: str = "") -> None:
    data = read_registry()
    gone = set(drop_panes)
    if live_panes is not None:
        # 顺手清理登记簿里早已不存在的死窗格，避免 members.json 无限膨胀。
        keep_alive = live_panes | {pane["pane_id"]}
        data = {pid: entry for pid, entry in data.items() if pid in keep_alive and pid not in gone}
    else:
        for pid in gone:
            data.pop(pid, None)
    entry: dict[str, Any] = {
        "scheduler": scheduler,
        "profile": profile,
        "tab_id": pane["tab_id"],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if issue:
        entry["issue"] = issue
    data[pane["pane_id"]] = entry
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def agents() -> list[dict[str, Any]]:
    return herdr("agent", "list")["result"]["agents"]


def current_scheduler(all_agents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise ClaimError("不在 Herdr pane 里（没有 HERDR_PANE_ID）")
    me = next((a for a in all_agents if a.get("pane_id") == pane), None)
    if not me or not str(me.get("name") or "").startswith("delivery-scheduler-"):
        raise ClaimError(f"当前 pane {pane} 上没有已登记的调度者（delivery-scheduler-*），先完成登记")
    return me


def member_tag(scheduler: dict[str, Any]) -> tuple[str, str]:
    """从调度者 tab 名取 issue：返回 (登记名前缀 i453/new, 编号文字 #453/待建issue)。"""
    label = herdr("tab", "get", scheduler["tab_id"])["result"]["tab"].get("label") or ""
    issue = _TAB.issue_of(label)
    if not issue:
        raise ClaimError(f"调度者 tab 名「{label}」不含 issue 编号；先运行 "
                         "python3 ~/.hwf/scripts/name-tab.py <issue 编号|-> <短标题>")
    return ("new" if issue == _TAB.NO_ISSUE else f"i{issue.lstrip('#')}"), issue


def is_role(agent: dict[str, Any], profile: str) -> bool:
    name = str(agent.get("name") or "")
    if re.fullmatch(rf"(i\d+|new)-{re.escape(profile)}(-\d+)?", name):
        return True
    # 旧名 delivery-<profile>-<handle>：后缀必须正好是 pane handle，免得 dev 误认 dev-fe 的成员
    return name == f"delivery-{profile}-{handle(str(agent.get('pane_id') or ''))}"


def free_name(base: str, all_agents: Sequence[dict[str, Any]], pane: Optional[str]) -> str:
    taken = {a.get("name") for a in all_agents if a.get("pane_id") != pane}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def team_tabs(scheduler: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    tabs = [scheduler["tab_id"]]
    for entry in registry.values():
        if entry.get("scheduler") == scheduler["name"] and entry.get("tab_id") not in tabs:
            tabs.append(entry["tab_id"])
    return tabs


def same_role(all_agents: Sequence[dict[str, Any]], profile: str, scheduler: dict[str, Any],
              registry: dict[str, Any]) -> list[dict[str, Any]]:
    """本调度者名下、登记名属于该角色的成员。只认登记名，不看会被执行体改掉的窗口标题。"""
    tabs = team_tabs(scheduler, registry)
    mine = []
    for agent in all_agents:
        if not is_role(agent, profile):
            continue
        owner = (registry.get(agent.get("pane_id")) or {}).get("scheduler")
        if owner == scheduler["name"] or (owner is None and agent.get("tab_id") in tabs):
            mine.append(agent)
    return mine


def same_dir(a: object, b: str) -> bool:
    return bool(a) and os.path.realpath(str(a)) == os.path.realpath(b)


def belongs_to_issue(member: dict[str, Any], tag: str) -> bool:
    """只复用本 issue（或建 issue 前 new-*）的成员，绝不跨 issue 抢别的未释放成员。"""
    return bool(re.fullmatch(rf"({re.escape(tag)}|new)-.+", str(member.get("name") or "")))


def pane_cwd(pane_id: str) -> str:
    pane = herdr("pane", "get", pane_id)["result"]["pane"]
    return str(pane.get("cwd") or "")


def _idle_sort_key(member: dict[str, Any], tag: str, same_cwd: bool) -> tuple[bool, bool, int, str]:
    name = str(member.get("name") or "")
    suffix = re.search(r"-(\d+)$", name)
    # 优先选已在目标 cwd 的窗格（可沿用会话），其次同 issue，再次最小编号
    return (not same_cwd, not name.startswith(f"{tag}-"), int(suffix.group(1)) if suffix else 1, name)


def eligible_idle(members: Sequence[dict[str, Any]], exclude: Sequence[str], tag: str,
                  cwd: str) -> list[dict[str, Any]]:
    idle: list[dict[str, Any]] = []
    for m in members:
        if m.get("agent_status") not in ("idle", "done") or m.get("name") in exclude:
            continue
        if not belongs_to_issue(m, tag):
            continue
        current_cwd = pane_cwd(str(m["pane_id"]))
        idle.append(dict(m, _same_cwd=same_dir(current_cwd, cwd)))
    idle.sort(key=lambda m: _idle_sort_key(m, tag, bool(m.get("_same_cwd"))))
    return idle


def pick_idle(members: Sequence[dict[str, Any]], exclude: Sequence[str], tag: str, cwd: str) -> Optional[dict[str, Any]]:
    idle = eligible_idle(members, exclude, tag, cwd)
    return idle[0] if idle else None


def _sibling_role_duplicate_groups(
    all_agents: Sequence[dict[str, Any]],
    active_profile: str,
    scheduler: dict[str, Any],
    registry: dict[str, Any],
    exclude: Sequence[str],
    tag: str,
    cwd: str,
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """扫描同一调度者、同一 issue 名下其他角色的空闲成员：若某角色攒了 >=2 个纯空闲窗格，保留最优 1 个并回收其余副本。"""
    tabs = team_tabs(scheduler, registry)
    by_role: dict[str, list[dict[str, Any]]] = {}
    prefix = f"{tag}-"
    for ag in all_agents:
        name = str(ag.get("name") or "")
        pid = str(ag.get("pane_id") or "")
        if not pid or not name.startswith(prefix):
            continue
        if ag.get("agent_status") not in ("idle", "done") or name in exclude:
            continue
        reg_entry = registry.get(pid) or {}
        owner = reg_entry.get("scheduler")
        if owner != scheduler["name"] and not (owner is None and ag.get("tab_id") in tabs):
            continue
        role = str(reg_entry.get("profile") or "") or re.sub(r"-\d+$", "", name[len(prefix):])
        if not role or role == active_profile or not is_role(ag, role):
            continue
        by_role.setdefault(role, []).append(ag)

    groups: list[tuple[str, str, list[dict[str, Any]]]] = []
    for role, role_members in sorted(by_role.items()):
        if len(role_members) < 2:
            continue
        enriched: list[dict[str, Any]] = []
        for m in role_members:
            current_cwd = pane_cwd(str(m["pane_id"]))
            enriched.append(dict(m, _same_cwd=same_dir(current_cwd, cwd)))
        enriched.sort(key=lambda m: _idle_sort_key(m, tag, bool(m.get("_same_cwd"))))
        groups.append((f"{tag}-{role}", str(enriched[0]["pane_id"]), enriched[1:]))
    return groups


def prune_duplicate_idle(extras: Sequence[dict[str, Any]], base: str, kept_pane: str, scheduler_pane: str,
                         panes: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """回收同 issue、同角色且未被 exclude 的多余空闲副本（含不同目录旧窗格），保留每个 workspace 至少一个窗格。"""
    left: dict[str, int] = {}
    for p in panes:
        ws = str(p.get("workspace_id") or "")
        left[ws] = left.get(ws, 0) + 1
    closed_panes: list[str] = []
    closed_names: list[str] = []
    for extra in extras:
        name = str(extra.get("name") or "")
        pid = str(extra.get("pane_id") or "")
        ws = str(extra.get("workspace_id") or "")
        if not pid or pid in (kept_pane, scheduler_pane):
            continue
        if not re.fullmatch(rf"{re.escape(base)}(?:-\d+)?", name):
            continue
        if left.get(ws, 0) <= 1:
            continue
        try:
            herdr("pane", "close", pid)
            left[ws] -= 1
            closed_panes.append(pid)
            closed_names.append(name)
        except RuntimeError:
            continue
    return closed_panes, closed_names


# ---------- 启动与重启 ----------

def running_args_match(pane: str, args: Sequence[str]) -> bool:
    info = herdr("pane", "process-info", "--pane", pane)["result"]["process_info"]
    want = list(args)
    for proc in info.get("foreground_processes") or []:
        argv = [str(a) for a in proc.get("argv") or []]
        if any(argv[i:i + len(want)] == want for i in range(len(argv) - len(want) + 1)):
            return True
    return False


def agent_on(pane: str) -> Optional[dict[str, Any]]:
    return next((a for a in agents() if a.get("pane_id") == pane), None)


def stop_agent(member: dict[str, Any]) -> None:
    pane = member["pane_id"]
    herdr("pane", "run", pane, EXIT_COMMAND.get(str(member.get("agent")), "/exit"))
    deadline = time.time() + EXIT_WAIT_S
    while time.time() < deadline:
        if agent_on(pane) is None:
            return
        time.sleep(1)
    raise ClaimError(f"{member['name']} 在 {EXIT_WAIT_S}s 内没有退出，未重启；请人工查看 pane {pane}")


def switch_pane_cwd(pane: str, cwd: str) -> None:
    """旧会话退出回到交互式 shell 后切换到目标工作目录，并回读确认生效，防止在旧 worktree 误启动。"""
    target = os.path.realpath(cwd)
    herdr("pane", "run", pane, f"cd -- {shlex.quote(target)}")
    deadline = time.time() + CWD_SWITCH_WAIT_S
    while True:
        if same_dir(pane_cwd(pane), target):
            return
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    raise ClaimError(f"pane {pane} 切换工作目录到 {target} 超时（当前：{pane_cwd(pane) or '未知'}），拒绝在错误目录启动")


def start_agent(name: str, pane: str, profile: dict[str, Any]) -> dict[str, Any]:
    if not NAME_RE.fullmatch(name):
        raise ClaimError(f"登记名过长或不合法：{name}（协议规定不截断，报阻塞）")
    for attempt in range(SHELL_RETRIES):
        try:
            herdr("agent", "start", name, "--kind", profile["kind"], "--pane", pane, "--timeout",
                  str(START_TIMEOUT_MS), "--", *profile["args"], timeout=START_TIMEOUT_MS // 1000 + 10)
            break
        except RuntimeError as error:
            if "shell" not in str(error).lower() or attempt == SHELL_RETRIES - 1:
                raise
            time.sleep(1)
    return herdr("agent", "get", name)["result"]["agent"]


def new_pane(scheduler: dict[str, Any], registry: dict[str, Any], cwd: str, issue: str) -> dict[str, str]:
    """在本调度者的 tab 里挑最宽敞的 pane 切一块；都太挤就在同一 workspace 开新 tab。"""
    for tab in team_tabs(scheduler, registry):
        anchor = next((a for a in agents() if a.get("tab_id") == tab), None)
        if anchor is None:
            continue
        layout = herdr("pane", "layout", "--pane", anchor["pane_id"])["result"]["layout"]
        target = max(layout["panes"], key=lambda p: p["rect"]["width"] * p["rect"]["height"])
        rect = target["rect"]
        direction = ("right" if rect["width"] // 2 >= MIN_WIDTH else
                     "down" if rect["height"] // 2 >= MIN_HEIGHT else None)
        if direction:
            pane = herdr("pane", "split", target["pane_id"], "--direction", direction, "--cwd", cwd,
                         "--no-focus")["result"]["pane"]
            return {"pane_id": pane["pane_id"], "tab_id": pane["tab_id"]}
    created = herdr("tab", "create", "--workspace", scheduler["workspace_id"], "--cwd", cwd, "--label",
                    f"{issue} 成员", "--no-focus")["result"]
    return {"pane_id": created["root_pane"]["pane_id"], "tab_id": created["tab"]["tab_id"]}


def claim(profile_name: str, task: str, cwd: str, exclude: Sequence[str], keep_session: bool,
          model_override: Optional[str] = None, authorized_by: Optional[str] = None) -> dict[str, Any]:
    repo = main_repo(cwd)
    layers = [global_config_path()]
    if repo:
        layers += [repo / ".hwf/config.yaml", repo / ".hwf/config.local.yaml"]
    profile = load_profile(profile_name, layers)
    if model_override:
        if not authorized_by:
            raise ClaimError("换模型必须附 --authorized-by（用户授权的 issue 评论链接），协议不允许擅自替换")
        profile["args"] = override_model(profile["args"], model_override)
    profile["args"] = prepare_launch_args(profile["kind"], profile["args"], cwd, repo)
    all_agents = agents()
    scheduler = current_scheduler(all_agents)
    registry = read_registry()
    tag, issue = member_tag(scheduler)
    members = same_role(all_agents, profile_name, scheduler, registry)
    idle_candidates = eligible_idle(members, exclude, tag, cwd)
    member = idle_candidates[0] if idle_candidates else None
    base = f"{tag}-{profile_name}"
    label = pane_label(profile_name, profile["args"], task, issue)

    if member:
        pane = {"pane_id": member["pane_id"], "tab_id": member["tab_id"]}
        old = str(member["name"])
        same_cwd = bool(member.get("_same_cwd"))
        same_issue = re.fullmatch(rf"{re.escape(base)}(-\d+)?", old)
        name = old if same_issue else free_name(base, all_agents, pane["pane_id"])
        if not NAME_RE.fullmatch(name):
            raise ClaimError(f"登记名过长或不合法：{name}（协议规定不截断，报阻塞）")
        herdr("pane", "rename", pane["pane_id"], label)
        if (same_cwd and keep_session and member.get("agent") == profile["kind"]
                and running_args_match(pane["pane_id"], profile["args"])):
            if name != old:
                herdr("agent", "rename", pane["pane_id"], name)
            action, agent = "kept", member
        else:
            stop_agent(member)
            if not same_cwd:
                switch_pane_cwd(pane["pane_id"], cwd)
            action, agent = "restarted", start_agent(name, pane["pane_id"], profile)
    else:
        name = free_name(base, all_agents, None)
        if not NAME_RE.fullmatch(name):
            raise ClaimError(f"登记名过长或不合法：{name}（协议规定不截断，报阻塞）")
        pane = new_pane(scheduler, registry, cwd, issue)
        action = "created"
        try:
            herdr("pane", "rename", pane["pane_id"], label)
            agent = start_agent(name, pane["pane_id"], profile)
        except (RuntimeError, KeyError):
            try:
                herdr("pane", "close", pane["pane_id"])
            except (RuntimeError, KeyError, AssertionError):
                subprocess.run([_START.HERDR, "pane", "close", pane["pane_id"]], capture_output=True, timeout=10,
                               check=False)
            raise

    sibling_dups = _sibling_role_duplicate_groups(all_agents, profile_name, scheduler, registry, exclude, tag, cwd)
    pruned_panes: list[str] = []
    pruned_names: list[str] = []
    live_panes: Optional[set[str]] = None
    if len(idle_candidates) > 1 or sibling_dups or registry:
        panes_snapshot = herdr("pane", "list")["result"]["panes"]
        if len(idle_candidates) > 1 and member is not None:
            p_ids, p_names = prune_duplicate_idle(
                idle_candidates[1:], base, pane["pane_id"], scheduler["pane_id"], panes_snapshot
            )
            pruned_panes.extend(p_ids)
            pruned_names.extend(p_names)
            panes_snapshot = [p for p in panes_snapshot if p.get("pane_id") not in set(p_ids)]
        for other_base, kept_other_pane, extras in sibling_dups:
            p_ids, p_names = prune_duplicate_idle(
                extras, other_base, kept_other_pane, scheduler["pane_id"], panes_snapshot
            )
            pruned_panes.extend(p_ids)
            pruned_names.extend(p_names)
            panes_snapshot = [p for p in panes_snapshot if p.get("pane_id") not in set(p_ids)]
        live_panes = {str(p.get("pane_id")) for p in panes_snapshot if p.get("pane_id")} - set(pruned_panes)

    record(pane, scheduler["name"], profile_name, drop_panes=pruned_panes, live_panes=live_panes, issue=issue)
    pruned_set = set(pruned_panes)
    picked_pane = pane["pane_id"] if member is not None else None
    busy = [m["name"] for m in members if m.get("pane_id") != picked_pane and m.get("pane_id") not in pruned_set]
    return {"action": action, "name": name, "pane_id": pane["pane_id"], "tab_id": pane["tab_id"],
            "session": (agent.get("agent_session") or {}).get("value"), "kind": profile["kind"],
            "args": profile["args"], "label": label, "created_by": scheduler["name"], "other_same_role": busy,
            "pruned_idle": pruned_names,
            "model_override": {"model": model_override, "authorized_by": authorized_by} if model_override else None}


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="按角色领取 Herdr 成员（复用优先，参数取角色配置）")
    parser.add_argument("profile")
    parser.add_argument("--task", required=True, help="pane 标题里的任务字段，如 #453评论缺口核查")
    parser.add_argument("--cwd", default=os.getcwd(), help="新建成员的工作目录（worktree），默认当前目录")
    parser.add_argument("--exclude", action="append", default=[], help="还有在途任务、不能复用的成员登记名")
    parser.add_argument("--keep-session", action="store_true",
                        help="同一 issue 回改时沿用旧会话（命令行与配置不一致时仍会重启）")
    parser.add_argument("--model-override", help="用户授权临时换模型（如额度耗尽），只替换模型值")
    parser.add_argument("--authorized-by", help="换模型的用户授权出处（issue 评论链接），--model-override 必填")
    opts = parser.parse_args(argv)
    try:
        print(json.dumps(claim(opts.profile, opts.task, opts.cwd, opts.exclude, opts.keep_session,
                               opts.model_override, opts.authorized_by),
                         ensure_ascii=False))
        return 0
    except (ClaimError, RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError,
            yaml.YAMLError) as error:
        print(f"member-start failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
