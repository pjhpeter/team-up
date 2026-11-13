"""聚焦验证交付收尾脚本：只关本调度者的成员、只删满足条件的 worktree、有残留时不改 INDEX。"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("wrapup", Path(__file__).resolve().parents[1] / "scripts/wrapup.py")
assert SPEC is not None and SPEC.loader is not None
WR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WR
SPEC.loader.exec_module(WR)

TASK = "proj-47"
SCHED = "delivery-scheduler-w1-ps"
ISSUE = "https://git.example.com/group/proj/-/work_items/47"
INDEX = f"""# 任务索引

| 任务标识 | 输入原文 | 调度者 | 状态 |
|---|---|---|---|
| `proj-40` | 旧任务 | `{SCHED}` / `w1:pS` | done（旧） |
| `{TASK}` | 修 bug | `{SCHED}` / `w1:pS` | in-progress（开发中） |
| `proj-48` | 别人的 | `delivery-scheduler-w1-px` / `w1:pX` | in-progress |
"""


def done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


class FakeGit:
    """按 worktree 记 HEAD/分支/是否干净/是否已进 release，模拟脚本用到的几条 git 命令。"""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.trees: dict[str, dict] = {repo: {"head": "m" * 40, "branch": "main"}}
        self.fetch_ok = True
        self.removed: list[str] = []

    def add(self, path: str, **info: object) -> str:
        self.trees[path] = {"head": "a" * 40, "branch": None, "dirty": False, "upstream": None, "ahead": 0,
                            "in": set(), "cherry_in": set()} | info
        Path(path).mkdir(parents=True, exist_ok=True)  # 脚本按目录发现 worktree
        return path

    def __call__(self, path: str, *args: str) -> subprocess.CompletedProcess[str]:
        tree = self.trees.get(path, {})
        if args[:2] == ("worktree", "list"):
            out = ""
            for p, t in self.trees.items():
                if t.get("bare"):
                    out += f"worktree {p}\nbare\n\n"
                    continue
                out += f"worktree {p}\nHEAD {t['head']}\n" + (f"branch refs/heads/{t['branch']}\n" if t["branch"]
                                                              else "detached\n") + "\n"
            return done(0, out)
        if args[0] == "fetch":
            return done(0 if self.fetch_ok else 1, err="network")
        if args[:2] == ("branch", "-r"):
            return done(0, "origin/merge/47\n" if tree.get("remote_has") else "")
        if args[0] == "rev-parse":
            return done(0, tree["upstream"] + "\n") if tree.get("upstream") else done(128, err="no upstream")
        if args[0] == "rev-list":
            return done(0, f"{tree['ahead']}\n")
        if args[0] == "status":
            return done(0, " M file.ts\n" if tree.get("dirty") else "")
        if args[0] == "merge-base":
            return done(0 if args[3] in tree["in"] else 1)
        if args[0] == "cherry":
            return done(0, ("- " if args[1] in tree["cherry_in"] else "+ ") + "abc123\n")
        if args[:2] == ("worktree", "remove"):
            self.trees.pop(args[3])
            shutil.rmtree(args[3])
            self.removed.append(args[3])
            return done()
        if args[:2] == ("worktree", "prune"):
            return done()
        raise AssertionError(f"unexpected git {path} {args}")


def agent(name: str | None, pane: str, tab: str = "w1:t1", cwd: str = "/repo") -> dict:
    return {"name": name, "pane_id": pane, "tab_id": tab, "workspace_id": pane.split(":")[0], "cwd": cwd,
            "foreground_cwd": cwd, "agent_status": "idle"}


class FakeHerdr:
    def __init__(self, agents: list[dict], extra_panes: list[dict] | None = None, stuck: set[str] | None = None) -> None:
        self.agents = agents
        self.panes = [dict(a) for a in agents] + (extra_panes or [])
        self.stuck = stuck or set()
        self.closed: list[str] = []

    def __call__(self, *args: str, timeout: int = 30) -> dict:
        if args[:2] == ("agent", "list"):
            return {"result": {"agents": [a for a in self.agents if a["pane_id"] in {p["pane_id"] for p in self.panes}]}}
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": list(self.panes)}}
        if args[:2] == ("pane", "close"):
            self.closed.append(args[2])
            if args[2] not in self.stuck:
                self.panes = [p for p in self.panes if p["pane_id"] != args[2]]
            return {}
        raise AssertionError(f"unexpected herdr {args}")


class FakeGlab:
    def __init__(self) -> None:
        self.notes: dict[int, str] = {}
        self.authored = {50: {"author": {"username": "pengjh"}}, 51: {"author": {"username": "pjhagent"}},
                         52: {"author": {"username": "pengjh"}, "system": True}}
        self.issue_state = "opened"
        self.close_sticks = True

    def __call__(self, host: str, method: str, path: str, *fields: str) -> dict:
        if path == "user":
            return {"username": "pjhagent"}
        if method == "PUT":
            assert fields == ("state_event=close",), fields
            self.issue_state = "closed" if self.close_sticks else self.issue_state
            return {}
        if path.endswith("/issues/47"):
            return {"state": self.issue_state}
        if method == "POST":
            note_id = 900 + len(self.notes)
            self.notes[note_id] = fields[0].split("=", 1)[1]
            return {"id": note_id}
        note_id = int(path.rsplit("/", 1)[1])
        if note_id in self.authored:
            return self.authored[note_id]
        return {"id": note_id, "body": self.notes[note_id]}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.repo = str(base / "proj")
        (base / "proj/.hwf/tmp").mkdir(parents=True)
        (base / "proj/.hwf/tmp/INDEX.md").write_text(INDEX, encoding="utf-8")
        self.state = base / "state"
        self.state.mkdir()
        self.wt = str(base / "worktrees/proj")
        for key, value in {"HWF_WORKTREE_ROOT": str(base / "worktrees"), "HWF_STATE_DIR": str(self.state),
                           "HERDR_PANE_ID": "w1:pS"}.items():
            patcher = patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.git = FakeGit(self.repo)
        self.glab = FakeGlab()
        self.services = done(0, "stop: pid=101 ports=[3167]\nport 3167 free\n")
        self.scripts: list[tuple[str, ...]] = []
        for target, value in (("git", self.git), ("glab_api", self.glab), ("run_script", self.fake_script),
                              ("CLOSE_WAIT_S", 0)):
            patcher = patch.object(WR, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_script(self, filename: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.scripts.append((filename, *args))
        if filename == "stop-test-services.py":
            return self.services
        return done(0, json.dumps({"released": ["13800000000"]}))

    def registry(self, entries: dict[str, str]) -> None:
        data = {pane: {"scheduler": owner, "profile": "dev", "tab_id": "w1:t1", "at": "x"}
                for pane, owner in entries.items()}
        (self.state / "members.json").write_text(json.dumps(data), encoding="utf-8")

    def run_wrapup(self, herdr: FakeHerdr, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with patch.object(WR, "herdr", herdr), redirect_stdout(out):
            code = WR.main(["--repo", self.repo, "--task", TASK, "--issue", ISSUE, "--release", "release-40",
                            "--status", "done（已上线）", "--port", "3167", *extra])
        return code, out.getvalue()

    def index_status(self) -> str:
        line = next(l for l in Path(self.repo, ".hwf/tmp/INDEX.md").read_text().splitlines() if f"`{TASK}`" in l)
        return line.split("|")[4].strip()

    def team(self, user_cwd: str = "/repo") -> list[dict]:
        dev = f"{self.wt}/{TASK}-dev"
        return [agent(SCHED, "w1:pS"), agent("i47-dev", "w1:pA", cwd=dev), agent("i47-test", "w1:pB", cwd=dev),
                agent("i40-arch", "w1:pC"), agent("i47-precheck", "w1:pD"), agent("i48-dev", "w1:pE"),
                agent(None, "w1:pU", cwd=user_cwd)]


class WrapupTest(Case):
    def test_closes_own_members_removes_shipped_worktrees_and_marks_index(self) -> None:
        self.registry({"w1:pA": SCHED, "w1:pB": SCHED, "w1:pC": SCHED, "w1:pE": "delivery-scheduler-w1-px"})
        dev = self.git.add(f"{self.wt}/{TASK}-dev", branch="fix/47", upstream="origin/fix/47", **{"in": {"origin/release-40"}})
        nested = self.git.add(f"{dev}/.hwf/tmp/base", **{"in": {"origin/main"}})
        picked = self.git.add(f"{self.wt}/{TASK}-release", branch="merge/47", remote_has=True,
                              cherry_in={"origin/release-40"})
        dirty = self.git.add(f"{self.wt}/{TASK}-wip", branch="wip", dirty=True, **{"in": {"origin/main"}})
        unshipped = self.git.add(f"{self.wt}/{TASK}-exp", branch="exp", upstream="origin/exp", ahead=2)
        other = self.git.add(f"{self.wt}/{TASK}0-dev", **{"in": {"origin/main"}})  # proj-470 不是本任务
        herdr = FakeHerdr(self.team())
        code, out = self.run_wrapup(herdr)
        self.assertEqual(code, 0, out)
        # 登记在本调度者名下的（含旧任务残留 i40-arch）+ 本调度者 tab 里未登记的 i47-*；不碰调度者、别人的、用户 pane
        self.assertEqual(sorted(herdr.closed), ["w1:pA", "w1:pB", "w1:pC", "w1:pD"])
        self.assertEqual(self.git.removed, [nested, dev, picked])  # 里层先删
        self.assertIn(other, self.git.trees)
        receipt = next(iter(self.glab.notes.values()))
        self.assertIn("**收尾回执**", receipt)
        self.assertIn(f"{dirty}（有未提交改动）", receipt)
        self.assertIn(unshipped, receipt)
        self.assertIn("exp：有 2 个提交未推送", receipt)
        self.assertIn("merge/47：未设上游，HEAD 已在 origin/merge/47", receipt)
        self.assertIn("wip：未推送（远端分支里都没有当前 HEAD）", receipt)
        self.assertIn("i47-dev / w1:pA", receipt)
        self.assertIn("已关闭 pid 101（端口 3167）", receipt)
        self.assertIn("测试账号：已释放 1 个", receipt)
        self.assertNotIn("13800000000", receipt)
        self.assertEqual(self.index_status(), "done（已上线）")
        self.assertIn("收尾回执", Path(self.repo, f".hwf/tmp/{TASK}/STATE.md").read_text())
        self.assertEqual(set(json.loads((self.state / "members.json").read_text())), {"w1:pE"})
        self.assertIn(("test-account.py", "release", "--task", TASK), self.scripts)
        stops = [s for s in self.scripts if s[0] == "stop-test-services.py"]
        self.assertEqual(len(stops), 2)  # 成员关闭前后各一次

    def test_worktree_still_used_by_other_pane_is_kept(self) -> None:
        dev = self.git.add(f"{self.wt}/{TASK}-dev", branch="fix/47", upstream="origin/fix/47", **{"in": {"origin/main"}})
        herdr = FakeHerdr(self.team(user_cwd=f"{self.wt}/{TASK}-dev/src"))
        code, out = self.run_wrapup(herdr)
        self.assertEqual(code, 0, out)
        self.assertNotIn(dev, self.git.removed)
        self.assertIn("还有 pane 在用：w1:pU", next(iter(self.glab.notes.values())))

    def test_bare_repo_dir_is_reported_not_touched(self) -> None:
        bare = self.git.add(f"{self.wt}/{TASK}-origin.git", bare=True)
        code, out = self.run_wrapup(FakeHerdr([agent(SCHED, "w1:pS")]))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git.removed, [])
        self.assertIn(f"{bare}（是裸仓库，不是 worktree，脚本不处理）", next(iter(self.glab.notes.values())))
        self.assertNotIn(bare, [a for s in self.scripts if s[0] == "stop-test-services.py" for a in s])

    def test_service_residue_keeps_worktrees_and_index(self) -> None:
        self.services = done(1, "RESIDUE: port 3167 busy (pid=55)\n")
        dev = self.git.add(f"{self.wt}/{TASK}-dev", branch="fix/47", **{"in": {"origin/main"}})
        code, out = self.run_wrapup(FakeHerdr([agent(SCHED, "w1:pS")]))
        self.assertEqual(code, 1)
        self.assertEqual(self.git.removed, [])
        self.assertIn(f"{dev}（测试服务没关干净，先不删）", out)
        self.assertIn("收尾未完成", next(iter(self.glab.notes.values())))
        self.assertEqual(self.index_status(), "in-progress（开发中）")

    def test_pane_still_alive_after_close_fails(self) -> None:
        self.registry({"w1:pA": SCHED})
        herdr = FakeHerdr([agent(SCHED, "w1:pS"), agent("i47-dev", "w1:pA")], stuck={"w1:pA"})
        code, out = self.run_wrapup(herdr)
        self.assertEqual(code, 1)
        self.assertIn("关闭后回读仍在", out)
        self.assertEqual(self.index_status(), "in-progress（开发中）")

    def test_sibling_open_task_limits_to_this_issue(self) -> None:
        index = Path(self.repo, ".hwf/tmp/INDEX.md")
        index.write_text(index.read_text().replace("done（旧）", "in-progress（兄弟任务）"), encoding="utf-8")
        self.registry({"w1:pA": SCHED, "w1:pC": SCHED})
        herdr = FakeHerdr([agent(SCHED, "w1:pS"), agent("i47-dev", "w1:pA"), agent("i40-arch", "w1:pC")])
        code, out = self.run_wrapup(herdr)
        self.assertEqual(code, 0, out)
        self.assertEqual(herdr.closed, ["w1:pA"])

    def test_last_pane_in_workspace_is_kept(self) -> None:
        self.registry({"w2:pA": SCHED})
        herdr = FakeHerdr([agent(SCHED, "w1:pS"), agent("i47-dev", "w2:pA")])
        code, out = self.run_wrapup(herdr)
        self.assertEqual(code, 0, out)
        self.assertEqual(herdr.closed, [])
        self.assertIn("最后一个 pane", next(iter(self.glab.notes.values())))

    def test_refuses_other_schedulers_task_before_touching_anything(self) -> None:
        self.registry({"w1:pA": SCHED})
        herdr = FakeHerdr([agent("delivery-scheduler-w1-px", "w1:pS"), agent("i47-dev", "w1:pA")])
        err = io.StringIO()
        with patch("sys.stderr", err):
            code, _ = self.run_wrapup(herdr, "--scheduler", "delivery-scheduler-w1-px")
        self.assertEqual(code, 1)
        self.assertIn("只能收尾自己的任务", err.getvalue())
        self.assertEqual(herdr.closed, [])
        self.assertEqual(self.scripts, [])

    def test_status_must_be_terminal(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            out = io.StringIO()
            with patch.object(WR, "herdr", FakeHerdr([agent(SCHED, "w1:pS")])), redirect_stdout(out):
                code = WR.main(["--repo", self.repo, "--task", TASK, "--issue", ISSUE, "--release", "r",
                                "--status", "blocked"])
        self.assertEqual(code, 1)
        self.assertEqual(self.index_status(), "in-progress（开发中）")

    def test_dry_run_changes_nothing(self) -> None:
        self.registry({"w1:pA": SCHED})
        self.git.add(f"{self.wt}/{TASK}-dev", branch="fix/47", **{"in": {"origin/main"}})
        herdr = FakeHerdr([agent(SCHED, "w1:pS"), agent("i47-dev", "w1:pA", cwd=f"{self.wt}/{TASK}-dev")])
        code, out = self.run_wrapup(herdr, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(herdr.closed, [])
        self.assertEqual(self.git.removed, [])
        self.assertEqual(self.glab.notes, {})
        self.assertIn("将关闭 i47-dev / w1:pA", out)
        self.assertIn(f"将删除 {self.wt}/{TASK}-dev", out)
        self.assertNotIn(("test-account.py", "release", "--task", TASK), self.scripts)
        self.assertEqual(self.index_status(), "in-progress（开发中）")

    def test_without_close_request_issue_stays_open(self) -> None:
        code, out = self.run_wrapup(FakeHerdr(self.team()))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.glab.issue_state, "opened")

    def test_close_request_from_user_closes_issue_after_receipt(self) -> None:
        code, out = self.run_wrapup(FakeHerdr(self.team()), "--close-request", f"{ISSUE}#note_50")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.glab.issue_state, "closed")
        self.assertIn(f"按 @pengjh 的要求关闭（{ISSUE}#note_50）", next(iter(self.glab.notes.values())))
        self.assertTrue(self.index_status().startswith("done"))

    def test_close_request_must_be_users_note_on_this_issue(self) -> None:
        for link in (f"{ISSUE}#note_51", f"{ISSUE}#note_52", ISSUE,
                     "https://git.example.com/group/proj/-/work_items/48#note_50"):
            herdr = FakeHerdr(self.team())
            with patch("sys.stderr", io.StringIO()):
                code, _ = self.run_wrapup(herdr, "--close-request", link)
            self.assertEqual(code, 1, link)
            self.assertEqual((herdr.closed, self.glab.notes, self.glab.issue_state), ([], {}, "opened"), link)
        self.assertTrue(self.index_status().startswith("in-progress"))

    def test_residue_keeps_issue_open_even_with_close_request(self) -> None:
        self.services = done(1, "RESIDUE: port 3167 busy (pid=55)\n")
        self.git.add(f"{self.wt}/{TASK}-dev", branch="fix/47", **{"in": {"origin/main"}})
        code, _ = self.run_wrapup(FakeHerdr([agent(SCHED, "w1:pS")]), "--close-request", f"{ISSUE}#note_50")
        self.assertEqual(code, 1)
        self.assertEqual(self.glab.issue_state, "opened")
        self.assertIn("收尾未完成，先不关闭", next(iter(self.glab.notes.values())))

    def test_close_readback_failure_keeps_index(self) -> None:
        self.glab.close_sticks = False
        code, out = self.run_wrapup(FakeHerdr(self.team()), "--close-request", f"{ISSUE}#note_50")
        self.assertEqual(code, 1, out)
        self.assertIn("关闭 issue", out)
        self.assertTrue(self.index_status().startswith("in-progress"))

    def test_receipt_readback_mismatch_keeps_index(self) -> None:
        def bad_glab(host: str, method: str, path: str, *fields: str) -> dict:
            return {"id": 1} if method == "POST" else {"id": 1, "body": "被改过"}
        with patch.object(WR, "glab_api", bad_glab), patch("sys.stderr", io.StringIO()):
            code, out = self.run_wrapup(FakeHerdr([agent(SCHED, "w1:pS")]))
        self.assertEqual(code, 1)
        self.assertIn("回读内容不一致", out)
        self.assertEqual(self.index_status(), "in-progress（开发中）")


if __name__ == "__main__":
    unittest.main()
