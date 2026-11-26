"""验证跨项目 GitLab 工具模块：链接解析、活跃项目缓存、本机仓库扫描与按需自动 clone。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "gitlab_projects", Path(__file__).resolve().parents[1] / "scripts/gitlab_projects.py"
)
assert SPEC is not None and SPEC.loader is not None
GP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GP
SPEC.loader.exec_module(GP)


class GitLabProjectsTest(unittest.TestCase):
    def test_parse_remote_supports_ssh_and_https(self) -> None:
        self.assertEqual(GP.parse_remote("git@git.example.com:frontend/web-v1.git"), ("git.example.com", "frontend/web-v1"))
        self.assertEqual(
            GP.parse_remote("ssh://git@git.example.com:2222/a/b/c.git"),
            ("git.example.com", "a/b/c"),
        )
        self.assertEqual(
            GP.parse_remote("https://oauth2:tok@git.example.com/backend/api-server/"),
            ("git.example.com", "backend/api-server"),
        )

    def test_canon_and_parse_issue_url_across_subgroups(self) -> None:
        self.assertEqual(
            GP.canon_issue_url("https://git.example.com/team/sub/repo/-/work_items/42/#note_9"),
            "https://git.example.com/team/sub/repo/-/issues/42",
        )
        self.assertEqual(
            GP.parse_issue_url("git.example.com", "https://git.example.com/team/sub/repo/-/work_items/42"),
            ("team/sub/repo", 42),
        )
        with self.assertRaises(ValueError):
            GP.parse_issue_url("git.example.com", "https://other.example.com/team/repo/-/issues/42")
        with self.assertRaises(ValueError):
            GP.parse_issue_url("git.example.com", "https://git.example.com/team/repo/-/issues/42#note_1")

    def test_format_issue_ref_shortens_default_project_only(self) -> None:
        self.assertEqual(GP.format_issue_ref("frontend/web-v1", 7, "frontend/web-v1"), "#7")
        self.assertEqual(GP.format_issue_ref("backend/api-server", 7, "frontend/web-v1"), "backend/api-server#7")

    def test_project_activity_cache_excludes_archived_and_caches_result(self) -> None:
        calls: list[str] = []
        now = [100.0]

        def fetch(path: str) -> dict:
            calls.append(path)
            if "archived" in path:
                return {"archived": True}
            if "missing" in path:
                raise RuntimeError("404")
            return {"archived": False}

        cache = GP.ProjectActivityCache(ttl_s=60.0, now=lambda: now[0])
        self.assertTrue(cache.is_active("backend/api", fetch))
        self.assertTrue(cache.is_active("backend/api", fetch))
        self.assertEqual(len(calls), 1)
        self.assertFalse(cache.is_active("legacy/archived", fetch))
        self.assertFalse(cache.is_active("secret/missing", fetch))

    def test_repo_resolver_scans_existing_clones_and_auto_clones_with_collision_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hwf-gp-") as tmp:
            root = Path(tmp)
            code_root = root / "code"
            code_root.mkdir()
            local_cfg = root / "gitlab-inbox.local.json"

            existing = code_root / "web-v1"
            (existing / ".git").mkdir(parents=True)
            other_same_slug = code_root / "service"
            (other_same_slug / ".git").mkdir(parents=True)

            remotes = {
                str(existing): "git@git.example.com:frontend/web-v1.git",
                str(other_same_slug): "git@git.example.com:team-a/service.git",
            }
            clones: list[tuple[str, str]] = []

            def fake_run(argv: list[str], **kw):
                if argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "get-url", "origin"]:
                    url = remotes.get(argv[2], "")
                    return subprocess.CompletedProcess(argv, 0 if url else 1, url + "\n" if url else "", "")
                if argv[:2] == ["git", "clone"]:
                    self.assertEqual(kw.get("env", {}).get("GIT_TERMINAL_PROMPT"), "0")
                    clones.append((argv[2], argv[3]))
                    dest = Path(argv[3])
                    (dest / ".git").mkdir(parents=True, exist_ok=True)
                    remotes[str(dest)] = argv[2]
                    return subprocess.CompletedProcess(argv, 0, "", "")
                raise AssertionError(f"unexpected {argv}")

            resolver = GP.RepoResolver(
                "git.example.com",
                code_root=code_root,
                local_config_path=local_cfg,
                run=fake_run,
            )
            # 已有本地克隆直接命中，不跑 git clone
            self.assertEqual(resolver.find_existing("frontend/web-v1"), str(existing))
            self.assertEqual(clones, [])

            # 未克隆仓库：find_existing 返回空，ensure 自动 clone 到 ~/code/<仓库名> 并写入 local.json
            self.assertEqual(resolver.find_existing("backend/api-server"), "")
            cloned = resolver.ensure("backend/api-server")
            self.assertEqual(cloned, str(code_root / "api-server"))
            self.assertEqual(clones[-1], ("git@git.example.com:backend/api-server.git", str(code_root / "api-server")))

            # 同名目录已被 team-a/service 占用时，team-b/service 自动退避到 team-b-service
            collided = resolver.ensure("team-b/service")
            self.assertEqual(collided, str(code_root / "team-b-service"))
            persisted = json.loads(local_cfg.read_text(encoding="utf-8"))["projects"]
            self.assertEqual(persisted["backend/api-server"], str(code_root / "api-server"))
            self.assertEqual(persisted["team-b/service"], str(code_root / "team-b-service"))


if __name__ == "__main__":
    unittest.main()
