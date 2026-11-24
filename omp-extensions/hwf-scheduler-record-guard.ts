// Herdr 调度者记录守卫：~/.hwf/protocol/scheduling.md「任务标识与索引」及 communication.md「通知模式」的机制兜底。
//
// 问题：2026-11-15 #483 调度者手写 INDEX 行没加反引号，收件箱找不到负责人；免打扰模式下仍单独发了 4 条阶段进度评论；
// 先请用户同意合并，之后才发现本账号没有合并权限。这些规则协议里都写了，调度者照样漏。
// 做法：调度者动手改 .hwf/tmp/INDEX.md，或直接用 glab 在 issue 下发新评论时，拦下并告诉它改用 task-record.py。
// 脚本自己用子进程调 glab，不经过这里。只对登记名以 delivery-scheduler- 开头的 pane 生效。
// @ts-nocheck

import { execFile } from "node:child_process";

const SCHEDULER_PREFIX = "delivery-scheduler-";
const RECHECK_MS = 30_000;
const SCRIPT = "python3 ~/.hwf/scripts/task-record.py";
const INDEX_FILE = /\.hwf\/tmp\/INDEX\.md\b/;
const INDEX_WRITE = /(>>?\s*["']?[^\s"'|;&]*\.hwf\/tmp\/INDEX\.md)|(\b(sed\s+-i|tee|perl\s+-[a-z]*i)\b[^|;&]*\.hwf\/tmp\/INDEX\.md)/;
const ISSUE_NOTE = /\bglab\s+issue\s+(note|comment)\b/;
const ISSUE_NOTES_API = /\bglab\s+api\b[^|;&]*\/issues\/[^/\s]+\/notes(?![\/\w])/;
const API_WRITE = /(-X|--method)[=\s]+["']?POST|\s(-f|-F|--field|--raw-field)[=\s]/i;
const BARE_WAIT = /\bherdr\s+agent\s+wait\s+(?!--help)\S/;
const AGENT_PROMPT = /\bherdr\s+agent\s+prompt\s+(?!--help)\S/;
const MAX_PROMPT_CHARS = 2500;
const WAIT_REASON = "等成员不用 herdr agent wait（2026-11-19 #484：成员断网后 herdr 仍报 working，干等了约 2 小时没人发现）。改用：python3 ~/.hwf/scripts/member-wait.py <成员> [--until …] --repo <主仓库> --task <任务标识>，用 bash async: true 后台运行；它每次最多等 10 分钟，会看屏幕判断卡住，并自动更新 issue 正文。";

function promptLengthReason(len) {
  return `单条派活命令长达 ${len} 字（上限 ${MAX_PROMPT_CHARS} 字）：万字长派活会撑爆调度者与成员上下文。请把长背景、验收清单或代码位置写进 <主仓库>/.hwf/tmp/<任务标识>/dispatch-<角色>.md，herdr agent prompt 里只写本次目标、可写范围和该文件的绝对路径（见 ~/.hwf/protocol/scheduling.md「派活契约」）。`;
}

// 返回拦截理由；不该拦返回 null。
export function violation(toolName, input) {
  if (toolName === "bash") {
    const command = String(input?.command ?? "");
    if (command.includes("task-record.py") || command.includes("wrapup.py")) return null;
    if (INDEX_WRITE.test(command)) return indexReason();
    if (ISSUE_NOTE.test(command) || (ISSUE_NOTES_API.test(command) && API_WRITE.test(command))) return noteReason();
    if (BARE_WAIT.test(command)) return WAIT_REASON;
    if (AGENT_PROMPT.test(command) && command.length > MAX_PROMPT_CHARS) return promptLengthReason(command.length);
    return null;
  }
  if (toolName === "write" || toolName === "edit") {
    const target = toolName === "write" ? String(input?.path ?? "") : String(input?.input ?? "").split("\n", 1)[0];
    return INDEX_FILE.test(target) ? indexReason() : null;
  }
  return null;
}

function indexReason() {
  return `INDEX.md 不手改（手写漏了反引号，收件箱就找不到负责人）。追加自己的行：${SCRIPT} index-add --repo <主仓库> --task <任务标识> --input <输入原文>；改状态：${SCRIPT} index-set --repo <主仓库> --task <任务标识> --status <状态>；收尾状态只由 wrapup.py 写。`;
}

function noteReason() {
  return `issue 评论不直接用 glab 发。把正文写进文件后运行：${SCRIPT} note --repo <主仓库> --task <任务标识> --issue <issue URL> --kind <todo|risk|answer|step-done|delivery-done|stage|record> --body-file <文件>。请用户同意或授权时用 --kind todo，并用 --will 写明用户同意后你要做的动作（merge=<MR URL>、push=<项目 URL>@<分支>、tag=<项目 URL>@<tag>，只需要用户自己做就写 none），脚本会先查你做不做得了；step-done 与 delivery-done 必须在 <details> 折叠区外附「验收标准逐条对账表」（含播放器 Markdown、时间段与 /-/project/<ID>/uploads/... 直链）；已确认方案、已评审用例或登记池备忘用 --kind record（只留 GitLab 存档，不刷飞书私聊）。`;
}

function schedulerName(selfPane) {
  return new Promise((resolve) => {
    execFile("herdr", ["agent", "get", selfPane], { timeout: 5000 }, (error, stdout) => {
      if (error) return resolve(null);
      try {
        resolve(String(JSON.parse(stdout)?.result?.agent?.name ?? ""));
      } catch {
        resolve(null);
      }
    });
  });
}

export default function (pi) {
  const selfPane = process.env.HERDR_PANE_ID;
  if (process.env.HERDR_ENV !== "1" || !selfPane || process.env.OMPCODE === "1") return;

  let isScheduler = false;
  let checkedAt = 0;

  pi.on("tool_call", async (event) => {
    const reason = violation(event?.toolName, event?.input);
    if (!reason) return;
    // 登记名在会话启动后才改，所以「不是调度者」的结论只短期有效；查不到身份时不拦。
    if (!isScheduler && Date.now() - checkedAt > RECHECK_MS) {
      checkedAt = Date.now();
      isScheduler = (await schedulerName(selfPane))?.startsWith(SCHEDULER_PREFIX) === true;
    }
    if (isScheduler) return { block: true, reason };
  });
}
