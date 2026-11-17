#!/usr/bin/env python3
"""调度者的「成员完成核对」一条命令版（2026-10-28 用户同意，用于降低调度者词元消耗）。

    python3 ~/.hwf/scripts/member-settle.py <成员> <落盘文件> [--lines 400] [--tail 40] [--interval 15]

等价于 PROTOCOL「派活、等待与结果收取」里的完成核对：agent get → 读屏 → 间隔 ≥15 秒 → agent get → 再读屏，
比较两次状态与屏幕内容是否都已稳定。

<落盘文件>（如 reviews/plan-r1.md）优先写成员最后一条回复的原始 Markdown（从成员的会话记录取）。
屏幕上的文字已被终端折行、去掉加粗和标题、表格变字符画，转贴到 GitLab 会很难看，所以只作退路；
完整读屏另存到 <落盘文件>.screen。取不到原文时 <落盘文件> 退回读屏，并打印 format: screen 与原因。
回复里有「## 给用户的摘要」时，另外生成 <落盘文件>.comment.md：摘要在前、完整内容折叠，直接当 issue 评论正文。
终端只打印状态、结论行与末尾几行，调度者不必把成员输出抄进自己的上下文。
状态为 blocked 时额外打印 detection 读屏，供调度者通知用户；本脚本不按任何键、不发 prompt。
它只在后台 agent wait 返回后调用一次，不是轮询工具。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN", "herdr")
VERDICT = re.compile(r"^\s*(通过|需修改|常规评审通过|预审完成|待澄清)\s*$")
SUMMARY_HEAD = "## 给用户的摘要"
FULL_HEAD = "## 完整内容"
SUMMARY_MAX_LINES = 15


def run(*args: str) -> str:
    result = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def agent_info(target: str) -> dict:
    return json.loads(run("agent", "get", target)).get("result", {}).get("agent", {})


def status(info: dict) -> str:
    return str(info.get("agent_status", "unknown"))


def body(screen: str) -> str:
    # 去掉末尾状态栏（会随费用/时间刷新），只比较正文是否还在变化。
    return "\n".join(screen.rstrip("\n").split("\n")[:-4])


def session_file(session: dict) -> str | None:
    """按 herdr 报告的 agent_session 找成员的会话记录文件；找不到返回 None。"""
    value = str(session.get("value") or "")
    if not value:
        return None
    if session.get("kind") == "path":
        return value if os.path.isfile(value) else None
    agent = session.get("agent")
    if agent == "claude":
        root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        pattern = os.path.join(root, "projects", "*", f"{glob.escape(value)}.jsonl")
    elif agent == "codex":
        root = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        pattern = os.path.join(root, "sessions", "**", f"rollout-*-{glob.escape(value)}.jsonl")
    elif agent == "droid":
        pattern = os.path.join(os.path.expanduser("~/.factory"), "sessions", "*", f"{glob.escape(value)}.jsonl")
    else:
        return None
    matches = glob.glob(pattern, recursive=True)
    return max(matches, key=os.path.getmtime) if matches else None


def record_text(record: dict) -> tuple[str, str] | None:
    """一行会话记录若是成员的正文回复，返回 (消息标识, 文字)；否则返回 None。兼容 claude/omp/codex/droid。"""
    if record.get("isSidechain"):  # Claude 子代理的回复，不是成员自己的
        return None
    if record.get("type") == "response_item":  # codex
        message = record.get("payload") or {}
        kinds = {"output_text"}
    else:  # claude: type=assistant；omp、droid: type=message
        message = record.get("message") or {}
        kinds = {"text"}
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") in kinds)
    return (str(message.get("id") or record.get("uuid") or record.get("id") or ""), text) if text.strip() else None


def last_reply(path: str) -> str | None:
    """取最后一条带文字的回复。Claude 会把一条回复拆成多行记录（message.id 相同），要拼回去。"""
    last_id, parts = None, []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                found = record_text(json.loads(line))
            except (ValueError, AttributeError):
                continue  # 成员还在写的半行，或不是对象的记录
            if not found:
                continue
            message_id, text = found
            if message_id and message_id == last_id:
                parts.append(text)
            else:
                last_id, parts = message_id, [text]
    return "".join(parts) if parts else None


def squeeze(text: str) -> str:
    """去掉空白和 Markdown 标记。屏幕上这些都会被改掉，比对只看文字本身。"""
    return re.sub(r"[\s*_`#>|~\\-]", "", text)


def on_screen(reply: str, screen: str) -> bool:
    """确认会话记录里的回复就是屏幕上这一轮的，防止取到上一轮的旧回复（如新任务还没开始写）。"""
    lines = [
        line for line in reply.split("\n")
        if line.strip() and not line.lstrip().startswith("|") and "](" not in line and "<" not in line
        and not line.strip().startswith("```")
    ][-10:]  # 只看结尾附近：长回复的开头可能已滚出读屏范围
    if not lines:
        return True  # 整条都是表格等无法比对的内容，不据此拒绝
    return squeeze(max(lines, key=len)) in squeeze(screen)


def read_reply(info: dict, screen: str) -> tuple[str | None, str]:
    """返回 (原始 Markdown, 说明)。取不到时原始 Markdown 为 None，说明写原因。不因此让核对失败。"""
    session = info.get("agent_session")
    if not isinstance(session, dict):
        return None, "herdr 没有报告这个成员的会话记录"
    path = session_file(session)
    if not path:
        return None, f"找不到 {session.get('agent')} 的会话记录文件"
    try:
        reply = last_reply(path)
    except OSError as error:
        return None, f"读会话记录失败：{error}"
    if not reply:
        return None, f"会话记录里没有回复：{path}"
    if not on_screen(reply, screen):
        return None, f"会话记录最后一条回复与屏幕对不上（可能是旧回复）：{path}"
    return reply, path


def text_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def comment_body(reply: str) -> tuple[str | None, str]:
    """把成员回复变成给用户的评论：摘要在前，完整内容折叠。返回 (评论正文, 说明)，不合要求时正文为 None。"""
    lines = reply.strip().split("\n")
    heads = [i for i, line in enumerate(lines) if line.strip() == SUMMARY_HEAD]
    fulls = [i for i, line in enumerate(lines) if line.strip() == FULL_HEAD]
    if heads and fulls and heads[0] < fulls[0]:
        summary = "\n".join(lines[heads[0] + 1:fulls[0]]).strip()
        full = "\n".join(lines[fulls[0] + 1:]).strip()
        if not summary:
            return None, "摘要是空的"
        if text_lines(summary) > SUMMARY_MAX_LINES:
            return None, f"摘要超过 {SUMMARY_MAX_LINES} 行"
        return f"{summary}\n\n<details><summary>完整内容</summary>\n\n{full}\n\n</details>\n", "摘要 + 折叠全文"
    if text_lines(reply) <= SUMMARY_MAX_LINES:
        return reply.strip() + "\n", "回复够短，不用折叠"
    return None, f"回复超过 {SUMMARY_MAX_LINES} 行，却没有「{SUMMARY_HEAD}」和「{FULL_HEAD}」两段"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("outfile")
    parser.add_argument("--lines", type=int, default=400)
    parser.add_argument("--tail", type=int, default=40)
    parser.add_argument("--interval", type=int, default=15)
    opts = parser.parse_args()

    first_status = status(agent_info(opts.target))
    first_screen = run("agent", "read", opts.target, "--lines", str(opts.lines), "--source", "recent-unwrapped")
    time.sleep(max(opts.interval, 15))
    info = agent_info(opts.target)
    second_status = status(info)
    screen = run("agent", "read", opts.target, "--lines", str(opts.lines), "--source", "recent-unwrapped")

    reply, source = read_reply(info, screen)
    result = reply if reply is not None else screen
    if not result.endswith("\n"):
        result += "\n"
    os.makedirs(os.path.dirname(os.path.abspath(opts.outfile)), exist_ok=True)
    with open(opts.outfile, "w", encoding="utf-8") as handle:
        handle.write(result)
    with open(f"{opts.outfile}.screen", "w", encoding="utf-8") as handle:
        handle.write(screen)

    settled = {"idle", "done"}
    stable = body(first_screen) == body(screen)
    verdicts = [line.strip() for line in result.split("\n") if VERDICT.match(line)]
    finished = first_status in settled and second_status in settled and stable

    print(f"status: {first_status} -> {second_status} | screen stable: {'yes' if stable else 'no'} | "
          f"finished: {'yes' if finished else 'no'}")
    print(f"saved: {opts.outfile} ({result.count(chr(10))} lines)")
    if reply is not None:
        print(f"format: markdown（成员原文，来自 {source}）")
        comment, why = comment_body(reply)
        if comment is None:
            print(f"comment: none（{why}）")
        else:
            with open(f"{opts.outfile}.comment.md", "w", encoding="utf-8") as handle:
                handle.write(comment)
            print(f"comment: {opts.outfile}.comment.md（{why}）")
    else:
        print(f"format: screen（屏幕文字，排版已损坏，转贴时要注明）| 原因：{source}")
    print(f"verdict line: {verdicts[-1] if verdicts else 'none'}")
    if second_status == "blocked":
        print("--- blocked: detection ---")
        print(run("agent", "read", opts.target, "--source", "detection").rstrip())
    print(f"--- last {opts.tail} lines ---")
    print("\n".join(result.rstrip("\n").split("\n")[-opts.tail:]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - 入口脚本统一报错退出
        print(f"member-settle failed: {error}", file=sys.stderr)
        sys.exit(1)
