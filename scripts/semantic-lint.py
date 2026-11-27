#!/usr/bin/env python3
"""团队铁律语义预检（Jev Gate ④）：读取 jev-gates.json 动态规则清单并行扫描冻结 git diff，在进 code-review 前拦截低级红线违规。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
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
CHUNK_DIFF_CHARS = 32_000
LOCKFILE_NAMES = frozenset({
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lockb",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
})
TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "__mocks__"})
DOC_SUFFIXES = frozenset({".md", ".mdx", ".txt", ".rst", ".adoc"})
DOC_FILENAMES = frozenset({"README", "LICENSE", "CHANGELOG", "NOTICE"})
HUNK_HEADER_RE = re.compile(r"^(@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@)")


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
    scope: str = "all"  # "all" | "non_test" | "prod_code" | "tests"
    added_only: bool = False


@dataclass(frozen=True)
class DiffFile:
    path: str
    raw_lines: tuple[str, ...]
    is_lockfile: bool
    is_test: bool
    is_doc: bool


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
            scope=str(r.get("scope") or "all"),
            added_only=bool(r.get("added_only", False)),
        )
        for r in rows
    )


RULES: tuple[RuleSpec, ...] = load_rules()


def _normalize_diff_path(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.lstrip("./")


def is_test_file(path: str) -> bool:
    """识别标准测试目录或测试文件命名，避免测试替身/断言噪音混入生产代码红线规则。"""
    norm = _normalize_diff_path(path)
    if not norm:
        return False
    parts = [p.lower() for p in norm.split("/") if p]
    if any(seg in TEST_DIR_NAMES for seg in parts[:-1]):
        return True
    name = parts[-1]
    stem = Path(name).stem
    return (
        ".test." in name
        or ".spec." in name
        or ".vitest." in name
        or name.startswith("test_")
        or stem.endswith("_test")
    )


def is_doc_file(path: str) -> bool:
    """识别纯说明/文档文件（不含 HTML 模板），避免文档举例干扰代码规则。"""
    norm = _normalize_diff_path(path)
    if not norm:
        return False
    p = Path(norm)
    return p.suffix.lower() in DOC_SUFFIXES or p.name.upper() in DOC_FILENAMES


def parse_diff_files(raw_diff: str) -> list[DiffFile]:
    """将 unified diff 按文件拆分为结构化列表并标注锁文件、测试与文档分类。"""
    if not raw_diff.strip():
        return []
    result: list[DiffFile] = []
    current: list[str] = []
    current_path = ""

    def flush() -> None:
        if not current:
            return
        norm = _normalize_diff_path(current_path)
        name = Path(norm).name if norm else ""
        result.append(
            DiffFile(
                path=norm,
                raw_lines=tuple(current),
                is_lockfile=name in LOCKFILE_NAMES,
                is_test=is_test_file(norm),
                is_doc=is_doc_file(norm),
            )
        )

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = [line]
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else ""
        else:
            current.append(line)
    flush()
    return result


def matches_scope(df: DiffFile, scope: str) -> bool:
    if df.is_lockfile:
        return False
    if scope == "tests":
        return df.is_test
    if scope == "non_test":
        return not df.is_test
    if scope == "prod_code":
        return (not df.is_test) and (not df.is_doc)
    return True


def render_file_diff(df: DiffFile, *, added_only: bool) -> str:
    """按规则口径渲染单文件 diff；added_only=True 时剔除未改动上下文行、删除行与 hunk 尾部旧函数名。"""
    if not added_only:
        return "\n".join(df.raw_lines) + "\n"

    headers: list[str] = []
    out_hunks: list[str] = []
    current_hunk_header: Optional[str] = None
    current_added: list[str] = []
    bare_added: list[str] = []
    in_hunk = False

    def flush_hunk() -> None:
        if current_added:
            if current_hunk_header:
                out_hunks.append(current_hunk_header)
            out_hunks.extend(current_added)

    for line in df.raw_lines:
        if line.startswith("@@ "):
            flush_hunk()
            in_hunk = True
            current_added = []
            m = HUNK_HEADER_RE.match(line)
            current_hunk_header = m.group(1) if m else "@@"
            continue
        if not in_hunk:
            if line.startswith(("diff --git ", "new file mode ", "rename from ", "rename to ", "+++ ")):
                headers.append(line)
            elif line.startswith("+") and not line.startswith("+++ "):
                # 兼容精简测试 diff（无 @@ hunk 头直接跟 + 行）
                bare_added.append(line)
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            current_added.append(line)
    flush_hunk()

    body = bare_added + out_hunks
    if not body:
        return ""
    return "\n".join(headers + body) + "\n"


def _chunk_rendered_files(
    rendered: Sequence[tuple[str, str]],
    *,
    lock_banner: str = "",
    chunk_chars: int = CHUNK_DIFF_CHARS,
    max_chars: int = MAX_DIFF_CHARS,
) -> list[tuple[list[str], str]]:
    """按文件边界分片控制单次送检体量，避免超长整包 diff 跨文件词元相互污染。"""
    chunks: list[tuple[list[str], str]] = []
    cur_files: list[str] = []
    cur_parts: list[str] = []
    cur_len = 0
    banner = lock_banner

    def flush() -> None:
        nonlocal cur_files, cur_parts, cur_len, banner
        if not cur_parts:
            return
        text = (banner + "".join(cur_parts)) if banner else "".join(cur_parts)
        banner = ""
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [diff truncated at MAX_DIFF_CHARS]\n"
        chunks.append((cur_files, text))
        cur_files = []
        cur_parts = []
        cur_len = 0

    for path, text in rendered:
        piece = text
        if len(piece) > max_chars:
            piece = piece[:max_chars] + "\n... [diff truncated at MAX_DIFF_CHARS]\n"
        if cur_parts and (cur_len + len(piece) > chunk_chars):
            flush()
        cur_files.append(path)
        cur_parts.append(piece)
        cur_len += len(piece)
    flush()
    return chunks


def filter_diff(raw_diff: str, max_chars: int = MAX_DIFF_CHARS) -> tuple[str, int]:
    """剔除锁文件大块噪音并限制总长度，返回（清洗后 diff, 纳入文件数）。"""
    parsed = parse_diff_files(raw_diff)
    if not parsed:
        return "", 0
    chunks: list[str] = []
    files_kept = 0
    for df in parsed:
        if df.is_lockfile:
            chunks.append(f"diff --git a/{df.path} b/{df.path}\n[omitted lockfile diff: {df.path}]\n")
        else:
            files_kept += 1
            chunks.append("\n".join(df.raw_lines) + "\n")

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


def _rule_question(rule: RuleSpec) -> JC.Noul:
    return JC.Noul(
        instructions=rule.instructions_en,
        criteria={"true": rule.true_en, "false": rule.false_en},
    )


def run_semantic_lint(
    diff_text: str,
    *,
    threshold: Optional[float] = None,
    jev: Optional[JC.JevClient] = None,
    hwf_home: Path = JG.HWF_HOME,
    state_dir: Optional[Path] = None,
) -> dict[str, Any]:
    parsed_files = parse_diff_files(diff_text)
    files_count = sum(1 for df in parsed_files if not df.is_lockfile)
    reported_thr = float(threshold) if threshold is not None else DEFAULT_THRESHOLD
    if files_count == 0:
        return {
            "status": "clean",
            "files_checked": 0,
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

    lock_banner = "".join(
        f"diff --git a/{df.path} b/{df.path}\n[omitted lockfile diff: {df.path}]\n"
        for df in parsed_files
        if df.is_lockfile
    )

    # 按实际送检的文件片段集合并规则，既严格落实每条规则的作用域与新增行边界，又避免重复请求
    grouped: dict[tuple[tuple[str, str], ...], list[RuleSpec]] = {}
    rule_rendered: dict[str, dict[str, str]] = {}
    for rule in rules:
        rendered_items: list[tuple[str, str]] = []
        for df in parsed_files:
            if not matches_scope(df, rule.scope):
                continue
            rendered_text = render_file_diff(df, added_only=rule.added_only)
            if rendered_text.strip():
                rendered_items.append((df.path, rendered_text))
        if not rendered_items:
            continue
        key = tuple(rendered_items)
        grouped.setdefault(key, []).append(rule)
        rule_rendered[rule.rule_id] = dict(rendered_items)

    if not grouped:
        return {
            "status": "clean",
            "files_checked": files_count,
            "threshold": reported_thr,
            "critical": [],
            "advisory": [],
        }

    client = jev if jev is not None else JC.default_client()
    scores: dict[str, float] = {rule.rule_id: 0.0 for rule in rules}
    peak_files: dict[str, list[str]] = {}
    evaluated_ids: set[str] = set()
    total_input_tokens = 0
    model_name = ""

    try:
        for rendered_tuple, group_rules in grouped.items():
            chunks = _chunk_rendered_files(rendered_tuple, lock_banner=lock_banner)
            questions = {rule.rule_id: _rule_question(rule) for rule in group_rules}
            for chunk_files, chunk_text in chunks:
                res = client.decide(state={"git_diff": chunk_text}, questions=questions, timeout_s=15.0)
                if not model_name and res.model:
                    model_name = res.model
                total_input_tokens += int(res.input_tokens or 0)
                for rule in group_rules:
                    evaluated_ids.add(rule.rule_id)
                    prob = round(res.noul(rule.rule_id), 4)
                    if prob > scores[rule.rule_id] or rule.rule_id not in peak_files:
                        scores[rule.rule_id] = prob
                        peak_files[rule.rule_id] = list(chunk_files)
    except JC.JevUnavailableError as exc:
        return {
            "status": "skipped",
            "reason": str(exc),
            "files_checked": files_count,
            "threshold": reported_thr,
            "critical": [],
            "advisory": [],
        }

    # 多文件分组命中红线时按单文件复核定位具体触发文件，方便开发与维护者快速定位
    pinpointed_files: dict[str, list[str]] = {}
    for rule in rules:
        cutoff = float(threshold) if threshold is not None else rule.threshold
        if rule.severity != "critical" or scores.get(rule.rule_id, 0.0) < cutoff:
            continue
        cands = peak_files.get(rule.rule_id, [])
        if len(cands) <= 1:
            pinpointed_files[rule.rule_id] = list(cands)
            continue
        rendered_map = rule_rendered.get(rule.rule_id, {})
        hits: list[str] = []
        q_single = {rule.rule_id: _rule_question(rule)}
        for fp in cands:
            ftext = rendered_map.get(fp, "")
            if not ftext.strip():
                continue
            try:
                sub = client.decide(state={"git_diff": ftext[:MAX_DIFF_CHARS]}, questions=q_single, timeout_s=15.0)
                total_input_tokens += int(sub.input_tokens or 0)
                if round(sub.noul(rule.rule_id), 4) >= cutoff:
                    hits.append(fp)
            except JC.JevUnavailableError:
                break
        pinpointed_files[rule.rule_id] = hits if hits else list(cands)

    critical_hits: list[dict[str, Any]] = []
    advisory_hits: list[dict[str, Any]] = []
    triggered_map: dict[str, float] = {}
    near_map: dict[str, float] = {}

    for rule in rules:
        prob = scores.get(rule.rule_id, 0.0)
        cutoff = float(threshold) if threshold is not None else rule.threshold
        if prob < cutoff:
            if rule.rule_id in evaluated_ids and prob >= cutoff - 0.15:
                near_map[rule.rule_id] = prob
            continue
        triggered_map[rule.rule_id] = prob
        item: dict[str, Any] = {
            "rule_id": rule.rule_id,
            "severity": rule.severity,
            "probability": prob,
            "threshold": cutoff,
            "title": rule.title_zh,
            "fix_hint": rule.fix_hint_zh,
        }
        hit_files = pinpointed_files.get(rule.rule_id) if rule.severity == "critical" else peak_files.get(rule.rule_id)
        if hit_files:
            item["files"] = hit_files
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
        rules_evaluated=[r.rule_id for r in rules if r.rule_id in evaluated_ids],
        rules_triggered=triggered_map,
        near_misses=near_map,
        meta={"files_checked": files_count},
        state_dir=state_dir,
    )
    return {
        "status": status,
        "model": model_name,
        "files_checked": files_count,
        "input_tokens": total_input_tokens,
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
        files_part = f" [{', '.join(item['files'])}]" if item.get("files") else ""
        lines.append(f"  [CRITICAL p={item['probability']:.2f}] {item['rule_id']}{files_part}: {item['title']} — {item['fix_hint']}")
    for item in report.get("advisory", []):
        files_part = f" [{', '.join(item['files'])}]" if item.get("files") else ""
        lines.append(f"  [ADVISORY p={item['probability']:.2f}] {item['rule_id']}{files_part}: {item['title']} — {item['fix_hint']}")
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
