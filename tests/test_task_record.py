"""聚焦验证 task-record.py：INDEX 行格式由脚本保证、免打扰拦下阶段进度、请用户同意前先自查做不做得了。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_record", ROOT / "scripts/task-record.py")
assert SPEC is not None and SPEC.loader is not None
TR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TR
SPEC.loader.exec_module(TR)

TASK = "group-proj-47"
SCHED = "delivery-scheduler-w1-ps"
ISSUE = "https://git.example.com/group/proj/-/work_items/47"
MR = "https://git.example.com/group/proj/-/merge_requests/9"


def herdr(*args: str, timeout: int = 30) -> dict[str, Any]:
    return {"result": {"agents": [{"name": SCHED, "pane_id": "w1:pS"}]}}


class FakeGlab:
    """按路径回放 GitLab 接口；POST 评论后可回读。"""

    def __init__(self) -> None:
        self.mr: dict[str, Any] = {"state": "opened", "has_conflicts": False, "target_branch": "main",
                                   "user": {"can_merge": True}}
        self.closes_issues: list[dict[str, Any]] = []
        self.level = 30
        self.protected_tags: Any = []
        self.posted: list[str] = []

    def __call__(self, host: str, method: str, path: str, *fields: str) -> Any:
        if method == "POST":
            self.posted.append(fields[0].split("=", 1)[1])
            return {"id": 900 + len(self.posted)}
        if "/notes/" in path:
            return {"body": self.posted[int(path.rsplit("/", 1)[1]) - 901]}
        if path.endswith("/closes_issues"):
            return self.closes_issues
        if "/merge_requests/" in path:
            return self.mr
        if path.endswith("/protected_tags?per_page=100"):
            if isinstance(self.protected_tags, Exception):
                raise self.protected_tags
            return self.protected_tags
        return {"permissions": {"project_access": None, "group_access": {"access_level": self.level}}}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        self.glab = FakeGlab()
        for target, value in (("herdr", herdr), ("glab_api", self.glab)):
            patcher = patch.object(TR._WR, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pS"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self) -> str:
        return TR.state_path(self.repo, TASK).read_text(encoding="utf-8")

    def note(self, kind: str, body: str = "正文", *wills: str) -> tuple[int, str]:
        return TR.note(self.repo, TASK, ISSUE, kind, body, wills)


class IndexTests(Case):
    def test_added_row_is_routable_and_keeps_pipes_in_one_cell(self) -> None:
        TR.index_add(self.repo, TASK, "第一行 | 带竖线\n第二行", "in-progress", False, None)
        owner = TR._FS.owner_of(ISSUE, self.repo, [{"name": SCHED, "pane_id": "w1:pS"}])
        self.assertEqual((owner["task"], owner["pane"], owner["status"]), (TASK, "w1:pS", "in-progress"))

    def test_status_change_goes_to_own_row_and_terminal_status_is_refused(self) -> None:
        TR.index_add(self.repo, TASK, "需求", "in-progress", False, None)
        TR.index_set(self.repo, TASK, "blocked（等用户）", None)
        self.assertEqual(TR._FS.read_index(self.repo)[-1].status, "blocked（等用户）")
        with self.assertRaises(TR.RecordError):
            TR.index_set(self.repo, TASK, "done（已上线）", None)
        with self.assertRaises(TR._WR.WrapupError):
            TR.index_set(self.repo, TASK, "blocked", "delivery-scheduler-w1-px")

    def test_queued_row_has_no_owner(self) -> None:
        TR.index_add(self.repo, TASK, "排队的需求", "queued", True, None)
        row = TR._FS.read_index(self.repo)[-1]
        self.assertEqual((row.task, row.scheduler, row.open), (TASK, "", True))

    def test_status_change_repairs_row_missing_trailing_pipe(self) -> None:
        # 2026-11-15 #483：手写的行末尾少了「 |」，index-set 和 wrapup 都报找不到行。
        path = Path(self.repo) / TR._FS.INDEX_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(f"| `{TASK}` | 需求 | `{SCHED}` / `w1:pS` | blocked：等用户。\n", encoding="utf-8")
        TR.index_set(self.repo, TASK, "in-progress", None)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"| `{TASK}` | 需求 | `{SCHED}` / `w1:pS` | in-progress |\n")


class StateTests(Case):
    def test_top_block_keeps_existing_body_and_other_fields(self) -> None:
        path = TR.state_path(self.repo, TASK)
        path.parent.mkdir(parents=True)
        path.write_text("# 调度记录\n\n- 通知模式：详尽（用户 note_1）\n- 历史：很长\n", encoding="utf-8")
        TR.state_set(self.repo, TASK, "当前阶段", "§7\n代码评审")
        TR.state_set(self.repo, TASK, "下一动作", "派 test")
        top = TR.read_top(self.state())
        self.assertEqual((top["通知模式"], top["当前阶段"], top["下一动作"]), ("详尽", "§7 代码评审", "派 test"))
        self.assertIn("- 历史：很长", self.state())
        self.assertEqual(self.state().count(TR.TOP_OPEN), 1)

    def test_mode_must_be_one_of_two(self) -> None:
        with self.assertRaises(TR.RecordError):
            TR.state_set(self.repo, TASK, "通知模式", "安静")

    def test_state_compact_archives_old_sections_and_long_bullet_logs_losslessly(self) -> None:
        path = TR.state_path(self.repo, TASK)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同时覆盖两种膨胀形态：过多 `## 阶段` 章节 + 单个 `## 成员与收取` 章节内大量 `- ` 流水（含代码块保护）
        sections = [
            "# group-proj-47 调度记录",
            "",
            "## 来源与授权",
            "- 发起人：@pengjh",
            "- 授权：已确认 /go",
            "",
            "## 调度与基线",
            "- 主仓库：/repo",
            "- 关键分支：feat/47",
            "```bash",
            "## 代码围栏内的注释标题不应被误切分",
            "- fence 内条目",
            "```",
            "",
        ]
        for idx in range(1, 11):
            sections.extend([f"## 第 {idx} 轮评审归档", f"- 轮次 {idx} 结论：通过", ""])
        sections.append("## 成员与收取")
        for b in range(1, 101):
            sections.append(f"- 2026-11-23 10:{b % 60:02d} 交接记录 #{b}（session=s{b}）")
            sections.append(f"  补充说明行 #{b}")
        path.write_text("\n".join(sections) + "\n", encoding="utf-8")

        # state_set 在超阈值（>220 行）时自动触发无损归档
        TR.state_set(self.repo, TASK, "当前阶段", "§9 待上线")
        compacted = self.state()
        hist = TR.state_history_path(self.repo, TASK).read_text(encoding="utf-8")

        # 1) 顶部块、常驻章节、代码块完整保留
        self.assertEqual(TR.read_top(compacted)["当前阶段"], "§9 待上线")
        self.assertIn("## 来源与授权", compacted)
        self.assertIn("## 调度与基线", compacted)
        self.assertIn("## 代码围栏内的注释标题不应被误切分", compacted)

        # 2) 早期评审章节迁入 STATE-history.md，最近章节保留在 STATE.md
        self.assertNotIn("## 第 1 轮评审归档", compacted)
        self.assertIn("## 第 1 轮评审归档", hist)
        self.assertIn("## 第 10 轮评审归档", compacted)

        # 3) 单个 `## 成员与收取` 早期条目迁入 STATE-history.md，最新 25 条留在 STATE.md
        self.assertNotIn("交接记录 #1（session=s1）", compacted)
        self.assertIn("交接记录 #1（session=s1）", hist)
        self.assertIn("补充说明行 #1", hist)
        self.assertIn("交接记录 #75（session=s75）", hist)
        self.assertIn("交接记录 #76（session=s76）", compacted)
        self.assertIn("交接记录 #100（session=s100）", compacted)
        self.assertIn("早期累计 75 条历史记录已无损归档", compacted)
        self.assertLess(compacted.count("\n"), 180)


class NoteTests(Case):
    def test_quiet_mode_holds_stage_archives_step_and_only_tracks_user_facing_notice(self) -> None:
        self.assertEqual(self.note("stage")[0], TR.EXIT_HELD)
        self.assertEqual(self.glab.posted, [])
        todo_code, todo_link = self.note("todo", "需要你决定：选 A 还是 B", "none")
        self.assertEqual(todo_code, TR.EXIT_OK)
        self.assertEqual(self.glab.posted[-1], "需要你决定：选 A 还是 B\n\n<!-- hwf-kind:todo -->")
        self.assertIn(todo_link, TR.read_top(self.state())["最近用户通知"])

        # 免打扰下 step-done 与 record 照常写 GitLab 备查，但带对应 hwf-kind 且不覆盖「最近用户通知」。
        step_code, _ = self.note("step-done", "步骤 1 完成")
        rec_code, _ = self.note("record", "已评审用例存档")
        self.assertEqual((step_code, rec_code), (TR.EXIT_OK, TR.EXIT_OK))
        self.assertEqual(self.glab.posted[-2:], [
            "步骤 1 完成\n\n<!-- hwf-kind:step-done -->",
            "已评审用例存档\n\n<!-- hwf-kind:record -->",
        ])
        self.assertIn(todo_link, TR.read_top(self.state())["最近用户通知"])

        done_code, done_link = self.note("delivery-done", "整项交付完成")
        self.assertEqual(done_code, TR.EXIT_OK)
        self.assertTrue(self.glab.posted[-1].endswith("<!-- hwf-kind:delivery-done -->"))
        self.assertIn(done_link, TR.read_top(self.state())["最近用户通知"])

    def test_verbose_mode_sends_stage_progress(self) -> None:
        TR.state_set(self.repo, TASK, "通知模式", "详尽（用户 note_3）")
        self.assertEqual(self.note("stage", "本轮评审通过")[0], TR.EXIT_OK)

    def test_same_body_is_sent_once(self) -> None:
        self.note("answer", "结论")
        self.assertEqual(self.note("answer", "结论")[0], TR.EXIT_HELD)
        self.assertEqual(len(self.glab.posted), 1)

    def test_todo_needs_declared_action(self) -> None:
        with self.assertRaises(TR.RecordError):
            self.note("todo", "请同意合并")
        self.assertEqual(self.note("todo", "请你自己合并", "none")[0], TR.EXIT_OK)

    def test_asking_approval_for_a_merge_we_cannot_do_is_held(self) -> None:
        # 2026-11-15 #483：用户同意合并后才发现本账号没有 main 的合并权限。
        self.glab.mr["user"]["can_merge"] = False
        code, text = self.note("todo", "同意的话我来合并", f"merge={MR}")
        self.assertEqual((code, self.glab.posted), (TR.EXIT_HELD, []))
        self.assertIn("没有把", text)

    def test_mergeable_request_is_sent(self) -> None:
        self.assertEqual(self.note("todo", "同意的话我来合并", f"merge={MR}")[0], TR.EXIT_OK)

    def test_mr_that_autoclose_open_issue_is_blocked_even_with_will_none(self) -> None:
        # 2026-11-21 #484：前端 main MR 标题写了 fix: #484，调度者用 --will none 请用户点合并，一点就把 #484 自动关了。
        self.glab.closes_issues = [{"iid": 484, "state": "opened"}]
        code1, text1 = self.note("todo", "同意的话我来合并", f"merge={MR}")
        self.assertEqual((code1, self.glab.posted), (TR.EXIT_HELD, []))
        self.assertIn("自动关闭 #484", text1)
        self.assertNotIn("--will none", text1)

        code2, text2 = self.note("todo", f"请点击合并：{MR}", "none")
        self.assertEqual((code2, self.glab.posted), (TR.EXIT_HELD, []))
        self.assertIn("自动关闭 #484", text2)

        code3, _ = self.note("delivery-done", f"交付完成，上线 MR：{MR}")
        self.assertEqual((code3, self.glab.posted), (TR.EXIT_HELD, []))

        # 登记池存档（record）、已关闭的子 issue 或已合并的历史 MR 不误拦。
        self.assertEqual(self.note("record", f"登记池备忘：{MR}")[0], TR.EXIT_OK)
        self.glab.closes_issues = [{"iid": 485, "state": "closed"}]
        self.assertEqual(self.note("todo", f"请点击合并：{MR}", "none")[0], TR.EXIT_OK)
        self.glab.closes_issues = [{"iid": 484, "state": "opened"}]
        self.glab.mr["state"] = "merged"
        self.assertEqual(self.note("step-done", f"步骤 1 已合并测试分支：{MR}")[0], TR.EXIT_OK)

    def test_protected_tag_blocks_and_unreadable_rule_is_disclosed(self) -> None:
        will = "tag=https://git.example.com/group/proj@2.7.20"
        self.glab.protected_tags = [{"name": "*", "create_access_levels": [{"access_level": 40}]}]
        self.assertEqual(self.note("todo", "同意的话我打 tag", will)[0], TR.EXIT_HELD)
        self.glab.protected_tags = TR._WR.WrapupError("403 Forbidden")
        self.assertEqual(self.note("todo", "同意的话我打 tag", will)[0], TR.EXIT_OK)
        self.assertIn("读不到", self.glab.posted[-1])


SHIPPED, PENDING = "a" * 40, "b" * 40
OLD_DESCRIPTION = """<!-- hwf:start 以下由交付流程维护 -->
## 进度
- [x] 1. 手勾的步骤，后面跟着一长串分支和流水线

## 上线清单
| 仓库 | release 分支 | 改了什么 | 有没有数据库改动 |
| --- | --- | --- | --- |
| frontend/web-v1 | release-47 | 接口接入 | 无 |

---
<!-- hwf:end -->

## 用户原文
别动我"""


class ProgressGlab:
    """issue 描述可读写；merge_base 只把 SHIPPED 判为已进 release。"""

    def __init__(self) -> None:
        self.description = OLD_DESCRIPTION
        self.puts = 0

    def __call__(self, host: str, method: str, path: str, *fields: str) -> Any:
        if "merge_base" in path:
            sha = path.split("refs[]=")[1].split("&")[0]
            return {"id": sha if sha == SHIPPED else "c" * 40}
        if method == "PUT":
            self.puts += 1
            self.description = fields[0].split("=", 1)[1]
            return {}
        return {"description": self.description}


class ProgressTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.issue = ProgressGlab()
        patcher = patch.object(TR._WR, "glab_api", self.issue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.steps = {"steps": [
            {"title": "接口接入", "commits": [{"project": "group/proj", "sha": SHIPPED}]},
            {"title": "页面改版", "status": "待汇总", "remaining": "等待合入发布分支", "blocked_by": [1],
             "commits": [{"project": "group/proj", "sha": PENDING}]},
            {"title": "桌面端移植", "blocked_by": [2]}]}

    def test_ticks_only_what_reached_release_and_keeps_user_text(self) -> None:
        # 2026-11-19 #484：代码还没进 release-484 就手勾了 9 项。
        TR.state_set(self.repo, TASK, "当前阶段", "页面改版开发中")
        TR.progress(self.repo, TASK, ISSUE, self.steps)
        text = self.issue.description
        self.assertIn("- [x] 1. 接口接入（已在 release-47）", text)
        self.assertIn("- [ ] 2. **待汇总**｜页面改版。等待合入发布分支（依赖步骤 1）", text)
        self.assertIn("- [ ] 3. **待核实**｜桌面端移植（依赖步骤 2）", text)
        self.assertIn("**现在**：页面改版开发中", text)
        self.assertIn("**等你处理**：无", text)
        self.assertIn("| frontend/web-v1 | release-47 | 接口接入 | 无 |", text)
        self.assertTrue(text.rstrip().endswith("## 用户原文\n别动我"))
        self.assertEqual(text.count(TR.AREA_OPEN), 1)
        self.assertNotIn("手勾的步骤", text)

    def test_state_change_refreshes_issue_without_restating_steps(self) -> None:
        # 2026-11-19 #484：进展只记在 STATE 和评论里，正文 7 小时没变。
        TR.progress(self.repo, TASK, ISSUE, self.steps)
        TR.state_set(self.repo, TASK, "待用户事项", "请拍板续传时机 note_27074")
        self.assertIn("**等你处理**：请拍板续传时机 note_27074", self.issue.description)
        TR.state_set(self.repo, TASK, "最近用户通知", "note_1")
        self.assertEqual(self.issue.puts, 2)

    def test_long_lines_are_refused_before_anything_is_written(self) -> None:
        TR.progress(self.repo, TASK, ISSUE, self.steps)
        with self.assertRaises(TR.RecordError):
            TR.state_set(self.repo, TASK, "当前阶段", "细" * (TR.NOW_LIMIT + 1))
        with self.assertRaises(TR.RecordError):
            TR.progress(self.repo, TASK, steps={"steps": [{"title": "长" * (TR.TITLE_LIMIT + 1)}]})
        self.assertEqual(self.issue.puts, 1)

    def test_missing_release_branch_means_nothing_ticked(self) -> None:
        def no_branch(host: str, method: str, path: str, *fields: str) -> Any:
            if "merge_base" in path:
                raise TR._WR.WrapupError("glab api GET 失败：Could not find ref: release-47")
            return self.issue(host, method, path, *fields)
        with patch.object(TR._WR, "glab_api", no_branch):
            TR.progress(self.repo, TASK, ISSUE, self.steps)
        self.assertNotIn("[x]", self.issue.description)


CARD = """## 给用户的摘要
直达路径。

## 完整内容
复杂度判定：直达路径

### 现状
- 入口在 src/views/Foo.vue:12，复用 useBar（src/composables/useBar.ts:40）

**历史与坑：**
- 已查 git log、关联 issue、记忆库，无相关记录

**验收条件**
1. 宽 1200px 时工具栏不换行 ｜ 证据：该宽度下的页面截图
2. 保存失败时出现重试按钮
   ｜ **证据：** 断网后点击保存的截图

验收前提：无特殊前提

关键决定：无
"""


class CardCheckTests(unittest.TestCase):
    def test_complete_card_passes(self) -> None:
        self.assertEqual(TR.card_check(CARD), [])

    def test_minimal_path_card_passes(self) -> None:
        minimal = CARD.replace("复杂度判定：直达路径", "复杂度判定：极简路径\n先出方案：不要\n防风险：不要")
        self.assertEqual(TR.card_check(minimal), [])

    def test_acceptance_without_evidence_is_reported_by_number(self) -> None:
        problems = TR.card_check(CARD.replace(" ｜ 证据：该宽度下的页面截图", ""))
        self.assertEqual(len(problems), 1)
        self.assertIn("第 1 条", problems[0])

    def test_old_style_verdict_without_card_is_held(self) -> None:
        old = "复杂度判定：直达路径\n理由：小改动\n依据：src/a.ts:3\n关键决定：无\n"
        self.assertEqual(len(TR.card_check(old)), 4)

    def test_acceptance_prerequisites_must_be_written(self) -> None:
        problems = TR.card_check(CARD.replace("验收前提：无特殊前提\n", ""))
        self.assertEqual(len(problems), 1)
        self.assertIn("验收前提", problems[0])

    def test_status_quo_needs_file_line(self) -> None:
        problems = TR.card_check(CARD.replace("src/views/Foo.vue:12", "Foo 页面").replace("（src/composables/useBar.ts:40）", ""))
        self.assertEqual(len(problems), 1)
        self.assertIn("现状", problems[0])

    def test_command_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "verdict.md"
            path.write_text(CARD, encoding="utf-8")
            self.assertEqual(TR.main(["card-check", "--file", str(path)]), TR.EXIT_OK)
            path.write_text("复杂度判定：直达路径\n", encoding="utf-8")
            self.assertEqual(TR.main(["card-check", "--file", str(path)]), TR.EXIT_HELD)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.remote = str(root / "origin.git")
        self.repo = str(root / "work")
        self._git(str(root), "init", "--bare", self.remote)
        self._git(str(root), "clone", self.remote, self.repo)
        self._git(self.repo, "config", "user.email", "test@example.com")
        self._git(self.repo, "config", "user.name", "Tester")
        self._git(self.repo, "config", "commit.gpgsign", "false")
        self._git(self.repo, "config", "core.hooksPath", "/dev/null")
        self._git(self.repo, "checkout", "-b", "main")
        self._write("app.ts", "export const v = 1;\n")
        self._write("shared.ts", "export const s = 'base';\n")
        self._commit("init main")
        self._git(self.repo, "push", "-u", "origin", "main")
        self._git(self.repo, "push", "origin", "main:develop")
        self._git(self.repo, "push", "origin", "main:release-47")

    def _git(self, cwd: str, *args: str) -> str:
        res = TR._WR.git(cwd, *args)
        if res.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}\n{res.stdout}")
        return res.stdout.strip()

    def _write(self, rel: str, content: str) -> None:
        target = Path(self.repo) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _commit(self, msg: str) -> str:
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", msg)
        return self._git(self.repo, "rev-parse", "HEAD")

    def test_clean_single_branch_matches_release_tree_and_develop(self) -> None:
        self._git(self.repo, "checkout", "-b", "feat/issue-47", "origin/release-47")
        self._write("app.ts", "export const v = 2;\n")
        feat_sha = self._commit("feat: update v")
        # 模拟 develop 另有无关文件改动，再合入本需求分支。
        self._git(self.repo, "checkout", "-b", "dev-local", "origin/develop")
        self._write("other.ts", "export const other = true;\n")
        self._commit("feat: unrelated develop work")
        self._git(self.repo, "merge", "--no-ff", feat_sha, "-m", "merge feat into develop")
        self._git(self.repo, "push", "origin", "dev-local:develop")
        # 合入 release-47。
        self._git(self.repo, "checkout", "-b", "rel-local", "origin/release-47")
        self._git(self.repo, "merge", "--no-ff", feat_sha, "-m", "merge feat into release-47")
        self._git(self.repo, "push", "origin", "rel-local:release-47")

        code = TR.main([
            "release-check", "--repo", self.repo, "--release", "release-47",
            "--base", "origin/main", "--develop", "origin/develop", "--commit", feat_sha,
        ])
        self.assertEqual(code, TR.EXIT_OK)
        report = TR.release_check(self.repo, "release-47", "origin/main", [feat_sha], "origin/develop")
        self.assertTrue(report["same_tree_as_single_commit"])
        self.assertEqual(report["changed_files"], ["app.ts"])
        self.assertEqual(report["problems"], [])

    def test_foreign_commit_on_release_is_held(self) -> None:
        self._git(self.repo, "checkout", "-b", "feat/issue-47", "origin/release-47")
        self._write("app.ts", "export const v = 2;\n")
        feat_sha = self._commit("feat: update v")
        self._git(self.repo, "checkout", "-b", "rel-local", "origin/release-47")
        self._git(self.repo, "merge", "--no-ff", feat_sha, "-m", "merge feat")
        self._write("rogue.ts", "export const bad = 1;\n")
        self._commit("chore: accidental extra commit")
        self._git(self.repo, "push", "origin", "rel-local:release-47")

        report = TR.release_check(self.repo, "release-47", "origin/main", [feat_sha])
        self.assertFalse(report["same_tree_as_single_commit"])
        self.assertTrue(any("非本需求提交" in p for p in report["problems"]))
        self.assertEqual(
            TR.main(["release-check", "--repo", self.repo, "--release", "release-47", "--commit", feat_sha]),
            TR.EXIT_HELD,
        )

    def test_unauthorized_develop_merge_into_feature_branch_is_held(self) -> None:
        self._git(self.repo, "checkout", "-b", "dev-local", "origin/develop")
        self._write("dev_only.ts", "export const devOnly = 1;\n")
        self._commit("feat: develop only wiring")
        self._git(self.repo, "push", "origin", "dev-local:develop")

        self._git(self.repo, "checkout", "-b", "feat/issue-47", "origin/release-47")
        self._write("app.ts", "export const v = 2;\n")
        self._commit("feat: update v")
        self._git(self.repo, "merge", "--no-ff", "origin/develop", "-m", "bad: merge develop into feature")
        polluted_sha = self._git(self.repo, "rev-parse", "HEAD")
        self._git(self.repo, "push", "origin", f"{polluted_sha}:refs/heads/release-47")

        report = TR.release_check(self.repo, "release-47", "origin/main", [polluted_sha], "origin/develop")
        self.assertTrue(any("未批准合并提交" in p for p in report["problems"]))

    def test_shared_file_diff_with_develop_returns_held_for_review(self) -> None:
        self._git(self.repo, "checkout", "-b", "feat/issue-47", "origin/release-47")
        self._write("shared.ts", "export const s = 'issue-47';\n")
        feat_sha = self._commit("feat: touch shared")
        self._git(self.repo, "push", "origin", f"{feat_sha}:refs/heads/release-47")
        # develop 上同一文件另有其他需求的后续改动。
        self._git(self.repo, "checkout", "-b", "dev-local", "origin/develop")
        self._git(self.repo, "merge", "--no-ff", feat_sha, "-m", "merge 47")
        self._write("shared.ts", "export const s = 'issue-47 + other';\n")
        self._commit("feat: other task also touched shared.ts")
        self._git(self.repo, "push", "origin", "dev-local:develop")

        report = TR.release_check(self.repo, "release-47", "origin/main", [feat_sha], "origin/develop")
        self.assertEqual(report["develop_diff_files"], ["shared.ts"])
        self.assertTrue(any("shared.ts" in p for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
