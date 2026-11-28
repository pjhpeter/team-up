#!/usr/bin/env python3
"""按 roles.yaml（叠加可选的 issue 指定模型/思考强度）新开一个调度者，供 team-up 入口使用。

    PROMPT=<需求文本> python3 ~/.hwf/scripts/start-scheduler.py <repo> [model] [thinking]

model / thinking 均可以留空（空串表示沿用 roles.yaml / roles.local.yaml 的 team.scheduler 默认值）。
步骤：在当前 workspace 新开一个 tab → 在它的 shell 里以登记名 `delivery-scheduler-<handle>` 启动配置的调度者工具
（如 agy + --settings trustedWorkspaces，或 omp / claude 等）→ 发送 PROMPT。直接用登记名启动，调度者不用再改名。
任一步失败就关掉刚开的 tab 并以非 0 退出，交给入口的 on_failure 通知；不留下没有调度者的空 tab。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

_RC_SPEC = importlib.util.spec_from_file_location("hwf_role_config", Path(__file__).with_name("role_config.py"))
assert _RC_SPEC is not None and _RC_SPEC.loader is not None
_RC = importlib.util.module_from_spec(_RC_SPEC)
sys.modules.setdefault("hwf_role_config", _RC)
_RC_SPEC.loader.exec_module(_RC)

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
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


def resolve_scheduler_launch(
    model: str,
    thinking: str,
    *,
    repo: Optional[str] = None,
    hwf_home: Path = HWF_HOME,
) -> tuple[str, list[str]]:
    """按 roles.yaml 的 team.scheduler 配置解析调度者工具与参数，并叠加 issue 指定的 model/thinking。"""
    spec = _RC.resolve_team_role("scheduler", hwf_home=hwf_home) or {
        "tool": "agy",
        "model": "gemini-4-neon-medium",
        "thinking": "medium",
        "yolo": True,
    }
    merged = dict(spec)
    if model:
        merged["model"] = model
    if thinking:
        merged["thinking"] = thinking
    compiled = _RC.compile_team_profile(
        "scheduler", merged, hwf_home=hwf_home, write_settings=True, settings_subdir="state"
    )
    kind = str(compiled["kind"])
    launch_args = list(compiled["args"])
    if repo:
        launch_args = _RC.prepare_agy_trusted_args(kind, launch_args, repo, Path(repo))
    return kind, launch_args


def _effective_label_parts(model: str, thinking: str, launch_args: Sequence[str]) -> tuple[str, str]:
    eff_model = model
    eff_thinking = thinking
    for i, arg in enumerate(launch_args):
        if not eff_model and arg in ("--model", "-m") and i + 1 < len(launch_args):
            eff_model = str(launch_args[i + 1]).rsplit("/", 1)[-1]
        elif not eff_thinking and arg in ("--effort", "--thinking") and i + 1 < len(launch_args):
            eff_thinking = str(launch_args[i + 1])
    return eff_model or "default", eff_thinking


def start(repo: str, model: str, thinking: str, prompt: str) -> str:
    kind, launch_args = resolve_scheduler_launch(model, thinking, repo=repo)
    label_model, label_thinking = _effective_label_parts(model, thinking, launch_args)
    args = ["tab", "create", "--cwd", repo, "--label", f"scheduler {label_model} {label_thinking}".strip(),
            "--no-focus"]
    if os.environ.get("HERDR_WORKSPACE_ID"):
        args[2:2] = ["--workspace", os.environ["HERDR_WORKSPACE_ID"]]
    created = herdr(*args)["result"]
    tab, pane = created["tab"]["tab_id"], created["root_pane"]["pane_id"]
    name = f"delivery-scheduler-{handle(pane)}"
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise RuntimeError(f"registration name too long or invalid: {name}")
        for attempt in range(SHELL_RETRIES):
            try:
                herdr("agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", str(START_TIMEOUT_MS),
                      "--", *launch_args, timeout=START_TIMEOUT_MS // 1000 + 10)
                break
            except RuntimeError as error:
                if "shell" not in str(error).lower() or attempt == SHELL_RETRIES - 1:
                    raise
                time.sleep(1)
        # 入口已直接用登记名启动，提前说明，免得调度者再改名。
        note = (f"（入口已用登记名 {name} 启动你，工具 {kind}、模型 {label_model}、思考强度 {label_thinking or '默认'}；"
                "你已登记，用 herdr agent get 回读核对即可，不要改名。）\n")
        herdr("agent", "prompt", name, note + prompt)
        return name
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
        subprocess.run([HERDR, "tab", "close", tab], capture_output=True, timeout=10, check=False)
        raise


def main(argv: Sequence[str]) -> int:
    if len(argv) not in (1, 2, 3) or not argv[0] or not os.environ.get("PROMPT"):
        print("usage: PROMPT=... start-scheduler.py <repo> [model] [thinking]",
              file=sys.stderr)
        return 1
    model = argv[1] if len(argv) > 1 else ""
    thinking = argv[2] if len(argv) > 2 else ""
    try:
        print(start(argv[0], model, thinking, os.environ["PROMPT"]))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"start scheduler failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
