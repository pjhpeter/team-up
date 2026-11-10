#!/usr/bin/env python3
"""给浏览器测试准备一个「全新、已登录」的浏览器目录，输出 omp 浏览器工具直接能用的打开参数。

    python3 ~/.hwf/scripts/test-browser.py <worktree> --url <被测地址> --account <测试账号> --out <本轮证据目录>

为什么有它：2026-11-09 #453 浏览器测试跑了 12 轮，8 轮卡在「浏览器目录不干净 / 登录点不上」这类准备工作上，
测试成员每轮新会话从头摸索，还用 `persist: true` 打开了共用的旧目录，再以「目录不干净」为由自停。
准备环境不再交给模型临场发挥：本脚本每次删掉重建 `<out>/browser-profile`，调用项目登录脚本登录，
成功后输出一行 JSON，其中 `open` 原样传给 omp 的 `browser.open(...)` 即是同一目录、已登录的浏览器。

项目在 docs/agents/herdr-project.yaml 里配置登录脚本（在 worktree 里运行，占位符 {url} {account} {profile}）：

    test_browser:
      login: ["node", "scripts/test/browser-login.mjs", "--url", "{url}", "--account", "{account}",
              "--profile", "{profile}", "--headless"]

测试工具以测试分支（`branches.test`，如 develop）为准：主仓库的 herdr-project.yaml 还没有 test_browser 时，
读 origin/<测试分支> 上的同一文件；被测分支上没有登录脚本时，从测试分支取出放到 worktree 的
node_modules/.cache/hwf/ 下运行，依赖仍从 worktree 的 node_modules 解析。
项目登录脚本约定：成功时最后一行输出含 `"ok": true` 的 JSON，失败时 `"ok": false` 并带 `stage`/`message`。

退出码：0 成功；1 失败（stdout 一行 JSON 说明卡在哪一步）。失败才算环境阻塞，测试成员按协议 §8 处理。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence

import yaml

PROJECT_FILE = "docs/agents/herdr-project.yaml"
PROFILE_DIR = "browser-profile"
LOGIN_TIMEOUT_S = 180
CHROME_ARGS = ["--no-first-run", "--no-default-browser-check"]


class ReadyError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=30,
                          check=False)


def main_repo(worktree: Path) -> Path:
    result = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        raise ReadyError("worktree", f"{worktree} 不是 git 仓库")
    return Path(result.stdout.strip()).parent


def valid_login(data: dict[str, Any]) -> bool:
    login = (data.get("test_browser") or {}).get("login")
    return isinstance(login, list) and len(login) >= 2 and all(isinstance(a, str) for a in login)


def from_test_branch(worktree: Path, branch: str, rel: str) -> Optional[str]:
    git(worktree, "fetch", "--quiet", "origin", branch)
    shown = git(worktree, "show", f"origin/{branch}:{rel}")
    return shown.stdout if shown.returncode == 0 else None


def load_project(repo: Path, worktree: Path) -> dict[str, Any]:
    path = repo / PROJECT_FILE
    if not path.exists():
        raise ReadyError("config", f"找不到 {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if valid_login(data):
        return data
    branch = (data.get("branches") or {}).get("test")
    text = from_test_branch(worktree, branch, PROJECT_FILE) if branch else None
    remote = (yaml.safe_load(text) or {}) if text else {}
    if not valid_login(remote):
        raise ReadyError("config", f"{PROJECT_FILE} 没有配置 test_browser.login（本地和 origin/{branch} 都没有）")
    return {**data, "test_browser": remote["test_browser"]}


def resolve_script(worktree: Path, rel: str, test_branch: Optional[str]) -> str:
    """分支上有就用分支上的；没有就从测试分支取一份放进 node_modules/.cache/hwf/。"""
    if (worktree / rel).exists():
        return rel
    text = from_test_branch(worktree, test_branch, rel) if test_branch else None
    if text is None:
        raise ReadyError("login-script", f"分支上没有 {rel}，测试分支 {test_branch or '（未配置）'} 上也没有")
    target = worktree / "node_modules/.cache/hwf" / Path(rel).name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)


def fill(argv: Sequence[str], values: dict[str, str]) -> list[str]:
    out = []
    for arg in argv:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def fresh_profile(out: Path) -> Path:
    profile = (out / PROFILE_DIR).resolve()
    if profile.exists():
        shutil.rmtree(profile)  # 只删本脚本自己建的固定子目录
    profile.mkdir(parents=True)
    return profile


def find_chrome(env: dict[str, str], home: Path) -> str:
    if env.get("CHROME_PATH"):
        return env["CHROME_PATH"]
    root = home / ".omp/puppeteer/chrome"
    if root.is_dir():
        for version in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            candidate = (version / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS"
                         / "Google Chrome for Testing")
            if candidate.exists():
                return str(candidate)
    system = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if system.exists():
        return str(system)
    raise ReadyError("browser", "找不到 Chrome，用环境变量 CHROME_PATH 指定")


def last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def ready(worktree: Path, url: str, account: str, out: Path) -> dict[str, Any]:
    worktree = worktree.resolve()
    project = load_project(main_repo(worktree), worktree)
    login = list(project["test_browser"]["login"])
    login[1] = resolve_script(worktree, login[1], (project.get("branches") or {}).get("test"))
    out.mkdir(parents=True, exist_ok=True)
    profile = fresh_profile(out)
    argv = fill(login, {"url": url, "account": account, "profile": str(profile)})
    try:
        done = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, timeout=LOGIN_TIMEOUT_S,
                              check=False)
    except subprocess.TimeoutExpired as error:
        raise ReadyError("login", f"登录脚本 {LOGIN_TIMEOUT_S}s 没有结束") from error
    result = last_json(done.stdout)
    if done.returncode != 0 or not result.get("ok"):
        stage = str(result.get("stage") or "login")
        message = str(result.get("message") or (done.stderr.strip().splitlines() or ["登录脚本没有输出结果"])[-1])
        raise ReadyError(f"login:{stage}", message)
    chrome = find_chrome(dict(os.environ), Path.home())
    return {
        "ok": True, "account": account, "url": url, "profile": str(profile),
        "login_screenshot": result.get("screenshot"),
        "open": {"name": "test", "url": url, "headed": True,
                 "app": {"path": chrome, "args": [f"--user-data-dir={profile}", *CHROME_ARGS]}},
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="准备全新、已登录的测试浏览器目录")
    parser.add_argument("worktree", type=Path)
    parser.add_argument("--url", required=True, help="被测地址，如 http://localhost:3469")
    parser.add_argument("--account", required=True, help="测试账号")
    parser.add_argument("--out", required=True, type=Path, help="本轮证据目录；浏览器目录建在它下面")
    opts = parser.parse_args(argv)
    try:
        print(json.dumps(ready(opts.worktree, opts.url, opts.account, opts.out), ensure_ascii=False))
        return 0
    except ReadyError as error:
        print(json.dumps({"ok": False, "stage": error.stage, "message": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
