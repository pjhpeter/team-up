#!/usr/bin/env python3
"""团队铁律语义预检（Jev Gate ④）：对冻结 git diff 一次并行扫描 12 条团队铁律，在进 code-review 前拦截低级红线违规。"""

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


RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        rule_id="feature_flag",
        severity="critical",
        title_zh="新增未授权灰度或功能开关",
        fix_hint_zh="删除灰度/开关分支，默认直接按最终行为全量实现（除非需求明确授权兼容开关）。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in production code/config of this git diff (ignore `-` lines and unit tests). "
            "Does the diff introduce a feature flag, gray-release toggle, A/B experiment switch, or config/env toggle to gate new behavior?"
        ),
        true_en="Adds a feature flag, rollout percentage, gray switch, or env/config toggle gating old vs new behavior.",
        false_en="Direct implementation, normal business config, error fallback on missing external service, or test-only code.",
    ),
    RuleSpec(
        rule_id="secret_exposed",
        severity="critical",
        title_zh="硬编码真实密钥或凭据",
        fix_hint_zh="立即移除硬编码密钥/密码/私钥，改从 macOS Keychain 或本机忽略配置读取，并轮换已暴露凭据。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in non-test files of this git diff. "
            "Does the diff hardcode a real API key, secret token, private key PEM block, or plaintext password/credential?"
        ),
        true_en="Contains a real-looking secret key, bearer token, private key, or hardcoded account password in non-test files.",
        false_en="Reads secrets from Keychain/env/config files, uses obvious dummy placeholders in tests (e.g. 'test-key'), or has no secrets.",
    ),
    RuleSpec(
        rule_id="remote_exec",
        severity="critical",
        title_zh="在远程服务器执行命令",
        fix_hint_zh="移除 ssh/scp/rsync 远端命令或 kubectl exec / 远端 docker 调用，改走 CI/CD 或把命令交用户执行。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in executable code/scripts of this git diff (ignore documentation/prose rules and tests). "
            "Does the code execute commands on a remote server via `ssh`, `scp`, `rsync`, `kubectl exec`, remote `docker -H`, or cloud run-command APIs?"
        ),
        true_en="Added production script/code invokes ssh, scp, rsync to remote host, kubectl exec, or remote command execution.",
        false_en="Local commands, git/glab/curl/HTTPS API calls, CI config, or lint/rule definitions merely mentioning ssh.",
    ),
    RuleSpec(
        rule_id="main_automerge",
        severity="critical",
        title_zh="对 main 分支开启自动合并",
        fix_hint_zh="禁止对 main 目标分支设置 auto_merge / merge_when_pipeline_succeeds / glab mr merge --auto。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in code/scripts of this git diff (ignore lint rules and tests). "
            "Does the code enable auto-merge (`auto_merge=true`, `merge_when_pipeline_succeeds`, `glab mr merge --auto`) targeting `main`?"
        ),
        true_en="Enables automatic merge or merge-when-pipeline-succeeds on a merge request targeting main.",
        false_en="Manual/immediate merge after checks, MRs targeting non-main branches, or rule text forbidding auto-merge.",
    ),
    RuleSpec(
        rule_id="non_english_runtime_log",
        severity="advisory",
        title_zh="开发者运行日志使用了非英文",
        fix_hint_zh="将 console.*/logger.*/logging.*/log(...) 等开发者运行日志改为英文；面向用户的文案、评论和报错保持中文。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in production code of this git diff (ignore tests, comments, docstrings, UI strings, issue/chat messages, and user-facing CLI errors). "
            "Does production code emit Chinese text inside developer runtime logging calls (`log(...)`, `logger.*`, `logging.*`, `console.log/warn/error`)?"
        ),
        true_en="Added developer runtime log statements output Chinese characters.",
        false_en="Runtime logs are in English, or Chinese text only appears in comments, UI text, user-facing messages/errors, or tests.",
    ),
    RuleSpec(
        rule_id="root_test_artifacts",
        severity="advisory",
        title_zh="在仓库根目录落盘临时测试截图或录屏",
        fix_hint_zh="改写到 tempfile 临时目录或受控任务目录，不在仓库根目录遗留 .png/.mp4 调试文件。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in this git diff. "
            "Does code or a test write temporary screenshots, videos, or debug dumps directly into the repository root directory?"
        ),
        true_en="Writes `.png`, `.jpg`, `.mp4`, `.mov`, or debug dump files directly to repo root relative paths.",
        false_en="Uses tempfile/tmp directories, dedicated state/artifact folders, or writes no root artifacts.",
    ),
    RuleSpec(
        rule_id="weakened_tests",
        severity="advisory",
        title_zh="删改或跳过既有测试断言以求过测",
        fix_hint_zh="核对是否未经批准删除回归断言、添加 skip 或放宽通过标准。",
        instructions_en=(
            "Inspect test file changes in this git diff. "
            "Does the diff delete existing regression assertions, mark existing tests as skipped (`.skip`, `@unittest.skip`), or weaken expected thresholds just to make failing tests pass?"
        ),
        true_en="Skips existing tests, deletes meaningful assertions without replacement, or loosens assertions to hide a bug.",
        false_en="Adds new tests, updates fixtures to match an intentional spec change, or leaves existing assertions intact.",
    ),
    RuleSpec(
        rule_id="filler_comments",
        severity="advisory",
        title_zh="新增冗长废话注释或英文代码注释",
        fix_hint_zh="代码注释统一用精炼中文短句，只写原因、边界与风险，删掉复述代码字面动作的空话注释。",
        instructions_en=(
            "Inspect ONLY newly added `+` code comments in production files of this git diff (ignore Jev English prompt strings). "
            "Does the diff add verbose tutorial-style comments, restate obvious code mechanics ('increment i', 'initialize variable'), or write code comments in English instead of concise Chinese?"
        ),
        true_en="Adds English code comments or long filler comments that merely restate what the next line obviously does.",
        false_en="Uses concise Chinese comments explaining non-obvious why/constraints, or has self-explanatory code without filler comments.",
    ),
    RuleSpec(
        rule_id="legacy_upload_url",
        severity="advisory",
        title_zh="手拼旧版 GitLab 上传附件路径",
        fix_hint_zh="使用 upload-media.py 返回的 markdown（/uploads/...）与 web_url（/-/project/<id>/uploads/...），禁止手拼 /<ns>/<proj>/uploads/。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in code/scripts of this git diff (ignore warning docs and tests). "
            "Does the code manually construct GitLab attachment URLs using `/<namespace>/<project>/uploads/...` instead of `/-/project/<id>/uploads/...`?"
        ),
        true_en="Manually constructs `<host>/<namespace>/<project>/uploads/...` URLs that 404 on GitLab.",
        false_en="Uses `/-/project/<id>/uploads/...`, relative `/uploads/...`, `upload-media.py`, or unrelated URLs.",
    ),
    RuleSpec(
        rule_id="bypass_task_record",
        severity="advisory",
        title_zh="绕过 task-record.py 直接裸发 GitLab 评论",
        fix_hint_zh="任务进度、待办与交付评论统一经 scripts/task-record.py note 发送，保证免打扰与状态同步。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in workflow/helper scripts of this git diff (excluding `task-record.py`, `gitlab-inbox.py`, and tests). "
            "Does new code invoke `glab issue note` or POST directly to GitLab `/issues/<iid>/notes` bypassing `task-record.py`?"
        ),
        true_en="Added code outside task-record.py/gitlab-inbox.py posts issue comments directly via glab/API.",
        false_en="Uses task-record.py, modifies task-record.py/gitlab-inbox.py itself, or does not post GitLab issue comments.",
    ),
    RuleSpec(
        rule_id="silent_exception_swallow",
        severity="advisory",
        title_zh="在主路径静默吞掉未知异常",
        fix_hint_zh="收窄捕获的异常类型，补充降级日志或显式错误返回，避免空 except/catch 掩盖故障。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in production code of this git diff. "
            "Does the code catch broad `Exception` / bare `except:` / empty `catch {}` on a core business path and silently ignore it without logging, fallback explanation, or re-raising?"
        ),
        true_en="Broad bare catch/except silently swallows unexpected runtime bugs on a core path with no rationale.",
        false_en="Catches specific expected errors (e.g. JevUnavailableError, OSError on optional cache), logs/degrades intentionally, or propagates errors.",
    ),
    RuleSpec(
        rule_id="degraded_browser_acceptance",
        severity="advisory",
        title_zh="将真实窗口验收降级为无头模拟或纯静态截图",
        fix_hint_zh="前端/Electron 真机验收必须保留真实窗口操作与 .mp4 过程录屏，不用 jsdom 或静态截图顶替。",
        instructions_en=(
            "Inspect ONLY newly added `+` lines in frontend acceptance or workflow files of this git diff. "
            "Does the diff replace required real-browser/Electron video acceptance with jsdom, curl/static HTML scraping, or screenshot-only checks?"
        ),
        true_en="Substitutes real browser/Electron video acceptance with jsdom, headless scraping, or static screenshots.",
        false_en="Normal unit tests, backend scripts, or preserves real-browser/Electron `.mp4` acceptance.",
    ),
)


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
    threshold: float = DEFAULT_THRESHOLD,
    jev: Optional[JC.JevClient] = None,
) -> dict[str, Any]:
    cleaned, files_count = filter_diff(diff_text)
    if not cleaned.strip() or files_count == 0:
        return {
            "status": "clean",
            "files_checked": files_count,
            "threshold": threshold,
            "critical": [],
            "advisory": [],
        }

    client = jev if jev is not None else JC.default_client()
    questions = {
        rule.rule_id: JC.Noul(
            instructions=rule.instructions_en,
            criteria={"true": rule.true_en, "false": rule.false_en},
        )
        for rule in RULES
    }
    try:
        res = client.decide(state={"git_diff": cleaned}, questions=questions, timeout_s=15.0)
    except JC.JevUnavailableError as exc:
        return {
            "status": "skipped",
            "reason": str(exc),
            "files_checked": files_count,
            "threshold": threshold,
            "critical": [],
            "advisory": [],
        }

    critical_hits: list[dict[str, Any]] = []
    advisory_hits: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    for rule in RULES:
        prob = round(res.noul(rule.rule_id), 4)
        scores[rule.rule_id] = prob
        if prob < threshold:
            continue
        item = {
            "rule_id": rule.rule_id,
            "severity": rule.severity,
            "probability": prob,
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

    return {
        "status": status,
        "model": res.model,
        "files_checked": files_count,
        "input_tokens": res.input_tokens,
        "threshold": threshold,
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
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Probability cutoff (default: 0.80)")
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
