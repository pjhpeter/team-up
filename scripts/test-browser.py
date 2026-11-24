#!/usr/bin/env python3
"""给浏览器测试准备已登录会话（支持独立 Chrome profile 与 Ego Lite 任务空间），全程脱敏不暴露明文账号密码。

    python3 ~/.hwf/scripts/test-browser.py <worktree> --url <被测地址> [--account <测试账号>] --out <本轮证据目录>
    python3 ~/.hwf/scripts/test-browser.py <worktree> --url <被测地址> --mode ego --space-name <空间名> --out <本轮证据目录>

设计边界：
1. 兼容项目 `docs/agents/herdr-project.yaml` 的 `test_browser.login`（Chrome 独立目录模式）与 `ego-browser` 任务空间（`--mode ego` / `test_browser.mode: ego`）；
2. 支持从环境变量 `HWF_TEST_ACCOUNT` / `HWF_TEST_PASSWORD`、本机凭据文件（`~/.agents/secrets/look-ai-test-accounts.md`）或 macOS 钥匙串（`hwf-test-account`）读取凭据，通过环境变量与 `0600` 临时文件传给子进程，标准输出与报错自动脱敏；
3. Ego Lite 模式切换账号（`--clear-origin`）只清理当前非生产测试站点的 `origin` 存储与同主机名 Cookie，严禁清理全局浏览器 Cookie 或生产域名。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

import yaml

PROJECT_FILE = "docs/agents/herdr-project.yaml"
PROFILE_DIR = "browser-profile"
LOGIN_TIMEOUT_S = 180
CHROME_ARGS = ["--no-first-run", "--no-default-browser-check"]
DEFAULT_ACCOUNTS_FILE = Path.home() / ".agents/secrets/look-ai-test-accounts.md"
KEYCHAIN_SERVICE = "hwf-test-account"
PROTECTED_PROD_HOSTS = frozenset({
    "look-ai.cn", "www.look-ai.cn", "lookfashion.ai", "www.lookfashion.ai",
    "lookai.cc", "www.lookai.cc", "app.lookai.cc",
    "git.bootingai.net", "admin.lookai.cc",
})
NON_PROD_HOST_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "develop", "test", "staging", "dev", "preview")

EGO_LOGIN_JS = r"""
import fs from "node:fs";

const credPath = __CRED_PATH__;
let cfg;
try {
  cfg = JSON.parse(fs.readFileSync(credPath, "utf8"));
} finally {
  try { fs.unlinkSync(credPath); } catch {}
}

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

try {
  let task = null;
  if (cfg.space_id) {
    const spaces = await listTaskSpaces();
    const found = Array.isArray(spaces) ? spaces.find((s) => s.id === cfg.space_id || s.spaceId === cfg.space_id) : null;
    if (found && typeof claimTaskSpace === "function" && found.claimable) {
      task = await claimTaskSpace(cfg.space_id);
    } else if (found && typeof takeOverTaskSpace === "function" && found.owned === false) {
      task = await takeOverTaskSpace(cfg.space_id);
    } else {
      task = await taskSpace(cfg.space_id);
    }
  } else {
    task = await taskSpace(cfg.space_name || "hwf-test");
  }

  let pages = await task.pages();
  let page = pages[0] || null;
  if (!page && typeof task.tabs === "function") {
    const tabs = await task.tabs();
    if (Array.isArray(tabs) && tabs.length > 0 && tabs[0]?.page) {
      page = await task.adopt(tabs[0].page);
    }
  }
  if (!page) {
    page = await task.newPage(cfg.url);
  } else {
    await page.goto(cfg.url);
  }

  if (cfg.clear_origin) {
    const targetUrl = new URL(cfg.url);
    const targetOrigin = targetUrl.origin;
    const targetHost = targetUrl.hostname.toLowerCase();
    await page.evaluate(() => {
      try { localStorage.clear(); } catch {}
      try { sessionStorage.clear(); } catch {}
    });
    await page.cdp("Storage.clearDataForOrigin", {
      origin: targetOrigin,
      storageTypes: "cookies,local_storage,session_storage,indexeddb",
    });
    const allCookies = await page.cdp("Network.getAllCookies");
    for (const c of allCookies?.cookies || []) {
      const domain = String(c.domain || "").replace(/^\./, "").toLowerCase();
      if (domain === targetHost) {
        await page.cdp("Network.deleteCookies", {
          name: c.name,
          domain: c.domain,
          path: c.path || "/",
        });
      }
    }
    await page.goto(cfg.url);
  }

  const selectors = cfg.selectors || {};
  const accountSel = selectors.account || "input[type='email'], input[type='tel'], input[name*='account' i], input[name*='email' i], input[name*='phone' i], input[name*='user' i], input[placeholder*='邮箱'], input[placeholder*='手机'], input[placeholder*='账号'], input[type='text']";
  const passwordSel = selectors.password || "input[type='password']";
  const submitSel = selectors.submit || "button[type='submit'], button:has-text('登录'), button:has-text('Sign in'), button:has-text('Log in')";

  const hasPasswordInput = await page.evaluate((sel) => Boolean(document.querySelector(sel)), passwordSel);
  if (hasPasswordInput) {
    if (!cfg.password) {
      emit({ ok: false, stage: "credentials", message: "检测到登录表单但未配置本机密码（可设 HWF_TEST_PASSWORD、凭据文件或钥匙串）" });
      process.exit(1);
    }
    await page.fill(accountSel, cfg.account);
    await page.fill(passwordSel, cfg.password);
    await page.click(submitSel);
    await new Promise((r) => setTimeout(r, 1500));
  }

  if (cfg.screenshot_path) {
    await page.screenshot({ path: cfg.screenshot_path });
  }

  emit({
    ok: true,
    mode: "ego",
    space_id: task.id || task.spaceId || cfg.space_id || "",
    page_label: page.label || "p1",
    screenshot: cfg.screenshot_path || null,
  });
} catch (err) {
  emit({
    ok: false,
    stage: "ego-login",
    message: String(err?.message || err || "ego-browser 登录执行失败"),
  });
  process.exit(1);
}
"""


class ReadyError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def mask_account(account: str) -> str:
    """对手机号、邮箱或测试账号做不可逆掩码，避免明文出现在终端输出或日志里。"""
    clean = account.strip()
    if not clean:
        return "***"
    if "@" in clean:
        local, _, domain = clean.partition("@")
        head = local[:2] if len(local) >= 2 else local[:1]
        return f"{head}***@{domain}"
    if len(clean) <= 4:
        return f"{clean[:1]}***"
    return f"{clean[:2]}***{clean[-2:]}"


def scrub_secrets(text: str, account: str = "", password: str = "") -> str:
    cleaned = text
    if password:
        cleaned = cleaned.replace(password, "***")
    if account and len(account.strip()) >= 3:
        cleaned = cleaned.replace(account.strip(), mask_account(account))
    return cleaned


def validate_clear_origin_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        raise ReadyError("clear-origin", f"无法解析被测地址主机名：{url}")
    if host in PROTECTED_PROD_HOSTS:
        raise ReadyError("clear-origin", f"拒绝清理受保护或生产域名登录态：{host}")
    if (host.endswith(".look-ai.cn") or host.endswith(".lookfashion.ai")) and not any(
        hint in host for hint in NON_PROD_HOST_HINTS
    ):
        raise ReadyError("clear-origin", f"拒绝清理疑似生产子域名登录态：{host}")


def lookup_password_in_markdown(path: Path, account: str) -> Optional[str]:
    """从本机测试账号 Markdown 表或键值行按账号查找密码，不回显任何原文。"""
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    target = account.strip().strip("`'\"")
    acc_col: Optional[int] = None
    pwd_col: Optional[int] = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip().strip("`'\"") for c in stripped.strip("|").split("|")]
        if all(not c or set(c) <= {"-", ":", " "} for c in cells):
            continue
        lowered = [c.lower() for c in cells]
        if acc_col is None and not any("@" in h for h in lowered) and any(
            h in ("账号", "测试账号", "邮箱", "手机号", "手机", "用户名", "account", "email", "phone", "username", "user")
            for h in lowered
        ):
            acc_col = next(
                (
                    i for i, h in enumerate(lowered)
                    if h in ("账号", "测试账号", "邮箱", "手机号", "手机", "用户名", "account", "email", "phone", "username", "user")
                ),
                0,
            )
            pwd_col = next(
                (i for i, h in enumerate(lowered) if any(k in h for k in ("密码", "password", "口令", "pwd"))),
                None,
            )
            continue
        if acc_col is None and len(cells) >= 2 and ("@" in cells[0] or cells[0] == target):
            acc_col, pwd_col = 0, 1
        if acc_col is not None and pwd_col is not None and max(acc_col, pwd_col) < len(cells):
            if cells[acc_col] == target and cells[pwd_col]:
                return cells[pwd_col]
    return None


def resolve_password(account: str, env: dict[str, str], home: Path, *, allow_keychain: bool = False) -> Optional[str]:
    if env.get("HWF_TEST_PASSWORD"):
        return env["HWF_TEST_PASSWORD"]
    pool_file = Path(env["HWF_TEST_ACCOUNTS_FILE"]) if env.get("HWF_TEST_ACCOUNTS_FILE") else (
        home / ".agents/secrets/look-ai-test-accounts.md"
    )
    from_file = lookup_password_in_markdown(pool_file, account)
    if from_file:
        return from_file
    if allow_keychain and sys.platform == "darwin":
        res = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


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


def load_project(repo: Path, worktree: Path, *, require_login: bool = True) -> dict[str, Any]:
    path = repo / PROJECT_FILE
    if not path.exists():
        if not require_login:
            return {}
        raise ReadyError("config", f"找不到 {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if valid_login(data) or not require_login:
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


def _write_temp_credentials(payload: dict[str, Any]) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="hwf-tb-cred-", suffix=".json")
    os.chmod(raw_path, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return Path(raw_path)


def _ready_ego(
    worktree: Path,
    url: str,
    account: str,
    out: Path,
    project: dict[str, Any],
    *,
    space_id: Optional[str],
    space_name: Optional[str],
    clear_origin: bool,
) -> dict[str, Any]:
    if clear_origin:
        validate_clear_origin_url(url)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    password = resolve_password(account, env, Path.home(), allow_keychain=True) or ""
    screenshot_path = str((out / "login-ready.png").resolve())
    ego_cfg = (project.get("test_browser") or {}).get("ego") or {}
    cred_path = _write_temp_credentials({
        "url": url,
        "account": account,
        "password": password,
        "space_id": space_id or "",
        "space_name": space_name or ego_cfg.get("space_name") or "hwf-test",
        "clear_origin": bool(clear_origin),
        "screenshot_path": screenshot_path,
        "selectors": ego_cfg.get("selectors") if isinstance(ego_cfg, dict) else {},
    })
    script = EGO_LOGIN_JS.replace("__CRED_PATH__", json.dumps(str(cred_path)))
    try:
        done = subprocess.run(
            ["ego-browser", "nodejs"],
            cwd=worktree,
            input=script,
            capture_output=True,
            text=True,
            timeout=LOGIN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ReadyError("login", f"ego-browser 登录 {LOGIN_TIMEOUT_S}s 没有结束") from error
    finally:
        cred_path.unlink(missing_ok=True)

    result = last_json(done.stdout)
    if done.returncode != 0 or not result.get("ok"):
        stage = str(result.get("stage") or "ego")
        raw_msg = str(result.get("message") or (done.stderr.strip().splitlines() or ["ego-browser 没有输出结果"])[-1])
        raise ReadyError(f"login:{stage}", scrub_secrets(raw_msg, account, password))

    resolved_space = str(result.get("space_id") or space_id or "")
    resolved_space_name = str(space_name or ego_cfg.get("space_name") or "hwf-test")
    return {
        "ok": True,
        "mode": "ego",
        "account": mask_account(account),
        "url": url,
        "space_id": resolved_space,
        "space_name": resolved_space_name,
        "page_label": result.get("page_label") or "p1",
        "login_screenshot": result.get("screenshot"),
        "open": {"mode": "ego", "space_id": resolved_space, "space_name": resolved_space_name, "url": url},
    }


def ready(
    worktree: Path,
    url: str,
    account: str,
    out: Path,
    *,
    mode: str = "auto",
    space_id: Optional[str] = None,
    space_name: Optional[str] = None,
    clear_origin: bool = False,
) -> dict[str, Any]:
    worktree = worktree.resolve()
    repo = main_repo(worktree)
    explicit_ego = mode == "ego" or bool(space_id or space_name)
    project = load_project(repo, worktree, require_login=not explicit_ego)
    cfg_mode = str((project.get("test_browser") or {}).get("mode") or "").lower()
    use_ego = explicit_ego or (mode == "auto" and cfg_mode == "ego")
    if use_ego:
        return _ready_ego(
            worktree, url, account, out, project,
            space_id=space_id, space_name=space_name, clear_origin=clear_origin,
        )
    if not valid_login(project):
        project = load_project(repo, worktree, require_login=True)

    login = list(project["test_browser"]["login"])
    login[1] = resolve_script(worktree, login[1], (project.get("branches") or {}).get("test"))
    out.mkdir(parents=True, exist_ok=True)
    profile = fresh_profile(out)
    env = dict(os.environ)
    password = resolve_password(account, env, Path.home(), allow_keychain=False) or ""
    cred_path = _write_temp_credentials({"account": account, "password": password, "url": url, "profile": str(profile)})
    child_env = {
        **env,
        "HWF_TEST_ACCOUNT": account,
        "HWF_TEST_CREDENTIAL_FILE": str(cred_path),
    }
    if password:
        child_env["HWF_TEST_PASSWORD"] = password
    argv = fill(login, {"url": url, "account": account, "profile": str(profile)})
    try:
        done = subprocess.run(argv, cwd=worktree, env=child_env, capture_output=True, text=True,
                              timeout=LOGIN_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as error:
        raise ReadyError("login", f"登录脚本 {LOGIN_TIMEOUT_S}s 没有结束") from error
    finally:
        cred_path.unlink(missing_ok=True)
    result = last_json(done.stdout)
    if done.returncode != 0 or not result.get("ok"):
        stage = str(result.get("stage") or "login")
        raw_msg = str(result.get("message") or (done.stderr.strip().splitlines() or ["登录脚本没有输出结果"])[-1])
        raise ReadyError(f"login:{stage}", scrub_secrets(raw_msg, account, password))
    chrome = find_chrome(env, Path.home())
    return {
        "ok": True,
        "account": mask_account(account),
        "url": url,
        "profile": str(profile),
        "login_screenshot": result.get("screenshot"),
        "open": {"name": "test", "url": url, "headed": True,
                 "app": {"path": chrome, "args": [f"--user-data-dir={profile}", *CHROME_ARGS]}},
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="准备已登录且自动脱敏的测试浏览器目录或 Ego Lite 会话")
    parser.add_argument("worktree", type=Path)
    parser.add_argument("--url", required=True, help="被测地址，如 http://localhost:3469")
    parser.add_argument("--account", help="测试账号；省略时从 --account-env 指定的环境变量读取")
    parser.add_argument("--account-env", default="HWF_TEST_ACCOUNT", help="读取测试账号的环境变量名（默认 HWF_TEST_ACCOUNT）")
    parser.add_argument("--out", required=True, type=Path, help="本轮证据目录；浏览器目录或登录截图建在它下面")
    parser.add_argument("--mode", choices=("auto", "profile", "ego"), default="auto",
                        help="登录模式：profile 为独立 Chrome 目录，ego 为 Ego Lite 任务空间")
    parser.add_argument("--space-id", help="Ego Lite 复用已有 TaskSpace ID")
    parser.add_argument("--space-name", help="Ego Lite 新建或打开指定名称的 TaskSpace")
    parser.add_argument("--clear-origin", action="store_true",
                        help="Ego Lite 登录前仅清理当前非生产测试站点 origin 的存储与同主机 Cookie")
    opts = parser.parse_args(argv)
    account = (opts.account or os.environ.get(opts.account_env) or "").strip()
    try:
        if not account:
            raise ReadyError("account", f"未提供测试账号（请传 --account 或设置环境变量 {opts.account_env}）")
        print(json.dumps(
            ready(
                opts.worktree,
                opts.url,
                account,
                opts.out,
                mode=opts.mode,
                space_id=opts.space_id,
                space_name=opts.space_name,
                clear_origin=opts.clear_origin,
            ),
            ensure_ascii=False,
        ))
        return 0
    except ReadyError as error:
        print(json.dumps({"ok": False, "stage": error.stage, "message": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
