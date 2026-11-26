"""跨项目 GitLab 链接解析、钥匙串读密、活跃项目判别与本机仓库自动定位/按需克隆。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

ISSUE_PATH_RE = re.compile(r"^/(.+)/-/(?:issues|work_items)/([1-9]\d*)/?$")
PROJECT_CACHE_TTL_S = 600.0


def parse_remote(url: str) -> tuple[str, str]:
    """解析 git remote 地址（SSH 或 HTTPS），返回 (host, group/repo)。"""
    text = url.strip()
    m = re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", text) or re.match(
        r"^https?://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", text
    )
    if not m:
        raise ValueError(f"unrecognized git remote url: {url}")
    return m.group(1), m.group(2)


def canon_issue_url(url: str) -> str:
    """统一 GitLab 的 work_items 与 issues 链接形态，去掉查询参数、锚点与末尾斜杠，防止同一单算两份。"""
    cleaned = url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return cleaned.replace("/-/work_items/", "/-/issues/")


def parse_issue_url(host: str, url: str) -> tuple[str, int]:
    """解析指定 GitLab host 下任意项目的完整 issue URL（不含查询参数或锚点），返回 (project, iid)。"""
    parsed = urlsplit(url.strip())
    match = ISSUE_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme not in ("https", "http")
        or parsed.netloc != host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not match
    ):
        raise ValueError(f"invalid GitLab issue URL for host {host}: {url}")
    return match.group(1), int(match.group(2))


def extract_project_from_web_url(url: str) -> str:
    """从 GitLab 返回的 issue/MR/pipeline web_url 提取 path_with_namespace；失败返回空字符串。"""
    parsed = urlsplit((url or "").strip())
    if "/-/" not in parsed.path:
        return ""
    return parsed.path.split("/-/", 1)[0].strip("/")


def format_issue_ref(project: str, iid: int, default_project: str = "") -> str:
    """默认项目简写 #iid，跨项目写清 group/repo#iid，避免同号工单混淆。"""
    if not project or (default_project and project == default_project):
        return f"#{iid}"
    return f"{project}#{iid}"


def read_keychain_token(spec: Optional[Mapping[str, str]]) -> str:
    """优先读环境变量 HWF_GITLAB_USER_TOKEN，否则从 macOS 钥匙串读取用户个人令牌。"""
    if os.environ.get("HWF_GITLAB_USER_TOKEN"):
        return os.environ["HWF_GITLAB_USER_TOKEN"].strip()
    if not spec or not spec.get("service") or not spec.get("account"):
        return ""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", str(spec["service"]), "-a", str(spec["account"]), "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def default_user_token_spec(hwf_home: Path) -> Optional[dict[str, str]]:
    """读取本机用户 GitLab 令牌钥匙串配置：优先收件箱本机配置，其次复用飞书桥本机配置。"""
    for filename, key in (
        ("gitlab-inbox.local.json", "user_token_keychain"),
        ("gitlab-inbox.local.json", "gitlab_token_keychain"),
        ("feishu-bridge.local.json", "gitlab_token_keychain"),
    ):
        path = hwf_home / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        spec = data.get(key) if isinstance(data, dict) else None
        if isinstance(spec, dict) and spec.get("service") and spec.get("account"):
            return {"service": str(spec["service"]), "account": str(spec["account"])}
    return None


class ProjectActivityCache:
    """缓存 GitLab 项目是否可访问且未归档，避免每轮重复请求同一项目元数据。"""

    def __init__(self, ttl_s: float = PROJECT_CACHE_TTL_S, now: Callable[[], float] = time.time) -> None:
        self.ttl_s = ttl_s
        self.now = now
        self._cache: dict[str, tuple[bool, float]] = {}

    def seed(self, project: str, active: bool) -> None:
        self._cache[project] = (active, self.now())

    def mark(self, project: str, active: bool) -> None:
        if project:
            self._cache[project] = (bool(active), self.now())

    def is_active(self, project: str, fetch_project: Callable[[str], Any]) -> bool:
        if not project:
            return False
        now = self.now()
        cached = self._cache.get(project)
        if cached is not None and now - cached[1] < self.ttl_s:
            return cached[0]
        try:
            info = fetch_project(f"projects/{quote(project, safe='')}")
            active = isinstance(info, Mapping) and not bool(info.get("archived"))
        except Exception:
            active = False
        self._cache[project] = (active, now)
        return active


class RepoResolver:
    """维护 GitLab 项目到本机主仓库目录的映射：显式配置优先，其次扫描 ~/code，启动调度或读代码时按需自动 clone。"""

    def __init__(
        self,
        host: str,
        explicit_paths: Optional[Mapping[str, str]] = None,
        *,
        code_root: Optional[Path] = None,
        hwf_home: Optional[Path] = None,
        local_config_path: Optional[Path] = None,
        run: Runner = subprocess.run,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.host = host
        self.paths: dict[str, str] = {str(k): str(v) for k, v in (explicit_paths or {}).items() if k and v}
        self.code_root = code_root
        self.hwf_home = hwf_home
        self.local_config_path = local_config_path
        self.run = run
        self.log_fn = log_fn or (lambda _: None)
        self._scanned = False

    def known_repos(self) -> dict[str, str]:
        return dict(self.paths)

    def find_existing(self, project: str) -> str:
        """只查找已存在的本机仓库路径，不执行 git clone（供仅登记不启动调度者时调用）。"""
        existing = self.paths.get(project, "")
        if existing:
            return existing
        if not self._scanned:
            self._scan_local_clones()
        return self.paths.get(project, "")

    def ensure(self, project: str, *, clone_timeout_s: int = 180) -> str:
        """返回本机仓库路径；若本机尚未克隆则自动 git clone 到 ~/code/<仓库名> 并写回本机配置。"""
        found = self.find_existing(project)
        if found:
            return found
        if self.code_root is None:
            raise RuntimeError(f"no local repo configured for {project} and auto-clone root is disabled")
        self.code_root.mkdir(parents=True, exist_ok=True)
        slug = project.rsplit("/", 1)[-1]
        dest = self.code_root / slug
        if dest.exists():
            # 若同名目录已被其他仓库或文件占用，改用带命名空间前缀的目录名，绝不覆盖用户现有目录。
            matched = self._remote_project_of(dest)
            if matched == project:
                self._record(project, str(dest))
                return str(dest)
            safe_ns = project.replace("/", "-")
            dest = self.code_root / safe_ns
            if dest.exists():
                if self._remote_project_of(dest) == project:
                    self._record(project, str(dest))
                    return str(dest)
                raise RuntimeError(f"target clone directory already exists for {project}: {dest}")
        remote_url = f"git@{self.host}:{project}.git"
        res = self.run(
            ["git", "clone", remote_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=clone_timeout_s,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:300]
            raise RuntimeError(f"git clone {remote_url} failed: {err}")
        self.log_fn(f"cloned {project} to {dest}")
        self._record(project, str(dest))
        return str(dest)

    def _scan_local_clones(self) -> None:
        self._scanned = True
        candidates_by_project: dict[str, list[tuple[tuple[int, int, int, str], str]]] = {}
        roots: list[Path] = []
        if self.hwf_home and self.hwf_home.is_dir():
            roots.append(self.hwf_home)
        if self.code_root and self.code_root.is_dir():
            try:
                roots.extend(sorted(p for p in self.code_root.iterdir() if p.is_dir() and not p.name.startswith(".")))
            except OSError:
                pass
        for candidate in roots:
            git_marker = candidate / ".git"
            if not git_marker.exists():
                continue
            project = self._remote_project_of(candidate)
            if not project or project in self.paths:
                continue
            slug = project.rsplit("/", 1)[-1]
            # 优先真正的主克隆（.git 是目录而非 worktree 文件）与仓库原名目录，避免误选临时 worktree 目录。
            rank = (
                0 if git_marker.is_dir() else 1,
                0 if candidate.name == slug else 1,
                len(candidate.name),
                str(candidate),
            )
            candidates_by_project.setdefault(project, []).append((rank, str(candidate)))
        for project, items in candidates_by_project.items():
            items.sort(key=lambda pair: pair[0])
            self.paths[project] = items[0][1]

    def _remote_project_of(self, repo_dir: Path) -> str:
        try:
            res = self.run(
                ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if res.returncode != 0 or not (res.stdout or "").strip():
            return ""
        try:
            host, project = parse_remote(res.stdout.strip())
        except ValueError:
            return ""
        return project if host == self.host else ""

    def _record(self, project: str, local_path: str) -> None:
        self.paths[project] = local_path
        if self.local_config_path is None:
            return
        try:
            data = (
                json.loads(self.local_config_path.read_text(encoding="utf-8"))
                if self.local_config_path.exists()
                else {}
            )
            if not isinstance(data, dict):
                data = {}
            projects = dict(data.get("projects") or {})
            projects[project] = local_path
            data["projects"] = projects
            self.local_config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.local_config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.local_config_path)
        except OSError as exc:
            self.log_fn(f"persist local repo mapping for {project} failed: {exc}")
