"""聚焦验证 init-project：空项目一次补齐、重复运行不重复建、只看模式不改东西、已有文件不覆盖。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

import yaml

HWF = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("init_project", HWF / "scripts/init-project.py")
assert SPEC is not None and SPEC.loader is not None
INIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INIT
SPEC.loader.exec_module(INIT)


class FakeGitLab:
    """只实现 init-project 用到的几个接口，状态放内存里。"""

    def __init__(self, has_develop: bool = True):
        self.labels: list[dict] = [{"id": 1, "name": "bug"}]
        self.issues: list[dict] = []
        self.boards: list[dict] = []
        self.has_develop = has_develop
        self.writes = 0
        self.next_id = 100

    def nid(self) -> int:
        self.next_id += 1
        return self.next_id

    def __call__(self, cmd, cwd=None, **_):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return self.ok("git@git.example.com:group/demo.git\n")
        method, path = cmd[cmd.index("-X") + 1], unquote(cmd[cmd.index("-X") + 2])
        fields = dict(cmd[i + 1].split("=", 1) for i, c in enumerate(cmd) if c == "-f")
        base = path.split("?")[0]
        if method != "GET":
            self.writes += 1
        if base == "user":
            return self.ok({"username": "agent"})
        if base == "projects/group/demo":
            return self.ok({"default_branch": "main"})
        if base.startswith("projects/group/demo/repository/branches/"):
            return self.ok({"name": "develop"}) if self.has_develop else self.fail("404 Not Found")
        if base == "projects/group/demo/labels":
            if method == "POST":
                self.labels.append({"id": self.nid(), **fields})
                return self.ok(self.labels[-1])
            return self.ok(self.labels)
        if base == "projects/group/demo/issues":
            if method == "POST":
                self.issues.append({"iid": len(self.issues) + 1, **fields})
                return self.ok(self.issues[-1])
            return self.ok(self.issues)
        if base == "projects/group/demo/boards":
            if method == "POST":
                self.boards.append({"id": self.nid(), "name": fields["name"], "lists": []})
                return self.ok(self.boards[-1])
            return self.ok(self.boards)
        if base.endswith("/lists") and method == "POST":
            bid = int(base.split("/")[-2])
            label = next(l for l in self.labels if l["id"] == int(fields["label_id"]))
            next(b for b in self.boards if b["id"] == bid)["lists"].append({"label": label})
            return self.ok({})
        raise AssertionError(f"unexpected {method} {path}")

    @staticmethod
    def ok(data):
        out = data if isinstance(data, str) else json.dumps(data)
        return subprocess.CompletedProcess([], 0, out, "")

    @staticmethod
    def fail(err):
        return subprocess.CompletedProcess([], 1, "", err)


class InitProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hwf-init-"))
        self.repo = self.tmp / "demo"
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text("# 原有规则\n\n别删我\n")
        self.inbox = self.tmp / "gitlab-inbox.config.json"
        self.inbox.write_text(json.dumps({"host": "git.example.com", "projects": ["a/b"]}))
        (self.tmp / "gitlab-inbox.local.json").write_text(json.dumps({"projects": {"a/b": "/x"}}))
        self.gl = FakeGitLab()

    def run_init(self, apply: bool, smoke=(("国内", "https://dev.example.com"),)) -> "INIT.Init":
        init = INIT.Init(self.repo, apply, list(smoke), None, run=self.gl,
                         inbox_config=self.inbox, templates=HWF / "templates/project")
        init.main()
        return init

    def test_dry_run_changes_nothing(self) -> None:
        init = self.run_init(apply=False)
        self.assertEqual(self.gl.writes, 0)
        self.assertGreater(init.todo, 0)
        self.assertFalse((self.repo / "docs").exists())
        self.assertNotIn("group/demo", self.inbox.read_text())
        self.assertNotIn("group/demo", (self.tmp / "gitlab-inbox.local.json").read_text())

    def test_apply_then_rerun_is_noop(self) -> None:
        self.run_init(apply=True)
        names = {l["name"] for l in self.gl.labels}
        self.assertTrue({n for n, _, _ in INIT.LABELS} <= names)
        self.assertEqual([i["title"] for i in self.gl.issues], [INIT.REGISTRY_TITLE])
        self.assertEqual([l["label"]["name"] for l in self.gl.boards[0]["lists"]], INIT.STATUS_LABELS)
        self.assertEqual(json.loads(self.inbox.read_text())["projects"], ["a/b", "group/demo"])
        local = json.loads((self.tmp / "gitlab-inbox.local.json").read_text())
        self.assertEqual(local["projects"], {"a/b": "/x", "group/demo": str(self.repo)})

        cfg = yaml.safe_load((self.repo / "docs/agents/herdr-project.yaml").read_text())
        self.assertEqual(cfg["gitlab"], {"host": "git.example.com", "project": "group/demo",
                                         "registry_issue": 1, "board_id": self.gl.boards[0]["id"]})
        self.assertEqual(cfg["branches"], {"test": "develop", "main": "main"})
        self.assertEqual(cfg["smoke"], [{"name": "国内", "url": "https://dev.example.com"}])
        agents = (self.repo / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith("# 原有规则\n\n别删我"))
        self.assertIn("Herdr Issue 交付流程", agents)
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertIn(".hwf/tmp/", (self.repo / ".gitignore").read_text())
        self.assertNotIn("{{", (self.repo / "docs/agents/issue-tracker.md").read_text())

        writes = self.gl.writes
        again = self.run_init(apply=True)
        self.assertEqual(again.todo, 0)
        self.assertEqual(self.gl.writes, writes)

    def test_no_test_branch_and_no_smoke(self) -> None:
        self.gl.has_develop = False
        init = self.run_init(apply=True, smoke=())
        cfg = yaml.safe_load((self.repo / "docs/agents/herdr-project.yaml").read_text())
        self.assertIsNone(cfg["branches"]["test"])
        self.assertEqual(cfg["smoke"], [])
        self.assertTrue(any("冒烟" in l for l in init.lines if l.startswith("[注意]")))

    def test_existing_file_not_overwritten(self) -> None:
        (self.repo / "docs/agents").mkdir(parents=True)
        (self.repo / "docs/agents/triage-labels.md").write_text("自己写的\n")
        self.run_init(apply=True)
        self.assertEqual((self.repo / "docs/agents/triage-labels.md").read_text(), "自己写的\n")

    def test_parse_remote(self) -> None:
        for url in ("git@h.com:a/b/c.git", "https://h.com/a/b/c.git", "ssh://git@h.com:2222/a/b/c.git"):
            self.assertEqual(INIT.parse_remote(url), ("h.com", "a/b/c"))


if __name__ == "__main__":
    unittest.main()
