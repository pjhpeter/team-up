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
import sqlite3
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN", "herdr")
VERDICT = re.compile(r"^\s*(通过|需修改|常规评审通过|预审完成|待澄清)\s*$")
SUMMARY_HEAD = "## 给用户的摘要"
FULL_HEAD = "## 完整内容"
# 兼容成员使用三四级标题、加粗标题、末尾冒号或省略「给/的」，避免仅因标题样式微差漏生成评论稿。
SUMMARY_LINE = re.compile(r"^\s*(?:#{1,4}\s+|\*\*\s*)给?用户的?摘要\s*(?:[:：]\s*)?\**\s*(?:[:：]\s*)?$")
FULL_LINE = re.compile(r"^\s*(?:#{1,4}\s+|\*\*\s*)完整内容\s*(?:[:：]\s*)?\**\s*(?:[:：]\s*)?$")
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


def read_screen(target: str, lines: int) -> str:
    """优先读未折行历史；成员刚在收尾仍报 agent_not_idle 时退回可视屏幕，避免第一次读屏直接报错退出。"""
    try:
        return run("agent", "read", target, "--lines", str(lines), "--source", "recent-unwrapped")
    except RuntimeError as error:
        if "agent_not_idle" not in str(error):
            raise
        return run("agent", "read", target, "--lines", str(lines), "--source", "visible")


def body(screen: str) -> str:
    # 去掉末尾状态栏（会随费用/时间刷新），只比较正文是否还在变化。
    return "\n".join(screen.rstrip("\n").split("\n")[:-4])


def agy_root() -> str:
    return os.environ.get("AGY_CONFIG_DIR") or os.path.expanduser("~/.gemini/antigravity-cli")


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
    elif agent == "agy":
        path = os.path.join(agy_root(), "conversations", f"{value}.db")
        return path if os.path.isfile(path) else None
    else:
        return None
    matches = glob.glob(pattern, recursive=True)
    return max(matches, key=os.path.getmtime) if matches else None


def _connect_sqlite_ro(path: str) -> sqlite3.Connection:
    """尽量只读打开 SQLite；WAL 模式只读偶发打不开时退回普通连接（仅执行 SELECT）。"""
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return sqlite3.connect(path, timeout=5)


def _read_varint(buf: bytes, idx: int) -> tuple[int, int]:
    val, shift = 0, 0
    while idx < len(buf):
        b = buf[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, idx
        shift += 7
    raise ValueError("truncated protobuf varint")


def _parse_pb(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    idx, out = 0, []
    while idx < len(buf):
        tag, idx = _read_varint(buf, idx)
        fnum, wtype = tag >> 3, tag & 7
        if wtype == 0:
            val, idx = _read_varint(buf, idx)
            out.append((fnum, wtype, val))
        elif wtype == 1:
            if idx + 8 > len(buf):
                break
            out.append((fnum, wtype, buf[idx:idx + 8]))
            idx += 8
        elif wtype == 2:
            ln, idx = _read_varint(buf, idx)
            if idx + ln > len(buf):
                break
            out.append((fnum, wtype, buf[idx:idx + ln]))
            idx += ln
        elif wtype == 5:
            if idx + 4 > len(buf):
                break
            out.append((fnum, wtype, buf[idx:idx + 4]))
            idx += 4
        else:
            break
    return out


def last_agy_reply(path: str) -> str | None:
    """从 agy 会话 SQLite（steps.step_type=15 的 protobuf 载荷）提取最后一条助手原始 Markdown 回复。"""
    conn = _connect_sqlite_ro(path)
    try:
        rows = conn.execute("SELECT step_payload FROM steps WHERE step_type = 15 ORDER BY idx DESC")
        for (payload,) in rows:
            if not payload:
                continue
            buf = bytes(payload)
            try:
                fields = _parse_pb(buf)
            except ValueError:
                continue
            for fnum, wtype, val in fields:
                if fnum != 20 or wtype != 2 or not isinstance(val, (bytes, bytearray)):
                    continue
                try:
                    subfields = _parse_pb(bytes(val))
                except ValueError:
                    continue
                primary = "".join(
                    sv.decode("utf-8", errors="replace")
                    for sf, sw, sv in subfields if sf == 1 and sw == 2 and isinstance(sv, (bytes, bytearray))
                )
                if primary.strip():
                    return primary
                fallback = "".join(
                    sv.decode("utf-8", errors="replace")
                    for sf, sw, sv in subfields if sf == 8 and sw == 2 and isinstance(sv, (bytes, bytearray))
                )
                if fallback.strip():
                    return fallback
    finally:
        conn.close()
    return None


def agy_candidate_dbs(info: dict) -> list[str]:
    """Herdr 未报告 agy 的 agent_session 时，按成员工作目录从 last_conversations.json 与 summaries 库定位候选会话。"""
    root = agy_root()
    conv_dir = os.path.join(root, "conversations")
    if not os.path.isdir(conv_dir):
        return []
    raw_cwd = str(info.get("foreground_cwd") or info.get("cwd") or "").strip()
    if not raw_cwd:
        return []
    cwds: list[str] = []
    for p in (raw_cwd, os.path.realpath(raw_cwd)):
        if p and p not in cwds:
            cwds.append(p)

    cids: list[str] = []
    cache_file = os.path.join(root, "cache", "last_conversations.json")
    try:
        with open(cache_file, encoding="utf-8") as handle:
            last_map = json.load(handle)
        if isinstance(last_map, dict):
            for p in cwds:
                cid = last_map.get(p)
                if isinstance(cid, str) and cid and cid not in cids:
                    cids.append(cid)
    except (OSError, ValueError):
        pass

    summary_db = os.path.join(root, "conversation_summaries.db")
    if os.path.isfile(summary_db):
        try:
            conn = _connect_sqlite_ro(summary_db)
            try:
                uri_set = {f"file://{p}" for p in cwds}
                rows = conn.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries "
                    "ORDER BY last_modified_time DESC LIMIT 50"
                )
                for cid, raw_uris in rows:
                    if not isinstance(cid, str) or not cid or cid in cids:
                        continue
                    try:
                        uris = json.loads(raw_uris) if isinstance(raw_uris, str) else []
                    except ValueError:
                        uris = []
                    if isinstance(uris, list) and any(str(u) in uri_set for u in uris):
                        cids.append(cid)
                        if len(cids) >= 5:
                            break
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    paths: list[str] = []
    for cid in cids:
        db_path = os.path.join(conv_dir, f"{cid}.db")
        if os.path.isfile(db_path) and db_path not in paths:
            paths.append(db_path)
    return paths


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
    """取最后一条带文字的回复。agy 读 SQLite steps；Claude/omp/codex/droid 读 JSONL。"""
    if path.endswith(".db"):
        return last_agy_reply(path)
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
    if isinstance(session, dict):
        path = session_file(session)
        if not path:
            return None, f"找不到 {session.get('agent')} 的会话记录文件"
        try:
            reply = last_reply(path)
        except (OSError, sqlite3.Error) as error:
            return None, f"读会话记录失败：{error}"
        if not reply:
            return None, f"会话记录里没有回复：{path}"
        if not on_screen(reply, screen):
            return None, f"会话记录最后一条回复与屏幕对不上（可能是旧回复）：{path}"
        return reply, path

    if info.get("agent") == "agy":
        candidates = agy_candidate_dbs(info)
        if not candidates:
            return None, "找不到 agy 的会话记录文件"
        last_reason = f"会话记录里没有回复：{candidates[0]}"
        for db_path in candidates:
            try:
                reply = last_agy_reply(db_path)
            except (OSError, sqlite3.Error) as error:
                last_reason = f"读会话记录失败：{error}"
                continue
            if not reply:
                last_reason = f"会话记录里没有回复：{db_path}"
                continue
            if on_screen(reply, screen):
                return reply, db_path
            last_reason = f"会话记录最后一条回复与屏幕对不上（可能是旧回复）：{db_path}"
        return None, last_reason

    return None, "herdr 没有报告这个成员的会话记录"


def text_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def comment_body(reply: str) -> tuple[str | None, str]:
    """把成员回复变成给用户的评论：摘要在前，完整内容折叠。返回 (评论正文, 说明)，不合要求时正文为 None。"""
    lines = reply.strip().split("\n")
    heads = [i for i, line in enumerate(lines) if SUMMARY_LINE.match(line)]
    fulls = [i for i, line in enumerate(lines) if FULL_LINE.match(line)]
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

    sleep_s = max(opts.interval, 15)
    settled = {"idle", "done"}
    first_status = status(agent_info(opts.target))
    first_screen = read_screen(opts.target, opts.lines)
    time.sleep(sleep_s)
    info = agent_info(opts.target)
    second_status = status(info)
    screen = read_screen(opts.target, opts.lines)
    # 第一次读还在收尾或刚滚屏、第二次已进入 idle/done 时，补等一轮确认连续两次稳定，省掉调度者额外空转一轮。
    if second_status in settled and not (first_status in settled and body(first_screen) == body(screen)):
        time.sleep(sleep_s)
        third_info = agent_info(opts.target)
        first_status, first_screen = second_status, screen
        info = third_info
        second_status = status(info)
        screen = read_screen(opts.target, opts.lines)

    reply, source = read_reply(info, screen)
    result = reply if reply is not None else screen
    if not result.endswith("\n"):
        result += "\n"
    os.makedirs(os.path.dirname(os.path.abspath(opts.outfile)), exist_ok=True)
    with open(opts.outfile, "w", encoding="utf-8") as handle:
        handle.write(result)
    with open(f"{opts.outfile}.screen", "w", encoding="utf-8") as handle:
        handle.write(screen)

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
