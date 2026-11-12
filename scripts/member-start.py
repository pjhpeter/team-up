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
   - pane 的工作目录就是 --cwd：重启只能在原 pane 原目录，目录不同会在错的 worktree 里干活。
   2026-11-09 #453：脚本把 #464 未释放的开发复用过来、在 #464 的 worktree 里重启，调度者只好再建一个。
4. 有空闲成员：--keep-session 且正在跑的命令行与配置一致 → 原样交回；否则在同一 pane 退出旧会话、按配置重启。
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

START_TIMEOUT_MS = 90000
SHELL_RETRIES = 10
EXIT_WAIT_S = 30
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


def load_profile(name: str, layers: Sequence[Path]) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for path in layers:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            profiles.update(data.get("profiles") or {})  # 整条 entry 覆盖，不逐字段合并
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

def pane_label(profile: str, args: Sequence[str], task: str) -> str:
    model = (model_of(args) or "").rsplit("/", 1)[-1]
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


def record(pane: dict[str, str], scheduler: str, profile: str) -> None:
    data = read_registry()
    data[pane["pane_id"]] = {"scheduler": scheduler, "profile": profile, "tab_id": pane["tab_id"],
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
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


def reusable(member: dict[str, Any], tag: str, cwd: str) -> bool:
    """本 issue（或建 issue 前）的成员，且 pane 就在要干活的目录里。"""
    if not re.fullmatch(rf"({re.escape(tag)}|new)-.+", str(member.get("name") or "")):
        return False
    pane = herdr("pane", "get", member["pane_id"])["result"]["pane"]
    return same_dir(pane.get("cwd"), cwd)


def pick_idle(members: Sequence[dict[str, Any]], exclude: Sequence[str], tag: str, cwd: str) -> Optional[dict[str, Any]]:
    idle = [m for m in members if m.get("agent_status") in ("idle", "done") and m.get("name") not in exclude
            and reusable(m, tag, cwd)]
    idle.sort(key=lambda m: not str(m.get("name") or "").startswith(f"{tag}-"))  # 同一 issue 的优先
    return idle[0] if idle else None


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
    all_agents = agents()
    scheduler = current_scheduler(all_agents)
    registry = read_registry()
    tag, issue = member_tag(scheduler)
    members = same_role(all_agents, profile_name, scheduler, registry)
    member = pick_idle(members, exclude, tag, cwd)
    base = f"{tag}-{profile_name}"
    label = pane_label(profile_name, profile["args"], task)

    if member:
        pane = {"pane_id": member["pane_id"], "tab_id": member["tab_id"]}
        old = str(member["name"])
        same_issue = re.fullmatch(rf"{re.escape(base)}(-\d+)?", old)
        name = old if same_issue else free_name(base, all_agents, pane["pane_id"])
        if not NAME_RE.fullmatch(name):
            raise ClaimError(f"登记名过长或不合法：{name}（协议规定不截断，报阻塞）")
        herdr("pane", "rename", pane["pane_id"], label)
        if keep_session and running_args_match(pane["pane_id"], profile["args"]):
            if name != old:
                herdr("agent", "rename", pane["pane_id"], name)
            action, agent = "kept", member
        else:
            stop_agent(member)
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
            subprocess.run([_START.HERDR, "pane", "close", pane["pane_id"]], capture_output=True, timeout=10,
                           check=False)
            raise
    record(pane, scheduler["name"], profile_name)
    busy = [m["name"] for m in members if m is not member]
    return {"action": action, "name": name, "pane_id": pane["pane_id"], "tab_id": pane["tab_id"],
            "session": (agent.get("agent_session") or {}).get("value"), "kind": profile["kind"],
            "args": profile["args"], "label": label, "created_by": scheduler["name"], "other_same_role": busy,
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
