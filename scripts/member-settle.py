#!/usr/bin/env python3
"""调度者的「成员完成核对」一条命令版（2026-10-28 用户同意，用于降低调度者词元消耗）。

    python3 ~/.hwf/scripts/member-settle.py <成员> <落盘文件> [--lines 400] [--tail 40] [--interval 15]

等价于 PROTOCOL「派活、等待与结果收取」里的完成核对：agent get → 读屏 → 间隔 ≥15 秒 → agent get → 再读屏，
比较两次状态与屏幕内容是否都已稳定。完整读屏原样写入 <落盘文件>（如 reviews/plan-r1.md），
终端只打印状态、结论行与末尾几行，调度者不必把成员输出抄进自己的上下文。
状态为 blocked 时额外打印 detection 读屏，供调度者通知用户；本脚本不按任何键、不发 prompt。
它只在后台 agent wait 返回后调用一次，不是轮询工具。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN", "herdr")
VERDICT = re.compile(r"^\s*(通过|需修改|常规评审通过|预审完成|待澄清)\s*$")


def run(*args: str) -> str:
    result = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def status(target: str) -> str:
    data = json.loads(run("agent", "get", target))
    return str(data.get("result", {}).get("agent", {}).get("agent_status", "unknown"))


def body(screen: str) -> str:
    # 去掉末尾状态栏（会随费用/时间刷新），只比较正文是否还在变化。
    return "\n".join(screen.rstrip("\n").split("\n")[:-4])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("outfile")
    parser.add_argument("--lines", type=int, default=400)
    parser.add_argument("--tail", type=int, default=40)
    parser.add_argument("--interval", type=int, default=15)
    opts = parser.parse_args()

    first_status = status(opts.target)
    first_screen = run("agent", "read", opts.target, "--lines", str(opts.lines), "--source", "recent-unwrapped")
    time.sleep(max(opts.interval, 15))
    second_status = status(opts.target)
    screen = run("agent", "read", opts.target, "--lines", str(opts.lines), "--source", "recent-unwrapped")

    os.makedirs(os.path.dirname(os.path.abspath(opts.outfile)), exist_ok=True)
    with open(opts.outfile, "w", encoding="utf-8") as handle:
        handle.write(screen)

    settled = {"idle", "done"}
    stable = body(first_screen) == body(screen)
    verdicts = [line.strip() for line in screen.split("\n") if VERDICT.match(line)]
    finished = first_status in settled and second_status in settled and stable

    print(f"status: {first_status} -> {second_status} | screen stable: {'yes' if stable else 'no'} | "
          f"finished: {'yes' if finished else 'no'}")
    print(f"saved: {opts.outfile} ({screen.count(chr(10))} lines)")
    print(f"verdict line: {verdicts[-1] if verdicts else 'none'}")
    if second_status == "blocked":
        print("--- blocked: detection ---")
        print(run("agent", "read", opts.target, "--source", "detection").rstrip())
    print(f"--- last {opts.tail} lines ---")
    print("\n".join(screen.rstrip("\n").split("\n")[-opts.tail:]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - 入口脚本统一报错退出
        print(f"member-settle failed: {error}", file=sys.stderr)
        sys.exit(1)
