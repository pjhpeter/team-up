#!/usr/bin/env python3
"""用指定模型/思考强度新开一个调度者，供 team-up 入口在 issue 指定了模型时使用。

    PROMPT=<需求文本> python3 ~/.hwf/scripts/start-scheduler.py <repo> <model> <thinking>

model / thinking 可以留空（空串表示用 omp 默认），但不能都为空（都为空时入口走普通 scheduler profile）。
步骤：在当前 workspace 新开一个 tab → 在它的 shell 里以登记名 `delivery-scheduler-<handle>` 启动 omp
（--model/--thinking/--config ~/.hwf/scheduler-omp.yml/--approval-mode yolo）→ 发送 PROMPT。直接用登记名启动，调度者不用再改名。
任一步失败就关掉刚开的 tab 并以非 0 退出，交给入口的 on_failure 通知；不留下没有调度者的空 tab。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Sequence

HERDR = os.environ.get("HERDR_DISCOVERY_BIN") or os.environ.get("HERDR_BIN_PATH") or "herdr"
START_TIMEOUT_MS = 90000
SHELL_RETRIES = 10  # 新 tab 的 shell 可能还没出提示符，agent start 会拒绝，隔一秒重试


def herdr(*args: str, timeout: int = 30) -> dict[str, Any]:
    result = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def handle(pane: str) -> str:
    """PROTOCOL 登记名规则：pane id 转小写，非 [a-z0-9] 连续段换成单个 -，去掉首尾 -。"""
    return re.sub(r"[^a-z0-9]+", "-", pane.lower()).strip("-")


def start(repo: str, model: str, thinking: str, prompt: str) -> str:
    args = ["tab", "create", "--cwd", repo, "--label", f"scheduler {model or 'default'} {thinking}".strip(),
            "--no-focus"]
    if os.environ.get("HERDR_WORKSPACE_ID"):
        args[2:2] = ["--workspace", os.environ["HERDR_WORKSPACE_ID"]]
    created = herdr(*args)["result"]
    tab, pane = created["tab"]["tab_id"], created["root_pane"]["pane_id"]
    name = f"delivery-scheduler-{handle(pane)}"
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise RuntimeError(f"registration name too long or invalid: {name}")
        # 调度者专用配置叠加：提前压缩上下文，降低长会话词元消耗（2026-10-28 用户同意）。
        omp_args = ["--config", os.path.expanduser("~/.hwf/scheduler-omp.yml"), "--approval-mode", "yolo"]
        if model:
            omp_args[:0] = ["--model", model]
        if thinking:
            omp_args[:0] = ["--thinking", thinking]
        for attempt in range(SHELL_RETRIES):
            try:
                herdr("agent", "start", name, "--kind", "omp", "--pane", pane, "--timeout", str(START_TIMEOUT_MS),
                      "--", *omp_args, timeout=START_TIMEOUT_MS // 1000 + 10)
                break
            except RuntimeError as error:
                if "shell" not in str(error).lower() or attempt == SHELL_RETRIES - 1:
                    raise
                time.sleep(1)
        # 共用的新建提示让调度者自己登记改名；这里已经用登记名启动，提前说明，免得它再改名。
        note = (f"（入口已用登记名 {name} 启动你，模型 {model or 'omp 默认'}、思考强度 {thinking or '默认'}；"
                "你已登记，用 herdr agent get 回读核对即可，不要改名。）\n")
        herdr("agent", "prompt", name, note + prompt)
        return name
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        subprocess.run([HERDR, "tab", "close", tab], capture_output=True, timeout=10, check=False)
        raise


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3 or not (argv[1] or argv[2]) or not os.environ.get("PROMPT"):
        print("usage: PROMPT=... start-scheduler.py <repo> <model> <thinking> (model or thinking required)",
              file=sys.stderr)
        return 1
    try:
        print(start(argv[0], argv[1], argv[2], os.environ["PROMPT"]))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"start scheduler failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
