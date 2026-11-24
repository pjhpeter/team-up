#!/usr/bin/env python3
"""调度者等成员的唯一入口：一边等状态，一边看屏幕，成员卡住时马上报出来。

    python3 ~/.hwf/scripts/member-wait.py <成员> [--until idle --until done ...] [--repo <主仓库> --task <任务标识>]

为什么有它：2026-11-19 #484 评审成员开工后不久断网，屏幕一直是「Reconnecting... waiting for network」，
herdr 仍报 working。调度者用 `herdr agent wait` 先等 40 分钟、又续等 90 分钟，中间没看过屏幕，
卡了约 2 小时才发现；正文顶部一直写「收代码评审结论」，用户也看不出它停了。

做法：
- 单次最多等 10 分钟（不能调长），每 30 秒查一次状态，同时读屏幕末尾。
- 屏幕末尾的断网/重连、模型服务报错、登录失效、额度用完等字样停留 5 分钟不变，或成员 working 但屏幕 15 分钟没变，
  就判卡住（短暂断网会自己重连，不算）。
  屏幕是否变化跨多次等待累计（记在 ~/.hwf/state/member-wait/），所以续挂不会把计时清零。
- 带 --repo/--task 时，卡住会自动写进 issue 正文顶部（经 task-record.py state-set）；
  需要用户处理（登录、代理、额度，或同一任务 60 分钟内第二次卡住）时写进「等你处理」并发 ntfy。

退出码：0 到达目标状态；3 等满 10 分钟仍在干活、屏幕在变（照常续挂）；
4 卡住、调度者换新会话重做；5 卡住、需要用户处理；1 失败；2 参数错误。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Optional

HERDR = os.environ.get("HERDR_BIN", "herdr")
STATE_DIR = Path(os.environ.get("HWF_STATE_DIR") or Path.home() / ".hwf/state") / "member-wait"
NTFY_URL = "https://ntfy.sh/pjh-herdr-task"
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import jev_client as JC

MAX_WAIT_S, POLL_S, ERROR_GRACE_S, JEV_QUIET_S, QUIET_S, REPEAT_S = 600, 30, 300, 180, 900, 3600
JEV_STALL_PROB_MIN = 0.80
EXIT_OK, EXIT_ERROR, EXIT_TIMEOUT, EXIT_STUCK, EXIT_USER = 0, 1, 3, 4, 5
STATUSES = ("idle", "working", "blocked", "done", "unknown")
TAIL_LINES = 15
# 本机环境问题：换会话也没用，要用户处理。
NEED_USER = re.compile(r"503 Service Unavailable|local proxy failed|\b401\b|Unauthorized|token (?:has )?expired|"
                       r"not logged in|please (?:re-?)?log ?in|/login|usage limit|quota|rate limit exceeded",
                       re.IGNORECASE)
# 网络或会话断了：换个新会话通常就好。
NETWORK = re.compile(r"Reconnecting|waiting for network|Connection failed|error sending request|"
                     r"stream disconnected|connection (?:reset|refused|closed)", re.IGNORECASE)


class WaitError(RuntimeError):
    pass


def herdr(*args: str) -> str:
    result = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise WaitError(f"herdr {' '.join(args[:3])} 失败：{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def agent(target: str) -> dict[str, Any]:
    return json.loads(herdr("agent", "get", target)).get("result", {}).get("agent", {})


def is_missing_agent(error: Exception) -> bool:
    text = str(error)
    return "agent_not_found" in text or "pane_not_found" in text


def screen_tail(target: str) -> str:
    """屏幕末尾的正文：去掉随费用/计时刷新的状态栏与转圈计时，只留能判断有没有进展的文字。

    注意：成员处于 working 时，herdr 对 recent-unwrapped 会报 agent_not_idle；
    固定用 --source visible 可在运行态稳定读取屏幕，且跨轮次保持同一折行口径。
    """
    lines = herdr("agent", "read", target, "--lines", "60", "--source", "visible").rstrip("\n").split("\n")
    body = lines[:-4] if len(lines) > 4 else lines
    cleaned = [re.sub(r"\(\d+[hms][\d hms]*•[^)]*\)|\d+h \d+m \d+s|\d+m \d+s|\d+s\b|[\u2800-\u28ff]", "", line)
               for line in body]
    return "\n".join(line for line in cleaned if line.strip())


def diagnose(tail: str) -> tuple[str, str]:
    """返回（类别, 原因）；类别 network / user / ""（看不出问题）。"""
    last = "\n".join(tail.split("\n")[-TAIL_LINES:])
    if match := NEED_USER.search(last):
        return "user", f"屏幕报「{match.group(0)}」，像是登录、代理或额度问题"
    if match := NETWORK.search(last):
        return "network", f"屏幕报「{match.group(0)}」，断网或连接中断"
    return "", ""


def state_file(pane: str) -> Path:
    return STATE_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", pane) + ".json")


def remember(pane: str, digest: str, now: float) -> tuple[float, dict[str, Any]]:
    """记下屏幕指纹，返回（画面静止起点秒数, 持久化状态字典）。同指纹保留 Jev 诊断缓存，避免每 30 秒重复请求。"""
    path = state_file(pane)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("digest") == digest:
        since = float(saved.get("since", now))
        record = dict(saved)
    else:
        since = now
        record = {"digest": digest, "since": since}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return since, record


def save_pane_state(pane: str, record: dict[str, Any]) -> None:
    path = state_file(pane)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def diagnose_quiet_with_jev(tail: str, client: Optional[JC.JevClient] = None) -> tuple[str, str]:
    """当成员 working 但屏幕已静默满 3 分钟且未命中固定正则时，用 Jev 识别隐藏的交互确认、鉴权失效或断连。"""
    jev = client if client is not None else JC.default_client()
    snippet = "\n".join(tail.split("\n")[-30:])[-4000:]
    if not snippet.strip():
        return "", ""
    try:
        res = jev.decide(
            state={"terminal_screen_tail": snippet},
            questions={
                "stall_kind": JC.Choice(
                    "A coding agent pane reports status=working, and its visible terminal screen has not changed for 3 minutes. Classify what the screen shows.",
                    {
                        "normal_running": "Normal long-running command, test run, build, compilation, or model thinking with no blocking prompt or fatal error.",
                        "interactive_prompt": "Blocked on an interactive confirmation (y/N), password/passphrase prompt, login prompt, editor prompt, or human input.",
                        "auth_or_quota": "Authentication/session failure, proxy/gateway outage, 401/402/403/503 error, or quota/rate-limit exhaustion.",
                        "network_drop": "Network disconnect, socket/TLS hang, stream dropped, or stuck retry loop.",
                    },
                )
            },
        )
        selected, _, probs = res.choice("stall_kind")
    except JC.JevUnavailableError:
        return "", ""

    prob = probs.get(selected, 0.0)
    if prob < JEV_STALL_PROB_MIN or selected == "normal_running":
        return "normal", ""
    if selected == "interactive_prompt":
        return "user_immediate", f"屏幕停在交互确认或输入提示（Jev 置信度 {prob:.0%}）"
    if selected == "auth_or_quota":
        return "user", f"屏幕出现鉴权、代理或额度异常（Jev 置信度 {prob:.0%}）"
    if selected == "network_drop":
        return "network", f"屏幕处于断网或连接挂死（Jev 置信度 {prob:.0%}）"
    return "normal", ""


def load_record() -> Any:
    spec = importlib.util.spec_from_file_location("task_record", Path(__file__).with_name("task-record.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repeated(repo: str, task: str, now: float) -> bool:
    """同一任务 60 分钟内已卡过一次（换过会话又卡），就不再自己重试。"""
    log = Path(repo) / ".hwf/tmp" / task / "stalls.jsonl"
    try:
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        events = []
    return any(now - float(e.get("at", 0)) < REPEAT_S for e in events)


def record_stall(repo: str, task: str, name: str, reason: str, minutes: int, needs_user: bool, now: float) -> None:
    log = Path(repo) / ".hwf/tmp" / task / "stalls.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now, "member": name, "reason": reason, "user": needs_user},
                                ensure_ascii=False) + "\n")
    tr = load_record()
    path = tr.state_path(repo, task)
    old = tr.read_top(path.read_text(encoding="utf-8")).get("当前阶段", "") if path.exists() else ""
    old = re.sub(r"^⚠[^；]*；", "", old)
    if needs_user:
        line = f"⚠ {name} 卡住 {minutes} 分钟（{reason}），停下等你处理；"
        tr.state_set(repo, task, "待用户事项", f"{name} {reason}；请处理后在 issue 回复「好了」。")
    else:
        line = f"⚠ {name} 卡住 {minutes} 分钟（{reason}），正在换新会话重做；"
    tr.state_set(repo, task, "当前阶段", (line + old)[:tr.NOW_LIMIT])


def ntfy(title: str, body: str) -> bool:
    try:
        result = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "15",
                                 "-H", f"Title: {title}", "-d", body, NTFY_URL],
                                capture_output=True, text=True, timeout=30, check=False)
        return result.stdout.strip().startswith("2")
    except (OSError, subprocess.SubprocessError):
        return False


def wait(target: str, until: set[str], repo: Optional[str], task: Optional[str],
         clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
         *, jev: Optional[JC.JevClient] = None) -> tuple[int, str]:
    start = clock()
    last_pane = target
    while True:
        try:
            info = agent(target)
            status = str(info.get("agent_status") or "unknown")
            name = str(info.get("name") or target)
            pane = str(info.get("pane_id") or target)
            last_pane = pane
            if status in until:
                state_file(pane).unlink(missing_ok=True)
                return EXIT_OK, f"{name} 已到 {status}"
            now = clock()
            tail = screen_tail(target)
        except WaitError as error:
            if is_missing_agent(error):
                state_file(last_pane).unlink(missing_ok=True)
                return EXIT_ERROR, f"{target} 已不存在（会话已退出或窗格已关闭），请用 STATE/herdr pane list 核对产物后处理"
            raise
        kind, reason = diagnose(tail)
        since, pane_record = remember(pane, hashlib.sha256(tail.encode()).hexdigest(), now)
        quiet = now - since
        # 报错字样要停留 5 分钟（画面不再变化）才算卡住：短暂断网成员自己会重连成功。
        if kind and quiet < ERROR_GRACE_S:
            kind = ""
        if not kind and status == "working" and quiet >= JEV_QUIET_S:
            if "jev_kind" not in pane_record:
                j_kind, j_reason = diagnose_quiet_with_jev(tail, jev)
                if j_kind:
                    pane_record["jev_kind"] = j_kind
                    pane_record["jev_reason"] = j_reason
                    save_pane_state(pane, pane_record)
            cached_kind = pane_record.get("jev_kind", "")
            cached_reason = pane_record.get("jev_reason", "")
            if cached_kind == "user_immediate":
                kind, reason = "user", cached_reason
            elif cached_kind in ("user", "network") and quiet >= ERROR_GRACE_S:
                kind, reason = cached_kind, cached_reason
        if not kind and status == "working" and quiet >= QUIET_S:
            kind, reason = "network", f"working 但屏幕 {int(quiet // 60)} 分钟没有任何变化"
        if kind:
            minutes = max(int(quiet // 60), 1)
            needs_user = kind == "user" or bool(repo and task and repeated(repo, task, now))
            if repo and task:
                try:
                    record_stall(repo, task, name, reason, minutes, needs_user, now)
                except Exception as error:  # noqa: BLE001 - 写正文失败不能吞掉卡住结论
                    reason += f"（正文没更新成功：{error}，请手动 state-set）"
            if needs_user:
                sent = ntfy(f"成员卡住：{name}", f"{task or name}：{reason}。需要你处理。")
                return EXIT_USER, (f"{name} 卡住，需要用户处理：{reason}。不要再换会话重试；"
                                   f"已写进「等你处理」，ntfy {'已发送' if sent else '未发送成功'}。")
            return EXIT_STUCK, (f"{name} 卡住：{reason}。关掉这个会话（herdr pane close {pane}），"
                                "用 member-start.py 开新会话，把原派活内容原样重发，再用 member-wait.py 等。")
        if now - start >= MAX_WAIT_S:
            return EXIT_TIMEOUT, f"{name} 等满 10 分钟仍是 {status}，屏幕在变；按协议问一次 checkpoint 后续挂"
        sleep(POLL_S)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="成员登记名或 pane")
    parser.add_argument("--until", action="append", choices=STATUSES,
                        help="到达哪些状态就返回；默认 idle、done、blocked")
    parser.add_argument("--repo", help="主仓库路径；和 --task 一起给，卡住时自动更新 issue 正文")
    parser.add_argument("--task", help="任务标识，如 frontend-web-v1-484")
    opts = parser.parse_args(argv)
    if bool(opts.repo) != bool(opts.task):
        parser.error("--repo 和 --task 要一起给")
    try:
        code, text = wait(opts.target, set(opts.until or ("idle", "done", "blocked")), opts.repo, opts.task)
    except (WaitError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"member-wait failed: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
