// Herdr 交付调度者「派活必挂等待 + 禁止进行中任务静默躺平」守卫（~/.hwf/PROTOCOL.md 机制兜底）。
//
// 问题 1：调度者派活（herdr agent prompt）后常常忘了挂后台 member-wait.py 就结束回合，
// 或成员 blocked 通知用户后不再等待；成员干完/用户处理完都没人知道，要用户来催。
// 问题 2：调度者释放全部成员（或根本没派人）后，在进行中任务且「待用户事项：无」时只在终端写「暂停」
// 就结束回合，不发 GitLab/飞书/ntfy，导致任务陷入无人知晓的静默空转。
// 做法：在 omp 准备结束回合（session_stop）时，先查属于本调度者且 working/blocked 却无后台等待的成员；
// 再查本调度者名下进行中任务是否处于「零活跃成员 + 零后台等待 + 待用户事项为无 + 非待上线/等你处理/对话模式」，
// 命中任一情况即拦下结束（最多连续拦 MAX_BLOCKS 次），要求补挂等待、继续派活或走正式阻塞通知流程。
// 只对登记名以 delivery-scheduler- 开头的 pane 生效；其他调度者与登记在别的调度者名下的成员一律跳过。
// @ts-nocheck

import { execFile } from "node:child_process";
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const MAX_BLOCKS = 2;
const STATE_DIR = process.env.HWF_STATE_DIR || path.join(os.homedir(), ".hwf", "state");
const LOG_FILE = path.join(STATE_DIR, "watch-guard.log");
const MEMBERS_FILE = path.join(STATE_DIR, "members.json");
const SCHEDULER_PREFIX = "delivery-scheduler-";
const MEMBER_NAME_RE = /^(?:i\d+|new)-[a-z0-9_-]+$|^delivery-(?!scheduler-)[a-z0-9_-]+$/i;
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const SCHEDULER_NAME_RE = /delivery-scheduler-[A-Za-z0-9_-]+/;
const TOP_OPEN_RE = /<!--\s*hwf:top\b[^>]*-->/;
const TOP_CLOSE = "<!-- /hwf:top -->";
const NO_USER_TODO_RE = /^(?:无|暂无|none|n\/a|-)(?:[。.;；\s]|（[^）]*）|\([^)]*\))*$/i;
const WAITING_STAGE_RE = /待上线|等待?(?:用户|发起人)|等你处理|待用户|对话模式|只读分析|已交付|已关闭|已取消|收尾完成/;

function issuePrefixOf(name) {
  const m = /^(i\d+)-/i.exec(String(name ?? ""));
  return m ? m[1].toLowerCase() : "";
}

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

// 从会话 JSONL 文本算出：派过活的成员、仍在运行的后台等待覆盖的成员（"*" 表示目标是变量，视为全覆盖）、以及全部仍在运行的后台作业。
export function analyzeSession(jsonl) {
  const prompted = new Set();
  const waitCalls = new Map(); // toolCallId -> targets[]
  const activeJobs = new Map(); // bg job id -> targets[]
  const activeBgJobs = new Set(); // 全部尚未回流的后台作业（含非 member-wait 的 CI 等待等）
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
          if (target && target !== "--help" && !target.startsWith(SCHEDULER_PREFIX)) {
            prompted.add(target);
          }
        }
        // 2026-11-19 #484 起只用 member-wait.py 等成员；旧的 herdr agent wait 仍算覆盖，记录守卫会另行拦下。
        const waits = [...command.matchAll(/(?:herdr agent wait|member-wait\.py)\s+([^\s;|&)]+)/g)]
          .map((m) => m[1])
          .filter((t) => t !== "--help")
          .map((t) => cleanTarget(t) ?? "*");
        if (waits.length > 0) waitCalls.set(part.id, waits);
      }
    } else if (entry.type === "message" && message?.role === "toolResult") {
      const job = /Backgrounded as job (bg_\d+)/.exec(textOf(message.content));
      if (job) {
        activeBgJobs.add(job[1]);
        if (waitCalls.has(message.toolCallId)) {
          activeJobs.set(job[1], waitCalls.get(message.toolCallId));
        }
      }
    } else if (entry.type === "custom_message" && entry.customType === "async-result") {
      const job = /Background job (bg_\d+)/.exec(textOf(entry.content));
      if (job) {
        activeJobs.delete(job[1]);
        activeBgJobs.delete(job[1]);
      }
    }
  }
  const watched = new Set([...activeJobs.values()].flat());
  return { prompted, watched, activeBgJobs };
}

// 结合实时状态与成员登记簿，找出属于本调度者且 working/blocked 但没有后台等待覆盖的成员。
export function findUnwatched({ prompted, watched }, agents, selfPane, registry = {}, selfScheduler = "") {
  if (watched.has("*")) return [];
  const selfName = selfScheduler || agents.find((a) => a.pane_id === selfPane)?.name || "";
  const schedulerPanes = new Set(
    agents
      .filter((a) => String(a?.name ?? "").startsWith(SCHEDULER_PREFIX))
      .map((a) => a.pane_id)
      .filter(Boolean),
  );
  // 统计其他调度者在登记簿中占用的 issue 前缀（如 i499），防止未写入登记簿的他人成员被误拦。
  const foreignIssuePrefixes = new Set();
  const ownIssuePrefixes = new Set();
  for (const [pid, meta] of Object.entries(registry ?? {})) {
    const ownerSched = meta?.scheduler;
    if (!ownerSched) continue;
    const issueNum = String(meta?.issue ?? "").replace(/^#/, "");
    const agentName = agents.find((a) => a.pane_id === pid)?.name ?? "";
    const prefix = (/^\d+$/.test(issueNum) ? `i${issueNum}` : "") || issuePrefixOf(agentName);
    if (!prefix) continue;
    if (selfName && ownerSched === selfName) ownIssuePrefixes.add(prefix);
    else if (selfName && ownerSched !== selfName) foreignIssuePrefixes.add(prefix);
  }
  const seenPanes = new Set();
  const result = [];
  for (const target of prompted) {
    const targetStr = String(target ?? "");
    if (!targetStr || targetStr.startsWith(SCHEDULER_PREFIX) || schedulerPanes.has(targetStr)) continue;
    const agent = agents.find((a) => a.name === targetStr || a.pane_id === targetStr);
    if (!agent || agent.pane_id === selfPane || seenPanes.has(agent.pane_id)) continue;
    const agentName = String(agent.name ?? "");
    // 调度者窗格及非成员命名的未登记窗格（如用户终端或尚未完成改名的并行调度者）一律跳过。
    if (agentName.startsWith(SCHEDULER_PREFIX) || schedulerPanes.has(agent.pane_id)) continue;
    const owner = registry?.[agent.pane_id]?.scheduler;
    if (owner && selfName && owner !== selfName) continue;
    if (!owner) {
      if (!MEMBER_NAME_RE.test(agentName)) continue;
      const prefix = issuePrefixOf(agentName);
      if (prefix && foreignIssuePrefixes.has(prefix) && !ownIssuePrefixes.has(prefix)) continue;
    }
    if (agent.agent_status !== "working" && agent.agent_status !== "blocked") continue;
    if (watched.has(targetStr) || watched.has(agentName) || watched.has(agent.pane_id)) continue;
    seenPanes.add(agent.pane_id);
    result.push({ name: agentName || targetStr, pane: agent.pane_id, status: agent.agent_status });
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

export function parseIndexLastRows(indexText) {
  const byTask = new Map();
  for (const line of String(indexText ?? "").split("\n")) {
    if (!line.startsWith("|")) continue;
    const cells = line.split(/(?<!\\)\|/);
    if (cells.length < 5) continue;
    const task = cells[1].trim().replace(/^`|`$/g, "");
    if (!TASK_ID_RE.test(task)) continue;
    const schedMatch = SCHEDULER_NAME_RE.exec(cells[3]);
    byTask.set(task, {
      task,
      scheduler: schedMatch ? schedMatch[0] : "",
      status: cells[4].trim(),
    });
  }
  return [...byTask.values()];
}

export function parseStateTop(stateText) {
  const text = String(stateText ?? "");
  const openMatch = TOP_OPEN_RE.exec(text);
  if (!openMatch) return {};
  const start = openMatch.index + openMatch[0].length;
  const end = text.indexOf(TOP_CLOSE, start);
  if (end < 0) return {};
  const fields = {};
  for (const line of text.slice(start, end).split("\n")) {
    const m = /^-\s*([^：:]+)[：:](.*)$/.exec(line.trim());
    if (m) fields[m[1].trim()] = m[2].trim();
  }
  return fields;
}

export function isActiveDeliveryStatus(status) {
  const clean = String(status ?? "").trim().toLowerCase();
  return clean.startsWith("in-progress") || clean.startsWith("active");
}

export function isNoUserTodo(value) {
  const clean = String(value ?? "").trim();
  return !clean || NO_USER_TODO_RE.test(clean);
}

export function isWaitingStage(stage) {
  return WAITING_STAGE_RE.test(String(stage ?? ""));
}

function readTextSafe(filePath) {
  try {
    return readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

// 拦截「进行中交付任务 + 待用户事项为无 + 0 活跃成员 + 0 后台等待」时的静默躺平。
export function findSilentStall(
  parsed,
  agents,
  selfPane,
  registry = {},
  selfScheduler = "",
  repoDir = "",
  readFile = readTextSafe,
) {
  if (parsed?.watched?.size > 0 || parsed?.activeBgJobs?.size > 0) return null;
  const selfName = selfScheduler || agents?.find((a) => a.pane_id === selfPane)?.name || "";
  if (!selfName || !repoDir) return null;

  for (const agent of agents ?? []) {
    if (!agent || agent.pane_id === selfPane) continue;
    if (agent.agent_status !== "working" && agent.agent_status !== "blocked") continue;
    if (registry?.[agent.pane_id]?.scheduler === selfName) return null;
  }

  const indexText = readFile(path.join(repoDir, ".hwf", "tmp", "INDEX.md"));
  if (!indexText) return null;
  const owned = parseIndexLastRows(indexText).filter(
    (row) => row.scheduler === selfName && isActiveDeliveryStatus(row.status),
  );
  if (owned.length === 0) return null;

  let anyLegitimatelyWaiting = false;
  const stalled = [];
  for (const row of owned) {
    const stateText = readFile(path.join(repoDir, ".hwf", "tmp", row.task, "STATE.md"));
    const top = parseStateTop(stateText);
    if (Object.keys(top).length === 0) continue;
    if (!isNoUserTodo(top["待用户事项"]) || isWaitingStage(top["当前阶段"])) {
      anyLegitimatelyWaiting = true;
      continue;
    }
    stalled.push({
      task: row.task,
      stage: top["当前阶段"] || "",
      nextAction: top["下一动作"] || "",
    });
  }
  if (anyLegitimatelyWaiting || stalled.length === 0) return null;
  return stalled[stalled.length - 1];
}

export function buildStallReason(stall) {
  return [
    `[hwf 监听守卫] 检测到进行中任务 \`${stall.task}\` 准备静默停止（当前阶段：${stall.stage || "未写明"}；待用户事项：无），但当前没有任何在干活的成员，也没有后台等待作业。`,
    "禁止只在终端或本地 STATE 写「暂停」就结束回合（用户与飞书秘书都收不到通知）：",
    "1. 如果在既有授权内还能继续推进：请当回合派发下一步成员并挂 `python3 ~/.hwf/scripts/member-wait.py`（`async: true`），确认返回「Backgrounded as job」后再结束回合；",
    "2. 如果确实受阻或需要用户拍板/操作：请先用 `task-record.py note --kind todo` 在 GitLab issue 写明阻塞原因与选项、贴 `状态:等你处理` 标签、发 ntfy，并用 `task-record.py state-set` 更新「待用户事项」后再停下。",
  ].join("\n");
}

function resolveRepoDir(selfAgent) {
  const candidates = [selfAgent?.cwd, selfAgent?.foreground_cwd, process.cwd()].filter(Boolean);
  for (const dir of candidates) {
    if (existsSync(path.join(dir, ".hwf", "tmp", "INDEX.md"))) return dir;
  }
  return candidates[0] || "";
}

function log(line) {
  try {
    appendFileSync(LOG_FILE, `${new Date().toISOString()} ${line}\n`);
  } catch {}
}

function readRegistry() {
  try {
    const parsed = JSON.parse(readFileSync(MEMBERS_FILE, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
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
    const parsed = analyzeSession(jsonl);
    const registry = readRegistry();
    const unwatched = findUnwatched(parsed, agents, selfPane, registry, self.name);
    if (unwatched.length > 0) {
      const signature = `unwatched:${unwatched.map((u) => `${u.name}:${u.status}`).sort().join(",")}`;
      blocksForSignature = signature === lastSignature ? blocksForSignature + 1 : 1;
      lastSignature = signature;
      if (blocksForSignature > MAX_BLOCKS) {
        log(`${self.name} allow session_stop after ${MAX_BLOCKS} blocks: ${signature}`);
        return;
      }
      log(`${self.name} block session_stop #${blocksForSignature}: ${signature}`);
      return { decision: "block", reason: buildReason(unwatched) };
    }

    const stall = findSilentStall(parsed, agents, selfPane, registry, self.name, resolveRepoDir(self));
    if (!stall) {
      lastSignature = "";
      blocksForSignature = 0;
      return;
    }

    const signature = `stall:${stall.task}`;
    blocksForSignature = signature === lastSignature ? blocksForSignature + 1 : 1;
    lastSignature = signature;
    if (blocksForSignature > MAX_BLOCKS) {
      log(`${self.name} allow session_stop after ${MAX_BLOCKS} blocks: ${signature}`);
      return;
    }
    log(`${self.name} block session_stop #${blocksForSignature}: ${signature}`);
    return { decision: "block", reason: buildStallReason(stall) };
  });
}
