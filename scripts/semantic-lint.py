#!/usr/bin/env python3
"""团队铁律语义预检（Jev Gate ④）：读取 jev-gates.json 动态规则清单并行扫描冻结 git diff，在进 code-review 前拦截低级红线违规。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Optional, Sequence

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import jev_client as JC
import jev_gates as JG

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CRITICAL = 2

DEFAULT_THRESHOLD = 0.80
MAX_DIFF_CHARS = 240_000
LOCKFILE_NAMES = frozenset({
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lockb",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
})


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    severity: str  # "critical" | "advisory"
    title_zh: str
    fix_hint_zh: str
    instructions_en: str
    true_en: str
    false_en: str
    threshold: float = DEFAULT_THRESHOLD
    protected: bool = False


def load_rules(hwf_home: Path = JG.HWF_HOME) -> tuple[RuleSpec, ...]:
    """从 jev-gates.json 读取当前启用的 Gate ④ 规则清单（按文件 mtime 自动热更新）。"""
    rows = JG.active_rules("gate4_semantic_lint", hwf_home)
    return tuple(
        RuleSpec(
            rule_id=str(r["rule_id"]),
            severity=str(r["severity"]),
            title_zh=str(r["title_zh"]),
            fix_hint_zh=str(r["fix_hint_zh"]),
            instructions_en=str(r["instructions_en"]),
            true_en=str(r["true_en"]),
            false_en=str(r["false_en"]),
            threshold=float(r.get("threshold", DEFAULT_THRESHOLD)),
            protected=bool(r.get("protected", False)),
        )
        for r in rows
    )


RULES: tuple[RuleSpec, ...] = load_rules()


def filter_diff(raw_diff: str, max_chars: int = MAX_DIFF_CHARS) -> tuple[str, int]:
    """剔除锁文件大块噪音并限制总长度，返回（清洗后 diff, 纳入文件数）。"""
    if not raw_diff.strip():
        return "", 0
    chunks: list[str] = []
    files_kept = 0
    current: list[str] = []
    current_file = ""

    def flush() -> None:
        nonlocal files_kept
        if not current:
            return
        name = Path(current_file).name if current_file else ""
        if name in LOCKFILE_NAMES:
            chunks.append(f"diff --git a/{current_file} b/{current_file}\n[omitted lockfile diff: {current_file}]\n")
        else:
            files_kept += 1
            chunks.append("\n".join(current) + "\n")

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = [line]
            parts = line.split(" b/", 1)
            current_file = parts[1].strip() if len(parts) == 2 else ""
        else:
            current.append(line)
    flush()

    joined = "".join(chunks)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n... [diff truncated at MAX_DIFF_CHARS]\n"
    return joined, files_kept


def collect_git_diff(repo: Path, base: Optional[str], head: Optional[str]) -> str:
    if base:
        target = f"{base}...{head or 'HEAD'}"
        cmd = ["git", "-C", str(repo), "diff", "--unified=3", target]
    elif head:
        cmd = ["git", "-C", str(repo), "show", "--format=", "--unified=3", head]
    else:
        cmd = ["git", "-C", str(repo), "diff", "--unified=3", "HEAD"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git diff failed with exit {proc.returncode}")
    return proc.stdout


def run_semantic_lint(
    diff_text: str,
    *,
    threshold: Optional[float] = None,
    jev: Optional[JC.JevClient] = None,
    hwf_home: Path = JG.HWF_HOME,
    state_dir: Optional[Path] = None,
) -> dict[str, Any]:
    cleaned, files_count = filter_diff(diff_text)
    reported_thr = float(threshold) if threshold is not None else DEFAULT_THRESHOLD
    if not cleaned.strip() or files_count == 0:
        return {
            "status": "clean",
            "files_checked": files_count,
            "threshold": reported_thr,
            "critical": [],
            "advisory": [],
        }

    rules = load_rules(hwf_home)
    if not rules:
        return {
            "status": "clean",
            "files_checked": files_count,
            "threshold": reported_thr,
            "critical": [],
            "advisory": [],
        }

    client = jev if jev is not None else JC.default_client()
    questions = {
        rule.rule_id: JC.Noul(
            instructions=rule.instructions_en,
            criteria={"true": rule.true_en, "false": rule.false_en},
        )
        for rule in rules
    }
    try:
        res = client.decide(state={"git_diff": cleaned}, questions=questions, timeout_s=15.0)
    except JC.JevUnavailableError as exc:
        return {
            "status": "skipped",
            "reason": str(exc),
            "files_checked": files_count,
            "threshold": reported_thr,
            "critical": [],
            "advisory": [],
        }

    critical_hits: list[dict[str, Any]] = []
    advisory_hits: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    triggered_map: dict[str, float] = {}
    near_map: dict[str, float] = {}

    for rule in rules:
        prob = round(res.noul(rule.rule_id), 4)
        scores[rule.rule_id] = prob
        cutoff = float(threshold) if threshold is not None else rule.threshold
        if prob < cutoff:
            if prob >= cutoff - 0.15:
                near_map[rule.rule_id] = prob
            continue
        triggered_map[rule.rule_id] = prob
        item = {
            "rule_id": rule.rule_id,
            "severity": rule.severity,
            "probability": prob,
            "threshold": cutoff,
            "title": rule.title_zh,
            "fix_hint": rule.fix_hint_zh,
        }
        if rule.severity == "critical":
            critical_hits.append(item)
        else:
            advisory_hits.append(item)

    if critical_hits:
        status = "critical_failed"
    elif advisory_hits:
        status = "advisory_flagged"
    else:
        status = "clean"

    JG.record_gate_event(
        "gate4_semantic_lint",
        status,
        rules_evaluated=[r.rule_id for r in rules],
        rules_triggered=triggered_map,
        near_misses=near_map,
        meta={"files_checked": files_count},
        state_dir=state_dir,
    )
    return {
        "status": status,
        "model": res.model,
        "files_checked": files_count,
        "input_tokens": res.input_tokens,
        "threshold": reported_thr,
        "critical": critical_hits,
        "advisory": advisory_hits,
        "scores": scores,
    }


def format_human(report: Mapping[str, Any]) -> str:
    status = report["status"]
    if status == "skipped":
        return f"semantic-lint: skipped ({report.get('reason', 'Jev unavailable')})"
    if status == "clean":
        return f"semantic-lint: clean (files={report.get('files_checked', 0)}, threshold={report.get('threshold')})"
    lines = [
        f"semantic-lint: {status} (files={report.get('files_checked', 0)}, "
        f"critical={len(report.get('critical', []))}, advisory={len(report.get('advisory', []))})"
    ]
    for item in report.get("critical", []):
        lines.append(f"  [CRITICAL p={item['probability']:.2f}] {item['rule_id']}: {item['title']} — {item['fix_hint']}")
    for item in report.get("advisory", []):
        lines.append(f"  [ADVISORY p={item['probability']:.2f}] {item['rule_id']}: {item['title']} — {item['fix_hint']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Jev semantic lint on a frozen git diff.")
    parser.add_argument("--repo", default=".", help="Target git repository path (default: current directory)")
    parser.add_argument("--base", help="Base git ref for three-dot diff (<base>...<head>)")
    parser.add_argument("--head", help="Head git ref (default: HEAD when --base is set)")
    parser.add_argument("--diff-file", help="Read unified diff from file path ('-' for stdin) instead of running git")
    parser.add_argument("--threshold", type=float, default=None, help="Override per-rule threshold from jev-gates.json")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args(argv)

    try:
        if args.diff_file:
            diff_text = sys.stdin.read() if args.diff_file == "-" else Path(args.diff_file).read_text(encoding="utf-8")
        else:
            diff_text = collect_git_diff(Path(args.repo).resolve(), args.base, args.head)
    except (OSError, RuntimeError) as exc:
        sys.stderr.write(f"semantic-lint error: {exc}\n")
        return EXIT_USAGE

    report = run_semantic_lint(diff_text, threshold=args.threshold)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_human(report))
    return EXIT_CRITICAL if report["status"] == "critical_failed" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
