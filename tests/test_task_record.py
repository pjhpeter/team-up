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


if __name__ == "__main__":
    unittest.main()
