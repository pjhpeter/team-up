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


if __name__ == "__main__":
    unittest.main()
