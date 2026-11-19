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
        self.level = 30
        self.protected_tags: Any = []
        self.posted: list[str] = []

    def __call__(self, host: str, method: str, path: str, *fields: str) -> Any:
        if method == "POST":
            self.posted.append(fields[0].split("=", 1)[1])
            return {"id": 900 + len(self.posted)}
        if "/notes/" in path:
            return {"body": self.posted[int(path.rsplit("/", 1)[1]) - 901]}
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


class NoteTests(Case):
    def test_quiet_mode_holds_stage_progress_but_sends_step_summary(self) -> None:
        self.assertEqual(self.note("stage")[0], TR.EXIT_HELD)
        self.assertEqual(self.glab.posted, [])
        code, link = self.note("step-done", "步骤 1 完成")
        self.assertEqual((code, self.glab.posted), (TR.EXIT_OK, ["步骤 1 完成"]))
        self.assertIn(link, TR.read_top(self.state())["最近用户通知"])

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
            {"title": "页面改版", "blocked_by": [1], "commits": [{"project": "group/proj", "sha": PENDING}]},
            {"title": "桌面端移植", "blocked_by": [2]}]}

    def test_ticks_only_what_reached_release_and_keeps_user_text(self) -> None:
        # 2026-11-19 #484：代码还没进 release-484 就手勾了 9 项。
        TR.state_set(self.repo, TASK, "当前阶段", "页面改版开发中")
        TR.progress(self.repo, TASK, ISSUE, self.steps)
        text = self.issue.description
        self.assertIn("- [x] 1. 接口接入（已在 release-47）", text)
        self.assertIn("- [ ] 2. 页面改版　Blocked by 1", text)
        self.assertIn("- [ ] 3. 桌面端移植　Blocked by 2", text)
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

关键决定：无
"""


class CardCheckTests(unittest.TestCase):
    def test_complete_card_passes(self) -> None:
        self.assertEqual(TR.card_check(CARD), [])

    def test_acceptance_without_evidence_is_reported_by_number(self) -> None:
        problems = TR.card_check(CARD.replace(" ｜ 证据：该宽度下的页面截图", ""))
        self.assertEqual(len(problems), 1)
        self.assertIn("第 1 条", problems[0])

    def test_old_style_verdict_without_card_is_held(self) -> None:
        old = "复杂度判定：直达路径\n理由：小改动\n依据：src/a.ts:3\n关键决定：无\n"
        self.assertEqual(len(TR.card_check(old)), 3)

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


if __name__ == "__main__":
    unittest.main()
