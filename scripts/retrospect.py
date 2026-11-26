#!/usr/bin/env python3
"""周期复盘（每周二、周五 12:00）：汇总本周期交付流程、飞书桥、收件箱历史数据，调 droid(gemini-4-carbon, high) 产出可优化点列表。

由 gitlab-inbox.py 在常驻循环后台线程调用（合盖唤醒后在有效期内自动补跑），也可单独运行命令行：

    python3 ~/.hwf/scripts/retrospect.py collect                 # 仅采集本周期量化数据包并输出摘要（不调模型、不推送）
    python3 ~/.hwf/scripts/retrospect.py run --no-notify         # 完整跑一轮分析并生成 HTML 报告（不推飞书和 ntfy）
    python3 ~/.hwf/scripts/retrospect.py run --open              # 完整跑一轮、推飞书私聊与 ntfy，并打开 HTML 报告
    python3 ~/.hwf/scripts/retrospect.py tick                    # 按周二/周五 12:00 槽位检查，到期才跑
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import html
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote
import urllib.request
import webbrowser

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import jev_gates as JG

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
INBOX_CONFIG_PATH = HWF_HOME / "gitlab-inbox.config.json"
INBOX_LOCAL_PATH = HWF_HOME / "gitlab-inbox.local.json"
STATE_DIR = HWF_HOME / "state"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "weekdays": [1, 4],           # 周二(1)、周五(4)
    "hour": 12,
    "minute": 0,
    "catchup_max_hours": 72,      # 合盖休眠唤醒后 72 小时内自动补跑本期
    "default_lookback_days": 3.5, # 无上次运行时间时默认回看 3.5 天
    "max_lookback_days": 14.0,
    "model": "gemini-4-carbon",
    "reasoning_effort": "high",
    "model_timeout_s": 600,
    "max_attempts_per_slot": 3,
    "retry_backoff_s": 900,
    "ntfy_topic": "pjh-herdr-task",
    "feishu_notify": True,
    "auto_evolve_gates": True,
    "git_sync_gates": True,
}

BOUNCE_HINT_RE = re.compile(r"需修改|阻断|不通过|打回|退回|失败|违规|漏|critical|advisory", re.I)

LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")
CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)
PROMPT_CMD_RE = re.compile(r"herdr\s+agent\s+prompt\s+\S+\s+(['\"])(.*?)\1", re.S)
RECEIPT_NOTE_RE = re.compile(
    r"^(@\S+\s+)?(?:已收到(?:，已转交目标 issue|目标 issue)|开始跑交付流程|已切到(?:免打扰|详尽)模式|已清空调度者上下文)"
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RetrospectError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [retrospect] {message}", flush=True)


def iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def fmt_local(ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt)


def parse_any_ts(value: str) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def find_executable(name: str, extra: Sequence[Path] = ()) -> str:
    found = shutil.which(name)
    if found:
        return found
    for path in extra:
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return name


class Files:
    """state/ 下的本机复盘状态、工作目录与报告路径；测试时可指向临时目录。"""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.dir = state_dir or STATE_DIR
        self.state = self.dir / "retrospect.json"
        self.history = self.dir / "retrospect-history.jsonl"
        self.reports_dir = self.dir / "retrospect-reports"
        self.latest_report = self.dir / "retrospect-latest.html"
        self.cwd = self.dir / "retrospect-cwd"
        self.inbox_state = self.dir / "gitlab-inbox.json"
        self.feishu_state = self.dir / "feishu-bridge.json"
        self.members_state = self.dir / "members.json"
        self.watch_guard_log = self.dir / "watch-guard.log"
        self.inbox_events_log = self.dir / "gitlab-inbox-events.log"
        self.feishu_events_log = self.dir / "feishu-bridge-events.log"
        self.proactive_records = self.dir / "proactive-observe.jsonl"
        self.proactive_marks = self.dir / "proactive-marks.jsonl"

    def load_state(self) -> dict[str, Any]:
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def save_state(self, data: Mapping[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self.state)

    def append_jsonl(self, path: Path, row: Mapping[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    @staticmethod
    def read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default


# ---------- 周期槽位计算 ----------

def slot_id_for(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d-%H:%M")


def due_slots(now_ts: float, weekdays: Sequence[int], hour: int, minute: int) -> tuple[tuple[str, float], tuple[str, float]]:
    """返回 (最近一个 <= now 的到期槽位, 再上一个槽位)，每个元素为 (slot_id, slot_timestamp)。"""
    days_set = {int(d) for d in weekdays}
    if not days_set:
        raise RetrospectError("retrospect.weekdays 不能为空")
    now_dt = datetime.fromtimestamp(now_ts)
    matched: list[datetime] = []
    for delta in range(0, 21):
        day = now_dt - timedelta(days=delta)
        if day.weekday() not in days_set:
            continue
        candidate = day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if candidate <= now_dt:
            matched.append(candidate)
            if len(matched) == 2:
                break
    if len(matched) < 2:
        raise RetrospectError("无法计算最近两个周期槽位")
    cur, prev = matched[0], matched[1]
    return (slot_id_for(cur), cur.timestamp()), (slot_id_for(prev), prev.timestamp())


# ---------- 过程数据采集 ----------

def _parse_state_top(state_md: str) -> dict[str, str]:
    info: dict[str, str] = {}
    in_top = False
    for line in state_md.splitlines()[:25]:
        if "<!-- hwf:top" in line:
            in_top = True
            continue
        if "<!-- /hwf:top" in line:
            break
        if in_top and line.startswith("- ") and "：" in line:
            k, v = line[2:].split("：", 1)
            info[k.strip()] = v.strip()
    return info


def _extract_bounce_lines(md_path: Path, prefix: str, limit: int = 3) -> list[str]:
    """抽取评审或测试报告里命中的打回/问题摘要行，供周期复盘归纳新增 Gate ④/⑤ 规则而免去逐个翻文件。"""
    try:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or len(s) < 6 or s.startswith("<!--"):
            continue
        if BOUNCE_HINT_RE.search(s):
            hits.append(f"{prefix}: {s[:200]}")
            if len(hits) >= limit:
                break
    return hits


def _collect_task_dir(task_dir: Path, since_ts: float, now_ts: float) -> Optional[dict[str, Any]]:
    hour_buckets: set[str] = set()
    mtimes_in_window: list[float] = []
    reviews_in_win = 0
    testing_rounds: set[str] = set()
    dev_files_in_win = 0
    bounce_samples: list[str] = []

    for root, _, filenames in os.walk(task_dir):
        rel_root = Path(root).relative_to(task_dir)
        parts = rel_root.parts
        for fn in filenames:
            if fn.startswith("."):
                continue
            fp = Path(root) / fn
            try:
                mt = fp.stat().st_mtime
            except OSError:
                continue
            if since_ts <= mt <= now_ts + 60:
                mtimes_in_window.append(mt)
                hour_buckets.add(datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H"))
                if parts and parts[0] == "reviews":
                    reviews_in_win += 1
                    if fp.suffix.lower() == ".md" and len(bounce_samples) < 4:
                        bounce_samples.extend(_extract_bounce_lines(fp, f"{task_dir.name}/reviews/{fn}", 2))
                elif parts and parts[0] == "testing":
                    if len(parts) >= 2:
                        testing_rounds.add(parts[1])
                    else:
                        testing_rounds.add(fn)
                    if fp.suffix.lower() == ".md" and len(bounce_samples) < 4:
                        bounce_samples.extend(_extract_bounce_lines(fp, f"{task_dir.name}/testing/{fn}", 2))
                elif parts and parts[0] == "development":
                    dev_files_in_win += 1

    if not mtimes_in_window:
        return None

    state_file = task_dir / "STATE.md"
    state_kb = 0.0
    state_lines = 0
    top_info: dict[str, str] = {}
    if state_file.exists():
        try:
            raw = state_file.read_text(errors="replace")
            state_kb = round(len(raw.encode("utf-8")) / 1024.0, 1)
            state_lines = raw.count("\n") + 1
            top_info = _parse_state_top(raw)
        except OSError:
            pass

    notes_by_kind: dict[str, int] = {}
    notes_in_win = 0
    for row in Files.read_jsonl(task_dir / "notes.jsonl"):
        ts = parse_any_ts(str(row.get("at") or ""))
        if ts is not None and since_ts <= ts <= now_ts + 60:
            notes_in_win += 1
            kind = str(row.get("kind") or "unknown")
            notes_by_kind[kind] = notes_by_kind.get(kind, 0) + 1

    span_h = round((max(mtimes_in_window) - min(mtimes_in_window)) / 3600.0, 2)
    active_h = len(hour_buckets)
    return {
        "task_id": task_dir.name,
        "path": str(task_dir),
        "state_path": str(state_file) if state_file.exists() else "",
        "active_hours": active_h,
        "window_span_hours": span_h,
        "idle_or_wait_hours_est": round(max(0.0, span_h - active_h), 2),
        "state_kb": state_kb,
        "state_lines": state_lines,
        "notify_mode": top_info.get("通知模式", ""),
        "stage": top_info.get("当前阶段", ""),
        "waiting_user": top_info.get("待用户事项", ""),
        "notes_in_window": notes_in_win,
        "notes_by_kind": notes_by_kind,
        "reviews_touched": reviews_in_win,
        "testing_rounds_touched": len(testing_rounds),
        "dev_artifacts_touched": dev_files_in_win,
        "bounce_samples": bounce_samples[:4],
    }


def _collect_omp_sessions(omp_root: Path, since_ts: float) -> list[dict[str, Any]]:
    if not omp_root.exists():
        return []
    out: list[dict[str, Any]] = []
    for jsonl in sorted(omp_root.rglob("*.jsonl")):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        if st.st_mtime < since_ts:
            continue
        compactions = 0
        prompt_lengths: list[int] = []
        async_bash_calls = 0
        long_gaps_over_180s = 0
        line_count = 0
        prev_ts: Optional[float] = None
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_count += 1
                    if '"compaction"' in line or "remote-compacted" in line:
                        compactions += 1
                    if '"async":true' in line or '"async": true' in line:
                        async_bash_calls += 1
                    if "herdr agent prompt" in line:
                        for m in PROMPT_CMD_RE.finditer(line):
                            prompt_lengths.append(len(m.group(2)))
                    ts_match = re.search(r'"timestamp"\s*:\s*"([^"]+)"', line)
                    if ts_match:
                        cur_ts = parse_any_ts(ts_match.group(1))
                        if cur_ts is not None and cur_ts >= since_ts:
                            if prev_ts is not None and 180 < (cur_ts - prev_ts) < 900:
                                long_gaps_over_180s += 1
                            prev_ts = cur_ts
        except OSError:
            continue
        kind = "member" if ".herdr-worktrees-" in str(jsonl) else ("scheduler" if prompt_lengths else "other")
        out.append({
            "session_file": str(jsonl),
            "session_kind": kind,
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "lines": line_count,
            "compactions": compactions,
            "dispatched_prompts": len(prompt_lengths),
            "prompt_chars_median": int(statistics.median(prompt_lengths)) if prompt_lengths else 0,
            "prompt_chars_max": max(prompt_lengths) if prompt_lengths else 0,
            "async_tool_calls": async_bash_calls,
            "step_gaps_3m_to_15m": long_gaps_over_180s,
        })
    out.sort(key=lambda x: (x["size_mb"], x["dispatched_prompts"]), reverse=True)
    return out[:15]


def _filter_timed_lines(lines: Sequence[str], since_ts: float) -> list[str]:
    stitched: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if LOG_TS_RE.match(s):
            stitched.append(s)
        elif stitched and len(stitched[-1]) < 480:
            # 终端窄窗格折行没有空格，英文单词/URL 直接拼合，JSON 键值对保留可读性
            sep = " " if s.startswith(("'", '"', "{", "}", "[")) else ""
            stitched[-1] = f"{stitched[-1]}{sep}{s}"[:500]
    kept: list[str] = []
    seen: set[str] = set()
    for line in stitched:
        key = line[:180]
        if key in seen:
            continue
        m = LOG_TS_RE.match(line)
        if not m:
            continue
        ts = parse_any_ts(m.group(1))
        if ts is not None and ts < since_ts:
            continue
        seen.add(key)
        kept.append(line)
    return kept


def _read_herdr_daemon_panes(run: Runner) -> tuple[list[str], list[str]]:
    """兜底从常驻 Herdr 窗格读取收件箱与飞书桥缓冲日志（补充持久化日志启用前的窗口）。"""
    herdr_bin = find_executable("herdr", (Path.home() / ".local" / "bin" / "herdr",))
    try:
        res = run([herdr_bin, "pane", "list"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return [], []
        payload = json.loads(res.stdout)
        panes = (payload.get("result") or {}).get("panes") or []
    except Exception:
        return [], []

    inbox_lines: list[str] = []
    feishu_lines: list[str] = []
    for pane in panes:
        if pane.get("agent"):
            continue
        pid = pane.get("pane_id")
        if not pid:
            continue
        try:
            read_res = run([herdr_bin, "pane", "read", str(pid), "--lines", "400"],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        text = read_res.stdout or ""
        if any(k in text for k in ("feishu-websocket", "lark:", "catch-up skip chat", "intent failed")):
            feishu_lines.extend(text.splitlines())
        elif any(k in text for k in ("gitlab-inbox", "[proactive]", "launched team-up", "protocol ")):
            inbox_lines.extend(text.splitlines())
    return inbox_lines, feishu_lines


def _summarize_feishu_logs(lines: Sequence[str]) -> dict[str, Any]:
    created = bound = intent_failed = api_errors = poll_failed = reconnects = reloads = 0
    chat_230002: dict[str, int] = {}
    samples: list[str] = []
    for line in lines:
        if " created " in line:
            created += 1
        elif " bound " in line:
            bound += 1
        elif "intent failed" in line:
            intent_failed += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "catch-up skip chat" in line or "handle " in line and " failed:" in line:
            api_errors += 1
            m = re.search(r"catch-up skip chat (\S+):", line)
            if m:
                cid = m.group(1)
                chat_230002[cid] = chat_230002.get(cid, 0) + 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "poll failed" in line:
            poll_failed += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "feishu-websocket: connected" in line:
            reconnects += 1
        elif "reloading" in line or "hot-reload" in line:
            reloads += 1
    return {
        "log_lines_in_window": len(lines),
        "created_issues": created,
        "bound_issues": bound,
        "intent_failed": intent_failed,
        "api_or_catchup_errors": api_errors,
        "unreachable_chats_repeated": chat_230002,
        "poll_failed": poll_failed,
        "websocket_connect_events": reconnects,
        "hot_reloads": reloads,
        "error_samples": samples,
    }


def _summarize_inbox_logs(lines: Sequence[str]) -> dict[str, Any]:
    launched = poll_failed = timeouts = rejected = reset_busy = status_failed = stage_failed = proto_pushes = reloads = 0
    samples: list[str] = []
    for line in lines:
        if "launched team-up" in line:
            launched += 1
        elif "poll failed:" in line:
            poll_failed += 1
            if "timed out" in line:
                timeouts += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "rejected:" in line:
            rejected += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "busy; retry next round" in line:
            reset_busy += 1
        elif "Status query failed" in line:
            status_failed += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "resume stage failed" in line:
            stage_failed += 1
            if len(samples) < 8:
                samples.append(line[:380])
        elif "protocol " in line and "notified" in line:
            proto_pushes += 1
        elif "reloading gitlab-inbox" in line:
            reloads += 1
    return {
        "log_lines_in_window": len(lines),
        "launched_team_up": launched,
        "poll_failed": poll_failed,
        "poll_timeouts": timeouts,
        "prompt_rejected": rejected,
        "reset_busy": reset_busy,
        "status_query_failed": status_failed,
        "resume_stage_failed": stage_failed,
        "protocol_update_broadcasts": proto_pushes,
        "hot_reloads": reloads,
        "error_samples": samples,
    }


def _collect_gitlab_issues(host: str, projects: Mapping[str, str], allowed_users: set[str],
                           active_iids_by_proj: Mapping[str, set[int]], since_ts: float, now_ts: float,
                           run: Runner) -> list[dict[str, Any]]:
    glab_bin = find_executable("glab", (Path("/opt/homebrew/bin/glab"), Path("/usr/local/bin/glab")))
    results: list[dict[str, Any]] = []
    for project in projects:
        pid = quote(project, safe="")
        iids = sorted(active_iids_by_proj.get(project) or set())[-12:]
        for iid in iids:
            try:
                ir = run([glab_bin, "api", "--hostname", host, "-X", "GET", f"projects/{pid}/issues/{iid}"],
                         capture_output=True, text=True, timeout=20)
                nr = run([glab_bin, "api", "--hostname", host, "-X", "GET",
                          f"projects/{pid}/issues/{iid}/notes?sort=asc&per_page=80"],
                         capture_output=True, text=True, timeout=20)
                if ir.returncode != 0 or nr.returncode != 0:
                    continue
                issue = json.loads(ir.stdout)
                notes = [n for n in json.loads(nr.stdout) if not n.get("system")]
            except Exception:
                continue

            user_notes = 0
            agent_notes = 0
            reply_latencies_min: list[float] = []
            pending_user_ts: Optional[float] = None
            pending_user_note_id: Optional[int] = None

            for n in notes:
                nts = parse_any_ts(str(n.get("created_at") or ""))
                if nts is None:
                    continue
                author = str((n.get("author") or {}).get("username") or "")
                body = str(n.get("body") or "").strip()
                in_win = nts >= since_ts
                if author in allowed_users:
                    if in_win:
                        user_notes += 1
                    if pending_user_ts is None:
                        pending_user_ts = nts
                        pending_user_note_id = n.get("id")
                else:
                    if RECEIPT_NOTE_RE.match(body):
                        continue
                    if in_win:
                        agent_notes += 1
                    if pending_user_ts is not None and nts >= pending_user_ts:
                        if in_win:
                            reply_latencies_min.append(round((nts - pending_user_ts) / 60.0, 1))
                        pending_user_ts = None
                        pending_user_note_id = None

            unanswered_min = (
                round((now_ts - pending_user_ts) / 60.0, 1)
                if pending_user_ts is not None and pending_user_ts >= since_ts and (now_ts - pending_user_ts) >= 30 * 60
                else None
            )
            results.append({
                "project": project,
                "iid": iid,
                "title": str(issue.get("title") or "")[:80],
                "state": issue.get("state"),
                "labels": issue.get("labels") or [],
                "web_url": issue.get("web_url") or "",
                "user_notes_in_window": user_notes,
                "agent_substantive_notes_in_window": agent_notes,
                "reply_latency_median_min": round(statistics.median(reply_latencies_min), 1) if reply_latencies_min else None,
                "reply_latency_max_min": max(reply_latencies_min) if reply_latencies_min else None,
                "unanswered_user_note_id": pending_user_note_id if unanswered_min is not None else None,
                "unanswered_minutes": unanswered_min,
            })
    return results


def _collect_pane_and_worktree_hygiene(files: Files, run: Runner, worktrees_root: Path,
                                       omp_root: Path) -> dict[str, Any]:
    members = files.read_json(files.members_state, {})
    herdr_bin = find_executable("herdr", (Path.home() / ".local" / "bin" / "herdr",))
    idle_by_scheduler_role: dict[str, int] = {}
    live_member_panes = 0
    idle_member_panes = 0
    try:
        res = run([herdr_bin, "pane", "list"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            panes = ((json.loads(res.stdout).get("result") or {}).get("panes") or [])
            live_by_id = {str(p.get("pane_id")): p for p in panes}
            for pid, meta in members.items():
                p = live_by_id.get(pid)
                if not p:
                    continue
                live_member_panes += 1
                st = str(p.get("agent_status") or "")
                if st in ("idle", "done", "unknown"):
                    idle_member_panes += 1
                    key = f"{meta.get('scheduler', '?')}:{meta.get('profile', '?')}"
                    idle_by_scheduler_role[key] = idle_by_scheduler_role.get(key, 0) + 1
    except Exception:
        pass

    duplicate_idle = {k: v for k, v in idle_by_scheduler_role.items() if v >= 2}
    worktree_dirs: list[str] = []
    if worktrees_root.exists():
        for proj_dir in sorted(worktrees_root.iterdir()):
            if proj_dir.is_dir():
                for wt in sorted(proj_dir.iterdir()):
                    if wt.is_dir():
                        worktree_dirs.append(f"{proj_dir.name}/{wt.name}")

    return {
        "registered_members_count": len(members),
        "live_member_panes": live_member_panes,
        "idle_member_panes": idle_member_panes,
        "duplicate_idle_same_role": duplicate_idle,
        "worktree_count": len(worktree_dirs),
        "worktree_samples": worktree_dirs[:20],
        "omp_sessions_dir": str(omp_root),
    }


def collect_digest(
    config: Mapping[str, Any],
    files: Files,
    since_ts: float,
    now_ts: float,
    *,
    run: Runner = subprocess.run,
    hwf_home: Path = HWF_HOME,
    omp_root: Optional[Path] = None,
    worktrees_root: Optional[Path] = None,
) -> dict[str, Any]:
    """采集 [since_ts, now_ts] 周期内交付流程、飞书桥、收件箱三大域的客观过程数据。"""
    omp_dir = omp_root if omp_root is not None else (Path.home() / ".omp" / "agent" / "sessions")
    wt_dir = worktrees_root if worktrees_root is not None else (Path.home() / ".herdr" / "worktrees")

    raw_projects = config.get("projects") or {}
    projects: dict[str, str] = (
        dict(raw_projects) if isinstance(raw_projects, Mapping) else {str(p): "" for p in raw_projects}
    )
    for p, r in (config.get("local_projects") or {}).items():
        if p and r and not projects.get(p):
            projects[str(p)] = str(r)
    inbox_state = files.read_json(files.inbox_state, {})
    for bucket in ("origins", "closed_origins"):
        for origin in (inbox_state.get(bucket) or {}).values():
            if not isinstance(origin, Mapping):
                continue
            proj = str(origin.get("project") or "")
            repo_str = str(origin.get("repo") or "")
            if proj and repo_str and not projects.get(proj):
                projects[proj] = repo_str
    allowed_users = set(config.get("allowed_users") or [])
    host = str(config.get("host") or "git.bootingai.net")

    active_tasks: list[dict[str, Any]] = []
    active_iids_by_proj: dict[str, set[int]] = {p: set() for p in projects}
    for proj, repo_str in projects.items():
        if not repo_str:
            continue
        tmp_root = Path(repo_str) / ".hwf" / "tmp"
        if not tmp_root.exists():
            continue
        for child in sorted(tmp_root.iterdir()):
            if not child.is_dir() or child.name in ("archive", "feishu-hub"):
                continue
            item = _collect_task_dir(child, since_ts, now_ts)
            if item:
                active_tasks.append(item)
                m = re.search(r"-(\d+)$", child.name)
                if m:
                    active_iids_by_proj.setdefault(proj, set()).add(int(m.group(1)))

    for origin in (inbox_state.get("origins") or {}).values():
        proj = origin.get("project")
        iid = origin.get("iid")
        if proj and iid:
            active_iids_by_proj.setdefault(str(proj), set()).add(int(iid))

    watch_guard_lines: list[str] = []
    if files.watch_guard_log.exists():
        watch_guard_lines = _filter_timed_lines(
            files.watch_guard_log.read_text(errors="replace").splitlines(), since_ts
        )

    pane_inbox_lines, pane_feishu_lines = _read_herdr_daemon_panes(run)
    file_inbox_lines = (
        files.inbox_events_log.read_text(errors="replace").splitlines() if files.inbox_events_log.exists() else []
    )
    file_feishu_lines = (
        files.feishu_events_log.read_text(errors="replace").splitlines() if files.feishu_events_log.exists() else []
    )
    inbox_lines = _filter_timed_lines([*file_inbox_lines, *pane_inbox_lines], since_ts)
    feishu_lines = _filter_timed_lines([*file_feishu_lines, *pane_feishu_lines], since_ts)

    feishu_state = files.read_json(files.feishu_state, {})
    sec_state = feishu_state.get("secretary") or {}
    held_map = sec_state.get("held") or {}
    held_total = sum(len(v) for v in held_map.values() if isinstance(v, list))
    failing_threads = [
        {"issue": t.get("issue_url"), "fails": t.get("fails")}
        for t in (feishu_state.get("threads") or {}).values()
        if isinstance(t, Mapping) and int(t.get("fails") or 0) > 0
    ]

    proactive_verdicts: dict[str, int] = {}
    for rec in files.read_jsonl(files.proactive_records):
        ts = parse_any_ts(str(rec.get("at") or ""))
        if ts is not None and since_ts <= ts <= now_ts + 60:
            v = str(rec.get("verdict") or "unknown")
            proactive_verdicts[v] = proactive_verdicts.get(v, 0) + 1

    proactive_marks: dict[str, int] = {}
    wrong_reasons: list[str] = []
    for mk in files.read_jsonl(files.proactive_marks):
        ts = parse_any_ts(str(mk.get("at") or ""))
        if ts is not None and since_ts <= ts <= now_ts + 60:
            kind = str(mk.get("mark") or "unknown")
            proactive_marks[kind] = proactive_marks.get(kind, 0) + 1
            if kind in ("wrong", "should") and mk.get("reason") and len(wrong_reasons) < 6:
                wrong_reasons.append(f"{mk.get('id')}({kind}): {mk.get('reason')}")

    recent_commits: list[str] = []
    try:
        git_res = run(
            ["git", "-C", str(hwf_home), "log", f"--since={iso_local(since_ts)}", "--oneline", "-n", "30"],
            capture_output=True, text=True, timeout=10,
        )
        if git_res.returncode == 0:
            recent_commits = [ln.strip() for ln in git_res.stdout.splitlines() if ln.strip()]
    except Exception:
        pass

    prev_history = files.read_jsonl(files.history)[-2:]
    prev_titles = [
        it.get("title")
        for row in prev_history
        for it in (row.get("items") or [])
        if isinstance(it, Mapping) and it.get("title")
    ]

    gitlab_issues = _collect_gitlab_issues(
        host, projects, allowed_users, active_iids_by_proj, since_ts, now_ts, run
    )
    omp_sessions = _collect_omp_sessions(omp_dir, since_ts)
    hygiene = _collect_pane_and_worktree_hygiene(files, run, wt_dir, omp_dir)
    jev_summary = JG.summarize_gate_telemetry(files.dir, hwf_home, since_ts, now_ts)
    review_bounce_samples = [
        line
        for t in active_tasks
        for line in (t.get("bounce_samples") or [])
    ][:15]

    return {
        "window": {
            "since": iso_local(since_ts),
            "until": iso_local(now_ts),
            "since_label": fmt_local(since_ts),
            "until_label": fmt_local(now_ts),
            "hours": round((now_ts - since_ts) / 3600.0, 1),
        },
        "hwf_home": str(hwf_home),
        "delivery": {
            "active_tasks_count": len(active_tasks),
            "active_tasks": active_tasks,
            "review_test_bounce_samples": review_bounce_samples,
            "gitlab_issues_checked": gitlab_issues,
            "scheduler_omp_sessions": omp_sessions,
            "watch_guard_events_count": len(watch_guard_lines),
            "watch_guard_samples": watch_guard_lines[-10:],
            "hygiene": hygiene,
        },
        "jev_gates": jev_summary,
        "feishu_bridge": {
            "log_summary": _summarize_feishu_logs(feishu_lines),
            "secretary_chat_ready": bool(sec_state.get("chat_id")),
            "active_threads_count": len(feishu_state.get("threads") or {}),
            "held_unsent_notes_count": held_total,
            "pending_intents_count": len(feishu_state.get("pending_intents") or {}),
            "failing_threads": failing_threads,
            "group_chats_count": len(feishu_state.get("group_chats") or {}),
            "history_cursors_count": len(feishu_state.get("history_cursors") or {}),
        },
        "gitlab_inbox": {
            "log_summary": _summarize_inbox_logs(inbox_lines),
            "open_origins_count": len(inbox_state.get("origins") or {}),
            "followed_issues_count": len(inbox_state.get("followed") or {}),
            "protocol_rev": inbox_state.get("protocol_rev") or "",
            "proactive_verdicts_in_window": proactive_verdicts,
            "proactive_marks_in_window": proactive_marks,
            "proactive_feedback_samples": wrong_reasons,
        },
        "dedup_context": {
            "recent_hwf_commits_in_window": recent_commits,
            "previous_review_item_titles": prev_titles,
        },
    }


# ---------- Droid (gemini-4-carbon, high) 深度分析 ----------

ANALYSIS_PROMPT_TEMPLATE = """你是研发交付体系的周期复盘架构师。请阅读当前目录下的 `digest-input.json`（周期：{since_label} 至 {until_label}），对「交付流程」「飞书桥（含秘书）」「收件箱（含主动接活）」「Jev关卡（`jev_gates`）」四块过程历史数据做深度体检，找出真正值得改的优化点，并在数据达标时给出可自动生效的 `gate_updates` 关卡演进指令。

硬性工具预算（必须严格遵守，防止超时）：
- 整个会话**最多只允许调用 3 次 `Script`**（总文件读取不超过 10 个片段），必须在 3 分钟内收口输出最终 JSON！
- 第 1 次 `Script`：只读当前目录 `digest-input.json` 与 `~/.agents/memory/team-up/quiet-feishu-and-self-service-boundaries.md`（了解不可触碰的人工把关红线）。禁止读取 `retrospect.py` 自身，禁止通读整个记忆库或状态大文件！
- 第 2～3 次 `Script`（按需）：只针对 `digest-input.json` 暴露的 Top 异常，用 `Grep` 定点定位行号，再用带 `offset`/`limit`（每次 ≤120 行）的 `Read` 抽查 `{hwf_home}/scripts/`、`{hwf_home}/protocol/` 或对应任务 `STATE.md` 关键段落，核实最新代码/规则是否仍有缺口。禁止从头到尾整本通读千行脚本！
- 第 3 次 `Script` 结束后，**严禁再调用任何工具**，立即输出最终 JSON。

核查与去重铁律：
1. 对照 `dedup_context.recent_hwf_commits_in_window` 与 `previous_review_item_titles`：本周期已提交修复且未再复发的旧问题，不要重复列入；用户已拍板保留的三道人工把关（默认对话模式等 `/go`、编码前复杂度与需求理解卡确认、手动关单与手动合 `main`）不要建议取消。
2. 每条优化点必须有本周期真实数字、issue 编号、日志样本或具体代码行号/条款位置支撑；没有把握的猜测不写。若本周期整体平稳无实质问题，`items` 允许为空数组 `[]`。
3. 精选最有价值的 0～7 条，按优先级 `P1`（高频报错/漏报/明显拖慢交付）→ `P2`（中等摩擦/资源残留）→ `P3`（顺手改进）排序。
4. 所有文字必须说通俗大白话（普通人一眼能懂，不堆砌黑话）：说清发生了什么、为什么卡或报错、具体改哪个文件/哪条规则怎么改、改完有什么好处。
5. Jev 关卡受保护自动演进规则（写入顶层 `gate_updates`，由外层引擎核验单测后自动落盘 `jev-gates.json`；数据不足时填 `[]`）：
   - **安全铁锁**：严禁淘汰任何 `protected: true` 的规则，严禁调松 `protected: true` 的授权/安全门槛！
   - `tune_threshold`：仅当 `jev_gates.window_by_gate` 擦边 `near_miss_counts` 或 `proactive_marks_in_window`（`wrong` 误报 / `should` 漏报）有明确量化证据时微调（单步 0.02～0.08）：`{{"op": "tune_threshold", "target": "thresholds"|"gate4_semantic_lint"|"gate5_media_guard"|"gate5_note_guard", "rule_id": "<阈值名或rule_id>", "new_threshold": 0.84, "reason": "大白话数据理由"}}`。
   - `add_rule`：仅当 `delivery.review_test_bounce_samples` 或本周期任务记录中**同类低级违规重复出现 ≥2 次（`evidence_count >= 2`）**、且 `jev_gates.rules_30d` 尚未覆盖、且不在 `recent_retired_rule_ids` 内时新增（每轮最多 2 条，阈值 0.78～0.88，英文判定词必须精准区分正反例）：`{{"op": "add_rule", "target": "gate4_semantic_lint"|"gate5_media_guard"|"gate5_note_guard", "rule_id": "snake_case_id", "threshold": 0.82, "evidence_count": 2, "title_zh": "...", "fix_hint_zh": "gate4必填", "error_zh": "gate5必填(可用{{prob:.0%}}/{{file_name}})", "instructions_en": "...", "true_en": "...", "false_en": "...", "reason": "具体哪2次评审/验收踩坑"}}`。
   - `retire_rule`：仅对 `protected: false` 规则，且满足以下任一客观标准时淘汰：(a) `jev_gates.retirement_candidates` 已列出（满 30 天、评估 ≥30 次且 0 命中）；(b) 30 天评估 ≥10 次且豁免占比 ≥50%；(c) 上线满 14 天且已被确定性脚本完全替代（填 `"superseded_by_script": "scripts/xxx.py"`）：`{{"op": "retire_rule", "target": "gate4_semantic_lint"|..., "rule_id": "...", "superseded_by_script": "", "reason": "大白话淘汰依据"}}`。
6. 严禁修改任何文件、严禁调用 TodoWrite；最终回复只输出一个合法 JSON 对象，不要包含任何额外解释文字。

JSON 结构要求：
{{
  "headline": "一两句话大白话总结本周期整体健康度与最值得处理的重点",
  "problem_plain": "遇到了什么问题（用日常大白话描述本周期暴露的主要痛点，供 HTML 报告开篇）",
  "solution_plain": "打算怎么解决（一句话说清改进思路，供 HTML 报告开篇）",
  "why_plain": "为什么这样做（不改会继续造成什么浪费或风险，供 HTML 报告开篇）",
  "items": [
    {{
      "priority": "P1",
      "area": "交付流程",
      "title": "15字以内的直白标题",
      "symptom_and_evidence": "具体现象 + 本周期数据/issue/日志证据",
      "root_cause": "一句话根因（指明脚本/规则具体位置）",
      "action": "具体可执行改法（改哪里、怎么改、预期效果）",
      "effort": "小"
    }}
  ],
  "gate_updates": []
}}
其中 `priority` 只能是 `"P1"` / `"P2"` / `"P3"`，`area` 只能是 `"交付流程"` / `"飞书桥"` / `"收件箱"` / `"Jev关卡"`，`effort` 只能是 `"小"` / `"中"` / `"大"`。
"""


def parse_analysis_json(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        text = CODE_FENCE_RE.sub("", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RetrospectError(f"模型未返回 JSON 对象：{raw_output[:240]}")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as err:
        raise RetrospectError(f"模型返回 JSON 解析失败：{err}: {text[start:start + 240]}") from err
    if not isinstance(data, dict):
        raise RetrospectError("模型返回顶层不是对象")

    for req in ("headline", "problem_plain", "solution_plain", "why_plain"):
        if not str(data.get(req) or "").strip():
            raise RetrospectError(f"模型结果缺少字段 {req}")

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise RetrospectError("模型结果 items 必须是数组")

    valid_areas = {"交付流程", "飞书桥", "收件箱", "Jev关卡"}
    valid_prios = {"P1", "P2", "P3"}
    valid_efforts = {"小", "中", "大"}
    cleaned: list[dict[str, str]] = []
    for idx, item in enumerate(raw_items[:10]):
        if not isinstance(item, Mapping):
            raise RetrospectError(f"items[{idx}] 不是对象")
        prio = str(item.get("priority") or "P2").upper()
        if prio not in valid_prios:
            prio = "P2"
        area = str(item.get("area") or "交付流程").strip()
        if area not in valid_areas:
            area = "交付流程"
        effort = str(item.get("effort") or "小").strip()
        if effort not in valid_efforts:
            effort = "小"
        title = str(item.get("title") or "").strip()
        symptom = str(item.get("symptom_and_evidence") or "").strip()
        cause = str(item.get("root_cause") or "").strip()
        action = str(item.get("action") or "").strip()
        if not (title and symptom and action):
            raise RetrospectError(f"items[{idx}] 缺少 title/symptom_and_evidence/action")
        cleaned.append({
            "priority": prio,
            "area": area,
            "title": title,
            "symptom_and_evidence": symptom,
            "root_cause": cause or "见现象证据",
            "action": action,
            "effort": effort,
        })

    prio_rank = {"P1": 0, "P2": 1, "P3": 2}
    cleaned.sort(key=lambda x: prio_rank.get(x["priority"], 9))
    raw_gate_updates = data.get("gate_updates")
    gate_updates = [dict(u) for u in raw_gate_updates[:8] if isinstance(u, Mapping)] if isinstance(raw_gate_updates, list) else []
    return {
        "headline": str(data["headline"]).strip(),
        "problem_plain": str(data["problem_plain"]).strip(),
        "solution_plain": str(data["solution_plain"]).strip(),
        "why_plain": str(data["why_plain"]).strip(),
        "items": cleaned,
        "gate_updates": gate_updates,
    }


# ---------- HTML 单文件报告与飞书/ntfy 格式化 ----------

GATE_TABLE_LABELS = {
    "gate4_semantic_lint": "关卡4 审前铁律预检",
    "gate5_media_guard": "关卡5 录屏与截图把关",
    "gate5_note_guard": "关卡5 评论出门把关",
}


def render_html_report(
    digest: Mapping[str, Any],
    analysis: Mapping[str, Any],
    generated_at: float,
    *,
    gate_evolution: Optional[Mapping[str, Any]] = None,
) -> str:
    e = html.escape
    win = digest["window"]
    items: Sequence[Mapping[str, str]] = analysis["items"]
    evo = gate_evolution or {"applied": [], "rejected": []}
    applied_list = list(evo.get("applied") or [])
    rejected_list = list(evo.get("rejected") or [])

    area_counts = {"飞书桥": 0, "收件箱": 0, "交付流程": 0, "Jev关卡": 0}
    for it in items:
        area_counts[it["area"]] = area_counts.get(it["area"], 0) + 1

    def node_status(area: str) -> str:
        cnt = area_counts.get(area, 0)
        return f"待优化 {cnt} 项" if cnt else "运行平稳"

    jev_data = digest.get("jev_gates") or {}
    win_gates = jev_data.get("window_by_gate") or {}
    total_jev_calls = sum(int(v.get("total") or 0) for v in win_gates.values() if isinstance(v, Mapping))
    jev_status_text = (
        f"本轮已自动更新 {len(applied_list)} 项规则"
        if applied_list
        else (f"待优化 {area_counts['Jev关卡']} 项" if area_counts["Jev关卡"] else f"本周期把关 {total_jev_calls} 次 · 运行平稳")
    )

    if items:
        item_rows = "".join(
            f"<tr data-item-idx='{idx}'>"
            f"<td><b>#{idx}</b> <span class='tag p-{e(it['priority'].lower())}'>{e(it['priority'])}</span></td>"
            f"<td><span class='tag'>{e(it['area'])}</span></td>"
            f"<td><b>{e(it['title'])}</b><div class='muted'>{e(it['symptom_and_evidence'])}</div></td>"
            f"<td>{e(it['root_cause'])}</td>"
            f"<td>{e(it['action'])}</td>"
            f"<td>{e(it['effort'])}</td>"
            f"<td><div class='pick-cell' data-idx='{idx}'>"
            f"<div class='pick-btns'>"
            f"<button type='button' class='pick-btn' data-idx='{idx}' data-act='do'>要做</button>"
            f"<button type='button' class='pick-btn' data-idx='{idx}' data-act='adjust'>调整</button>"
            f"<button type='button' class='pick-btn' data-idx='{idx}' data-act='skip'>先不做</button>"
            f"</div>"
            f"<input type='text' class='pick-note' data-idx='{idx}' placeholder='可选备注/补充要求'>"
            f"</div></td>"
            f"</tr>"
            for idx, it in enumerate(items, 1)
        )
    else:
        item_rows = "<tr><td colspan='7' class='empty'>本周期各项运行平稳，未发现需要折腾的新优化点。</td></tr>"

    evo_rows_parts: list[str] = []
    git_meta = evo.get("git") or {}
    git_badge = f"（已提交 {e(str(git_meta.get('sha') or ''))}）" if git_meta.get("committed") else ""
    for a in applied_list:
        evo_rows_parts.append(
            f"<tr>"
            f"<td><span class='tag p-p3'>已自动生效</span></td>"
            f"<td><code>{e(str(a.get('op') or ''))}</code></td>"
            f"<td><b>{e(str(a.get('summary') or ''))}</b>{git_badge}</td>"
            f"<td>{e(str(a.get('reason') or '—'))}</td>"
            f"</tr>"
        )
    for r in rejected_list:
        target_name = f"{r.get('target', '')}.{r.get('rule_id') or r.get('key') or ''}".strip(".")
        evo_rows_parts.append(
            f"<tr>"
            f"<td><span class='tag p-p2'>护栏拦截</span></td>"
            f"<td><code>{e(str(r.get('op') or ''))}</code></td>"
            f"<td><code>{e(target_name or '—')}</code></td>"
            f"<td>{e(str(r.get('reason') or ''))}</td>"
            f"</tr>"
        )
    gate_evo_rows = "".join(evo_rows_parts) or (
        "<tr><td colspan='4' class='empty'>本周期关卡参数与规则清单稳定，无自动新增、调优或淘汰动作。</td></tr>"
    )

    rules_30d = jev_data.get("rules_30d") or {}
    rule_health_parts: list[str] = []
    for table_key, table_label in GATE_TABLE_LABELS.items():
        for st in rules_30d.get(table_key) or []:
            if st.get("protected"):
                badge = "<span class='tag p-p1'>红线锁死</span>"
            elif st.get("retirement_eligible"):
                badge = "<span class='tag p-p2'>零命中待淘汰</span>"
            else:
                badge = "<span class='tag'>常规运行</span>"
            rule_health_parts.append(
                f"<tr>"
                f"<td>{e(table_label)}</td>"
                f"<td><code>{e(str(st.get('rule_id')))}</code> · {e(str(st.get('title_zh')))}</td>"
                f"<td>{badge}</td>"
                f"<td>{st.get('threshold')}</td>"
                f"<td>{st.get('evals_30d', 0)} 次</td>"
                f"<td>拦截 {st.get('hits_30d', 0)} · 豁免 {st.get('exempts_30d', 0)} · 擦边 {st.get('near_misses_30d', 0)}</td>"
                f"</tr>"
            )
    gate_rule_rows = "".join(rule_health_parts) or "<tr><td colspan='6' class='empty'>暂无规则清单数据</td></tr>"

    tasks = digest["delivery"]["active_tasks"]
    task_rows = "".join(
        f"<tr>"
        f"<td><code>{e(t['task_id'])}</code></td>"
        f"<td>{t['active_hours']}h / {t['window_span_hours']}h（空档 {t['idle_or_wait_hours_est']}h）</td>"
        f"<td>{t['state_kb']} KB（{t['state_lines']} 行）</td>"
        f"<td>{t['reviews_touched']} / {t['testing_rounds_touched']}</td>"
        f"<td>{t['notes_in_window']} 条 {e(json.dumps(t['notes_by_kind'], ensure_ascii=False))}</td>"
        f"<td>{e(t['stage'] or '—')}<div class='muted'>{e(t['waiting_user'] or '')}</div></td>"
        f"</tr>"
        for t in tasks
    ) or "<tr><td colspan='6' class='empty'>本周期无活跃本地任务目录</td></tr>"

    fb = digest["feishu_bridge"]
    fblog = fb["log_summary"]
    inb = digest["gitlab_inbox"]
    inblog = inb["log_summary"]
    hyg = digest["delivery"]["hygiene"]

    jev_win_brief = " · ".join(
        f"{g}: {info.get('total', 0)}({json.dumps(info.get('outcomes', {}), ensure_ascii=False)})"
        for g, info in sorted(win_gates.items())
    ) or "本周期尚无新触发记录"

    sys_rows = "".join([
        f"<tr><td>飞书桥与秘书</td>"
        f"<td>建单 {fblog['created_issues']} · 关联 {fblog['bound_issues']} · 绑定话题 {fb['active_threads_count']}</td>"
        f"<td>识别失败 {fblog['intent_failed']} · 接口/补读报错 {fblog['api_or_catchup_errors']} · 轮询失败 {fblog['poll_failed']}</td>"
        f"<td>待重发评论 {fb['held_unsent_notes_count']} · 未决意图 {fb['pending_intents_count']} · 失效群重复报错 {e(json.dumps(fblog['unreachable_chats_repeated'], ensure_ascii=False))}</td></tr>",
        f"<tr><td>GitLab 收件箱与主动接活</td>"
        f"<td>启动交付 {inblog['launched_team_up']} · 在途 {inb['open_origins_count']} · 协议变更提醒 {inblog['protocol_update_broadcasts']}</td>"
        f"<td>轮询失败 {inblog['poll_failed']}（超时 {inblog['poll_timeouts']}）· 投递被拒 {inblog['prompt_rejected']} · 状态/标签失败 {inblog['status_query_failed'] + inblog['resume_stage_failed']}</td>"
        f"<td>主动接活 {e(json.dumps(inb['proactive_verdicts_in_window'], ensure_ascii=False))} · 标注 {e(json.dumps(inb['proactive_marks_in_window'], ensure_ascii=False))}</td></tr>",
        f"<tr><td>调度与资源卫生</td>"
        f"<td>活跃任务 {digest['delivery']['active_tasks_count']} · 活跃调度会话 {len(digest['delivery']['scheduler_omp_sessions'])}</td>"
        f"<td>漏挂等待被拦日志 {digest['delivery']['watch_guard_events_count']} 行</td>"
        f"<td>存活成员窗格 {hyg['live_member_panes']}（空闲 {hyg['idle_member_panes']}，同角重复空闲 {e(json.dumps(hyg['duplicate_idle_same_role'], ensure_ascii=False))}）· worktree {hyg['worktree_count']} 个</td></tr>",
        f"<tr><td>Jev 五道决策关卡</td>"
        f"<td>本周期调用共 {total_jev_calls} 次</td>"
        f"<td>待淘汰零命中规则 {len(jev_data.get('retirement_candidates') or [])} 条 · 历史已淘汰 {jev_data.get('retired_rules_count', 0)} 条</td>"
        f"<td>{e(jev_win_brief)}</td></tr>",
    ])

    period_text = f"{win['since_label']} ~ {win['until_label']}"
    pick_payload = {
        "period": period_text,
        "items": [
            {
                "idx": idx,
                "priority": str(it["priority"]),
                "area": str(it["area"]),
                "title": str(it["title"]),
                "action": str(it["action"]),
            }
            for idx, it in enumerate(items, 1)
        ],
    }
    pick_json = json.dumps(pick_payload, ensure_ascii=False).replace("</", "<\\/")

    fq_what = (
        f"本周期复盘 {digest['delivery']['active_tasks_count']} 个活跃任务与 {total_jev_calls} 次 Jev 关卡调用，"
        f"自动生效 {len(applied_list)} 项规则变更，提炼 {len(items)} 项待拍板优化点。"
    )
    fq_why = f"{analysis['solution_plain']}（原因：{analysis['why_plain']}）"
    fq_break = (
        f"安全与授权红线已硬锁死不可自动放宽；本轮护栏拦截 {len(rejected_list)} 项不合规自动提议。"
        "若强行放开红线、跳过单测回滚或在样本不足时自动淘汰规则，会造成误放行或漏拦。"
    )
    fq_learn = (
        f"本周期最核心信号：{analysis['headline']}"
        + (f"（最高优：{items[0]['title']}）" if items else "")
    )

    return HTML_TEMPLATE.format(
        period=e(period_text),
        when=e(fmt_local(generated_at)),
        headline=e(analysis["headline"]),
        problem=e(analysis["problem_plain"]),
        solution=e(analysis["solution_plain"]),
        why=e(analysis["why_plain"]),
        fb_status=e(node_status("飞书桥")),
        inbox_status=e(node_status("收件箱")),
        delivery_status=e(node_status("交付流程")),
        jev_status=e(jev_status_text.replace("#", "")),
        item_count=len(items),
        item_rows=item_rows,
        gate_evo_rows=gate_evo_rows,
        gate_rule_rows=gate_rule_rows,
        sys_rows=sys_rows,
        task_rows=task_rows,
        fq_what=e(fq_what),
        fq_why=e(fq_why),
        fq_break=e(fq_break),
        fq_learn=e(fq_learn),
        script=RETROSPECT_PICKER_SCRIPT.replace("__PICK_DATA__", pick_json),
    )


RETROSPECT_PICKER_SCRIPT = """<script>
const RETRO_DATA = __PICK_DATA__;
const STORAGE_KEY = "hwf-retrospect-picks-" + RETRO_DATA.period;
let picks = {};
try { picks = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch (_e) { picks = {}; }

function savePicks() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(picks)); } catch (_e) {}
}

function buildInstruction() {
  const chosen = [];
  for (const it of RETRO_DATA.items) {
    const cur = picks[String(it.idx)];
    if (!cur || !cur.act || cur.act === "skip") continue;
    chosen.push({ it, cur });
  }
  if (!chosen.length) return "";
  const lines = [`按本期周期复盘（${RETRO_DATA.period}）勾选结果处理以下优化项：`];
  for (const { it, cur } of chosen) {
    const label = cur.act === "do" ? "直接做" : "按备注调整后做";
    const note = (cur.note || "").trim();
    lines.push(`- #${it.idx} [${it.priority}/${it.area}] ${it.title}（${label}）｜原建议：${it.action}${note ? "｜补充：" + note : ""}`);
  }
  return lines.join("\\n");
}

function syncPicker() {
  let doCnt = 0, adjCnt = 0, skipCnt = 0;
  for (const it of RETRO_DATA.items) {
    const k = String(it.idx);
    const cur = picks[k] || {};
    if (cur.act === "do") doCnt++;
    else if (cur.act === "adjust") adjCnt++;
    else if (cur.act === "skip") skipCnt++;
    const cell = document.querySelector(`.pick-cell[data-idx="${k}"]`);
    if (!cell) continue;
    cell.querySelectorAll(".pick-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.act === cur.act);
    });
    const inp = cell.querySelector(".pick-note");
    if (inp && inp.value !== (cur.note || "")) inp.value = cur.note || "";
  }
  document.getElementById("dockStat").innerHTML = RETRO_DATA.items.length
    ? `复盘挑项：直接做 <b>${doCnt}</b> · 调整后做 <b>${adjCnt}</b> · 先不做 <b>${skipCnt}</b>`
    : "本周期无可优化待办项";
  document.getElementById("previewArea").value = buildInstruction() || "（请先勾选「要做」或「调整」的优化项）";
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { el.textContent = ""; }, 3000);
}

async function copyInstruction() {
  const text = buildInstruction();
  if (!text) { showToast("请先勾选至少一项「要做」或「调整」"); return; }
  const area = document.getElementById("previewArea");
  area.value = text;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); showToast("已复制待办指令，直接粘回会话即可"); return; } catch (_e) {}
  }
  document.getElementById("previewWrap").classList.add("open");
  area.focus();
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_e) {}
  showToast(ok ? "已复制待办指令，直接粘回会话即可" : "已展开文本框，请按 Cmd+C 复制");
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".pick-btn");
  if (btn) {
    const k = btn.dataset.idx;
    const act = btn.dataset.act;
    const cur = picks[k] || { note: "" };
    picks[k] = cur.act === act ? { act: "", note: cur.note || "" } : { act, note: cur.note || "" };
    savePicks();
    syncPicker();
    return;
  }
  if (ev.target.id === "btnCopy") { copyInstruction(); return; }
  if (ev.target.id === "btnClear") { picks = {}; savePicks(); syncPicker(); showToast("已清空勾选"); return; }
  if (ev.target.id === "btnPreview") { document.getElementById("previewWrap").classList.toggle("open"); }
});

document.addEventListener("input", (ev) => {
  const inp = ev.target.closest(".pick-note");
  if (!inp) return;
  const k = inp.dataset.idx;
  const cur = picks[k] || { act: "adjust" };
  if (!cur.act || cur.act === "skip") cur.act = "adjust";
  cur.note = inp.value;
  picks[k] = cur;
  savePicks();
  syncPicker();
});

syncPicker();
</script>"""


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Herdr 交付链路周期复盘（{period}）</title>
<style>
:root {{
  --bg: #f7f8fa;
  --card: #ffffff;
  --ink: #1f2328;
  --muted: #59636e;
  --line: #d0d7de;
  --accent: #0969da;
  --warn: #9a6700;
  --danger: #cf222e;
  --tag-bg: #eef1f4;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --ink: #e6edf3;
    --muted: #8b949e;
    --line: #30363d;
    --accent: #58a6ff;
    --warn: #d29922;
    --danger: #f85149;
    --tag-bg: #21262d;
  }}
}}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
main {{ max-width: 1120px; margin: 0 auto; padding: 28px 18px 130px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 17px; margin: 26px 0 10px; }}
.sub {{ color: var(--muted); margin: 0 0 18px; }}
.plain-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 18px; }}
.fq-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-top: 8px; }}
.box {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }}
.box b {{ display: block; margin-bottom: 4px; }}
.headline {{ border-left: 4px solid var(--accent); margin-bottom: 14px; }}
.flow-wrap {{ overflow-x: auto; }}
svg.flow {{ width: 100%; max-width: 960px; height: auto; display: block; margin: 4px auto; color: var(--ink); }}
table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
th, td {{ text-align: left; vertical-align: top; padding: 10px 12px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 13px; color: var(--muted); background: var(--tag-bg); }}
tr:last-child td {{ border-bottom: none; }}
.tag {{ display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 12px; font-weight: 600; background: var(--tag-bg); color: var(--ink); }}
.p-p1 {{ color: var(--danger); border: 1px solid var(--danger); background: transparent; }}
.p-p2 {{ color: var(--warn); border: 1px solid var(--warn); background: transparent; }}
.p-p3 {{ color: var(--accent); border: 1px solid var(--accent); background: transparent; }}
.muted {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
.empty {{ color: var(--muted); text-align: center; padding: 18px; }}
code {{ font: 12px ui-monospace, Menlo, monospace; background: var(--tag-bg); padding: 1px 5px; border-radius: 4px; }}
.pick-cell {{ display: flex; flex-direction: column; gap: 6px; min-width: 170px; }}
.pick-btns {{ display: flex; gap: 4px; flex-wrap: wrap; }}
.pick-btn, .dock-btn {{ cursor: pointer; border: 1px solid var(--line); background: var(--card); color: var(--ink); border-radius: 7px; padding: 3px 8px; font-size: 12px; }}
.pick-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }}
.pick-note {{ width: 100%; box-sizing: border-box; border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 6px; padding: 4px 7px; font-size: 12px; }}
.dock {{ position: fixed; left: 0; right: 0; bottom: 0; background: var(--card); border-top: 1px solid var(--line); box-shadow: 0 -4px 18px rgba(0,0,0,.08); padding: 10px 16px; z-index: 50; }}
.dock-in {{ max-width: 1120px; margin: 0 auto; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; }}
.dock-actions {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
.dock-btn.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; padding: 5px 12px; }}
.toast {{ font-size: 12px; color: var(--accent); font-weight: 600; }}
.preview-wrap {{ max-width: 1120px; margin: 8px auto 0; display: none; }}
.preview-wrap.open {{ display: block; }}
.preview-area {{ width: 100%; box-sizing: border-box; min-height: 92px; border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: var(--bg); color: var(--ink); font: 12px/1.5 ui-monospace, Menlo, monospace; }}
</style>
</head>
<body>
<main>
  <h1>Herdr 交付链路周期复盘</h1>
  <p class="sub">统计周期：{period} · 生成于 {when} · 分析模型：droid (gemini-4-carbon, high)</p>

  <div class="box headline"><b>一句话结论</b>{headline}</div>

  <h2>先说人话</h2>
  <div class="plain-grid">
    <div class="box"><b>遇到了什么问题</b>{problem}</div>
    <div class="box"><b>打算怎么解决</b>{solution}</div>
    <div class="box"><b>为什么这样做</b>{why}</div>
  </div>

  <h2>链路环节健康分布</h2>
  <div class="box flow-wrap">
    <svg class="flow" viewBox="0 0 920 172" role="img" aria-label="飞书桥、GitLab、收件箱、交付团队与 Jev 五道关卡协作链路">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" fill="currentColor"/>
        </marker>
      </defs>
      <rect x="10" y="14" width="190" height="74" rx="10" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <text x="105" y="42" text-anchor="middle" fill="currentColor" font-size="14" font-weight="600">1. 飞书桥与秘书</text>
      <text x="105" y="66" text-anchor="middle" fill="var(--muted)" font-size="12">{fb_status}</text>

      <line x1="204" y1="51" x2="246" y2="51" stroke="var(--muted)" stroke-width="1.5" marker-start="url(#arrow)" marker-end="url(#arrow)"/>

      <rect x="250" y="14" width="170" height="74" rx="10" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <text x="335" y="42" text-anchor="middle" fill="currentColor" font-size="14" font-weight="600">2. GitLab 平台</text>
      <text x="335" y="66" text-anchor="middle" fill="var(--muted)" font-size="12">唯一任务与证据源</text>

      <line x1="424" y1="51" x2="466" y2="51" stroke="var(--muted)" stroke-width="1.5" marker-start="url(#arrow)" marker-end="url(#arrow)"/>

      <rect x="470" y="14" width="195" height="74" rx="10" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <text x="567" y="42" text-anchor="middle" fill="currentColor" font-size="14" font-weight="600">3. 收件箱与主动接活</text>
      <text x="567" y="66" text-anchor="middle" fill="var(--muted)" font-size="12">{inbox_status}</text>

      <line x1="669" y1="51" x2="711" y2="51" stroke="var(--muted)" stroke-width="1.5" marker-start="url(#arrow)" marker-end="url(#arrow)"/>

      <rect x="715" y="14" width="195" height="74" rx="10" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <text x="812" y="42" text-anchor="middle" fill="currentColor" font-size="14" font-weight="600">4. 调度者与成员团队</text>
      <text x="812" y="66" text-anchor="middle" fill="var(--muted)" font-size="12">{delivery_status}</text>

      <rect x="10" y="104" width="900" height="54" rx="10" fill="none" stroke="var(--line)" stroke-dasharray="5 4" stroke-width="1.5"/>
      <text x="460" y="128" text-anchor="middle" fill="currentColor" font-size="13" font-weight="600">5. TypeSafe Jev 五道决策关卡护栏（红线锁死 · 常规项数据达标并过单测自动演进）</text>
      <text x="460" y="147" text-anchor="middle" fill="var(--muted)" font-size="12">{jev_status}</text>
    </svg>
  </div>

  <h2>可优化点列表（{item_count} 项，勾选后底部一键复制给 Agent）</h2>
  <table>
    <thead>
      <tr>
        <th style="width:86px">优先级</th>
        <th style="width:88px">环节</th>
        <th style="width:28%">问题与本周期证据</th>
        <th style="width:18%">根因</th>
        <th>具体建议改法</th>
        <th style="width:58px">改动量</th>
        <th style="width:185px">拍板</th>
      </tr>
    </thead>
    <tbody>{item_rows}</tbody>
  </table>

  <h2>Jev 关卡本轮自动演进记录</h2>
  <table>
    <thead>
      <tr>
        <th style="width:110px">状态</th>
        <th style="width:130px">动作</th>
        <th style="width:36%">变更内容</th>
        <th>数据依据 / 拦截原因</th>
      </tr>
    </thead>
    <tbody>{gate_evo_rows}</tbody>
  </table>

  <h2>关卡 4 与关卡 5 规则清单近 30 天表现</h2>
  <table>
    <thead>
      <tr>
        <th style="width:160px">所属关卡</th>
        <th>规则项</th>
        <th style="width:120px">护栏属性</th>
        <th style="width:70px">阈值</th>
        <th style="width:90px">30天评估</th>
        <th style="width:210px">30天命中与擦边</th>
      </tr>
    </thead>
    <tbody>{gate_rule_rows}</tbody>
  </table>

  <h2>四大环节过程指标对照</h2>
  <table>
    <thead>
      <tr>
        <th style="width:160px">环节</th>
        <th>吞吐与活动</th>
        <th>异常与重试</th>
        <th>积压与资源状态</th>
      </tr>
    </thead>
    <tbody>{sys_rows}</tbody>
  </table>

  <h2>本周期活跃任务耗时与轮次明细</h2>
  <table>
    <thead>
      <tr>
        <th>任务</th>
        <th>活跃 / 跨度（空档）</th>
        <th>STATE 规模</th>
        <th>评审 / 测试轮数</th>
        <th>本周期评论分布</th>
        <th>当前阶段与待办</th>
      </tr>
    </thead>
    <tbody>{task_rows}</tbody>
  </table>
  <h2>本周期核心四问自检</h2>
  <div class="fq-grid">
    <div class="box"><b>1. 做了什么</b>{fq_what}</div>
    <div class="box"><b>2. 为什么这样取舍</b>{fq_why}</div>
    <div class="box"><b>3. 什么情况下会坏掉</b>{fq_break}</div>
    <div class="box"><b>4. 这次踩坑与收获</b>{fq_learn}</div>
  </div>
</main>

<div class="dock">
  <div class="dock-in">
    <div id="dockStat"></div>
    <div class="dock-actions">
      <span class="toast" id="toast" role="status" aria-live="polite"></span>
      <button type="button" class="dock-btn" id="btnClear">清空勾选</button>
      <button type="button" class="dock-btn" id="btnPreview">查看/手动复制</button>
      <button type="button" class="dock-btn primary" id="btnCopy">复制待办指令给 Agent</button>
    </div>
  </div>
  <div class="preview-wrap" id="previewWrap">
    <textarea class="preview-area" id="previewArea" readonly></textarea>
  </div>
</div>
{script}
</body>
</html>
"""


def format_feishu_markdown(
    digest: Mapping[str, Any],
    analysis: Mapping[str, Any],
    report_path: Path,
    *,
    gate_evolution: Optional[Mapping[str, Any]] = None,
) -> str:
    win = digest["window"]
    items: Sequence[Mapping[str, str]] = analysis["items"]
    evo = gate_evolution or {}
    applied = list(evo.get("applied") or [])
    rejected = list(evo.get("rejected") or [])
    lines = [
        f"**周期复盘（{win['since_label']} ~ {win['until_label']}）**",
        analysis["headline"],
        "",
    ]
    if not items:
        lines.append("本周期交付流程、飞书桥、收件箱与 Jev 关卡运行平稳，未发现需要人工改动的新优化点。")
    else:
        lines.append(f"共找出 **{len(items)}** 个可优化点（按优先级排序）：")
        for idx, it in enumerate(items, 1):
            lines.append(
                f"{idx}. **[{it['priority']} · {it['area']}] {it['title']}**（改动：{it['effort']}）\n"
                f"   - 现象：{it['symptom_and_evidence']}\n"
                f"   - 建议：{it['action']}"
            )
    if applied:
        git_meta = evo.get("git") or {}
        git_note = f"（已提交 `{git_meta.get('sha')}`）" if git_meta.get("committed") else ""
        lines.append(f"\n**Jev 关卡本轮已自动更新并过单测{git_note}：**")
        for a in applied:
            lines.append(f"- {a.get('summary')}：{a.get('reason')}")
    if rejected:
        lines.append(f"- 另有 {len(rejected)} 项关卡提议因触碰红线护栏或未达客观门槛已自动拦截（详见 HTML 报告）。")
    lines.append(f"\n详细图文报告（本机双击打开）：{report_path}")
    return "\n".join(lines)


# ---------- 主控类 ----------

class Retrospect:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        files: Optional[Files] = None,
        run: Runner = subprocess.run,
        now: Callable[[], float] = time.time,
        hwf_home: Path = HWF_HOME,
        send_ntfy_fn: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        self.raw_config = config
        self.cfg = {**DEFAULTS, **dict(config.get("retrospect") or {})}
        self.files = files or Files()
        self.run = run
        self.now = now
        self.hwf_home = hwf_home
        self._send_ntfy_fn = send_ntfy_fn
        self.state = self.files.load_state()
        for k, default in (("last_slot", ""), ("last_run_at", 0.0), ("attempts", {}), ("pending_notify", None)):
            self.state.setdefault(k, default)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def tick_in_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.safe_tick, name="retrospect", daemon=True)
        self._thread.start()

    def safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as err:  # 周期复盘出任何错都不能让常驻收件箱退出
            log(f"tick failed: {err!r}")

    def tick(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if not self.cfg.get("enabled", True):
                return None
            now = self.now()
            if self.state.get("pending_notify"):
                self._flush_pending_notify()
                self.files.save_state(self.state)

            (slot_id, slot_ts), (_, prev_slot_ts) = due_slots(
                now, self.cfg["weekdays"], int(self.cfg["hour"]), int(self.cfg["minute"])
            )
            if not self.state.get("last_slot"):
                self.state["last_slot"] = slot_id
                self.state["initialized_at"] = iso_local(now)
                self.files.save_state(self.state)
                log(f"initialized watermark at {slot_id}; next run on upcoming scheduled Tue/Fri")
                return None

            if slot_id <= str(self.state["last_slot"]):
                return None

            max_age_s = float(self.cfg["catchup_max_hours"]) * 3600.0
            if (now - slot_ts) > max_age_s:
                log(f"skip expired slot {slot_id} (age {(now - slot_ts) / 3600:.1f}h > {self.cfg['catchup_max_hours']}h)")
                self.state["last_slot"] = slot_id
                self.files.save_state(self.state)
                return None

            attempts_map: dict[str, dict[str, Any]] = self.state.setdefault("attempts", {})
            att = attempts_map.setdefault(slot_id, {"count": 0, "last_at": 0.0})
            max_att = int(self.cfg["max_attempts_per_slot"])
            if int(att["count"]) >= max_att:
                return None
            if int(att["count"]) > 0 and (now - float(att.get("last_at") or 0)) < float(self.cfg["retry_backoff_s"]):
                return None

            last_run_at = float(self.state.get("last_run_at") or 0.0)
            max_lb_s = float(self.cfg["max_lookback_days"]) * 86400.0
            since_ts = last_run_at if (0 < last_run_at < now and (now - last_run_at) <= max_lb_s) else prev_slot_ts

            try:
                result = self.execute_review(since_ts, now, slot_id=slot_id, notify=True)
            except Exception as err:
                att["count"] = int(att["count"]) + 1
                att["last_at"] = now
                att["error"] = str(err)[:300]
                log(f"slot {slot_id} attempt {att['count']}/{max_att} failed: {err}")
                if att["count"] >= max_att:
                    self.send_ntfy(
                        "周期复盘失败",
                        f"❌ 失败: 周期复盘({slot_id})连续 {max_att} 次失败 | {str(err)[:140]}",
                    )
                self.files.save_state(self.state)
                return None

            self.state["last_slot"] = slot_id
            self.state["last_run_at"] = now
            attempts_map.pop(slot_id, None)
            self.files.save_state(self.state)
            return result

    def execute_review(
        self,
        since_ts: float,
        now_ts: float,
        *,
        slot_id: str = "",
        notify: bool = True,
        apply_gates: Optional[bool] = None,
        verify_gates_fn: Optional[Callable[[Path], tuple[bool, str]]] = None,
    ) -> dict[str, Any]:
        digest = collect_digest(
            self.raw_config, self.files, since_ts, now_ts, run=self.run, hwf_home=self.hwf_home
        )
        self.files.cwd.mkdir(parents=True, exist_ok=True)
        digest_path = self.files.cwd / "digest-input.json"
        digest_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2))

        prompt_text = ANALYSIS_PROMPT_TEMPLATE.format(
            since_label=digest["window"]["since_label"],
            until_label=digest["window"]["until_label"],
            hwf_home=self.hwf_home,
        )
        prompt_path = self.files.cwd / "prompt.txt"
        prompt_path.write_text(prompt_text)

        raw = self.ask_droid(prompt_path)
        analysis = parse_analysis_json(raw)

        should_evolve = bool(self.cfg.get("auto_evolve_gates", True)) if apply_gates is None else apply_gates
        if should_evolve and analysis.get("gate_updates"):
            gate_evolution = JG.apply_gate_updates(
                analysis["gate_updates"],
                digest["jev_gates"],
                hwf_home=self.hwf_home,
                state_dir=self.files.dir,
                now_ts=now_ts,
                verify_fn=verify_gates_fn,
                auto_git_sync=bool(notify and self.cfg.get("git_sync_gates", True)),
                git_runner=self.run,
            )
            # 规则更新生效后刷新 digest 里的 30 天快照，保证当期 HTML 报告反映最新清单
            if gate_evolution.get("applied"):
                digest["jev_gates"] = JG.summarize_gate_telemetry(self.files.dir, self.hwf_home, since_ts, now_ts)
        else:
            gate_evolution = {"applied": [], "rejected": [], "verified": True, "git": {"committed": False}}

        self.files.reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d-%H%M")
        report_path = self.files.reports_dir / f"retrospect-{stamp}.html"
        html_doc = render_html_report(digest, analysis, now_ts, gate_evolution=gate_evolution)
        report_path.write_text(html_doc)
        self.files.latest_report.write_text(html_doc)

        record = {
            "slot": slot_id or f"manual-{stamp}",
            "at": iso_local(now_ts),
            "since": digest["window"]["since"],
            "until": digest["window"]["until"],
            "headline": analysis["headline"],
            "item_count": len(analysis["items"]),
            "items": analysis["items"],
            "gate_applied_count": len(gate_evolution.get("applied") or []),
            "gate_evolution": gate_evolution,
            "report_path": str(report_path),
        }
        self.files.append_jsonl(self.files.history, record)
        log(
            f"generated report ({len(analysis['items'])} items, "
            f"{len(gate_evolution.get('applied') or [])} gate updates): {report_path}"
        )

        if notify:
            key = f"retro-{slot_id or stamp}"
            feishu_md = format_feishu_markdown(
                digest, analysis, self.files.latest_report, gate_evolution=gate_evolution
            )
            applied_cnt = len(gate_evolution.get("applied") or [])
            gate_suffix = f"，自动更新关卡 {applied_cnt} 项" if applied_cnt else ""
            ntfy_body = (
                f"✅ 完成: 周期复盘已生成（{len(analysis['items'])} 个可优化点{gate_suffix}） | "
                f"{analysis['headline'][:90]} | 报告: {self.files.latest_report}"
            )
            self.state["pending_notify"] = {
                "key": key,
                "feishu_md": feishu_md,
                "feishu_done": not bool(self.cfg.get("feishu_notify", True)),
                "ntfy_title": "Herdr 周期复盘已生成",
                "ntfy_body": ntfy_body,
                "ntfy_done": False,
            }
            self._flush_pending_notify()

        return {
            "digest": digest,
            "analysis": analysis,
            "gate_evolution": gate_evolution,
            "report_path": str(report_path),
        }

    def ask_droid(self, prompt_path: Path) -> str:
        droid_bin = find_executable("droid", (Path.home() / ".local" / "bin" / "droid",))
        cmd = [
            droid_bin,
            "exec",
            "-m",
            str(self.cfg["model"]),
            "-r",
            str(self.cfg["reasoning_effort"]),
            "--cwd",
            str(self.files.cwd),
            "-f",
            str(prompt_path),
        ]
        try:
            res = self.run(
                cmd, capture_output=True, text=True, timeout=int(self.cfg["model_timeout_s"])
            )
        except subprocess.TimeoutExpired as err:
            raise RetrospectError(f"droid exec 超时（>{self.cfg['model_timeout_s']}s）") from err
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()[:300]
            raise RetrospectError(f"droid exec 退出码 {res.returncode}：{detail}")
        return res.stdout

    def _flush_pending_notify(self) -> None:
        pending = self.state.get("pending_notify")
        if not isinstance(pending, dict):
            self.state["pending_notify"] = None
            return
        if not pending.get("feishu_done"):
            pending["feishu_done"] = self.send_feishu_private(pending["feishu_md"], pending["key"])
        if not pending.get("ntfy_done"):
            pending["ntfy_done"] = self.send_ntfy(pending["ntfy_title"], pending["ntfy_body"])
        if pending.get("feishu_done") and pending.get("ntfy_done"):
            self.state["pending_notify"] = None

    def send_feishu_private(self, markdown: str, idempotency_key: str) -> bool:
        fb_state = self.files.read_json(self.files.feishu_state, {})
        chat_id = str(((fb_state.get("secretary") or {}).get("chat_id")) or "")
        if not chat_id:
            log("feishu notify skipped: secretary.chat_id not bound yet")
            return True
        lark_bin = find_executable(
            "lark-cli",
            tuple(sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/lark-cli"), reverse=True)),
        )
        cmd = [
            lark_bin, "im", "+messages-send",
            "--chat-id", chat_id,
            "--markdown", "-",
            "--idempotency-key", idempotency_key[:50],
            "--as", "bot",
        ]
        try:
            res = self.run(cmd, input=markdown, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                log(f"feishu send failed rc={res.returncode}: {(res.stderr or res.stdout)[:200]}")
                return False
            data = json.loads(res.stdout) if res.stdout.strip() else {}
            if isinstance(data, dict) and data.get("ok") is False:
                log(f"feishu send rejected: {data.get('error')}")
                return False
            return True
        except Exception as err:
            log(f"feishu send error: {err}")
            return False

    def send_ntfy(self, title: str, body: str) -> bool:
        if self._send_ntfy_fn is not None:
            return self._send_ntfy_fn(title, body)
        topic = str(self.cfg.get("ntfy_topic") or "").strip()
        if not topic:
            return True
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8").decode("latin1", errors="ignore") if title.isascii() else "Herdr Retrospect"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except Exception as err:
            log(f"ntfy send failed: {err}")
            return False


# ---------- 配置与 CLI ----------

def load_merged_config(config_path: Path = INBOX_CONFIG_PATH, local_path: Path = INBOX_LOCAL_PATH) -> dict[str, Any]:
    if not config_path.exists():
        raise RetrospectError(f"缺少配置 {config_path}")
    shared = json.loads(config_path.read_text())
    local = json.loads(local_path.read_text()) if local_path.exists() else {}
    shared_projects = shared.get("projects") or []
    local_projects = local.get("projects") or {}
    if isinstance(shared_projects, Mapping):
        projects = {**shared_projects, **local_projects}
    else:
        projects = {p: str(local_projects.get(p) or "") for p in shared_projects}
        for k, v in local_projects.items():
            projects.setdefault(k, str(v))
    merged = {**shared, **{k: v for k, v in local.items() if k != "projects"}}
    merged["projects"] = projects
    return merged


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_col = sub.add_parser("collect", help="仅采集本周期量化过程数据并写入 state/retrospect-cwd/digest-input.json")
    p_col.add_argument("--days", type=float, default=3.5, help="回看天数（默认 3.5 天）")

    p_run = sub.add_parser("run", help="立即执行一次完整复盘分析并产出 HTML 报告")
    p_run.add_argument("--days", type=float, default=3.5, help="回看天数（默认 3.5 天）")
    p_run.add_argument("--no-notify", action="store_true", help="只生成本地 HTML 报告，不推飞书私聊和 ntfy")
    p_run.add_argument("--no-apply-gates", action="store_true", help="仅评估不自动落盘更新 jev-gates.json")
    p_run.add_argument("--open", action="store_true", help="生成后在默认浏览器打开 HTML 报告")

    sub.add_parser("tick", help="按周二/周五 12:00 槽位检查是否到期并执行")

    args = parser.parse_args(argv)
    cfg = load_merged_config()
    retro = Retrospect(cfg)
    now = time.time()

    if args.cmd == "collect":
        since = now - float(args.days) * 86400.0
        digest = collect_digest(cfg, retro.files, since, now)
        retro.files.cwd.mkdir(parents=True, exist_ok=True)
        out = retro.files.cwd / "digest-input.json"
        out.write_text(json.dumps(digest, ensure_ascii=False, indent=2))
        print(json.dumps({
            "digest_path": str(out),
            "window": digest["window"],
            "active_tasks": digest["delivery"]["active_tasks_count"],
            "gitlab_issues_checked": len(digest["delivery"]["gitlab_issues_checked"]),
            "omp_sessions": len(digest["delivery"]["scheduler_omp_sessions"]),
            "feishu_log_lines": digest["feishu_bridge"]["log_summary"]["log_lines_in_window"],
            "inbox_log_lines": digest["gitlab_inbox"]["log_summary"]["log_lines_in_window"],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "run":
        since = now - float(args.days) * 86400.0
        res = retro.execute_review(
            since, now, notify=not args.no_notify, apply_gates=not args.no_apply_gates
        )
        retro.files.save_state(retro.state)
        if args.open:
            webbrowser.open(Path(res["report_path"]).as_uri())
        print(json.dumps({
            "report_path": res["report_path"],
            "latest_report": str(retro.files.latest_report),
            "headline": res["analysis"]["headline"],
            "item_count": len(res["analysis"]["items"]),
            "gate_applied_count": len(res["gate_evolution"].get("applied") or []),
        }, ensure_ascii=False, indent=2))
        return 0

    retro.tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
