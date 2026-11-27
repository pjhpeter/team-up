import importlib.util
import json
from datetime import datetime
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


def load_module(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


retro_mod = load_module("retrospect", "retrospect.py")
inbox_mod = load_module("gitlab_inbox_for_retro", "gitlab-inbox.py")


def ts_of(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, 0).timestamp()


SAMPLE_MODEL_JSON = json.dumps(
    {
        "headline": "本周期整体交付顺畅，主要摩擦在飞书桥失效群重复补读与个别任务评审多轮往返。",
        "problem_plain": "机器人已被移出某个旧群，但每次重连还在尝试补读该群历史，白白刷报错日志。",
        "solution_plain": "遇到飞书 230002 不在群内错误时自动清理该群历史游标，不再每轮空转。",
        "why_plain": "减少无效接口请求和报错噪音，避免掩盖真正需要人工处理的桥接故障。",
        "items": [
            {
                "priority": "P1",
                "area": "飞书桥",
                "title": "清理已退群的补读游标",
                "symptom_and_evidence": "本周期 oc_dead 群触发 2 次 230002 Bot/User can NOT be out of the chat",
                "root_cause": "feishu-bridge.py poll_history 未在 230002 时移除失效群游标",
                "action": "在 feishu-bridge.py 捕获 230002 时清理对应 history_cursors 键",
                "effort": "小",
            },
            {
                "priority": "P2",
                "area": "收件箱",
                "title": "拉取长评论列表偶发超时",
                "symptom_and_evidence": "收件箱轮询 issues/496/notes 出现 1 次 30s 超时",
                "root_cause": "单页拉取 100 条大体积评论偶发网络抖动",
                "action": "保持增量容错并观察是否集中在特定大工单",
                "effort": "小",
            },
        ],
    },
    ensure_ascii=False,
)


class RetrospectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True)
        self.repo_dir = self.root / "web-v1"
        (self.repo_dir / ".hwf" / "tmp").mkdir(parents=True)
        self.files = retro_mod.Files(self.state_dir)
        self.cfg = {
            "host": "git.example",
            "allowed_users": ["pengjh"],
            "projects": {"frontend/web-v1": str(self.repo_dir)},
            "retrospect": {
                "enabled": True,
                "weekdays": [1, 4],  # Tue=1, Fri=4
                "hour": 12,
                "minute": 0,
                "model": "gemini-4-carbon",
                "reasoning_effort": "high",
                "max_attempts_per_slot": 3,
                "retry_backoff_s": 600,
            },
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_due_slots_tuesday_and_friday_noon(self) -> None:
        # 2026-11-23 is Monday -> latest due slot is Fri 2026-11-20 12:00, prev is Tue 2026-11-17 12:00
        mon_ts = ts_of(2026, 11, 23, 13, 0)
        (cur_id, _), (prev_id, _) = retro_mod.due_slots(mon_ts, [1, 4], 12, 0)
        self.assertEqual(cur_id, "2026-11-20-12:00")
        self.assertEqual(prev_id, "2026-11-17-12:00")

        # Tue 2026-11-24 11:59 -> still Fri 2026-11-20 12:00
        before_noon = ts_of(2026, 11, 24, 11, 59)
        (cur_id2, _), _ = retro_mod.due_slots(before_noon, [1, 4], 12, 0)
        self.assertEqual(cur_id2, "2026-11-20-12:00")

        # Tue 2026-11-24 12:00 -> flips to Tue 2026-11-24 12:00
        at_noon = ts_of(2026, 11, 24, 12, 0)
        (cur_id3, _), (prev_id3, _) = retro_mod.due_slots(at_noon, [1, 4], 12, 0)
        self.assertEqual(cur_id3, "2026-11-24-12:00")
        self.assertEqual(prev_id3, "2026-11-20-12:00")

    def test_first_boot_sets_watermark_then_runs_on_tuesday_and_catches_up_after_sleep(self) -> None:
        calls: list[list[str]] = []
        ntfy_sent: list[tuple[str, str]] = []
        feishu_sent: list[str] = []
        self.files.feishu_state.write_text(json.dumps({"secretary": {"chat_id": "oc_sec_1"}}))

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            exe = Path(cmd[0]).name
            if exe == "droid":
                self.assertIn("exec", cmd)
                self.assertIn("gemini-4-carbon", cmd)
                self.assertIn("high", cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout=f"```json\n{SAMPLE_MODEL_JSON}\n```", stderr="")
            if exe == "lark-cli":
                feishu_sent.append(kwargs.get("input", ""))
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true, "data": {"message_id": "om_1"}}', stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not mocked")

        now_box = [ts_of(2026, 11, 23, 14, 0)]  # Monday afternoon
        retro = retro_mod.Retrospect(
            self.cfg,
            files=self.files,
            run=fake_run,
            now=lambda: now_box[0],
            hwf_home=self.root,
            send_ntfy_fn=lambda t, b: (ntfy_sent.append((t, b)) or True),
        )

        # 1. Monday first tick only initializes watermark, does NOT invoke droid
        self.assertIsNone(retro.tick())
        self.assertEqual(retro.state["last_slot"], "2026-11-20-12:00")
        self.assertEqual(len([c for c in calls if Path(c[0]).name == "droid"]), 0)

        # 2. Laptop slept over Tue 12:00, woke up Tue 15:30 -> catches up once!
        now_box[0] = ts_of(2026, 11, 24, 15, 30)
        res = retro.tick()
        self.assertIsNotNone(res)
        self.assertEqual(retro.state["last_slot"], "2026-11-24-12:00")
        self.assertEqual(len(feishu_sent), 1)
        self.assertIn("清理已退群的补读游标", feishu_sent[0])
        self.assertEqual(len(ntfy_sent), 1)
        self.assertTrue(self.files.latest_report.exists())

        # HTML checks: plain language sections present, SVG has no hardcoded hex colors
        html_doc = self.files.latest_report.read_text()
        self.assertIn("遇到了什么问题", html_doc)
        self.assertIn("打算怎么解决", html_doc)
        self.assertIn("为什么这样做", html_doc)
        svg_part = html_doc.split("<svg", 1)[1].split("</svg>", 1)[0]
        self.assertNotIn("#", svg_part.replace("url(#arrow)", ""))

        # 3. Subsequent tick on same Tuesday does not re-run
        now_box[0] = ts_of(2026, 11, 24, 16, 0)
        self.assertIsNone(retro.tick())
        self.assertEqual(len(feishu_sent), 1)

    def test_feishu_failure_retries_notify_without_rerunning_droid(self) -> None:
        droid_runs = 0
        lark_ok = [False]
        self.files.feishu_state.write_text(json.dumps({"secretary": {"chat_id": "oc_sec_1"}}))
        self.files.save_state({"last_slot": "2026-11-20-12:00", "last_run_at": ts_of(2026, 11, 20, 12, 1)})

        def fake_run(cmd, **kwargs):
            nonlocal droid_runs
            exe = Path(cmd[0]).name
            if exe == "droid":
                droid_runs += 1
                return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_MODEL_JSON, stderr="")
            if exe == "lark-cli":
                if not lark_ok[0]:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        retro = retro_mod.Retrospect(
            self.cfg,
            files=self.files,
            run=fake_run,
            now=lambda: ts_of(2026, 11, 24, 12, 2),
            hwf_home=self.root,
            send_ntfy_fn=lambda t, b: True,
        )
        retro.tick()
        self.assertEqual(droid_runs, 1)
        self.assertIsNotNone(retro.state["pending_notify"])
        self.assertFalse(retro.state["pending_notify"]["feishu_done"])

        # Next tick retries Feishu only, without invoking droid a second time
        lark_ok[0] = True
        retro.tick()
        self.assertEqual(droid_runs, 1)
        self.assertIsNone(retro.state["pending_notify"])

    def test_collect_digest_extracts_task_inbox_and_feishu_metrics(self) -> None:
        now = ts_of(2026, 11, 24, 12, 0)
        since = ts_of(2026, 11, 20, 12, 0)

        tdir = self.repo_dir / ".hwf" / "tmp" / "frontend-web-v1-499"
        (tdir / "reviews").mkdir(parents=True)
        (tdir / "testing" / "r1").mkdir(parents=True)
        (tdir / "STATE.md").write_text(
            "# #499\n<!-- hwf:top -->\n- 通知模式：免打扰\n- 当前阶段：测试中\n- 待用户事项：无\n<!-- /hwf:top -->\n"
        )
        (tdir / "reviews" / "rev1.md").write_text("ok")
        (tdir / "testing" / "r1" / "rec.mp4").write_text("vid")
        (tdir / "notes.jsonl").write_text(
            json.dumps({"at": "2026-11-23 10:00:00", "kind": "answer"}) + "\n"
            + json.dumps({"at": "2026-11-23 11:00:00", "kind": "todo"}) + "\n"
        )
        fixture_ts = ts_of(2026, 11, 23, 15, 0)
        for fp in (tdir / "STATE.md", tdir / "reviews" / "rev1.md", tdir / "testing" / "r1" / "rec.mp4", tdir / "notes.jsonl"):
            os.utime(fp, (fixture_ts, fixture_ts))

        self.files.feishu_events_log.write_text(
            "2026-11-23T11:00:00 created frontend/web-v1#501 from om_1 by pengjh\n"
            "2026-11-23T11:05:00 intent failed mid=om_2 reason=timeout\n"
            "2026-11-23T11:10:00 catch-up skip chat oc_dead: code 230002\n"
        )
        self.files.inbox_events_log.write_text(
            "2026-11-23T11:20:00 poll failed: Command timed out after 30 seconds\n"
            "2026-11-23T11:25:00 prompt to w1:p1 rejected: busy\n"
        )

        digest = retro_mod.collect_digest(
            self.cfg,
            self.files,
            since,
            now,
            run=lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
            hwf_home=self.root,
            omp_root=self.root / "no-omp",
            worktrees_root=self.root / "no-wt",
        )
        self.assertEqual(digest["delivery"]["active_tasks_count"], 1)
        task = digest["delivery"]["active_tasks"][0]
        self.assertEqual(task["notes_by_kind"], {"answer": 1, "todo": 1})
        self.assertEqual(task["reviews_touched"], 1)
        self.assertEqual(task["testing_rounds_touched"], 1)
        self.assertEqual(digest["feishu_bridge"]["log_summary"]["created_issues"], 1)
        self.assertEqual(digest["feishu_bridge"]["log_summary"]["intent_failed"], 1)
        self.assertEqual(digest["feishu_bridge"]["log_summary"]["unreachable_chats_repeated"], {"oc_dead": 1})
        self.assertEqual(digest["gitlab_inbox"]["log_summary"]["poll_timeouts"], 1)
        self.assertEqual(digest["gitlab_inbox"]["log_summary"]["prompt_rejected"], 1)

    def test_inbox_watched_scripts_includes_retrospect_and_jev_gates(self) -> None:
        names = [p.name for p in inbox_mod.watched_scripts()]
        self.assertIn("retrospect.py", names)
        self.assertIn("jev_gates.py", names)

    def test_execute_review_auto_evolves_jev_gates_locks_redlines_and_renders_report(self) -> None:
        import shutil
        shutil.copy2(Path(__file__).resolve().parents[1] / "jev-gates.json", self.root / "jev-gates.json")
        self.files.feishu_state.write_text(json.dumps({"secretary": {"chat_id": "oc_sec_1"}}))

        now = ts_of(2026, 11, 24, 12, 5)
        since = ts_of(2026, 11, 20, 12, 0)
        tdir = self.repo_dir / ".hwf" / "tmp" / "frontend-web-v1-520"
        (tdir / "reviews").mkdir(parents=True)
        rev_md = tdir / "reviews" / "r1.md"
        rev_md.write_text("结论：需修改 — 新增硬编码 localhost:3000 调试地址未清理\n", encoding="utf-8")
        fixture_ts = ts_of(2026, 11, 23, 16, 0)
        os.utime(rev_md, (fixture_ts, fixture_ts))

        model_payload = json.dumps(
            {
                "headline": "本周期关卡拦截稳定，已将重复出现的调试地址硬编码沉淀为关卡 4 自动检查项。",
                "problem_plain": "开发提交代码时两次把本地 localhost 调试地址写死在前端生产配置里，到人工评审才打回。",
                "solution_plain": "在审前铁律预检清单自动增加本地调试地址检查项，提审前直接提醒改掉。",
                "why_plain": "省掉来回派评审员抓同类低级疏漏的时间。",
                "items": [
                    {
                        "priority": "P2",
                        "area": "Jev关卡",
                        "title": "审前预检补齐调试地址硬编码检查",
                        "symptom_and_evidence": "本周期任务 520 等 2 次评审均因硬编码 localhost 退回",
                        "root_cause": "jev-gates.json 尚未收录硬编码 localhost 生产配置检查",
                        "action": "已通过本轮 gate_updates 自动加入 gate4_semantic_lint 清单",
                        "effort": "小",
                    }
                ],
                "gate_updates": [
                    {
                        "op": "add_rule",
                        "target": "gate4_semantic_lint",
                        "rule_id": "hardcoded_localhost_prod",
                        "threshold": 0.82,
                        "evidence_count": 2,
                        "title_zh": "生产代码硬编码 localhost 调试地址",
                        "fix_hint_zh": "改读环境变量或运行时配置，不要在生产代码中写死 localhost/127.0.0.1。",
                        "instructions_en": "Inspect added lines in production code (ignore tests/dev configs). Does it hardcode localhost or 127.0.0.1 as a production endpoint?",
                        "true_en": "Hardcodes localhost/127.0.0.1 in production endpoint logic.",
                        "false_en": "Uses config/env fallback or appears only in tests/local dev config.",
                        "reason": "本周期 2 次代码评审因硬编码 localhost 调试地址打回",
                    },
                    {
                        "op": "tune_threshold",
                        "target": "thresholds",
                        "rule_id": "proactive_same_as_min",
                        "new_threshold": 0.78,
                        "reason": "收紧飞书同话题归并阈值，减少相邻不同话题误并",
                    },
                    {
                        "op": "retire_rule",
                        "target": "gate4_semantic_lint",
                        "rule_id": "feature_flag",
                        "reason": "测试试图淘汰受保护红线规则",
                    },
                ],
            },
            ensure_ascii=False,
        )

        feishu_msgs: list[str] = []
        git_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            exe = Path(cmd[0]).name
            if exe == "droid":
                return subprocess.CompletedProcess(cmd, 0, stdout=model_payload, stderr="")
            if exe == "lark-cli":
                feishu_msgs.append(kwargs.get("input", ""))
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true, "data": {"message_id": "om_2"}}', stderr="")
            if exe == "git":
                git_cmds.append(list(cmd))
                if "diff" in cmd and "--cached" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if "rev-parse" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout="a1b2c3d\n", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        retro = retro_mod.Retrospect(
            self.cfg,
            files=self.files,
            run=fake_run,
            now=lambda: now,
            hwf_home=self.root,
            send_ntfy_fn=lambda t, b: True,
        )
        res = retro.execute_review(
            since,
            now,
            slot_id="2026-11-24-12:00",
            notify=True,
            verify_gates_fn=lambda _: (True, "ok"),
        )

        evo = res["gate_evolution"]
        self.assertEqual(len(evo["applied"]), 2)
        self.assertEqual(len(evo["rejected"]), 1)
        self.assertEqual(evo["rejected"][0]["rule_id"], "feature_flag")
        self.assertTrue(evo["git"]["committed"])
        self.assertEqual(evo["git"]["sha"], "a1b2c3d")
        self.assertTrue(any("push" in c and "origin" in c for c in git_cmds))
        self.assertTrue(any("push" in c and "github" in c for c in git_cmds))

        updated_cfg = json.loads((self.root / "jev-gates.json").read_text(encoding="utf-8"))
        g4_ids = {r["rule_id"] for r in updated_cfg["gate4_semantic_lint"]}
        self.assertIn("hardcoded_localhost_prod", g4_ids)
        self.assertIn("feature_flag", g4_ids)
        self.assertAlmostEqual(updated_cfg["thresholds"]["proactive_same_as_min"]["value"], 0.78)

        self.assertEqual(len(feishu_msgs), 1)
        self.assertIn("Jev 关卡本轮已自动更新并过单测", feishu_msgs[0])
        self.assertIn("hardcoded_localhost_prod", feishu_msgs[0])

        html_doc = self.files.latest_report.read_text(encoding="utf-8")
        self.assertIn("Jev 关卡本轮自动演进记录", html_doc)
        self.assertIn("hardcoded_localhost_prod", html_doc)
        self.assertIn("护栏拦截", html_doc)
        self.assertIn("复制待办指令给 Agent", html_doc)
        self.assertIn("本周期核心四问自检", html_doc)
        self.assertIn("data-act='do'", html_doc)
        svg_part = html_doc.split("<svg", 1)[1].split("</svg>", 1)[0]
        self.assertNotIn("#", svg_part.replace("url(#arrow)", ""))

    def test_collect_gitlab_issues_fetches_notes_desc_paginated_for_long_threads(self) -> None:
        since_ts = ts_of(2026, 11, 24, 12, 0)
        now_ts = ts_of(2026, 11, 27, 12, 0)
        queries: list[str] = []

        # 模拟超过 100 条系统动态的长工单：第 1 页含最新用户问题与 Agent 实质回复 + 98 条系统动态，
        # 第 2 页含更早期的窗口内互动；若仍用旧版 sort=asc&per_page=80 会完全截断掉最新回复并误报漏回。
        page1 = [
            {
                "id": 502,
                "system": False,
                "created_at": "2026-11-27T10:15:00+08:00",
                "author": {"username": "pjhagent"},
                "body": "已完成验证并附录屏证据。",
            },
            {
                "id": 501,
                "system": False,
                "created_at": "2026-11-27T10:00:00+08:00",
                "author": {"username": "pengjh"},
                "body": "请补充一下验证结果",
            },
        ] + [
            {
                "id": 500 - i,
                "system": True,
                "created_at": "2026-11-26T12:00:00+08:00",
                "author": {"username": "pjhagent"},
                "body": "changed labels",
            }
            for i in range(98)
        ]
        page2 = [
            {
                "id": 301,
                "system": False,
                "created_at": "2026-11-23T09:05:00+08:00",
                "author": {"username": "pjhagent"},
                "body": "窗口前已答复",
            },
            {
                "id": 300,
                "system": False,
                "created_at": "2026-11-23T09:00:00+08:00",
                "author": {"username": "pengjh"},
                "body": "窗口前的早期留言",
            },
        ]

        def fake_run(cmd, **kwargs):
            path = cmd[-1]
            queries.append(path)
            if path.endswith("/issues/504"):
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "iid": 504, "title": "长评论单", "state": "opened",
                        "labels": ["状态:待上线"], "web_url": "https://git.example/frontend/web-v1/-/issues/504",
                    }),
                    stderr="",
                )
            if "page=2" in path:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(page2), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(page1), stderr="")

        rows = retro_mod._collect_gitlab_issues(
            "git.example",
            {"frontend/web-v1": str(self.repo_dir)},
            {"pengjh"},
            {"frontend/web-v1": {504}},
            since_ts,
            now_ts,
            fake_run,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["user_notes_in_window"], 1)
        self.assertEqual(row["agent_substantive_notes_in_window"], 1)
        self.assertIsNone(row["unanswered_user_note_id"])
        self.assertAlmostEqual(row["reply_latency_median_min"], 15.0, places=1)
        note_queries = [q for q in queries if "/notes?" in q]
        self.assertTrue(any("sort=desc&per_page=100" in q for q in note_queries))
        self.assertTrue(any("page=2" in q for q in note_queries))


if __name__ == "__main__":
    unittest.main()
