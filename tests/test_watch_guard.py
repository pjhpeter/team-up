"""验证 omp 监听守卫：拦下本调度者未挂 member-wait 的活跃成员，忽略其他调度者及归属他人的成员。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

EXT = Path(__file__).resolve().parents[1] / "omp-extensions/hwf-scheduler-watch-guard.ts"


class WatchGuardTest(unittest.TestCase):
    def test_ignores_peer_schedulers_and_other_schedulers_members(self) -> None:
        # CI 镜像自带的 Node 20 不支持直接 import .ts 后缀；扩展源码本身是纯 ESM JS，拷为临时 .mjs 兼容各版本 Node。
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "hwf-scheduler-watch-guard.mjs"
            shutil.copyfile(EXT, mod)
            script = f"""
import {{ analyzeSession, findUnwatched }} from {json.dumps(mod.as_uri())};

const lines = [
  JSON.stringify({{
    type: "message",
    message: {{
      role: "assistant",
      content: [
        {{ type: "toolCall", id: "c1", arguments: {{ command: "herdr agent prompt delivery-scheduler-w18-p73 'sync'" }} }},
        {{ type: "toolCall", id: "c2", arguments: {{ command: "herdr agent prompt w18:p73 'sync by pane'" }} }},
        {{ type: "toolCall", id: "c3", arguments: {{ command: "herdr agent prompt i487-dev-fe 'other team'" }} }},
        {{ type: "toolCall", id: "c4", arguments: {{ command: "herdr agent prompt i484-dev-fe 'my member'" }} }},
        {{ type: "toolCall", id: "c5", arguments: {{ command: "herdr agent prompt i484-arch 'watched member'" }} }},
        {{ type: "toolCall", id: "c6", arguments: {{ command: "herdr agent prompt zsh 'shell pane'" }} }},
        {{ type: "toolCall", id: "c7", arguments: {{ command: "herdr agent prompt i487-test 'peer issue unregistered'" }} }},
        {{ type: "toolCall", id: "w1", arguments: {{ command: "python3 ~/.hwf/scripts/member-wait.py i484-arch --repo /r --task t" }} }},
      ],
    }},
  }}),
  JSON.stringify({{
    type: "message",
    message: {{ role: "toolResult", toolCallId: "w1", content: "Backgrounded as job bg_101" }},
  }}),
].join("\\n");

const parsed = analyzeSession(lines);
const agents = [
  {{ name: "delivery-scheduler-w18-p6z", pane_id: "w18:p6Z", agent_status: "working" }},
  {{ name: "delivery-scheduler-w18-p73", pane_id: "w18:p73", agent_status: "working" }},
  {{ name: "i487-dev-fe", pane_id: "w18:pBQ", agent_status: "working" }},
  {{ name: "i484-dev-fe", pane_id: "w18:pAN", agent_status: "working" }},
  {{ name: "i484-arch", pane_id: "w18:pBB", agent_status: "working" }},
  {{ name: "zsh", pane_id: "w18:pSH", agent_status: "working" }},
  {{ name: "i487-test", pane_id: "w18:pBT", agent_status: "working" }},
];
const registry = {{
  "w18:pBQ": {{ scheduler: "delivery-scheduler-w18-p73", profile: "dev-fe" }},
  "w18:pAN": {{ scheduler: "delivery-scheduler-w18-p6z", profile: "dev-fe" }},
  "w18:pBB": {{ scheduler: "delivery-scheduler-w18-p6z", profile: "arch" }},
}};

const unwatched = findUnwatched(parsed, agents, "w18:p6Z", registry, "delivery-scheduler-w18-p6z");
console.log(JSON.stringify(unwatched));
"""
            res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        unwatched = json.loads(res.stdout.strip())
        self.assertEqual(unwatched, [{"name": "i484-dev-fe", "pane": "w18:pAN", "status": "working"}])

    def test_record_guard_blocks_oversized_agent_prompt_and_allows_compact_dispatch(self) -> None:
        rec_ext = EXT.parent / "hwf-scheduler-record-guard.ts"
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "hwf-scheduler-record-guard.mjs"
            shutil.copyfile(rec_ext, mod)
            script = f"""
import {{ violation }} from {json.dumps(mod.as_uri())};

const okRes = violation("bash", {{ command: "herdr agent prompt i484-dev '请按 /repo/.hwf/tmp/t/dispatch-dev.md 执行'" }});
const huge = "herdr agent prompt i484-dev '" + "x".repeat(2600) + "'";
const blockRes = violation("bash", {{ command: huge }});
console.log(JSON.stringify({{ okRes, blockRes }}));
"""
            res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout.strip())
        self.assertIsNone(out["okRes"])
        self.assertIn("2500 字", out["blockRes"])
        self.assertIn("dispatch-<角色>.md", out["blockRes"])

    def test_detects_silent_idle_stall_on_active_delivery_and_respects_exemptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mod = tmp_path / "hwf-scheduler-watch-guard.mjs"
            shutil.copyfile(EXT, mod)
            repo = tmp_path / "repo"
            task_dir = repo / ".hwf/tmp/frontend-web-v1-514"
            task_dir.mkdir(parents=True)
            index_file = repo / ".hwf/tmp/INDEX.md"
            state_file = task_dir / "STATE.md"

            # 1. 典型静默躺平：进行中交付、待用户事项：无、0 活跃成员、0 后台等待（即便他人调度者有活跃成员也不豁免）
            index_file.write_text(
                "# 任务索引\n\n| 任务标识 | 输入原文 | 调度者 | 状态 |\n|---|---|---|---|\n"
                "| `frontend-web-v1-514` | 【对话模式】#514 | `delivery-scheduler-w18-phq` / `w18:pHQ` | in-progress（P0 实测） |\n"
                "| `frontend-web-v1-499` | #499 | `delivery-scheduler-w18-phq` / `w18:pHQ` | in-progress |\n"
                "| `frontend-web-v1-499` | #499 交接 | `delivery-scheduler-w18-other` / `w18:pOT` | in-progress |\n",
                encoding="utf-8",
            )
            state_file.write_text(
                "# #514\n\n<!-- hwf:top 本段只由 task-record.py 改 -->\n"
                "- 通知模式：免打扰\n"
                "- 当前阶段：P0 暂停，等待进一步安排\n"
                "- 下一动作：暂无\n"
                "- 待用户事项：无\n"
                "- 最近用户通知：无\n"
                "<!-- /hwf:top -->\n",
                encoding="utf-8",
            )

            script = f"""
import {{ analyzeSession, findSilentStall, buildStallReason }} from {json.dumps(mod.as_uri())};
import {{ writeFileSync }} from "node:fs";

const repo = {json.dumps(str(repo))};
const statePath = {json.dumps(str(state_file))};
const indexPath = {json.dumps(str(index_file))};
const selfPane = "w18:pHQ";
const selfName = "delivery-scheduler-w18-phq";
const agentsIdle = [
  {{ name: selfName, pane_id: selfPane, agent_status: "idle", cwd: repo }},
  {{ name: "i499-dev-fe", pane_id: "w18:p99", agent_status: "working" }},
  {{ name: "i514-dev-fe", pane_id: "w18:p54", agent_status: "idle" }},
];
const registry = {{
  "w18:p99": {{ scheduler: "delivery-scheduler-w18-other", profile: "dev-fe" }},
  "w18:p54": {{ scheduler: selfName, profile: "dev-fe" }},
}};

// 场景 A：零活跃己方成员 + 零后台作业 + 待用户事项无 -> 必须检出静默躺平
const emptyParsed = analyzeSession("");
const stallA = findSilentStall(emptyParsed, agentsIdle, selfPane, registry, selfName, repo);
const reasonA = stallA ? buildStallReason(stallA) : "";

// 场景 B：存在仍在运行的非 member-wait 后台作业（如等 CI）-> 放行；作业回流完成后 -> 重新检出
const bgRunningLines = [
  JSON.stringify({{ type: "message", message: {{ role: "toolResult", toolCallId: "ci1", content: "Backgrounded as job bg_77" }} }}),
].join("\\n");
const stallBgRunning = findSilentStall(analyzeSession(bgRunningLines), agentsIdle, selfPane, registry, selfName, repo);
const bgDoneLines = [
  bgRunningLines,
  JSON.stringify({{ type: "custom_message", customType: "async-result", content: "Background job bg_77 finished" }}),
].join("\\n");
const stallBgDone = findSilentStall(analyzeSession(bgDoneLines), agentsIdle, selfPane, registry, selfName, repo);

// 场景 C：己方有正在干活的成员 -> 不按零成员躺平拦截
const agentsOwnWorking = [
  {{ name: selfName, pane_id: selfPane, agent_status: "idle", cwd: repo }},
  {{ name: "i514-dev-fe", pane_id: "w18:p54", agent_status: "working" }},
];
const stallOwnWorking = findSilentStall(emptyParsed, agentsOwnWorking, selfPane, registry, selfName, repo);

// 场景 D：已填明确「待用户事项」-> 放行
writeFileSync(statePath, [
  "# #514",
  "<!-- hwf:top 本段只由 task-record.py 改 -->",
  "- 当前阶段：P0 阻塞",
  "- 待用户事项：请确认是否调整 Windows Runner 配置（见 #514#note_30001）",
  "<!-- /hwf:top -->",
].join("\\n"));
const stallWithTodo = findSilentStall(emptyParsed, agentsIdle, selfPane, registry, selfName, repo);

// 场景 E：当前阶段为待上线 -> 放行
writeFileSync(statePath, [
  "# #514",
  "<!-- hwf:top 本段只由 task-record.py 改 -->",
  "- 当前阶段：待上线（已发 release MR）",
  "- 待用户事项：无",
  "<!-- /hwf:top -->",
].join("\\n"));
const stallShipping = findSilentStall(emptyParsed, agentsIdle, selfPane, registry, selfName, repo);

// 场景 F：INDEX 为 chat 对话模式 -> 放行
writeFileSync(statePath, [
  "# #514",
  "<!-- hwf:top 本段只由 task-record.py 改 -->",
  "- 当前阶段：已答复",
  "- 待用户事项：无",
  "<!-- /hwf:top -->",
].join("\\n"));
writeFileSync(indexPath, [
  "# 任务索引",
  "| 任务标识 | 输入原文 | 调度者 | 状态 |",
  "|---|---|---|---|",
  "| `frontend-web-v1-514` | #514 | `delivery-scheduler-w18-phq` / `w18:pHQ` | chat（已答复待决定） |",
].join("\\n"));
const stallChat = findSilentStall(emptyParsed, agentsIdle, selfPane, registry, selfName, repo);

console.log(JSON.stringify({{
  stallA,
  reasonA,
  stallBgRunning,
  stallBgDone,
  stallOwnWorking,
  stallWithTodo,
  stallShipping,
  stallChat,
}}));
"""
            res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout.strip())
        self.assertEqual(out["stallA"]["task"], "frontend-web-v1-514")
        self.assertIn("静默停止", out["reasonA"])
        self.assertIn("task-record.py note --kind todo", out["reasonA"])
        self.assertIsNone(out["stallBgRunning"])
        self.assertEqual(out["stallBgDone"]["task"], "frontend-web-v1-514")
        self.assertIsNone(out["stallOwnWorking"])
        self.assertIsNone(out["stallWithTodo"])
        self.assertIsNone(out["stallShipping"])
        self.assertIsNone(out["stallChat"])


if __name__ == "__main__":
    unittest.main()
