#!/usr/bin/env python3
"""GitLab 收件箱：轮询本机 glab 账号的待办与发起 issue 评论，转交给 Herdr 调度者。

必须在 Herdr 管理的 pane 里常驻运行（find-scheduler.py 与 team-up 入口都要求 HERDR_ENV=1）：

    python3 ~/.hwf/scripts/gitlab-inbox.py            # 每 interval_seconds 轮询一次
    python3 ~/.hwf/scripts/gitlab-inbox.py --once     # 只跑一轮（调试）

配置分两份（2026-11-02 起，共用的入库、本机的不入库）：
  ~/.hwf/gitlab-inbox.config.json（入库）：host、allowed_users、label、interval_seconds、projects（要盯的 GitLab 项目路径列表）；
  ~/.hwf/gitlab-inbox.local.json（本机）：projects（GitLab 路径 → 本机仓库路径），其余键可覆盖入库值。
  只盯两边都有的项目；入库登记了、本机没配路径的项目跳过并记日志。
状态 ~/.hwf/state/gitlab-inbox.json：已发起的 issue、每个发起 issue 已转交到的评论 id、已转交的评论 id。

三类事件（只认 allowed_users 的操作；其他人的待办原样留着，不碰）：
  1. 指派：白名单用户把带开关标签的 issue 指派给本账号 → hwf run team-up 开一个对话模式的调度者（它不自己分析，
     按用户点名派角色只读分析，没点名派 arch），用户发 `/go` 或明确说开工才进入交付流程（2026-10-30 用户指示）；`/go 极简`（或 `/go 极简模式`、`/go minimal`）是用户明确要求极简模式（只开发 + 提交 MR 到 release-<根编号>）。
     issue 描述里可写 `/model <模型>`、`/thinking <强度>` 两行指定调度者参数。
     入口复用空闲调度者或新建一个，允许多个调度者并行，不必等在途交付结束。
     本账号自己指派（调度者拆出的子 issue）作者不在白名单，天然被忽略，不会重复发起。
     标签后加也能触发：未带标签时待办保持 pending，下一轮再看。
  2. 发起 issue 的新评论（不要求 @）→ 转交调度者，这是用户回复调度者提问的通道。
     评论正文只有 `/new` 或 `/clear` 时是清空指令：收件箱让该调度者开新会话，再让它按 INDEX/STATE 接续本任务。
     任何 issue 里只有 `/status` 的评论由收件箱直接回复进度，不打扰调度者。
  每条转交的评论都会立即得到回执（转给谁、它在不在干活、在做什么）；调度者卡在确认界面时在发起 issue @发起人一次。
  卡住时能读出界面上的单选题，就把题目和编号选项贴出来；发起人回复一个数字（如 `1`、`选 2`），收件箱核对界面
  没变后替他按键选中，并回帖结果（2026-11-03 用户指示：不在电脑旁也能在飞书里作答）。
  3. 任意 issue 里 @本账号 的评论 → 转交调度者。
  4. 本账号参与的其他 issue 的新评论（不要求 @）→ 转交调度者。参与 = 本账号建的 issue（交付拆出的子 issue
     等），或用户在里面 @ 过本账号的 issue。用户常直接在 Agent 的评论下回复而忘了 @（2026-10-27 #422）。
评论按归属转交：find-scheduler --owner-of 找出负责该 issue 的未收尾交付及其调度者 pane，直接发给它；
交付存在但调度者已退出，或没有任何交付负责该 issue，就把评论作为需求走 team-up 入口（调度者按 INDEX 接续
或当作新需求）。转交失败不推进水位，下一轮重试，消息不丢、也不重复。
发起 issue 只由用户关闭（2026-10-22 用户指示）：关闭前其调度者不接其他需求（find-scheduler 读本状态文件）；
关闭后收件箱通知该调度者收尾，并停止跟踪。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
CONFIG_PATH = HWF_HOME / "gitlab-inbox.config.json"
LOCAL_CONFIG_PATH = HWF_HOME / "gitlab-inbox.local.json"
STATE_PATH = HWF_HOME / "state" / "gitlab-inbox.json"
FIND_SCHEDULER = HWF_HOME / "scripts" / "find-scheduler.py"
RESET_SESSION = HWF_HOME / "scripts" / "reset-session.py"

CMD_TIMEOUT_S = 30
LAUNCH_TIMEOUT_S = 60
NOTE_RE = re.compile(r"#note_(\d+)$")
FORWARDED_CAP = 1000  # 只用于去重，保留最近的即可。
# 新调度者要先读协议才登记名字、写 INDEX；这段时间看不到归属，同一 issue 再走入口会多建一个调度者。
LAUNCH_COOLDOWN_S = 600
RESET_COMMANDS = frozenset({"/new", "/clear"})
STATUS_COMMANDS = frozenset({"/status"})
GO_COMMANDS = frozenset({"/go"})
# `/go` 后紧跟其中一个词 = 用户明确要求极简模式（只开发 + 提交 MR 到 release-<根编号>，见 PROTOCOL「极简模式」）。
MINIMAL_WORDS = frozenset({"极简", "极简模式", "minimal"})
# 发起 issue 的两种模式：对话 = 只读 + 评论答疑；交付 = 跑完整交付流程。指派后默认对话，/go 切到交付。
CHAT, DELIVER = "chat", "deliver"
CHAT_TAG = "【对话模式】"  # team-up 需求文本前缀，调度者据此只派人分析、不进入交付流程
STATUS_WORDS = {"working": "正在干活", "idle": "空闲", "done": "空闲",
                "blocked": "卡在确认界面（需要到电脑上处理）", "unknown": "状态无法判断"}
# 参与 issue 按 updated_after 增量查询；往回多看一段，抵消本机与 GitLab 的时钟差，重复的评论靠 forwarded 去重。
FOLLOW_OVERLAP_S = 300
FOLLOWED_CAP = 100  # 用户 @ 过本账号的非发起 issue，一次 iids[] 查询能装下的量
CLOSED_CAP = 200  # 已关闭的发起 issue 只为“用户重开后接着跟踪”保留，留最近的即可。
STATUS_RECORD_CAP = 160  # 进度回复里 INDEX 状态的最大字数，详细内容在 STATE.md
TITLE_PREFIX = re.compile(r"^[π\s>\u2800-\u28ff]+")  # omp 标题前缀与转圈字符，如 "π ⠹ " / "π > "
# 发起 issue 描述里指定调度者参数的行：`/model jc-gpt/gpt-6.1-sol`、`/thinking high`（2026-10-22 用户指示）。
PARAM_LINE = re.compile(r"^\s*/(model|thinking)\s+(\S+)\s*$", re.IGNORECASE)
# 卡住界面的单选题：选项行是「编号.」或「○ ●」开头，光标行前有「❯」等标记；外框「│」先去掉。
DIALOG_OPTION = re.compile(r"^(?P<cursor>[❯›▸➤>]\s*)?(?:(?P<num>\d{1,2})[.)]|[○●◯◉])\s+(?P<label>\S.*?)\s*$")
DIALOG_CHECKBOX = re.compile(r"^(?:[❯›▸➤>]\s*)?(?:\d{1,2}[.)]\s*)?(?:[☐☑☒]|\[[ xX✓✔]\])\s")  # 多选题，不远程作答
DIALOG_RULE = re.compile(r"^[─━═├┤╭╮╰╯┌┐└┘]")  # 分隔线、外框的上下边（可带标题，如「╭─ Ask ─╮」）
DIALOG_TYPING = re.compile(r"type your own|type something|tell claude what to do|自己输入|其他.*输入", re.IGNORECASE)
CHOICE_REPLY = re.compile(r"^(?:选(?:择)?\s*)?(?:第\s*)?(\d{1,2})\s*(?:项|个)?$")
FEISHU_SUFFIX = "_（来自飞书）_"
CHOICE_SETTLE_S = 3  # 按键后等界面刷新再读屏核对
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")  # omp --thinking 可选值


class InboxError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


class Inbox:
    def __init__(self, config: Mapping[str, Any], state: dict[str, Any], run: Runner = subprocess.run):
        self.host: str = config["host"]
        self.allowed = set(config["allowed_users"])
        self.label: str = config["label"]
        self.projects: dict[str, str] = dict(config["projects"])
        self.state = state
        state.setdefault("origins", {})      # issue URL → {project, iid, repo, initiator, last_note_id}
        state.setdefault("forwarded", [])    # 已转交的评论 id
        state.setdefault("launched_at", {})  # 任务标识或 issue URL → 最近一次走入口的时间戳
        state.setdefault("closed_origins", {})  # 用户关闭的发起 issue（含水位），重开时接回 origins
        state.setdefault("bad_params_notified", [])  # 已提示过模型参数写错的 issue，避免每轮重复评论
        state.setdefault("followed", {})     # 用户 @ 过本账号的 issue URL → {project, iid}
        self.run = run
        self.sleep: Callable[[float], None] = time.sleep
        self.me: str = ""

    # ---- 外部命令 ----

    def _exec(self, argv: Sequence[str], *, cwd: str | None = None, timeout: int = CMD_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
        return self.run(list(argv), capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)

    def api(self, path: str, method: str = "GET") -> Any:
        result = self._exec(["glab", "api", "--hostname", self.host, "-X", method, path])
        if result.returncode != 0:
            raise InboxError(f"glab api {method} {path} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def owner(self, repo: str, url: str) -> tuple[str | None, str | None]:
        """返回 (任务标识, 在线调度者 pane)，都可能为 None；发现失败抛错（fail closed）。"""
        info = self.owner_info(repo, url)
        return info["task"], info["pane"]

    def owner_info(self, repo: str, url: str) -> dict[str, Any]:
        result = self._exec([sys.executable, str(FIND_SCHEDULER), repo, "--owner-of", url])
        if result.returncode != 0:
            raise InboxError(f"find-scheduler exited {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def agent_state(self, pane: str) -> tuple[str, str]:
        """(herdr 状态, 终端标题)。标题是调度者自己维护的“正在做什么”，查不到时返回空串。"""
        result = self._exec(["herdr", "agent", "get", pane])
        if result.returncode != 0:
            return "unknown", ""
        agent = json.loads(result.stdout)["result"]["agent"]
        title = str(agent.get("terminal_title_stripped") or "")
        return str(agent.get("agent_status") or "unknown"), TITLE_PREFIX.sub("", title).strip()

    def status_text(self, repo: str, url: str) -> str:
        """给用户看的一句话进度：哪个调度者、它现在什么状态、在做什么、任务记录写到哪。"""
        info = self.owner_info(repo, url)
        if info["task"] is None:
            return "这个 issue 现在没有对应的交付记录。"
        record = info["status"].strip()
        record = f"任务记录：{record[:STATUS_RECORD_CAP]}{'…' if len(record) > STATUS_RECORD_CAP else ''}"
        pane = info["live_pane"]
        if pane is None:
            return f"负责的调度者 `{info['scheduler']}` 已经退出。{record}"
        status, title = self.agent_state(pane)
        doing = f"，正在：{title}" if status == "working" and title else ""  # 空闲时标题是上一件事，不展示
        other = "（它手上还有别的任务，新消息会交给空闲调度者接手）" if info["pane"] is None else ""
        return f"调度者 `{info['scheduler']}` {STATUS_WORDS.get(status, status)}{doing}{other}。{record}"

    def launch(self, repo: str, task: str, key: str, url: str | None = None) -> bool:
        """走 team-up 入口；key（任务标识或 issue URL）冷却期内不重复走，返回是否已发出。

        url 是发起 issue 时带上它记录的模型与思考强度，接手的新调度者用同样参数。
        """
        if time.time() - self.state["launched_at"].get(key, 0) < LAUNCH_COOLDOWN_S:
            log(f"{key}: entry launched recently, waiting for scheduler to register")
            return False
        origin = self.state["origins"].get(url or "") or {}
        result = self._exec(
            ["hwf", "run", "team-up", "--detach", "--json", "--input", f"task={task}",
             "--input", f"model={origin.get('model', '')}", "--input", f"thinking={origin.get('thinking', '')}"],
            cwd=repo, timeout=LAUNCH_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise InboxError(f"hwf run team-up failed: {result.stderr.strip() or result.stdout.strip()}")
        self.state["launched_at"][key] = time.time()
        save_state(self.state)
        return True

    def scheduler_params(self, description: str) -> tuple[str, str, str]:
        """从 issue 描述读 `/model <模型>`、`/thinking <强度>` 行，返回 (模型, 思考强度, 错误说明)。

        模型必须是 omp 能找到的唯一模型（完整 selector 如 jc-gpt/gpt-6.1-sol，或唯一的模型 id）；思考强度必须
        是该模型支持的档位（没写模型时按 omp 通用档位检查）。写错时返回错误说明，不猜。
        """
        found = {m.group(1).lower(): m.group(2) for line in (description or "").splitlines()
                 if (m := PARAM_LINE.match(line))}
        model, thinking = found.get("model", ""), found.get("thinking", "").lower()
        levels: Sequence[str] = THINKING_LEVELS
        if model:
            result = self._exec(["omp", "models", "find", model, "--json"])
            if result.returncode != 0:
                raise InboxError(f"omp models find failed: {result.stderr.strip()}")
            candidates = json.loads(result.stdout)["models"]
            exact = [c for c in candidates if model in (c["selector"], c["id"])]
            if len(exact) != 1:
                hint = "、".join(f"`{c['selector']}`" for c in candidates[:5])
                return "", "", f"找不到唯一的模型 `{model}`。" + (f"相近的有：{hint}。" if hint else "")
            model, levels = exact[0]["selector"], exact[0].get("thinking") or THINKING_LEVELS
        if thinking and thinking not in levels:
            return "", "", f"思考强度 `{thinking}` 不可用，可选：{'、'.join(levels)}。"
        return model, thinking, ""

    def prompt(self, pane: str, text: str) -> bool:
        result = self._exec(["herdr", "agent", "prompt", pane, text])
        if result.returncode != 0:
            log(f"prompt to {pane} rejected: {result.stderr.strip()}")
        return result.returncode == 0

    def reset(self, pane: str, task: str) -> str | None:
        """调 reset-session.py 开新会话并让调度者接续；未确认换会话返回 None，由下一轮重试。"""
        result = self._exec([sys.executable, str(RESET_SESSION), pane])
        if result.returncode != 0:
            log(f"reset {task}: {result.stderr.strip()}; retry next round")
            return None
        self.prompt(pane, (
            f"[GitLab 收件箱] 用户要求清空上下文，你已在新会话中。你是任务 {task} 的调度者：按 "
            "~/.hwf/PROTOCOL.md 开头「调度者阅读指引与省词元」读必读章节，用 herdr agent get 核对登记身份（不要改名），再按 .hwf/tmp/INDEX.md 与该任务 "
            "STATE.md 接续；在 STATE 记录新会话。任务已收尾就停下等用户关闭发起 issue，不做别的。"))
        return result.stdout

    def comment(self, where: Mapping[str, Any], body: str) -> None:
        """在 issue 下评论；where 只需 project_id 与 iid（发起 issue 记录和待办 target 都有）。"""
        self.api(f"projects/{where['project_id']}/issues/{where['iid']}/notes?body={quote(body)}", "POST")

    # ---- 投递 ----

    def deliver(self, repo: str, url: str, text: str, mode: str = CHAT) -> str | None:
        """发给负责该 issue 的调度者；没有在线负责人时把消息作为需求走入口（按 mode 决定对话还是交付）。

        返回给用户的回执（None 表示没送出，下一轮重试）。调度者在一个回合里时消息要排队到回合结束，
        可能几分钟才回复，回执让用户在远程立即知道消息已到、它现在在干什么。
        """
        task, pane = self.owner(repo, url)
        if pane:
            if not self.prompt(pane, text):
                return None
            return f"已收到，已转给{self.status_text(repo, url)}它做完手头这一步就会在这里回复。"
        if not self.launch(repo, self.entry_task(text, mode), task or url, url):
            return None
        return "已收到。现在没有在线的调度者负责这里，已交给新的调度者接手，它启动后会在这里回复。"

    @staticmethod
    def entry_task(text: str, mode: str) -> str:
        """入口需求文本：对话模式加前缀，team-up 的调度者据此只读答疑、不跑交付流程。"""
        return f"{CHAT_TAG}{text}" if mode == CHAT else text

    def mode_of(self, url: str) -> str:
        """发起 issue 的模式；旧记录没有 mode 字段，按当时的规则视为交付。非发起 issue 一律对话。"""
        origin = self.state["origins"].get(url)
        return str(origin.get("mode", DELIVER)) if origin is not None else CHAT

    def handle_reset(self, url: str, origin: Mapping[str, Any], note: Mapping[str, Any]) -> bool:
        """处理发起 issue 里的清空指令；返回 False 表示下一轮重试。"""
        task, pane = self.owner(origin["repo"], url)
        if task is None:
            self.comment(origin, f"@{note['author']['username']} 这个 issue 现在没有对应的交付记录，无需清空。")
            return True
        if pane is None:
            # 原调度者已退出或已在负责别的任务：走入口时本来就是新会话，按任务记录接续。
            text = f"{url}（GitLab 收件箱：用户要求清空上下文后接续，note_{note['id']}）"
            return self.launch(origin["repo"], self.entry_task(text, self.mode_of(url)), task, url)
        session = self.reset(pane, task)
        if session is None:
            return False
        self.comment(origin, f"@{note['author']['username']} 已清空调度者上下文（新会话 `{Path(session).stem}`），"
                             "它会按任务记录接着做。")
        return True

    def words(self, body: str) -> list[str]:
        """正文去掉 @本账号 后的词（用户常顺手 @ 一下）。"""
        return [w for w in body.split() if w.lower() != f"@{self.me}".lower()]

    def is_command(self, body: str, commands: frozenset[str]) -> bool:
        """去掉 @本账号 后只剩一个指令词才算指令。"""
        words = self.words(body)
        return len(words) == 1 and words[0].lower() in commands

    def handle_go(self, url: str, repo: str, note: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
        """`/go [极简] [补充说明]`：从对话切到交付流程。非发起 issue 上直接把它登记为发起 issue 并开跑。"""
        user = note["author"]["username"]
        rest = self.words(note["body"])[1:]
        minimal = bool(rest) and rest[0].lower() in MINIMAL_WORDS
        extra = " ".join(rest[1:] if minimal else rest)
        origin = self.state["origins"].get(url)
        if origin is not None and self.mode_of(url) == DELIVER:
            forwarded = bool(rest) and self.deliver(repo, url, self.message(url, note, True), DELIVER) is not None
            self.comment(where, f"@{user} 这个 issue 已经在交付流程中了。" + ("补充说明已转给调度者。" if forwarded else ""))
            return True
        flow = ("用户明确要求极简模式：按 ~/.hwf/PROTOCOL.md「极简模式」只做开发并提交 MR 到 release-<根编号>，不做复杂度判定"
                if minimal else "按 ~/.hwf/PROTOCOL.md 从 §0 复杂度判定开始")
        go = (f"[GitLab 收件箱] 用户在 {url}#note_{note['id']} 发出 /go：从对话模式切换到交付流程，"
              f"{flow}交付这个 issue（发起人 @{user}）。"
              + (f"\n用户补充：{extra}" if extra else ""))
        if origin is None and where.get("state", "opened") != "opened":
            self.comment(where, f"@{user} 这个 issue 已关闭，重新打开后再发 /go。")
            return True
        created = origin is None
        if created:
            # 在普通 issue 上 /go：它就成了发起 issue，之后的评论和关闭都按发起 issue 处理。
            model, thinking, error = self.scheduler_params(where.get("description", ""))
            if error:
                self.comment(where, f"@{user} {error}改好 issue 描述里的 `/model`、`/thinking` 后再发 /go。")
                return True
            origin = {"project": next(p for p, r in self.projects.items() if r == repo),
                      "project_id": where["project_id"], "iid": where["iid"], "repo": repo,
                      "initiator": user, "last_note_id": note["id"], "model": model, "thinking": thinking}
            self.state["origins"][url] = origin  # 先登记，launch 才能按它的模型参数启动
        try:
            receipt = self.deliver(repo, url, go, DELIVER)
        except InboxError:
            if created:
                del self.state["origins"][url]
            raise
        if receipt is None:
            if created:
                del self.state["origins"][url]
            return False
        origin["mode"] = DELIVER
        save_state(self.state)
        self.comment(where, f"@{user} 开始跑交付流程{'（极简模式：只开发并提交 MR 到 release-<根编号>，不评审不测试）' if minimal else ''}。{receipt}")
        return True

    def handle_note(self, url: str, repo: str, note: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
        """评论统一入口。返回 False 表示下一轮重试。

        `/status` 由收件箱直接回答、不打扰调度者；`/go` 切到交付；发起 issue 的 `/new` 走清空；其余转交并回执。
        """
        origin = self.state["origins"].get(url)
        if self.is_command(note["body"], STATUS_COMMANDS):
            self.comment(where, f"@{note['author']['username']} {self.status_text(repo, url)}")
            return True
        if origin is not None and origin.get("blocked_dialog") and note["author"]["username"] == origin["initiator"]:
            number = self.choice_of(note["body"])
            if number is not None:
                return self.handle_choice(url, origin, note, number)
        words = self.words(note["body"])
        if words and words[0].lower() in GO_COMMANDS:
            return self.handle_go(url, repo, note, where)
        if origin is not None and self.is_command(note["body"], RESET_COMMANDS):
            return self.handle_reset(url, origin, note)
        receipt = self.deliver(repo, url, self.message(url, note, origin is not None), self.mode_of(url))
        if receipt is None:
            return False
        self.comment(where, receipt)
        return True

    def message(self, url: str, note: Mapping[str, Any], origin: bool) -> str:
        if origin:
            kind = "发起 issue 的回复"
        elif f"@{self.me}".lower() in note["body"].lower():
            kind = "@提及"
        else:
            kind = "本账号参与的 issue 下的回复"
        mode = ("交付模式：按交付流程处理" if self.mode_of(url) == DELIVER else
                "对话模式：你自己不分析，按用户点名派角色只读分析（没点名派 arch），把成员结论原样转贴到 issue；"
                "用户发 /go 或明确说开工才进入交付流程；STATE 已是交付模式时以 STATE 为准")
        return (
            f"[GitLab 收件箱] {kind}（{mode}）：@{note['author']['username']} 在 {url}#note_{note['id']} 评论：\n"
            f"{note['body']}\n"
            "按 ~/.hwf/PROTOCOL.md「GitLab 收件箱」处理这条用户消息。"
        )

    def remember(self, note_id: int) -> None:
        forwarded: list[int] = self.state["forwarded"]
        forwarded.append(note_id)
        del forwarded[:-FORWARDED_CAP]

    # ---- 三类事件 ----

    def handle_assignment(self, todo: Mapping[str, Any]) -> None:
        issue, url = todo["target"], todo["target_url"]
        if url in self.state["origins"] or issue.get("state") != "opened":
            self.api(f"todos/{todo['id']}/mark_as_done", "POST")  # 重复指派或已关闭：不重复发起。
            return
        if self.label not in issue.get("labels", []):
            return  # 开关未打开：保持 pending，标签加上后下一轮再处理。
        project = todo["project"]["path_with_namespace"]
        repo = self.projects[project]
        initiator = todo["author"]["username"]
        model, thinking, error = self.scheduler_params(issue.get("description", ""))
        if error:
            # 参数写错时不猜，也不标记待办：用户改好描述后下一轮自动发起。只提示一次。
            if url not in self.state["bad_params_notified"]:
                self.comment(issue, f"@{initiator} {error}改好 issue 描述里的 `/model`、`/thinking` 后会自动开始。")
                self.state["bad_params_notified"].append(url)
                save_state(self.state)
            return
        notes = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes?sort=desc&per_page=1")
        # 先登记再走入口：launch 按登记的模型参数启动；入口失败就撤销登记，下一轮重来。
        self.state["origins"][url] = {
            "project": project, "project_id": issue["project_id"], "iid": issue["iid"],
            "repo": repo, "initiator": initiator, "mode": CHAT, "model": model, "thinking": thinking,
            "last_note_id": notes[0]["id"] if notes else 0,  # 已有评论由调度者自行读取，不重复转交。
        }
        # 指派只开对话：调度者按用户点名派角色分析、转贴结论，用户发 /go 或明确说开工才跑交付（2026-10-30 用户指示）。
        task = (f"{url}（GitLab 收件箱：@{initiator} 指派并打开 {self.label}，发起 issue，发起人 @{initiator}；"
                "开工前只派角色做只读分析，调度者自己不分析）")
        try:
            launched = self.launch(repo, self.entry_task(task, CHAT), url, url)
        except InboxError:
            del self.state["origins"][url]
            raise
        if not launched:
            # 冷却期内：同一 issue 刚因 @提及走过入口，调度者正在路上，照常登记为发起 issue。不能留到下一轮：
            # 调度者一在 issue 下评论，GitLab 就把本账号在该 issue 的待办全部自动标记完成，指派就再也收不到了。
            log(f"{url}: entry already launched for this issue, tracking it as origin")
        save_state(self.state)
        self.api(f"todos/{todo['id']}/mark_as_done", "POST")
        if launched:
            log(f"launched team-up for {url}")

    def handle_mention(self, todo: Mapping[str, Any]) -> None:
        match = NOTE_RE.search(todo["target_url"])
        if not match:
            self.api(f"todos/{todo['id']}/mark_as_done", "POST")  # 写在正文里的 @，交付流程会读正文。
            return
        note_id = int(match.group(1))
        issue = todo["target"]
        url = todo["target_url"].split("#", 1)[0]
        if note_id not in self.state["forwarded"]:
            note = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes/{note_id}")
            repo = self.projects[todo["project"]["path_with_namespace"]]
            if not self.handle_note(url, repo, note, issue):
                return
            self.remember(note_id)
            self.follow(url, todo["project"]["path_with_namespace"], issue["iid"])
            save_state(self.state)
        self.api(f"todos/{todo['id']}/mark_as_done", "POST")

    def follow(self, url: str, project: str, iid: int) -> None:
        """记下用户 @ 过本账号的 issue：之后这里的评论不 @ 也转交。"""
        followed: dict[str, Any] = self.state["followed"]
        followed.pop(url, None)
        followed[url] = {"project": project, "iid": iid}
        for stale in list(followed)[:-FOLLOWED_CAP]:
            del followed[stale]

    def poll_followed(self) -> None:
        """本账号参与的非发起 issue：白名单用户的新评论不要求 @ 也转交（事件 4）。

        每个项目两次查询拿到上一轮以来有更新的参与 issue（本账号建的 + 用户 @ 过的），再取其中的新评论。
        首轮只记下时间，不回放历史评论。有评论转交失败就不推进水位，下一轮重试；已转交的靠 forwarded 去重。
        """
        now = time.time()
        since = self.state.get("follow_since")
        if since is None:
            self.state["follow_since"] = now
            save_state(self.state)
            return
        floor = since - FOLLOW_OVERLAP_S
        after = quote(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(floor)))
        for project, repo in self.projects.items():
            base = f"projects/{quote(project, safe='')}/issues?scope=all&updated_after={after}&per_page=100"
            issues = list(self.api(f"{base}&author_username={self.me}"))
            iids = [f["iid"] for f in self.state["followed"].values() if f["project"] == project]
            if iids:
                issues += self.api(base + "".join(f"&iids[]={iid}" for iid in iids))
            seen: set[str] = set()
            for issue in issues:
                url = issue["web_url"]
                if url in seen or url in self.state["origins"]:
                    continue  # 发起 issue 由 poll_origin 按评论水位处理。
                seen.add(url)
                if not self.poll_followed_issue(url, repo, issue, floor):
                    return
        self.state["follow_since"] = now
        save_state(self.state)

    def poll_followed_issue(self, url: str, repo: str, issue: Mapping[str, Any], floor: float) -> bool:
        notes = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes?sort=desc&per_page=100")
        fresh = sorted((n for n in notes
                        if not n.get("system") and n["author"]["username"] in self.allowed
                        and n["id"] not in self.state["forwarded"] and parse_time(n["created_at"]) >= floor),
                       key=lambda n: n["id"])
        for note in fresh:
            if not self.handle_note(url, repo, note, issue):
                return False
            self.remember(note["id"])
            save_state(self.state)
        return True

    def track_reopened(self) -> None:
        """把被用户重新打开的发起 issue 接回跟踪（关闭时移到 closed_origins 保留水位）。

        一次查询拿到指派给本账号、带开关标签、仍打开的 issue；在 closed_origins 里却不在 origins 的，就是被
        重开了。接回后调度者重新被占用，重开后的评论照常回到原任务，所以不另发通知：评论本身就是用户要它做的事。
        """
        for project, repo in self.projects.items():
            issues = self.api(f"projects/{quote(project, safe='')}/issues?state=opened&assignee_username={self.me}"
                              f"&labels={quote(self.label)}&per_page=100")
            for issue in issues:
                url = issue["web_url"]
                if url in self.state["origins"]:
                    continue
                origin = self.state["closed_origins"].pop(url, None)
                if origin is None:
                    continue  # 不是关闭过的发起 issue：新指派交给指派待办处理。
                self.state["origins"][url] = origin
                save_state(self.state)
                log(f"origin reopened by user: {url}")

    def poll_origin(self, url: str, origin: dict[str, Any]) -> None:
        base = f"projects/{origin['project_id']}/issues/{origin['iid']}"
        issue = self.api(base)
        # 先处理新评论再看是否关闭：GitLab「评论并关闭」会同时产生评论和关闭，先关会丢掉这条评论
        # （例如 `/new`）。评论没处理完就不做关闭收尾，下一轮接着来，调度者在此期间保持占用。
        notes = self.api(f"{base}/notes?sort=desc&per_page=100")
        fresh = sorted((n for n in notes if n["id"] > origin["last_note_id"]), key=lambda n: n["id"])
        for note in fresh:
            wanted = (not note.get("system") and note["author"]["username"] in self.allowed
                      and note["id"] not in self.state["forwarded"])
            if wanted:
                if not self.handle_note(url, origin["repo"], note, origin):
                    return  # 水位停在这里，下一轮从这条继续。
                self.remember(note["id"])
            origin["last_note_id"] = note["id"]
            save_state(self.state)
        if issue.get("state") != "opened":
            # 用户关闭发起 issue = 交付结束，调度者从此可接新需求；通知它收尾（失败只记录，不阻塞释放）。
            _, pane = self.owner(origin["repo"], url)
            if pane:
                self.prompt(pane, f"[GitLab 收件箱] 用户已关闭发起 issue {url}：把 INDEX 中该任务状态改为 "
                                  "closed（若还不是收尾状态），停止推进，并按 ~/.hwf/PROTOCOL.md §9「成员收尾与收尾回执」"
                                  "关闭测试服务、成员 pane 与 worktree，在发起 issue 发收尾回执。")
            closed: dict[str, Any] = self.state["closed_origins"]
            closed[url] = self.state["origins"].pop(url)  # 保留水位：用户重开后接着处理关闭后的评论。
            for stale in list(closed)[:-CLOSED_CAP]:
                del closed[stale]
            save_state(self.state)
            log(f"origin closed by user: {url}")
            return
        self.watch_blocked(url, origin)

    def watch_blocked(self, url: str, origin: dict[str, Any]) -> None:
        """调度者卡在审批/提问界面时，它自己无法发评论，由收件箱在发起 issue @发起人。

        读得出单选题就贴出题目和编号选项，发起人回数字即可远程作答；题目换了（同一次卡住里出了下一题）再贴一次。
        读不出就只提醒一次去电脑上处理。恢复后清除，下次卡住再提醒。
        """
        _, pane = self.owner(origin["repo"], url)
        status = self.agent_state(pane)[0] if pane else ""
        if status != "blocked":
            if origin.get("blocked_notified") or origin.get("blocked_dialog"):
                origin["blocked_notified"] = False
                origin.pop("blocked_dialog", None)
                save_state(self.state)
            return
        dialog = parse_dialog(self.read_screen(pane)) if pane else None
        if dialog is not None:
            known = origin.get("blocked_dialog") or {}
            if known.get("fingerprint") != dialog["fingerprint"]:
                self.comment(origin, f"@{origin['initiator']} ⚠️ 调度者在等你选择，选好之前它收不到新评论。\n\n"
                                     + self.dialog_text(dialog))
                origin["blocked_dialog"] = dialog
                origin["blocked_notified"] = True
                save_state(self.state)
            return
        if not origin.get("blocked_notified"):
            self.comment(origin, f"@{origin['initiator']} ⚠️ 调度者停在一个需要人工确认的界面，自己没法继续，"
                                 "也收不到新评论。请到电脑上打开它的窗口处理。")
            origin["blocked_notified"] = True
            save_state(self.state)

    @staticmethod
    def dialog_text(dialog: Mapping[str, Any]) -> str:
        lines = [f"**{dialog['question']}**", ""] if dialog["question"] else []
        for i, option in enumerate(dialog["options"], 1):
            typing = "（要打字，只能到电脑上选）" if option["typing"] else ""
            lines.append(f"{i}. {option['label']}{typing}")
            if option["detail"]:
                lines.append(f"   {option['detail']}")
        lines += ["", "直接回复编号（例如 `1`）就会替你选上。"]
        return "\n".join(lines)

    def read_screen(self, pane: str) -> str:
        result = self._exec(["herdr", "agent", "read", pane, "--source", "visible"])
        return result.stdout if result.returncode == 0 else ""

    def choice_of(self, body: str) -> int | None:
        """评论是不是「选第几项」：只有一个数字（可带「选」「第」「项」，可带 @本账号 与飞书来源尾巴）。"""
        text = " ".join(self.words(body.replace(FEISHU_SUFFIX, "")))
        match = CHOICE_REPLY.match(text.strip())
        return int(match.group(1)) if match else None

    def handle_choice(self, url: str, origin: dict[str, Any], note: Mapping[str, Any], number: int) -> bool:
        """发起人回数字：核对调度者仍卡在同一道题，再按方向键移到该项并回车；结果回帖。不重试按键。"""
        user = note["author"]["username"]
        _, pane = self.owner(origin["repo"], url)
        if not pane or self.agent_state(pane)[0] != "blocked":
            origin.pop("blocked_dialog", None)
            save_state(self.state)
            self.comment(origin, f"@{user} 调度者已经不在选择界面了，这次没有按键。要它做什么请直接说。")
            return True
        dialog = parse_dialog(self.read_screen(pane))
        known = origin.get("blocked_dialog") or {}
        if dialog is None or dialog["fingerprint"] != known.get("fingerprint"):
            if dialog is None:
                self.comment(origin, f"@{user} 界面变了，读不出选项，这次没有按键。请到电脑上处理。")
                origin.pop("blocked_dialog", None)
            else:
                self.comment(origin, f"@{user} 界面上的题目已经换了，这次没有按键。新的题目：\n\n" + self.dialog_text(dialog))
                origin["blocked_dialog"] = dialog
            origin["blocked_notified"] = True
            save_state(self.state)
            return True
        options = dialog["options"]
        if not 1 <= number <= len(options):
            self.comment(origin, f"@{user} 没有第 {number} 项，请回复 1 到 {len(options)} 之间的数字。")
            return True
        option = options[number - 1]
        if option["typing"]:
            self.comment(origin, f"@{user} 「{option['label']}」要打字回答，只能到电脑上选。")
            return True
        steps = number - 1 - dialog["cursor"]
        keys = ["down"] * steps if steps > 0 else ["up"] * -steps
        result = self._exec(["herdr", "agent", "send-keys", pane, *keys, "enter"])
        if result.returncode != 0:
            log(f"send-keys to {pane} failed: {result.stderr.strip()}")
            self.comment(origin, f"@{user} 按键没发出去（{result.stderr.strip()[:100]}），请到电脑上选。")
            return True
        log(f"{url}: chose option {number} for {user} on {pane}")
        self.sleep(CHOICE_SETTLE_S)
        still = self.agent_state(pane)[0] == "blocked" and (parse_dialog(self.read_screen(pane)) or {}).get(
            "fingerprint") == dialog["fingerprint"]
        if still:
            self.comment(origin, f"@{user} 已按第 {number} 项「{option['label']}」，但界面还停在这道题上，"
                                 "可能没生效，请到电脑上看看。")
            return True
        origin.pop("blocked_dialog", None)
        save_state(self.state)
        self.comment(origin, f"@{user} 已替你选了第 {number} 项：{option['label']}。")
        return True

    # ---- 一轮 ----

    def poll(self) -> None:
        if not self.me:
            self.me = self.api("user")["username"]
        # 先接回重开的发起 issue：同一轮里的 @ 待办（如 `/new @本账号`）才会按发起 issue 处理。
        self.track_reopened()
        todos = self.api("todos?state=pending&type=Issue&per_page=100")
        for todo in todos:
            relevant = (todo["author"]["username"] in self.allowed
                        and todo["project"]["path_with_namespace"] in self.projects)
            if not relevant:
                continue  # 非白名单/非登记项目：不处理也不标记，留给人看。
            action = todo["action_name"]
            if action == "assigned":
                self.handle_assignment(todo)
            elif action in ("mentioned", "directly_addressed"):
                self.handle_mention(todo)
        for url, origin in list(self.state["origins"].items()):
            self.poll_origin(url, origin)
        self.poll_followed()


def parse_dialog(screen: str) -> dict[str, Any] | None:
    """从屏幕文字读出最下面那道单选题：{question, options[{label, detail, typing}], cursor, fingerprint}。

    认 omp 的 Ask 框（「○」选项、外框「│」）和 Claude Code 的编号选项（「1. Yes」）；光标行带「❯」。
    读不出、少于两个选项、编号不连续或是多选题时返回 None，交给人到电脑上处理，不猜。
    """
    lines = [line.strip().strip("│┃").strip() for line in screen.splitlines()]
    last = next((i for i in range(len(lines) - 1, -1, -1) if DIALOG_OPTION.match(lines[i])), None)
    if last is None:
        return None
    start = last  # 往上找到这一组的顶：遇到空行或分隔线为止
    while start > 0 and lines[start - 1] and not DIALOG_RULE.match(lines[start - 1]):
        start -= 1
    block = lines[start:last + 1]
    if any(DIALOG_CHECKBOX.match(line) for line in block):
        return None
    first = next(i for i, line in enumerate(block) if DIALOG_OPTION.match(line))
    question = block[:first]
    if not question:  # omp：题目在分隔线上面
        top = start - 1
        while top > 0 and lines[top] and DIALOG_RULE.match(lines[top]):
            top -= 1
        while top >= 0 and lines[top] and not DIALOG_RULE.match(lines[top]) and len(question) < 4:
            question.insert(0, lines[top])
            top -= 1
    options: list[dict[str, Any]] = []
    cursor, numbers = 0, []
    for line in block[first:]:
        match = DIALOG_OPTION.match(line)
        if match:
            if match.group("cursor"):
                cursor = len(options)
            if match.group("num"):
                numbers.append(int(match.group("num")))
            label = match.group("label")
            options.append({"label": label, "detail": "", "typing": bool(DIALOG_TYPING.search(label))})
        else:
            options[-1]["detail"] = f"{options[-1]['detail']} {line}".strip()
    if len(options) < 2 or (numbers and numbers != list(range(1, len(options) + 1))):
        return None
    text = " ".join(question)
    fingerprint = json.dumps([text, [o["label"] for o in options]], ensure_ascii=False)
    return {"question": text, "options": options, "cursor": cursor, "fingerprint": fingerprint}


def parse_time(text: str) -> float:
    """GitLab 时间（如 2026-10-27T09:00:00.123Z 或带 +08:00）转时间戳。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def load_config(shared_path: Path = CONFIG_PATH, local_path: Path = LOCAL_CONFIG_PATH) -> dict[str, Any] | None:
    """合并入库配置与本机配置；projects 取入库列表里本机配了路径的项目。"""
    shared = load_json(shared_path, None)
    if shared is None:
        return None
    local = load_json(local_path, {})
    paths = dict(local.get("projects", {}))
    wanted = shared.get("projects", [])
    if isinstance(wanted, dict):  # 旧格式：入库文件里直接写了路径
        paths = {**wanted, **paths}
        wanted = list(wanted)
    merged = {**shared, **{k: v for k, v in local.items() if k != "projects"}}
    merged["projects"] = {p: paths[p] for p in wanted if p in paths}
    for p in wanted:
        if p not in paths:
            log(f"skip {p}: no local repo path in {local_path.name}")
    return merged


def save_state(state: Mapping[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE_PATH)  # 原子替换，避免中途退出留下半截状态。


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single polling round")
    args = parser.parse_args(argv)
    if os.environ.get("HERDR_ENV") != "1":
        print("gitlab-inbox must run inside a Herdr-managed pane", file=sys.stderr)
        return 1
    config = load_config()
    if config is None:
        print(f"missing config {CONFIG_PATH}", file=sys.stderr)
        return 1
    inbox = Inbox(config, load_json(STATE_PATH, {}))
    while True:
        try:
            inbox.poll()
        except (InboxError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            # 单轮失败（网络、glab、herdr）不退出常驻进程；--once 时如实返回失败。
            log(f"poll failed: {error}")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(int(config.get("interval_seconds", 60)))


if __name__ == "__main__":
    sys.exit(main())
