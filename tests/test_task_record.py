"""聚焦验证 task-record.py：INDEX 行格式由脚本保证、免打扰拦下阶段进度、请用户同意前先自查做不做得了。"""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
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
        self.issue_notes: list[dict[str, Any]] = []

    def __call__(self, host: str, method: str, path: str, *fields: str) -> Any:
        if path == "user":
            return {"username": "pjhagent"}
        if "/notes?" in path:
            return list(self.issue_notes)
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
        valid_table = (
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |\n"
            "| --- | --- | --- |\n"
            "| 1. 按钮展示与点击 | 通过 | ![AC1](/uploads/0123456789abcdef0123456789abcdef/ac1.mp4) "
            "00:02–00:11 https://git.example.com/-/project/10/uploads/0123456789abcdef0123456789abcdef/ac1.mp4 |"
        )
        step_body = f"步骤 1 完成\n\n{valid_table}"
        step_code, _ = self.note("step-done", step_body)
        rec_code, _ = self.note("record", "已评审用例存档")
        self.assertEqual((step_code, rec_code), (TR.EXIT_OK, TR.EXIT_OK))
        self.assertEqual(self.glab.posted[-2:], [
            f"{step_body}\n\n<!-- hwf-kind:step-done -->",
            "已评审用例存档\n\n<!-- hwf-kind:record -->",
        ])
        self.assertIn(todo_link, TR.read_top(self.state())["最近用户通知"])

        done_code, done_link = self.note("delivery-done", f"整项交付完成\n\n{valid_table}")
        self.assertEqual(done_code, TR.EXIT_OK)
        self.assertTrue(self.glab.posted[-1].endswith("<!-- hwf-kind:delivery-done -->"))
        self.assertIn(done_link, TR.read_top(self.state())["最近用户通知"])

    def test_verbose_mode_sends_stage_progress(self) -> None:
        TR.state_set(self.repo, TASK, "通知模式", "详尽（用户 note_3）")
        self.assertEqual(self.note("stage", "本轮评审通过")[0], TR.EXIT_OK)

    def test_quiet_mode_blocks_unsolicited_answer_without_new_user_note(self) -> None:
        # 1) 全新工单尚无面向用户的 Agent 回复（即使已有收件箱回执和 record 备案）：首次 answer（如对话模式首轮分析）放行。
        self.glab.issue_notes = [
            {"id": 102, "author": {"username": "pjhagent"}, "system": False, "body": "备案\n<!-- hwf-kind:record -->"},
            {"id": 101, "author": {"username": "pjhagent"}, "system": False,
             "body": "已收到目标 issue https://git.example.com/group/proj/-/issues/47 的消息。目前没有在线负责人，已转交调度入口，等待接手后回复。"},
        ]
        code0, _ = self.note("answer", "首次只读分析结论")
        self.assertEqual(code0, TR.EXIT_OK)

        # 2) 已发过 answer 回复用户后，即使用户中间发了 /status 且收件箱回了状态文本，也没有新的用户提问：再发 answer 须拦下。
        self.glab.issue_notes = [
            {"id": 115, "author": {"username": "pjhagent"}, "system": False,
             "body": "@pengjh 目标 issue https://git.example.com/group/proj/-/issues/47：打开中；状态:方案中。\n会话状态：调度者空闲。"},
            {"id": 114, "author": {"username": "pengjh"}, "system": False, "body": "/status"},
            {"id": 113, "author": {"username": "pjhagent"}, "system": False, "body": "阶段存档\n<!-- hwf-kind:record -->"},
            {"id": 112, "author": {"username": "pjhagent"}, "system": False, "body": "已答复\n<!-- hwf-kind:answer -->"},
            {"id": 110, "author": {"username": "pengjh"}, "system": False, "body": "现在进展如何？"},
        ]
        code1, msg1 = self.note("answer", "Windows CI 第一轮失败，正在继续排查")
        self.assertEqual(code1, TR.EXIT_HELD)
        self.assertIn("没有新的用户留言或提问", msg1)
        self.assertIn("#note_112", msg1)

        # 同期真正的 todo / risk / record 仍照常放行。
        self.assertEqual(self.note("todo", "需要你决定：是否切方案 B", "none")[0], TR.EXIT_OK)
        self.assertEqual(self.note("risk", "发现安全风险：隔离容器退出")[0], TR.EXIT_OK)
        self.assertEqual(self.note("record", "补充备案")[0], TR.EXIT_OK)

        # 3) 用户发出新的非纯指令留言后（即使中间已有收件箱回执和 step-done 归档，也未推飞书）：answer 恢复放行。
        self.glab.issue_notes = [
            {"id": 122, "author": {"username": "pjhagent"}, "system": False, "body": "步骤 1 完成\n<!-- hwf-kind:step-done -->"},
            {"id": 121, "author": {"username": "pjhagent"}, "system": False,
             "body": "已收到，已转交目标 issue https://git.example.com/group/proj/-/issues/47 的消息，等待调度者回复。"},
            {"id": 120, "author": {"username": "pengjh"}, "system": False, "body": "那图片缓存这一项结论如何？"},
            {"id": 112, "author": {"username": "pjhagent"}, "system": False, "body": "已答复\n<!-- hwf-kind:answer -->"},
        ]
        code2, _ = self.note("answer", "图片缓存热态命中已验证通过。")
        self.assertEqual(code2, TR.EXIT_OK)

        # 4) 详尽模式下不拦截 answer。
        TR.state_set(self.repo, TASK, "通知模式", "详尽（用户 note_125）")
        self.glab.issue_notes = [
            {"id": 130, "author": {"username": "pjhagent"}, "system": False, "body": "已答复\n<!-- hwf-kind:answer -->"},
            {"id": 120, "author": {"username": "pengjh"}, "system": False, "body": "那图片缓存这一项结论如何？"},
        ]
        self.assertEqual(self.note("answer", "详尽模式补充进展")[0], TR.EXIT_OK)

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
        merged_step = (
            f"步骤 1 已合并测试分支：{MR}\n\n"
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |\n"
            "| --- | --- | --- |\n"
            "| 1. 接口返回校验 | 通过 | 纯后端接口单测：`tests/api.spec.ts` 8 条通过 |"
        )
        self.assertEqual(self.note("step-done", merged_step)[0], TR.EXIT_OK)

    def test_delivery_and_step_done_enforce_outer_acceptance_reconciliation_table(self) -> None:
        sec1 = "11111111111111111111111111111111"
        sec2 = "22222222222222222222222222222222"
        good_row_1 = (
            f"| E1 · 顶部提示关闭 | 通过 | ![E1](/uploads/{sec1}/ac1-close.mp4)<br>"
            f"时间段：00:01.5–00:12.0<br>直链：https://git.example.com/-/project/10/uploads/{sec1}/ac1-close.mp4 |"
        )

        # 1) 完全不带对账表 -> 拦下
        code, msg = self.note("delivery-done", "整项交付完成，请合并上线 MR")
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("缺少「验收标准逐条对账表」", msg)

        # 2) 对账表只藏在 <details> 折叠区里（#495 漏法） -> 拦下
        folded_only = (
            "整项交付完成\n\n<details><summary>完整内容</summary>\n\n"
            f"| 验收项 | 结果 | 录屏与时间段 |\n| --- | --- | --- |\n{good_row_1}\n\n</details>"
        )
        code, msg = self.note("delivery-done", folded_only)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("<details>", msg)

        # 3) 编号跳漏（只贴 E1 与 E5，漏掉 E2/E3/E4 —— #496 漏法） -> 拦下
        skipped_nums = "\n".join([
            "整项交付完成",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            good_row_1,
            f"| E5 · 冒烟切页 | 通过 | ![E5](/uploads/{sec2}/ac5.mp4) 00:00–00:08 "
            f"https://git.example.com/-/project/10/uploads/{sec2}/ac5.mp4 |",
        ])
        code, msg = self.note("delivery-done", skipped_nums)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("2、3、4", msg)

        # 4) 仅贴静态截图充数、未写无法录屏原因（#495 第 2/3/4 条漏法） -> 拦下
        screenshot_only = "\n".join([
            "步骤 1 完成",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            f"| 1. 右键粘贴避让 | 通过 | ![截图](/uploads/{sec1}/shot.png) "
            f"https://git.example.com/-/project/10/uploads/{sec1}/shot.png |",
        ])
        code, msg = self.note("step-done", screenshot_only)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("仅贴静态截图", msg)

        # 5) 播放器被包进反引号、缺飞书直链或缺时间段 -> 分别拦下
        backticked = "\n".join([
            "| 验收项 | 结果 | 录屏与时间段 |",
            "| --- | --- | --- |",
            f"| 1. 弹窗关闭 | 通过 | `![AC1](/uploads/{sec1}/ac1.mp4)` 00:02–00:09 "
            f"https://git.example.com/-/project/10/uploads/{sec1}/ac1.mp4 |",
        ])
        self.assertIn("反引号", self.note("step-done", backticked)[1])

        no_direct = "\n".join([
            "| 验收项 | 结果 | 录屏与时间段 |",
            "| --- | --- | --- |",
            f"| 1. 弹窗关闭 | 通过 | ![AC1](/uploads/{sec1}/ac1.mp4) 00:02–00:09 |",
        ])
        self.assertIn("/-/project/", self.note("step-done", no_direct)[1])

        no_time = "\n".join([
            "| 验收项 | 结果 | 录屏与时间段 |",
            "| --- | --- | --- |",
            f"| 1. 弹窗关闭 | 通过 | ![AC1](/uploads/{sec1}/ac1.mp4) "
            f"https://git.example.com/-/project/10/uploads/{sec1}/ac1.mp4 |",
        ])
        self.assertIn("时间段", self.note("step-done", no_time)[1])

        # 6) 手拼旧式 `/<组>/<项目>/uploads/...` 404 链接（#499 漏法） -> answer/todo/delivery-done 全部拦下
        bad_url_body = f"补发录屏：https://git.example.com/group/proj/uploads/{sec1}/ac1.mp4"
        code, msg = self.note("answer", bad_url_body)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("404", msg)

    def test_card_count_recorded_mp4_and_todo_final_acceptance_bypass_are_enforced(self) -> None:
        sec1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sec2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        # 写入含 3 条确认验收条件的需求理解卡，以及 testing/ 下录制好的 .mp4 文件。
        planning_dir = TR.state_path(self.repo, TASK).parent / "planning"
        planning_dir.mkdir(parents=True, exist_ok=True)
        card_3 = CARD.replace(
            "验收前提：无特殊前提",
            "3. 既有重复卡流程不回归 ｜ 证据：定向单测通过输出\n\n验收前提：无特殊前提",
        )
        (planning_dir / "01-arch.md").write_text(card_3, encoding="utf-8")
        testing_dir = TR.state_path(self.repo, TASK).parent / "testing" / "r1"
        testing_dir.mkdir(parents=True, exist_ok=True)
        (testing_dir / "ac1-toolbar.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42video")

        # 1) testing/ 有已录制的 .mp4，但全表 0 段视频 -> 拦下
        zero_video_table = "\n".join([
            "整项交付完成",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            "| 1. 工具栏不换行 | 通过 | 纯单测：`tests/toolbar.spec.ts` 4 条通过 |",
            "| 2. 断网重试按钮 | 通过 | 用户同意免测 #note_12 |",
            "| 3. 既有重复卡回归 | 通过 | 纯单测：`tests/dup.vitest.ts` 6 条通过 |",
        ])
        code, msg = self.note("delivery-done", zero_video_table)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("testing/ 目录已录制 .mp4", msg)

        # 2) 理解卡确认 3 条，但对账表只列了前 2 条 -> 拦下
        only_two_rows = "\n".join([
            "整项交付完成",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            f"| 1. 工具栏不换行 | 通过 | ![AC1](/uploads/{sec1}/ac1.mp4) 00:01–00:08 "
            f"https://git.example.com/-/project/10/uploads/{sec1}/ac1.mp4 |",
            "| 2. 断网重试按钮 | 通过 | 用户同意免真机验证 #note_18 |",
        ])
        code, msg = self.note("delivery-done", only_two_rows)
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("确认了 3 条验收条件", msg)

        # 3) 用 --kind todo 宣告「最终验收通过」却不附逐条对账表（#499 note_28782 绕过路径） -> 拦下
        code, msg = self.note("todo", "§9.1 最终验收通过，请先部署后端再合并上线", "none")
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("验收标准逐条对账表", msg)

        # 4) 补齐全部 3 条（含真机视频、无法录屏截图回退、纯单测）并在折叠区放完整报告 -> 放行
        complete_delivery = "\n".join([
            "整项交付完成，上线 MR 待你点击合并。",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            f"| 1. 工具栏不换行 | 通过 | ![AC1](/uploads/{sec1}/ac1.mp4)<br>00:01–00:08<br>"
            f"https://git.example.com/-/project/10/uploads/{sec1}/ac1.mp4 |",
            f"| 2. 断网重试按钮 | 通过 | 沙箱断网代理切换瞬态无法录屏，附同轮截图 ![AC2](/uploads/{sec2}/ac2.png) "
            f"https://git.example.com/-/project/10/uploads/{sec2}/ac2.png |",
            "| 3. 既有重复卡回归 | 通过 | 纯单测：`tests/dup.vitest.ts` 6 条通过 |",
            "",
            "<details><summary>完整内容</summary>\n\n详细评审与归档流水\n\n</details>",
        ])
        code, _ = self.note("delivery-done", complete_delivery)
        self.assertEqual(code, TR.EXIT_OK)

        # 已发过 delivery-done 后，后续补充 todo 不再重复要求对账表
        self.assertEqual(self.note("todo", "补充提醒：最终验收通过的前置后端已部署，请合并", "none")[0], TR.EXIT_OK)

    def test_delivery_done_independence_gate(self) -> None:
        sec1 = "cccccccccccccccccccccccccccccccc"
        video_delivery = "\n".join([
            "整项交付完成，上线 MR 待你合并。",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            f"| 1. 核心流程 | 通过 | ![AC1](/uploads/{sec1}/ac1.mp4) 00:01–00:08 "
            f"https://git.example.com/-/project/10/uploads/{sec1}/ac1.mp4 |",
        ])
        unit_only_delivery = "\n".join([
            "整项交付完成，上线 MR 待你合并。",
            "",
            "| 验收项 | 结果 | 录屏与时间段（或免录说明） |",
            "| --- | --- | --- |",
            "| 1. 后端接口校验 | 通过 | 纯单测：`pytest tests/test_api.py` 12 条通过 |",
        ])
        with tempfile.TemporaryDirectory() as state_dir:
            ledger = Path(state_dir) / "role-ledger.jsonl"

            def write_ledger(rows: list[dict[str, str]]) -> None:
                ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

            with patch.dict(os.environ, {"HWF_STATE_DIR": state_dir}):
                # 1) 正文显式写 dev 自审 -> 即使无台账也直接拦下
                code, msg = self.note("delivery-done", f"由 dev 自审通过。\n\n{unit_only_delivery}")
                self.assertEqual(code, TR.EXIT_HELD)
                self.assertIn("独立性硬门禁", msg)

                # 2) 常规交付仅有 dev，无 code-review、无 test -> 拦下并指出两者缺失
                write_ledger([
                    {"issue": "#47", "profile": "dev", "name": "p-47-dev", "pane_id": "p1", "session": "s1"},
                ])
                code, msg = self.note("delivery-done", video_delivery)
                self.assertEqual(code, TR.EXIT_HELD)
                self.assertIn("缺少独立代码评审记录", msg)
                self.assertIn("缺少独立测试角色记录", msg)

                # 3) dev 与 code-review 撞同一个 pane/session -> 拦下
                write_ledger([
                    {"issue": "#47", "profile": "dev", "name": "p-47-dev", "pane_id": "p1", "session": "s1"},
                    {"issue": "#47", "profile": "code-review", "name": "p-47-cr", "pane_id": "p1", "session": "s2"},
                    {"issue": "#47", "profile": "test", "name": "p-47-test", "pane_id": "p3", "session": "s3"},
                ])
                code, msg = self.note("delivery-done", video_delivery)
                self.assertEqual(code, TR.EXIT_HELD)
                self.assertIn("成员独立性冲突", msg)
                self.assertIn("同窗格 p1", msg)

                # 4) 纯单测交付：dev + 独立 code-review（无 test） -> 放行
                write_ledger([
                    {"issue": "#47", "profile": "dev", "name": "p-47-dev", "pane_id": "p1", "session": "s1"},
                    {"issue": "#47", "profile": "code-review", "name": "p-47-cr", "pane_id": "p2", "session": "s2"},
                ])
                code, _ = self.note("delivery-done", unit_only_delivery)
                self.assertEqual(code, TR.EXIT_OK)

                # 5) debug 模式免 code-review，但必须有独立 test（不与 dev 重合）
                TR.state_set(self.repo, TASK, "当前阶段", "debug 模式验证完成")
                write_ledger([
                    {"issue": "#47", "profile": "dev", "name": "p-47-dev", "pane_id": "p1", "session": "s1"},
                    {"issue": "#47", "profile": "test", "name": "p-47-test", "pane_id": "p3", "session": "s3"},
                ])
                code, _ = self.note("delivery-done", video_delivery + "\n\n（debug 模式交付）")
                self.assertEqual(code, TR.EXIT_OK)

                # 6) 常规交付三角色完全独立 -> 放行
                TR.state_set(self.repo, TASK, "当前阶段", "常规交付验收完成")
                write_ledger([
                    {"issue": "#47", "profile": "dev", "name": "p-47-dev", "pane_id": "p1", "session": "s1"},
                    {"issue": "#47", "profile": "code-review", "name": "p-47-cr", "pane_id": "p2", "session": "s2"},
                    {"issue": "#47", "profile": "test", "name": "p-47-test", "pane_id": "p3", "session": "s3"},
                ])
                code, _ = self.note("delivery-done", video_delivery + "\n\n（常规全独立交付）")
                self.assertEqual(code, TR.EXIT_OK)

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
        self.issue_notes: list[dict[str, Any]] = []

    def __call__(self, host: str, method: str, path: str, *fields: str) -> Any:
        if path == "user":
            return {"username": "pjhagent"}
        if "/notes?" in path:
            return list(self.issue_notes)
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


class ContentGuardTests(Case):
    def test_deterministic_secret_regex_holds_note_offline(self) -> None:
        tok = "glpat-" + "A9b8C7d6E5f4G3h2J1k0L9m8"
        code, msg = self.note("answer", f"已排障完成，临时调试令牌 {tok} 供参考")
        self.assertEqual(code, TR.EXIT_HELD)
        self.assertIn("高危私钥/Token", msg)
        self.assertEqual(self.glab.posted, [])

    def test_jev_holds_plaintext_password_and_buried_conclusion_on_long_notes(self) -> None:
        class FakeNoteJev:
            def __init__(self, secret: float, buried: float = 0.0) -> None:
                self.secret, self.buried = secret, buried

            def decide(self, *, questions: Any = None, **_: Any) -> Any:
                ans: dict[str, Any] = {"secret_exposed": {"type": "noul", "noul": self.secret}}
                if "missing_top_conclusion" in questions:
                    ans["missing_top_conclusion"] = {"type": "noul", "noul": self.buried}
                return TR._JC.JevResult.from_payload({"model": "jev-1.14.0", "answers": ans})

        code1, msg1 = TR.note(
            self.repo, TASK, ISSUE, "answer",
            "测试账号 admin@example.com 的密码是 P@ssw0rd2026!，已验证登录。",
            [], jev=FakeNoteJev(secret=0.92),
        )
        self.assertEqual(code1, TR.EXIT_HELD)
        self.assertIn("明文密码或敏感密钥", msg1)

        long_rambling = "首先我去查了 nginx 日志和网关转发配置，接着对比了三个文件的超时参数，" * 14 + "所以最后请拍板选方案 A。"
        code2, msg2 = TR.note(
            self.repo, TASK, ISSUE, "todo", long_rambling, ["none"],
            jev=FakeNoteJev(secret=0.01, buried=0.93),
        )
        self.assertEqual(code2, TR.EXIT_HELD)
        self.assertIn("先说结论，再说细节", msg2)
        self.assertEqual(self.glab.posted, [])

    def test_state_set_waiting_user_holds_when_origin_issue_has_unanswered_user_note(self) -> None:
        # 先发一条 stage 评论写入 notes.jsonl，登记原 issue URL。
        TR.state_set(self.repo, TASK, "通知模式", "详尽")
        code0, _ = self.note("stage", "进入集成验证")
        self.assertEqual(code0, TR.EXIT_OK)

        # 用户追问晚于最近实质回复（仅有一条 stage 不算答复用户）：切等待态返回 EXIT_HELD(3)。
        self.glab.issue_notes = [
            {"id": 910, "author": {"username": "pjhagent"}, "system": False, "body": "内部阶段\n<!-- hwf-kind:stage -->"},
            {"id": 909, "author": {"username": "pengjh"}, "system": False, "body": "这个开关会影响存量画布吗？"},
            {"id": 905, "author": {"username": "pjhagent"}, "system": False, "body": "前期确认\n<!-- hwf-kind:todo -->"},
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            rc_wait = TR.main([
                "state-set", "--repo", self.repo, "--task", TASK,
                "--field", "待用户事项", "--value", "请合并 release MR",
            ])
            rc_stage = TR.main([
                "state-set", "--repo", self.repo, "--task", TASK,
                "--field", "当前阶段", "--value", "状态:待上线",
            ])
        self.assertEqual(rc_wait, TR.EXIT_HELD)
        self.assertEqual(rc_stage, TR.EXIT_HELD)
        self.assertIn("尚未得到实质回复", out.getvalue())

        # 非等待用户的状态推进不受阻拦。
        self.assertEqual(
            TR.main(["state-set", "--repo", self.repo, "--task", TASK, "--field", "当前阶段", "--value", "开发中"]),
            TR.EXIT_OK,
        )

        # 补发 answer 实质答复后，切等待态正常放行。
        self.glab.issue_notes.insert(
            0, {"id": 911, "author": {"username": "pjhagent"}, "system": False, "body": "不影响存量画布。\n<!-- hwf-kind:answer -->"}
        )
        self.assertEqual(
            TR.main([
                "state-set", "--repo", self.repo, "--task", TASK,
                "--field", "待用户事项", "--value", "请合并 release MR",
            ]),
            TR.EXIT_OK,
        )

    def test_brief_html_renders_recovery_page_with_svg_four_questions_and_reply_dock(self) -> None:
        st_path = TR.state_path(self.repo, TASK)
        st_path.parent.mkdir(parents=True, exist_ok=True)
        st_path.write_text(
            f"# {TASK} 调度记录\n\n"
            f"{TR.TOP_OPEN}\n"
            "- 通知模式：免打扰\n"
            "- 当前阶段：评审与冒烟验收\n"
            "- 下一动作：等待用户确认上线顺序\n"
            "- 待用户事项：请确认是否先上线后端再合并前端 MR\n"
            "- 最近用户通知：https://git.example.com/frontend/web-v1/-/issues/484#note_10\n"
            f"{TR.TOP_CLOSE}\n\n"
            "## 关键决定\n"
            "- 理由：复用现有上传队列，不另起新协议。\n"
            "- 刻意不做：暂不重构旧版移动端入口。\n\n"
            "## 历史与坑\n"
            "- 回滚方案：如接口不兼容，先回退前端版本并保留新字段向后兼容。\n"
            "- 这次踩坑与收获：上传前校验写席位，避免无权限用户静默失败。\n",
            encoding="utf-8",
        )
        (st_path.parent / "reviews").mkdir(exist_ok=True)
        (st_path.parent / "reviews" / "r1.md").write_text("通过\n", encoding="utf-8")
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            rc = TR.main(["brief-html", "--repo", self.repo, "--task", TASK])
        self.assertEqual(rc, TR.EXIT_OK)
        brief_path = Path(out_buf.getvalue().strip())
        self.assertTrue(brief_path.is_file())
        doc = brief_path.read_text(encoding="utf-8")
        self.assertIn("30 秒恢复现场", doc)
        self.assertIn("刻意不做：暂不重构旧版移动端入口", doc)
        self.assertIn("4. 核心四问自检", doc)
        self.assertIn("一键复制回复指令", doc)
        self.assertIn("r1.md", doc)
        # 结构 SVG 遵守透明底主题变量铁线（仅允许 url(#arrow)）
        svg_part = doc.split("<svg", 1)[1].split("</svg>", 1)[0]
        self.assertNotIn("#", svg_part.replace("url(#arrow)", ""))


if __name__ == "__main__":
    unittest.main()
