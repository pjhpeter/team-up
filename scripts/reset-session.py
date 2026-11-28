#!/usr/bin/env python3
"""让一个 Herdr 里的调度者开新会话（清空上下文），确认会话确实已切换才算成功。

    python3 ~/.hwf/scripts/reset-session.py <pane-or-name>

exit 0 + stdout = 新会话标识（omp 等为新会话路径，agy 为 agy-cleared:<时间戳>）；
exit 1 = 未能确认（原因写 stderr），调用方不得当作已清空。
team-up 入口复用调度者前、GitLab 收件箱处理用户 `/new` 评论时都调用本脚本。

只在 idle/done 时清空：正在干活时插入清屏命令会丢掉进行中的回合。
- omp / codex / claude / droid（有 agent_session.value）：发送 `/new` 并轮询等待 agent_session 换新。
- agy（herdr 返回 agent_session 为 null）：发送 `/clear` 并等待回到 idle/done 且屏幕回到欢迎界面（或新增对话 db）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

WAIT_S = 15
HERDR = os.environ.get("HERDR_DISCOVERY_BIN") or os.environ.get("HERDR_BIN_PATH") or "herdr"
AGY_CONV_DIR = Path.home() / ".gemini/antigravity-cli/conversations"


def agent(target: str) -> dict[str, Any]:
    result = subprocess.run([HERDR, "agent", "get", target], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr agent get {target} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)["result"]["agent"]


def screen_text(target: str, lines: int = 40) -> str:
    result = subprocess.run(
        [HERDR, "agent", "read", target, "--lines", str(lines)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        return str(data.get("result", {}).get("text") or data.get("result", {}).get("content") or result.stdout)
    except ValueError:
        return result.stdout


def _agy_db_snapshot() -> set[str]:
    if not AGY_CONV_DIR.is_dir():
        return set()
    try:
        return {p.name for p in AGY_CONV_DIR.glob("*.db")}
    except OSError:
        return set()


def _reset_agy(target: str) -> str:
    before_dbs = _agy_db_snapshot()
    sent = subprocess.run(
        [HERDR, "agent", "prompt", target, "/clear"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if sent.returncode != 0 and "agent_prompt_stalled" not in sent.stdout + sent.stderr:
        raise RuntimeError(f"sending /clear to {target} failed: {sent.stderr.strip() or sent.stdout.strip()}")
    deadline = time.time() + WAIT_S
    while time.time() < deadline:
        cur = agent(target)
        if cur.get("agent_status") in ("idle", "done"):
            after_dbs = _agy_db_snapshot()
            screen = screen_text(target, lines=40)
            if (after_dbs - before_dbs) or ("Antigravity CLI" in screen):
                return f"agy-cleared:{int(time.time())}"
        time.sleep(0.5)
    raise RuntimeError(f"agy session of {target} did not confirm /clear within {WAIT_S}s")


def reset(target: str) -> str:
    before = agent(target)
    if before.get("agent_status") not in ("idle", "done"):
        raise RuntimeError(f"{target} is {before.get('agent_status')}, not idle")
    session_obj = before.get("agent_session")
    old = session_obj.get("value") if isinstance(session_obj, dict) else None
    kind = str(before.get("agent") or before.get("kind") or "").lower()
    if not old:
        if kind == "agy":
            return _reset_agy(target)
        raise RuntimeError(f"{target} has no agent_session (kind={kind or 'unknown'})")
    sent = subprocess.run([HERDR, "agent", "prompt", target, "/new"], capture_output=True, text=True,
                          timeout=10, check=False)
    if sent.returncode != 0 and "agent_prompt_stalled" not in sent.stdout + sent.stderr:
        raise RuntimeError(f"sending /new to {target} failed: {sent.stderr.strip() or sent.stdout.strip()}")
    deadline = time.time() + WAIT_S
    while time.time() < deadline:
        cur_session = agent(target).get("agent_session")
        session = cur_session.get("value") if isinstance(cur_session, dict) else None
        if session and session != old:
            return str(session)
        time.sleep(1)
    raise RuntimeError(f"session of {target} did not change within {WAIT_S}s")


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: reset-session.py <pane-or-name>", file=sys.stderr)
        return 1
    try:
        sys.stdout.write(reset(argv[0]))
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f"reset failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
