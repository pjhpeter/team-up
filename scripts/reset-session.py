#!/usr/bin/env python3
"""让一个 Herdr 里的 omp 调度者开新会话（清空上下文），确认会话确实换了才算成功。

    python3 ~/.hwf/scripts/reset-session.py <pane-or-name>

exit 0 + stdout = 新会话路径；exit 1 = 未能确认（原因写 stderr），调用方不得当作已清空。
team-up 入口复用调度者前、GitLab 收件箱处理用户 `/new` 评论时都调用本脚本。

只在 idle/done 时清空：正在干活时插入 /new 会丢掉进行中的回合。2026-10-22 实测：向 idle 的 omp
发送 `/new` 后 1 秒内 agent_session 换成新文件，紧接着发送的消息由新会话接收、不记得旧内容。
herdr 对 `/new` 会报 agent_prompt_stalled（命令不进入 working），所以不用 --wait，改为轮询会话路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Sequence

WAIT_S = 15
HERDR = os.environ.get("HERDR_DISCOVERY_BIN") or os.environ.get("HERDR_BIN_PATH") or "herdr"


def agent(target: str) -> dict[str, Any]:
    result = subprocess.run([HERDR, "agent", "get", target], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr agent get {target} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)["result"]["agent"]


def reset(target: str) -> str:
    before = agent(target)
    if before.get("agent_status") not in ("idle", "done"):
        raise RuntimeError(f"{target} is {before.get('agent_status')}, not idle")
    old = before["agent_session"]["value"]
    sent = subprocess.run([HERDR, "agent", "prompt", target, "/new"], capture_output=True, text=True,
                          timeout=10, check=False)
    if sent.returncode != 0 and "agent_prompt_stalled" not in sent.stdout + sent.stderr:
        raise RuntimeError(f"sending /new to {target} failed: {sent.stderr.strip() or sent.stdout.strip()}")
    deadline = time.time() + WAIT_S
    while time.time() < deadline:
        session = agent(target)["agent_session"]["value"]
        if session != old:
            return session
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
