#!/usr/bin/env python3
"""Jev 五道关卡规则库、滚动遥测账本与受保护自动演进引擎。

提供三项核心能力：
1. 动态读取 `~/.hwf/jev-gates.json`（按 mtime 缓存，修改后免重启即时生效）；
2. 将五道关卡每次判定结果写入 `state/jev-gates.jsonl`（滚动截断，单测自动隔离）；
3. 供每周二/周五 `retrospect.py` 汇总 30 天规则命中率/擦边率/僵尸规则，并在锁死安全红线的前提下自动执行新增、调优与淘汰。
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
DEFAULT_GATES_FILENAME = "jev-gates.json"
TELEMETRY_FILENAME = "jev-gates.jsonl"
CHANGELOG_FILENAME = "jev-gates-changelog.jsonl"
MAX_TELEMETRY_BYTES = 4 * 1024 * 1024

RULE_TABLES = ("gate4_semantic_lint", "gate5_media_guard", "gate5_note_guard")
MAX_ACTIVE_RULES = {
    "gate4_semantic_lint": 20,
    "gate5_media_guard": 8,
    "gate5_note_guard": 8,
}
MAX_ADDS_PER_RUN = 2
MAX_THRESHOLD_STEP = 0.08
MIN_THRESHOLD_STEP = 0.02
RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

_CONFIG_CACHE: dict[Path, tuple[int, dict[str, Any]]] = {}


class JevGatesError(RuntimeError):
    """规则库格式非法或校验失败。"""


def _running_under_unittest() -> bool:
    return "unittest" in sys.modules


def _iso_now(now_ts: Optional[float] = None) -> str:
    ts = time.time() if now_ts is None else now_ts
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _parse_ts(value: str) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text[:19], text[:10]):
        try:
            return datetime.fromisoformat(candidate).timestamp()
        except ValueError:
            continue
    return None


def gates_config_path(hwf_home: Path = HWF_HOME) -> Path:
    return hwf_home / DEFAULT_GATES_FILENAME


def validate_gates_schema(data: Mapping[str, Any]) -> None:
    if not isinstance(data, Mapping):
        raise JevGatesError("jev-gates.json top-level must be an object")
    thresholds = data.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise JevGatesError("jev-gates.json missing 'thresholds' object")
    for key, spec in thresholds.items():
        if not isinstance(spec, Mapping):
            raise JevGatesError(f"threshold {key!r} must be an object")
        val = spec.get("value")
        lo = spec.get("min")
        hi = spec.get("max")
        if not all(isinstance(x, (int, float)) for x in (val, lo, hi)):
            raise JevGatesError(f"threshold {key!r} missing numeric value/min/max")
        if not (0.0 < float(lo) <= float(val) <= float(hi) < 1.0):
            raise JevGatesError(f"threshold {key!r} value={val} out of bounds [{lo}, {hi}]")

    for table in RULE_TABLES:
        rules = data.get(table)
        if not isinstance(rules, list):
            raise JevGatesError(f"jev-gates.json missing list {table!r}")
        seen_ids: set[str] = set()
        for idx, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                raise JevGatesError(f"{table}[{idx}] must be an object")
            rid = str(rule.get("rule_id") or "")
            if not RULE_ID_RE.match(rid):
                raise JevGatesError(f"{table}[{idx}] invalid rule_id {rid!r}")
            if rid in seen_ids:
                raise JevGatesError(f"{table} duplicate rule_id {rid!r}")
            seen_ids.add(rid)
            thr = rule.get("threshold")
            if not isinstance(thr, (int, float)) or not (0.50 <= float(thr) <= 0.98):
                raise JevGatesError(f"{table}.{rid} threshold={thr!r} must be in [0.50, 0.98]")
            for req in ("title_zh", "instructions_en", "true_en", "false_en"):
                if not str(rule.get(req) or "").strip():
                    raise JevGatesError(f"{table}.{rid} missing non-empty {req!r}")
            if table == "gate4_semantic_lint":
                if rule.get("severity") not in ("critical", "advisory"):
                    raise JevGatesError(f"{table}.{rid} invalid severity {rule.get('severity')!r}")
                if not str(rule.get("fix_hint_zh") or "").strip():
                    raise JevGatesError(f"{table}.{rid} missing fix_hint_zh")
            else:
                if not str(rule.get("error_zh") or "").strip():
                    raise JevGatesError(f"{table}.{rid} missing error_zh")


def load_gates_config(hwf_home: Path = HWF_HOME) -> dict[str, Any]:
    candidate = gates_config_path(hwf_home)
    # 兼容单测传入未放置 jev-gates.json 的临时目录，回退读取仓库自带默认规则库
    path = (candidate if candidate.is_file() else gates_config_path(REPO_ROOT)).resolve()
    st = path.stat()
    cached = _CONFIG_CACHE.get(path)
    if cached is not None and cached[0] == st.st_mtime_ns:
        return copy.deepcopy(cached[1])
    parsed = json.loads(path.read_text(encoding="utf-8"))
    validate_gates_schema(parsed)
    _CONFIG_CACHE[path] = (st.st_mtime_ns, parsed)
    return copy.deepcopy(parsed)


def threshold_of(key: str, default: float, hwf_home: Path = HWF_HOME) -> float:
    try:
        cfg = load_gates_config(hwf_home)
        spec = (cfg.get("thresholds") or {}).get(key)
        if isinstance(spec, Mapping) and isinstance(spec.get("value"), (int, float)):
            return float(spec["value"])
    except (OSError, ValueError, JevGatesError):
        pass
    return default


def active_rules(table: str, hwf_home: Path = HWF_HOME) -> list[dict[str, Any]]:
    cfg = load_gates_config(hwf_home)
    rows = cfg.get(table) or []
    return [dict(r) for r in rows if isinstance(r, Mapping) and r.get("enabled", True)]


# ---------- 滚动遥测账本 ----------

def record_gate_event(
    gate: str,
    outcome: str,
    *,
    rules_evaluated: Sequence[str] = (),
    rules_triggered: Optional[Mapping[str, float]] = None,
    near_misses: Optional[Mapping[str, float]] = None,
    meta: Optional[Mapping[str, Any]] = None,
    state_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> None:
    """记录一次关卡决策摘要（不含原文与敏感信息）；单测默认隔离，不污染本机真实 state/。"""
    if state_dir is None and _running_under_unittest():
        return
    target_dir = state_dir if state_dir is not None else (HWF_HOME / "state")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_path = target_dir / TELEMETRY_FILENAME
        row: dict[str, Any] = {
            "at": _iso_now(now_ts),
            "gate": gate,
            "outcome": outcome,
        }
        if rules_evaluated:
            row["eval"] = list(rules_evaluated)
        if rules_triggered:
            row["trig"] = {str(k): round(float(v), 4) for k, v in rules_triggered.items()}
        if near_misses:
            row["near"] = {str(k): round(float(v), 4) for k, v in near_misses.items()}
        if meta:
            row["meta"] = dict(meta)

        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        if log_path.stat().st_size > MAX_TELEMETRY_BYTES:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            kept = "\n".join(lines[len(lines) // 2 :]) + "\n"
            tmp = log_path.with_suffix(".tmp")
            tmp.write_text(kept, encoding="utf-8")
            tmp.replace(log_path)
    except OSError:
        # 遥测写入失败绝不能中断主业务关卡。
        return


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# ---------- 周期与 30 天规则健康度统计 ----------

def summarize_gate_telemetry(
    state_dir: Path,
    hwf_home: Path,
    since_ts: float,
    now_ts: float,
    *,
    lifecycle_days: float = 30.0,
) -> dict[str, Any]:
    cfg = load_gates_config(hwf_home)
    rows = _read_jsonl(state_dir / TELEMETRY_FILENAME)
    lifecycle_since = now_ts - lifecycle_days * 86400.0

    window_by_gate: dict[str, dict[str, Any]] = {}
    rule_stats_30d: dict[tuple[str, str], dict[str, Any]] = {}

    gate_to_table = {
        "gate4_semantic_lint": "gate4_semantic_lint",
        "gate5_media_guard": "gate5_media_guard",
        "gate5_note_guard": "gate5_note_guard",
    }

    for table in RULE_TABLES:
        for rule in cfg.get(table) or []:
            if not rule.get("enabled", True):
                continue
            rid = str(rule["rule_id"])
            created_ts = _parse_ts(str(rule.get("created_at") or "")) or now_ts
            age_days = max(0.0, round((now_ts - created_ts) / 86400.0, 1))
            rule_stats_30d[(table, rid)] = {
                "rule_id": rid,
                "protected": bool(rule.get("protected")),
                "severity": rule.get("severity", "guard"),
                "threshold": float(rule["threshold"]),
                "title_zh": rule["title_zh"],
                "created_at": rule.get("created_at", ""),
                "age_days": age_days,
                "evals_30d": 0,
                "hits_30d": 0,
                "exempts_30d": 0,
                "near_misses_30d": 0,
                "last_hit_at": "",
                "retirement_eligible": False,
            }

    for row in rows:
        ts = _parse_ts(str(row.get("at") or ""))
        if ts is None or ts > now_ts + 60:
            continue
        gate = str(row.get("gate") or "unknown")
        outcome = str(row.get("outcome") or "unknown")
        eval_ids = [str(x) for x in (row.get("eval") or [])]
        trig_map = row.get("trig") if isinstance(row.get("trig"), Mapping) else {}
        near_map = row.get("near") if isinstance(row.get("near"), Mapping) else {}

        if ts >= since_ts:
            g_bucket = window_by_gate.setdefault(
                gate,
                {"total": 0, "outcomes": {}, "triggered_counts": {}, "near_miss_counts": {}},
            )
            g_bucket["total"] += 1
            g_bucket["outcomes"][outcome] = g_bucket["outcomes"].get(outcome, 0) + 1
            for rid in trig_map:
                g_bucket["triggered_counts"][rid] = g_bucket["triggered_counts"].get(rid, 0) + 1
            for rid in near_map:
                g_bucket["near_miss_counts"][rid] = g_bucket["near_miss_counts"].get(rid, 0) + 1

        table = gate_to_table.get(gate)
        if table and ts >= lifecycle_since:
            for rid in eval_ids:
                st = rule_stats_30d.get((table, rid))
                if st is not None:
                    st["evals_30d"] += 1
            for rid in trig_map:
                st = rule_stats_30d.get((table, rid))
                if st is not None:
                    if outcome == "exempted":
                        st["exempts_30d"] += 1
                    else:
                        st["hits_30d"] += 1
                        st["last_hit_at"] = str(row.get("at") or "")
            for rid in near_map:
                st = rule_stats_30d.get((table, rid))
                if st is not None:
                    st["near_misses_30d"] += 1

    rules_by_table: dict[str, list[dict[str, Any]]] = {t: [] for t in RULE_TABLES}
    retirement_candidates: list[dict[str, str]] = []
    for (table, _), st in rule_stats_30d.items():
        if (
            not st["protected"]
            and st["age_days"] >= 30.0
            and st["evals_30d"] >= 30
            and st["hits_30d"] == 0
        ):
            st["retirement_eligible"] = True
            retirement_candidates.append({"target": table, "rule_id": st["rule_id"], "title_zh": st["title_zh"]})
        rules_by_table[table].append(st)

    recent_changes = _read_jsonl(state_dir / CHANGELOG_FILENAME)[-10:]
    return {
        "thresholds": cfg.get("thresholds") or {},
        "window_by_gate": window_by_gate,
        "rules_30d": rules_by_table,
        "retirement_candidates": retirement_candidates,
        "retired_rules_count": len(cfg.get("retired_rules") or []),
        "recent_retired_rule_ids": [str(r.get("rule_id")) for r in (cfg.get("retired_rules") or [])[-10:]],
        "recent_gate_changelog": recent_changes,
    }


# ---------- 受保护自动演进引擎 ----------

def _default_verify_suite(hwf_home: Path) -> tuple[bool, str]:
    test_files = [
        hwf_home / "tests" / name
        for name in (
            "test_jev_gates.py",
            "test_semantic_lint.py",
            "test_upload_media.py",
            "test_task_record.py",
        )
        if (hwf_home / "tests" / name).is_file()
    ]
    if not test_files:
        return True, "no target test files found"
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *[str(p) for p in test_files]],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    ok = proc.returncode == 0
    detail = (proc.stderr or proc.stdout or "").strip()[-400:]
    return ok, detail


def _try_git_commit_and_push(
    hwf_home: Path,
    applied_summaries: Sequence[str],
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> dict[str, Any]:
    """仅当仓库在更新 jev-gates.json 之外无已暂存改动时，自动提交并同步双远端。"""
    staged = run(["git", "-C", str(hwf_home), "diff", "--cached", "--name-only"], capture_output=True, text=True, check=False)
    if staged.returncode != 0 or staged.stdout.strip():
        return {"committed": False, "reason": "index has pre-existing staged files"}

    add_res = run(["git", "-C", str(hwf_home), "add", DEFAULT_GATES_FILENAME], capture_output=True, text=True, check=False)
    if add_res.returncode != 0:
        return {"committed": False, "reason": "git add failed"}

    msg = (
        "chore(jev-gates): 周期复盘自动演进关卡清单\n\n"
        + "\n".join(f"- {s}" for s in applied_summaries)
        + "\n\nCo-authored-by: factory-droid[bot] <138933559+factory-droid[bot]@users.noreply.github.com>"
    )
    commit_res = run(
        ["git", "-C", str(hwf_home), "commit", "-m", msg, "--", DEFAULT_GATES_FILENAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_res.returncode != 0:
        return {"committed": False, "reason": (commit_res.stderr or commit_res.stdout).strip()[:200]}

    sha_res = run(["git", "-C", str(hwf_home), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    sha = sha_res.stdout.strip()
    pushed: list[str] = []
    for remote in ("origin", "github"):
        pr = run(["git", "-C", str(hwf_home), "push", remote, "main"], capture_output=True, text=True, timeout=30, check=False)
        if pr.returncode == 0:
            pushed.append(remote)
    return {"committed": True, "sha": sha, "pushed_remotes": pushed}


def apply_gate_updates(
    raw_updates: Sequence[Mapping[str, Any]],
    telemetry_summary: Mapping[str, Any],
    *,
    hwf_home: Path = HWF_HOME,
    state_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
    verify_fn: Optional[Callable[[Path], tuple[bool, str]]] = None,
    auto_git_sync: bool = False,
    git_runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> dict[str, Any]:
    """按安全护栏校验并落盘周期复盘产出的关卡更新；单测不通过时自动回滚原配置。"""
    if not raw_updates:
        return {"applied": [], "rejected": [], "verified": True, "git": {"committed": False}}

    ts = time.time() if now_ts is None else now_ts
    today = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    cfg_path = gates_config_path(hwf_home)
    existed = cfg_path.is_file()
    original_raw = (cfg_path if existed else gates_config_path(REPO_ROOT)).read_text(encoding="utf-8")
    cfg = json.loads(original_raw)

    stats_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for table, items in (telemetry_summary.get("rules_30d") or {}).items():
        for st in items:
            stats_index[(table, str(st.get("rule_id")))] = st

    retired_recent: dict[str, float] = {}
    for r in cfg.get("retired_rules") or []:
        rid = str(r.get("rule_id") or "")
        rts = _parse_ts(str(r.get("retired_at") or ""))
        if rid and rts is not None:
            retired_recent[rid] = rts

    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    adds_done = 0

    for item in raw_updates[:8]:
        if not isinstance(item, Mapping):
            continue
        op = str(item.get("op") or "").strip()
        target = str(item.get("target") or "").strip()
        reason = str(item.get("reason") or "").strip()

        if op == "add_rule":
            if adds_done >= MAX_ADDS_PER_RUN:
                rejected.append({"op": op, "target": target, "reason": "exceeded max 2 new rules per review cycle"})
                continue
            if target not in RULE_TABLES:
                rejected.append({"op": op, "target": target, "reason": f"invalid rule table {target!r}"})
                continue
            active_count = sum(1 for r in cfg[target] if r.get("enabled", True))
            if active_count >= MAX_ACTIVE_RULES[target]:
                rejected.append({"op": op, "target": target, "reason": f"{target} reached active limit {MAX_ACTIVE_RULES[target]}"})
                continue

            rid = str(item.get("rule_id") or "").strip()
            if not RULE_ID_RE.match(rid):
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "invalid rule_id format"})
                continue
            if any(r.get("rule_id") == rid for r in cfg[target]):
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "rule_id already exists"})
                continue
            if rid in retired_recent and (ts - retired_recent[rid]) < 14 * 86400:
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "rule_id was retired within last 14 days"})
                continue

            ev_count = int(item.get("evidence_count") or 0)
            if ev_count < 2 or not reason:
                rejected.append({
                    "op": op, "target": target, "rule_id": rid,
                    "reason": "new rule requires evidence_count >= 2 and non-empty reason",
                })
                continue

            raw_thr = float(item.get("threshold") or 0.82)
            thr = round(min(0.92, max(0.75, raw_thr)), 2)
            title_zh = str(item.get("title_zh") or "").strip()
            inst_en = str(item.get("instructions_en") or "").strip()
            true_en = str(item.get("true_en") or "").strip()
            false_en = str(item.get("false_en") or "").strip()
            if not all((title_zh, inst_en, true_en, false_en)):
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "missing required prompt/title fields"})
                continue

            # 安全铁锁：自动新增规则一律禁止设为 protected 或 critical 阻断级。
            new_entry: dict[str, Any] = {
                "rule_id": rid,
                "enabled": True,
                "protected": False,
                "threshold": thr,
                "created_at": today,
                "title_zh": title_zh,
                "instructions_en": inst_en,
                "true_en": true_en,
                "false_en": false_en,
                "added_reason": reason[:240],
            }
            if target == "gate4_semantic_lint":
                fix_zh = str(item.get("fix_hint_zh") or "").strip()
                if not fix_zh:
                    rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "missing fix_hint_zh"})
                    continue
                new_entry["severity"] = "advisory"
                new_entry["fix_hint_zh"] = fix_zh
            elif target == "gate5_media_guard":
                err_zh = str(item.get("error_zh") or "").strip()
                if not err_zh:
                    rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "missing error_zh"})
                    continue
                new_entry["allow_on_error_alt"] = bool(item.get("allow_on_error_alt", True))
                new_entry["error_zh"] = err_zh
            else:
                err_zh = str(item.get("error_zh") or "").strip()
                if not err_zh:
                    rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "missing error_zh"})
                    continue
                new_entry["min_outer_chars"] = max(0, int(item.get("min_outer_chars") or 0))
                kinds = item.get("kinds") or ["todo", "risk", "answer", "delivery-done"]
                new_entry["kinds"] = [str(k) for k in kinds]
                new_entry["error_zh"] = err_zh

            cfg[target].append(new_entry)
            adds_done += 1
            applied.append({
                "op": "add_rule",
                "target": target,
                "rule_id": rid,
                "title_zh": title_zh,
                "threshold": thr,
                "summary": f"新增 {target}.{rid}（{title_zh}，阈值 {thr}）",
                "reason": reason,
            })

        elif op == "retire_rule":
            rid = str(item.get("rule_id") or "").strip()
            if target not in RULE_TABLES:
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": f"invalid rule table {target!r}"})
                continue
            idx = next((i for i, r in enumerate(cfg[target]) if r.get("rule_id") == rid), None)
            if idx is None:
                rejected.append({"op": op, "target": target, "rule_id": rid, "reason": "rule_id not found"})
                continue
            rule = cfg[target][idx]
            if rule.get("protected"):
                rejected.append({
                    "op": op, "target": target, "rule_id": rid,
                    "reason": "protected security/core rule cannot be retired",
                })
                continue

            st = stats_index.get((target, rid)) or {}
            evals = int(st.get("evals_30d") or 0)
            exempts = int(st.get("exempts_30d") or 0)
            age_days = float(st.get("age_days") or 0.0)
            eligible_silent = bool(st.get("retirement_eligible"))
            eligible_noisy = evals >= 10 and (exempts / max(1, evals)) >= 0.50
            superseded_path = str(item.get("superseded_by_script") or "").strip()
            eligible_superseded = (
                age_days >= 14.0
                and bool(superseded_path)
                and (hwf_home / superseded_path).is_file()
            )
            if not (eligible_silent or eligible_noisy or eligible_superseded):
                rejected.append({
                    "op": op, "target": target, "rule_id": rid,
                    "reason": f"retirement criteria not met (age={age_days}d, evals_30d={evals}, hits_30d={st.get('hits_30d', 0)})",
                })
                continue

            removed = cfg[target].pop(idx)
            cfg.setdefault("retired_rules", []).append({
                "target": target,
                "rule_id": rid,
                "title_zh": removed.get("title_zh", ""),
                "retired_at": _iso_now(ts),
                "reason": reason or "auto-retired by retrospect",
                "evals_30d": evals,
                "hits_30d": int(st.get("hits_30d") or 0),
            })
            applied.append({
                "op": "retire_rule",
                "target": target,
                "rule_id": rid,
                "title_zh": removed.get("title_zh", ""),
                "summary": f"淘汰 {target}.{rid}（{removed.get('title_zh', '')}）",
                "reason": reason,
            })

        elif op == "tune_threshold":
            key = str(item.get("rule_id") or item.get("key") or "").strip()
            try:
                proposed = float(item["new_threshold"])
            except (KeyError, TypeError, ValueError):
                rejected.append({"op": op, "target": target, "key": key, "reason": "missing numeric new_threshold"})
                continue

            if target == "thresholds":
                spec = (cfg.get("thresholds") or {}).get(key)
                if not isinstance(spec, dict):
                    rejected.append({"op": op, "target": target, "key": key, "reason": "threshold key not found"})
                    continue
                old_val = float(spec["value"])
                lo, hi = float(spec["min"]), float(spec["max"])
                # 红线授权门槛禁止调松（调低），只允许往严微调。
                if spec.get("protected") and proposed < old_val:
                    rejected.append({"op": op, "target": target, "key": key, "reason": "protected threshold cannot be loosened"})
                    continue
                delta = proposed - old_val
                if abs(delta) < MIN_THRESHOLD_STEP:
                    rejected.append({"op": op, "target": target, "key": key, "reason": "change smaller than 0.02"})
                    continue
                clamped_step = old_val + (MAX_THRESHOLD_STEP if delta > 0 else -MAX_THRESHOLD_STEP) if abs(delta) > MAX_THRESHOLD_STEP else proposed
                new_val = round(min(hi, max(lo, clamped_step)), 2)
                if new_val == old_val:
                    rejected.append({"op": op, "target": target, "key": key, "reason": "already at boundary"})
                    continue
                spec["value"] = new_val
                applied.append({
                    "op": "tune_threshold",
                    "target": target,
                    "key": key,
                    "old": old_val,
                    "new": new_val,
                    "summary": f"调优阈值 thresholds.{key}：{old_val} -> {new_val}",
                    "reason": reason,
                })
            elif target in RULE_TABLES:
                rule = next((r for r in cfg[target] if r.get("rule_id") == key), None)
                if rule is None:
                    rejected.append({"op": op, "target": target, "rule_id": key, "reason": "rule_id not found"})
                    continue
                old_val = float(rule["threshold"])
                # 红线拦截规则禁止调高阈值放水（变松），只允许往严微调。
                if rule.get("protected") and proposed > old_val:
                    rejected.append({"op": op, "target": target, "rule_id": key, "reason": "protected rule threshold cannot be loosened"})
                    continue
                delta = proposed - old_val
                if abs(delta) < MIN_THRESHOLD_STEP:
                    rejected.append({"op": op, "target": target, "rule_id": key, "reason": "change smaller than 0.02"})
                    continue
                clamped_step = old_val + (MAX_THRESHOLD_STEP if delta > 0 else -MAX_THRESHOLD_STEP) if abs(delta) > MAX_THRESHOLD_STEP else proposed
                new_val = round(min(0.95, max(0.65, clamped_step)), 2)
                if new_val == old_val:
                    rejected.append({"op": op, "target": target, "rule_id": key, "reason": "already at boundary"})
                    continue
                rule["threshold"] = new_val
                applied.append({
                    "op": "tune_threshold",
                    "target": target,
                    "rule_id": key,
                    "old": old_val,
                    "new": new_val,
                    "summary": f"调优阈值 {target}.{key}：{old_val} -> {new_val}",
                    "reason": reason,
                })
            else:
                rejected.append({"op": op, "target": target, "key": key, "reason": f"unknown target {target!r}"})
        else:
            rejected.append({"op": op, "target": target, "reason": f"unsupported op {op!r}"})

    if not applied:
        return {"applied": [], "rejected": rejected, "verified": True, "git": {"committed": False}}

    cfg["updated_at"] = _iso_now(ts)
    try:
        validate_gates_schema(cfg)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _CONFIG_CACHE.pop(cfg_path.resolve(), None)
        checker = verify_fn or _default_verify_suite
        ok, detail = checker(hwf_home)
        if not ok:
            raise JevGatesError(f"regression suite failed: {detail}")
    except Exception as exc:
        if existed:
            cfg_path.write_text(original_raw, encoding="utf-8")
        else:
            cfg_path.unlink(missing_ok=True)
        _CONFIG_CACHE.pop(cfg_path.resolve(), None)
        return {
            "applied": [],
            "rejected": [*rejected, {"op": "rollback", "reason": str(exc)}],
            "verified": False,
            "git": {"committed": False},
        }

    target_state_dir = state_dir if state_dir is not None else (hwf_home / "state")
    target_state_dir.mkdir(parents=True, exist_ok=True)
    with (target_state_dir / CHANGELOG_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"at": _iso_now(ts), "applied": applied, "rejected": rejected},
                ensure_ascii=False,
            )
            + "\n"
        )

    git_info = (
        _try_git_commit_and_push(hwf_home, [a["summary"] for a in applied], run=git_runner)
        if auto_git_sync
        else {"committed": False}
    )
    return {"applied": applied, "rejected": rejected, "verified": True, "git": git_info}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect Jev decision gates config and 30-day telemetry.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_stats = sub.add_parser("stats", help="Show gate telemetry and rule lifecycle summary")
    p_stats.add_argument("--days", type=float, default=3.5, help="Window days for recent counts (default: 3.5)")
    p_stats.add_argument("--json", action="store_true")
    sub.add_parser("validate", help="Validate jev-gates.json schema")
    args = parser.parse_args(argv)

    if args.cmd == "validate":
        cfg = load_gates_config(HWF_HOME)
        counts = {t: len(cfg[t]) for t in RULE_TABLES}
        print(json.dumps({"ok": True, "rule_counts": counts}, ensure_ascii=False))
        return 0

    now = time.time()
    since = now - float(args.days) * 86400.0
    summary = summarize_gate_telemetry(HWF_HOME / "state", HWF_HOME, since, now)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"Jev Gates Summary (window: last {args.days:g}d, lifecycle: 30d)")
    for gate, info in sorted(summary["window_by_gate"].items()):
        print(f"  - {gate}: total={info['total']} outcomes={info['outcomes']}")
    for table in RULE_TABLES:
        rows = summary["rules_30d"][table]
        print(f"\n[{table}] ({len(rows)} active rules)")
        for r in rows:
            lock = "LOCKED" if r["protected"] else ("RETIRE?" if r["retirement_eligible"] else "active")
            print(
                f"  * {r['rule_id']} [{lock}, thr={r['threshold']}]: "
                f"30d evals={r['evals_30d']}, hits={r['hits_30d']}, near={r['near_misses_30d']} — {r['title_zh']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
