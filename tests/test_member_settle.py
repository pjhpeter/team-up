"""聚焦验证成员结果收取：优先取会话记录里的原始 Markdown，取不到或对不上才退回屏幕文字。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "member_settle", Path(__file__).resolve().parents[1] / "scripts/member-settle.py"
)
assert SPEC is not None and SPEC.loader is not None
SETTLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SETTLE
SPEC.loader.exec_module(SETTLE)


def claude(text: str, message_id: str = "m1", **extra: object) -> dict:
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant",
                                             "content": [{"type": "text", "text": text}]}, **extra}


def omp(text: str, record_id: str = "r1") -> dict:
    return {"type": "message", "id": record_id, "message": {"role": "assistant",
                                                            "content": [{"type": "text", "text": text}]}}


def codex(text: str, message_id: str = "c1") -> dict:
    return {"type": "response_item", "payload": {"type": "message", "id": message_id, "role": "assistant",
                                                 "content": [{"type": "output_text", "text": text}]}}


class SessionCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, *records: object) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class LastReplyTest(SessionCase):
    def test_each_agent_format(self) -> None:
        for record in (claude("## 结论"), omp("## 结论"), codex("## 结论")):
            self.assertEqual(SETTLE.last_reply(str(self.write("s.jsonl", record))), "## 结论")

    def test_takes_last_reply_not_earlier_ones(self) -> None:
        path = self.write("s.jsonl", claude("先看代码", "m1"), {"type": "user", "message": {"role": "user"}},
                          claude("最终结论", "m2"))
        self.assertEqual(SETTLE.last_reply(str(path)), "最终结论")

    def test_joins_records_of_same_message(self) -> None:
        path = self.write("s.jsonl", claude("上半", "m1"), claude("下半", "m1"))
        self.assertEqual(SETTLE.last_reply(str(path)), "上半下半")

    def test_skips_subagent_and_bad_lines(self) -> None:
        path = self.write("s.jsonl", claude("成员回复", "m1"), claude("子代理", "m2", isSidechain=True), '{"半行', "[1]")
        self.assertEqual(SETTLE.last_reply(str(path)), "成员回复")

    def test_tool_only_record_does_not_replace_reply(self) -> None:
        tool = {"type": "assistant", "message": {"id": "m2", "role": "assistant",
                                                 "content": [{"type": "tool_use", "name": "Bash"}]}}
        self.assertEqual(SETTLE.last_reply(str(self.write("s.jsonl", claude("回复"), tool))), "回复")


class SessionFileTest(SessionCase):
    def test_path_kind(self) -> None:
        path = self.write("omp.jsonl", omp("x"))
        self.assertEqual(SETTLE.session_file({"agent": "omp", "kind": "path", "value": str(path)}), str(path))
        self.assertIsNone(SETTLE.session_file({"agent": "omp", "kind": "path", "value": str(self.dir / "无")}))

    def test_claude_and_codex_ids(self) -> None:
        cl = self.write("claude/projects/-x/abc.jsonl", claude("x"))
        co = self.write("codex/sessions/2026/11/05/rollout-2026-abc.jsonl", codex("x"))
        env = {"CLAUDE_CONFIG_DIR": str(self.dir / "claude"), "CODEX_HOME": str(self.dir / "codex")}
        with patch.dict(os.environ, env):
            self.assertEqual(SETTLE.session_file({"agent": "claude", "kind": "id", "value": "abc"}), str(cl))
            self.assertEqual(SETTLE.session_file({"agent": "codex", "kind": "id", "value": "abc"}), str(co))
            self.assertIsNone(SETTLE.session_file({"agent": "other", "kind": "id", "value": "abc"}))


    def test_droid_id_and_record_format(self) -> None:
        reply = {"type": "message", "id": "d2", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "想"}, {"type": "text", "text": "## 结论"}]}}
        tool = {"type": "message", "id": "d3", "message": {"role": "assistant", "content": [{"type": "tool_use"}]}}
        path = self.write("home/.factory/sessions/-Users-x-repo/abc.jsonl", reply, tool,
                          {"type": "agent_turn_outcome", "reason": "completed"})
        with patch.dict(os.environ, {"HOME": str(self.dir / "home")}):
            found = SETTLE.session_file({"agent": "droid", "kind": "id", "value": "abc"})
        self.assertEqual(found, str(path))
        self.assertEqual(SETTLE.last_reply(found), "## 结论")


class OnScreenTest(unittest.TestCase):
    def test_matches_hard_wrapped_screen_without_markup(self) -> None:
        reply = "## 结论\n\n**不一定要改**，取决于 `html` 入口指什么，以及 Agent 怎么转换。\n\n完成"
        screen = "⏺ 结论\n\n  不一定要改，取决于 html 入口\n  指什么，以及 Agent 怎么转换。\n\n  完成\n"
        self.assertTrue(SETTLE.on_screen(reply, screen))

    def test_rejects_older_reply(self) -> None:
        self.assertFalse(SETTLE.on_screen("上一轮的结论写在这里", "这一轮还没写完\n"))

    def test_table_only_reply_is_not_rejected(self) -> None:
        self.assertTrue(SETTLE.on_screen("| a | b |\n|---|---|\n| 1 | 2 |", "┌──┐\n"))


class CommentBodyTest(unittest.TestCase):
    def test_summary_first_and_full_folded(self) -> None:
        body, _ = SETTLE.comment_body("## 给用户的摘要\n\n能做，选 A。\n\n## 完整内容\n\n| a |\n|---|\n\n完成\n")
        self.assertEqual(body, "能做，选 A。\n\n<details><summary>完整内容</summary>\n\n| a |\n|---|\n\n完成\n\n</details>\n")

    def test_short_reply_used_as_is(self) -> None:
        self.assertEqual(SETTLE.comment_body("一句话结论\n\n完成")[0], "一句话结论\n\n完成\n")

    def test_long_reply_without_summary_is_refused(self) -> None:
        body, why = SETTLE.comment_body("\n".join(f"第 {i} 行" for i in range(16)))
        self.assertIsNone(body)
        self.assertIn("给用户的摘要", why)

    def test_too_long_summary_is_refused(self) -> None:
        summary = "\n".join(f"第 {i} 行" for i in range(16))
        self.assertIsNone(SETTLE.comment_body(f"## 给用户的摘要\n{summary}\n## 完整内容\n全文")[0])

    def test_heading_level_and_bold_variants_are_accepted(self) -> None:
        full_lines = "\n".join(f"详情 {i}" for i in range(20))
        for summary_head, full_head in (
            ("### 给用户的摘要：", "### 完整内容"),
            ("**用户摘要**：", "**完整内容：**"),
            ("#### 给用户摘要", "## 完整内容："),
        ):
            body, why = SETTLE.comment_body(f"{summary_head}\n\n简短结论。\n\n{full_head}\n\n{full_lines}\n")
            self.assertIsNotNone(body, msg=f"failed on {summary_head} / {full_head}: {why}")
            self.assertTrue(body.startswith("简短结论。\n\n<details><summary>完整内容</summary>"))


class MainTest(SessionCase):
    def settle(self, session: object, screen: str) -> tuple[str, Path]:
        info = {"agent_status": "idle"}
        if session is not None:
            info["agent_session"] = session

        def fake_run(*args: str) -> str:
            return json.dumps({"result": {"agent": info}}) if args[1] == "get" else screen

        out = self.dir / "reviews" / "r1.md"
        with patch.object(SETTLE, "run", fake_run), patch.object(SETTLE.time, "sleep"), \
                patch.object(sys, "argv", ["member-settle", "arch", str(out)]), \
                patch("builtins.print") as printed:
            self.assertEqual(SETTLE.main(), 0)
        return "\n".join(str(c.args[0]) for c in printed.call_args_list), out

    def test_writes_raw_markdown_and_keeps_screen(self) -> None:
        path = self.write("s.jsonl", claude("| a |\n|---|\n\n## 结论写在这一行\n\n通过"))
        printed, out = self.settle({"agent": "omp", "kind": "path", "value": str(path)}, "⏺ 结论写在这一行\n通过\n")
        self.assertEqual(out.read_text(encoding="utf-8"), "| a |\n|---|\n\n## 结论写在这一行\n\n通过\n")
        self.assertEqual(Path(f"{out}.screen").read_text(encoding="utf-8"), "⏺ 结论写在这一行\n通过\n")
        self.assertIn("format: markdown", printed)
        self.assertIn("verdict line: 通过", printed)
        self.assertEqual(Path(f"{out}.comment.md").read_text(encoding="utf-8"), out.read_text(encoding="utf-8"))

    def test_falls_back_to_screen_and_says_why(self) -> None:
        for session in (None, {"agent": "omp", "kind": "path", "value": str(self.dir / "无")}):
            printed, out = self.settle(session, "屏幕文字\n")
            self.assertEqual(out.read_text(encoding="utf-8"), "屏幕文字\n")
            self.assertIn("format: screen", printed)

    def test_stale_reply_falls_back_to_screen(self) -> None:
        path = self.write("s.jsonl", claude("上一轮的旧结论"))
        printed, out = self.settle({"agent": "omp", "kind": "path", "value": str(path)}, "新一轮的屏幕\n")
        self.assertEqual(out.read_text(encoding="utf-8"), "新一轮的屏幕\n")
        self.assertIn("format: screen", printed)

    def test_working_to_idle_transition_rechecks_once_and_finishes(self) -> None:
        path = self.write("s.jsonl", claude("最终结论\n\n通过"))
        session = {"agent": "omp", "kind": "path", "value": str(path)}
        statuses = iter(["working", "idle", "idle"])
        current_status = "working"
        sleeps: list[int] = []

        def fake_run(*args: str) -> str:
            nonlocal current_status
            if args[1] == "get":
                current_status = next(statuses)
                return json.dumps({"result": {"agent": {"agent_status": current_status, "agent_session": session}}})
            if current_status == "working" and "recent-unwrapped" in args:
                raise RuntimeError("herdr agent read failed: agent_not_idle")
            if current_status == "working":
                return "正在输出...\n\n\n\n状态栏\n"
            return "⏺ 最终结论\n通过\n\n\n\n状态栏\n"

        out = self.dir / "reviews" / "transition.md"
        with patch.object(SETTLE, "run", fake_run), patch.object(SETTLE.time, "sleep", sleeps.append), \
                patch.object(sys, "argv", ["member-settle", "arch", str(out)]), \
                patch("builtins.print") as printed:
            self.assertEqual(SETTLE.main(), 0)
        output = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertEqual(sleeps, [15, 15])
        self.assertIn("status: idle -> idle | screen stable: yes | finished: yes", output)
        self.assertIn("verdict line: 通过", output)


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_bytes(field_num: int, data: bytes) -> bytes:
    return _pb_varint((field_num << 3) | 2) + _pb_varint(len(data)) + data


def _agy_reply_step(text: str | None = None, thinking: str = "") -> bytes:
    inner = bytearray()
    if text is not None:
        inner.extend(_pb_bytes(1, text.encode("utf-8")))
    if thinking:
        inner.extend(_pb_bytes(3, thinking.encode("utf-8")))
    return _pb_bytes(20, bytes(inner))


class AgySessionTest(SessionCase):
    def _create_agy_db(self, root: Path, cid: str, steps: list[tuple[int, int, bytes]]) -> Path:
        db_path = root / "conversations" / f"{cid}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, step_payload blob)")
        conn.executemany("INSERT INTO steps (idx, step_type, step_payload) VALUES (?, ?, ?)", steps)
        conn.commit()
        conn.close()
        return db_path

    def test_extracts_agy_markdown_via_cwd_cache_and_summaries_fallback(self) -> None:
        root = self.dir / "agy"
        work_dir = self.dir / "repo"
        work_dir.mkdir()
        real_work = os.path.realpath(work_dir)
        # 最新会话只有一轮旧回复，次新会话才是当前屏幕上的回复（验证 on_screen 校验与候选回退）
        cid_new = "conv-newer"
        cid_match = "conv-match"
        self._create_agy_db(root, cid_new, [(1, 15, _agy_reply_step("别的窗格的回复内容"))])
        reply_md = "## 给用户的摘要\n\n**方案可行**，已核对 `config.yaml`。\n\n## 完整内容\n\n| 项 | 值 |\n|---|---|\n| 模型 | Neon |\n\n通过"
        match_db = self._create_agy_db(root, cid_match, [
            (0, 14, b""),
            (1, 15, _agy_reply_step("上一轮草稿")),
            (2, 15, _agy_reply_step(reply_md, thinking="thought")),
            (3, 15, _agy_reply_step(None, thinking="tool call only")),
        ])

        cache = root / "cache" / "last_conversations.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({real_work: cid_new}), encoding="utf-8")

        summary_db = root / "conversation_summaries.db"
        sconn = sqlite3.connect(summary_db)
        sconn.execute("CREATE TABLE conversation_summaries (conversation_id text, workspace_uris text, last_modified_time text)")
        sconn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            (cid_new, json.dumps([f"file://{real_work}"]), "2026-11-26 10:00:00+00:00"),
        )
        sconn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
            (cid_match, json.dumps([f"file://{real_work}"]), "2026-11-26 09:59:00+00:00"),
        )
        sconn.commit()
        sconn.close()

        screen = "  给用户的摘要\n\n  方案可行，已核对 config.yaml。\n\n  完整内容\n\n  通过\n"
        with patch.dict(os.environ, {"AGY_CONFIG_DIR": str(root)}):
            reply, source = SETTLE.read_reply({"agent": "agy", "cwd": str(work_dir)}, screen)
            self.assertEqual(reply, reply_md)
            self.assertEqual(source, str(match_db))

            # 未来 Herdr 直接返回 agent_session 时也能定位
            found = SETTLE.session_file({"agent": "agy", "kind": "id", "value": cid_match})
            self.assertEqual(found, str(match_db))
            self.assertEqual(SETTLE.last_reply(found), reply_md)

            # 屏幕与所有候选会话都对不上时安全退回屏幕文字
            miss, reason = SETTLE.read_reply({"agent": "agy", "cwd": str(work_dir)}, "完全不相关的屏幕文字\n")
            self.assertIsNone(miss)
            self.assertIn("与屏幕对不上", reason)


if __name__ == "__main__":
    unittest.main()
