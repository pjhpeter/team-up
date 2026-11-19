// Herdr 交付调度者「派活必挂等待」守卫（~/.hwf/PROTOCOL.md「派活、等待与结果收取」的机制兜底）。
//
// 问题：调度者派活（herdr agent prompt）后常常忘了挂后台 herdr agent wait 就结束回合，
// 或成员 blocked 通知用户后不再等待；成员干完/用户处理完都没人知道，要用户来催。
// 做法：在 omp 准备结束回合（session_stop）时，从会话日志算出「本会话派过活的成员」与
// 「仍在运行的后台等待」，再用 herdr agent list 查成员实时状态；
// 有成员 working/blocked 却没有后台等待覆盖时，拦下结束并要求先补挂等待。
// 只对登记名以 delivery-scheduler- 开头的 pane 生效；同一组未覆盖成员最多拦 MAX_BLOCKS 次，防止死循环。
// @ts-nocheck

import { execFile } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const MAX_BLOCKS = 2;
const LOG_FILE = path.join(os.homedir(), ".hwf", "state", "watch-guard.log");
const SCHEDULER_PREFIX = "delivery-scheduler-";

// 去掉 shell 引号；变量形式（$NAME）无法静态解析，返回 null。
function cleanTarget(raw) {
  const t = String(raw).replace(/^['"]|['"]$/g, "");
  return !t || t.startsWith("$") || t.startsWith("-") ? null : t;
}

function bashCommand(toolCall) {
  const args = toolCall?.arguments;
  return typeof args?.command === "string" ? args.command : "";
}

function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((c) => (typeof c?.text === "string" ? c.text : "")).join("");
  return "";
}

// 从会话 JSONL 文本算出：派过活的成员、仍在运行的后台等待覆盖的成员（"*" 表示目标是变量，视为全覆盖）。
export function analyzeSession(jsonl) {
  const prompted = new Set();
  const waitCalls = new Map(); // toolCallId -> targets[]
  const activeJobs = new Map(); // bg job id -> targets[]
  for (const line of jsonl.split("\n")) {
    if (!line) continue;
    let entry;
    try {
      entry = JSON.parse(line);
    } catch {
      continue;
    }
    const message = entry?.message;
    if (entry.type === "message" && message?.role === "assistant" && Array.isArray(message.content)) {
      for (const part of message.content) {
        if (part?.type !== "toolCall") continue;
        const command = bashCommand(part);
        for (const m of command.matchAll(/herdr agent prompt\s+((?:--?\S+\s+)*)(\S+)/g)) {
          const target = cleanTarget(m[2]);
          if (target && target !== "--help") prompted.add(target);
        }
        // 2026-11-19 #484 起只用 member-wait.py 等成员；旧的 herdr agent wait 仍算覆盖，记录守卫会另行拦下。
        const waits = [...command.matchAll(/(?:herdr agent wait|member-wait\.py)\s+([^\s;|&)]+)/g)]
          .map((m) => m[1])
          .filter((t) => t !== "--help")
          .map((t) => cleanTarget(t) ?? "*");
        if (waits.length > 0) waitCalls.set(part.id, waits);
      }
    } else if (entry.type === "message" && message?.role === "toolResult" && waitCalls.has(message.toolCallId)) {
      const job = /Backgrounded as job (bg_\d+)/.exec(textOf(message.content));
      if (job) activeJobs.set(job[1], waitCalls.get(message.toolCallId));
    } else if (entry.type === "custom_message" && entry.customType === "async-result") {
      const job = /Background job (bg_\d+)/.exec(textOf(entry.content));
      if (job) activeJobs.delete(job[1]);
    }
  }
  const watched = new Set([...activeJobs.values()].flat());
  return { prompted, watched };
}

// 结合实时状态，找出 working/blocked 但没有后台等待覆盖的成员。
export function findUnwatched({ prompted, watched }, agents, selfPane) {
  if (watched.has("*")) return [];
  const result = [];
  for (const target of prompted) {
    const agent = agents.find((a) => a.name === target || a.pane_id === target);
    if (!agent || agent.pane_id === selfPane) continue;
    if (agent.agent_status !== "working" && agent.agent_status !== "blocked") continue;
    if (watched.has(target) || watched.has(agent.name) || watched.has(agent.pane_id)) continue;
    result.push({ name: agent.name ?? target, pane: agent.pane_id, status: agent.agent_status });
  }
  return result;
}

export function buildReason(unwatched) {
  const lines = unwatched.map((u) =>
    u.status === "blocked"
      ? `- ${u.name}（blocked，卡在审批/提问界面）：已通知用户的前提下，后台挂 \`python3 ~/.hwf/scripts/member-wait.py ${u.name} --until working --until idle --until done --repo <主仓库> --task <任务标识>\``
      : `- ${u.name}（working）：后台挂 \`python3 ~/.hwf/scripts/member-wait.py ${u.name} --repo <主仓库> --task <任务标识>\``,
  );
  return [
    "[hwf 监听守卫] 你准备结束回合，但下面这些成员还在干活或卡住，却没有任何后台 member-wait.py 在盯：",
    ...lines,
    "按 ~/.hwf/protocol/scheduling.md「等待与完成判定」：用 bash `async: true` 挂上述等待（不要写 for 循环或 sleep），确认返回「Backgrounded as job」后再结束回合。",
    "若某成员已不属于本任务（已交接或改由其他调度者使用），在 STATE 记一句并直接结束回合即可。",
  ].join("\n");
}

function log(line) {
  try {
    appendFileSync(LOG_FILE, `${new Date().toISOString()} ${line}\n`);
  } catch {}
}

function listAgents() {
  return new Promise((resolve) => {
    execFile("herdr", ["agent", "list"], { timeout: 5000 }, (error, stdout) => {
      if (error) return resolve(null);
      try {
        resolve(JSON.parse(stdout)?.result?.agents ?? null);
      } catch {
        resolve(null);
      }
    });
  });
}

export default function (pi) {
  const selfPane = process.env.HERDR_PANE_ID;
  // OMP 子进程里嵌套的 omp 会继承 OMPCODE=1，不是 pane 的根调度者。
  if (process.env.HERDR_ENV !== "1" || !selfPane || process.env.OMPCODE === "1") return;

  let lastSignature = "";
  let blocksForSignature = 0;

  pi.on("session_stop", async (event) => {
    const sessionFile = event?.session_file;
    if (!sessionFile) return;
    const agents = await listAgents();
    const self = agents?.find((a) => a.pane_id === selfPane);
    if (!self?.name?.startsWith(SCHEDULER_PREFIX)) return;

    let jsonl;
    try {
      jsonl = readFileSync(sessionFile, "utf8");
    } catch {
      return;
    }
    const unwatched = findUnwatched(analyzeSession(jsonl), agents, selfPane);
    if (unwatched.length === 0) {
      lastSignature = "";
      blocksForSignature = 0;
      return;
    }

    const signature = unwatched.map((u) => `${u.name}:${u.status}`).sort().join(",");
    blocksForSignature = signature === lastSignature ? blocksForSignature + 1 : 1;
    lastSignature = signature;
    if (blocksForSignature > MAX_BLOCKS) {
      log(`${self.name} 放行（同一组已拦 ${MAX_BLOCKS} 次）: ${signature}`);
      return;
    }
    log(`${self.name} 拦下结束回合 #${blocksForSignature}: ${signature}`);
    return { decision: "block", reason: buildReason(unwatched) };
  });
}
