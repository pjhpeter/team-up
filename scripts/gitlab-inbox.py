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
     按用户点名派角色只读分析，没点名派 arch），用户发 `/go` 或明确说开工才进入交付流程；`/go debug` 或单独的 debug、改bug、修bug、极简、minimal 模式指令进入 debug 流程：普通开发角色保留单测，develop 部署后自动化验收，再提交 release MR。
     issue 描述里可写 `/model <模型>`、`/thinking <强度>` 两行指定调度者参数。
     入口复用空闲调度者或新建一个，允许多个调度者并行，不必等在途交付结束。
     本账号自己指派（调度者拆出的子 issue）作者不在白名单，天然被忽略，不会重复发起。
     标签后加也能触发：未带标签时待办保持 pending，下一轮再看。
  2. 发起 issue 的新评论（不要求 @）→ 转交调度者，这是用户回复调度者提问的通道。
     评论正文只有 `/new` 或 `/clear` 时是清空指令：收件箱让该调度者开新会话，再让它按 INDEX/STATE 接续本任务。
     任何 issue 里只有 `/status` 的评论由收件箱直接回复进度，不打扰调度者。
     只有 `/免打扰`（`/quiet`）或 `/详尽`（`/verbose`）的评论切换本交付的通知模式：转给调度者记入 STATE，
     收件箱回一条确认，调度者不再另发确认评论。
  用户回复「状态:等你处理」的 issue 时，收件箱按标签历史把它改回进入等待前的阶段，并告诉调度者（仍要用户处理时由调度者改回）。
  ~/.hwf/PROTOCOL.md、protocol/、skills/ 或 herdr/ 有新提交时，提醒名下有未收尾交付的在线调度者看改动。
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
关闭后收件箱转交关闭事实，由调度者按协议核对剩余工作；关闭事件本身不证明交付完成。
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
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import gitlab_projects as GP
import jev_client as JC
import jev_gates as JG
import poll_backoff as PB

JEV_GO_AUTH_MIN = 0.90
JEV_DEBUG_FLOW_MIN = 0.80

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
# 通知模式（见 protocol/communication.md「通知模式」）：免打扰 = 飞书私聊只推要用户决定、实质答复和整项交卷（步骤汇总留 GitLab）；详尽 = 每个小阶段都发。
QUIET, VERBOSE = "quiet", "verbose"
NOTIFY_COMMANDS = {"/免打扰": QUIET, "/quiet": QUIET, "/详尽": VERBOSE, "/verbose": VERBOSE}
NOTIFY_LABELS = {QUIET: ("免打扰", "之后私聊只在需要你决定、答复你问题和整项交付完成时发消息（步骤与过程记录留 GitLab）"),
                 VERBOSE: ("详尽", "之后每个小阶段都会发消息")}
# `/go` 后的模式词或整条独立模式指令进入 debug 流程，见 protocol/implementation.md「debug 模式」。
# 兼容两个词之间的空格，例如「debug 模式」「改 bug」「修 bug」「极简 模式」；普通正文交调度者判断。
DEBUG_WORDS = frozenset({"debug", "debug模式", "改bug", "修bug", "极简", "极简模式", "minimal"})
# 发起 issue 的两种模式：对话 = 只读 + 评论答疑；交付 = 跑完整交付流程。指派后默认对话，/go 切到交付。
CHAT, DELIVER = "chat", "deliver"
CHAT_TAG = "【对话模式】"  # team-up 需求文本前缀，调度者据此只派人分析、不进入交付流程
STATUS_WORDS = {"working": "正在干活", "idle": "空闲", "done": "空闲",
                "blocked": "卡在确认界面（需要到电脑上处理）", "unknown": "状态无法判断"}
# 参与 issue 按 updated_after 增量查询；往回多看一段，抵消本机与 GitLab 的时钟差，重复的评论靠 forwarded 去重。
FOLLOW_OVERLAP_S = 300
FOLLOWED_CAP = 100  # 用户 @ 过本账号的非发起 issue，一次 iids[] 查询能装下的量
FOLLOWED_CLOSED_RETENTION_S = 7 * 86400  # 已关闭满 7 天的跟进 issue 自动移出 followed，再次 @ 时会自动接回
FOLLOWED_PRUNE_INTERVAL_S = 3600  # 低频批量巡检久未更新的已关跟进单
MAX_IDLE_SCHEDULER_SESSION_BYTES = 30 * 1024 * 1024  # 完全空闲且无在途任务的调度者会话超过 30MB 时自动轮换
CLOSED_CAP = 200  # 已关闭的发起 issue 只为“用户重开后接着跟踪”保留，留最近的即可。
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
STAGE_PREFIX = "状态:"
WAITING_LABEL = "状态:等你处理"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")  # omp --thinking 可选值
# 每次投递都把通知规则放回上下文，避免调度者只在首次启动时读过、续跑后忘记。
# 成员 skill 与 herdr 角色配置同样决定在途交付行为，改动一并提醒在途调度者。
PROTOCOL_PATHS = ("PROTOCOL.md", "protocol/", "skills/", "herdr/")
NOTIFY_REMINDER = (
    "\n[通知] issue 评论只用 task-record.py note，发前按 ~/.hwf/protocol/communication.md「通知模式」「发布入口」核对；"
    "落实完用户中途提问或修改指令并再次停下等待前，须发 --kind answer 回报结果。"
)


class InboxError(RuntimeError):
    pass


_EVENT_LOG_PATH: Path | None = None
_EVENT_LOG_MAX_BYTES = 2 * 1024 * 1024


def append_event_log(path: Path | None, line: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _EVENT_LOG_MAX_BYTES:
            tail = path.read_bytes()[-(_EVENT_LOG_MAX_BYTES // 2):]
            nl = tail.find(b"\n")
            path.write_bytes(tail[nl + 1:] if nl != -1 else tail)
        flat = " ".join(line.splitlines())
        with path.open("a", encoding="utf-8") as out:
            out.write(flat + "\n")
    except OSError:
        pass


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
    print(line, flush=True)
    append_event_log(_EVENT_LOG_PATH, line)


class Inbox:
    def __init__(self, config: Mapping[str, Any], state: dict[str, Any], run: Runner = subprocess.run,
                 *, jev: Optional[JC.JevClient] = None):
        self.host: str = config["host"]
        self.allowed = set(config["allowed_users"])
        self.label: str = config.get("label", "herdr-auto")
        self.projects: dict[str, str] = dict(config.get("projects") or {})
        self.dynamic_projects: bool = (
            bool(config["dynamic_projects"]) if "dynamic_projects" in config else not bool(self.projects)
        )
        self.state = state
        state.setdefault("origins", {})      # issue URL → {project, iid, repo, initiator, last_note_id}
        state.setdefault("forwarded", [])    # 已转交的评论 id
        state.setdefault("launched_at", {})  # 任务标识或 issue URL → 最近一次走入口的时间戳
        state.setdefault("closed_origins", {})  # 用户关闭的发起 issue（含水位），重开时接回 origins
        state.setdefault("bad_params_notified", [])  # 已提示过模型参数写错的 issue，避免每轮重复评论
        state.setdefault("followed", {})     # 用户 @ 过本账号的 issue URL → {project, iid}
        state.setdefault("watched_issues", {})  # 跨项目登记给本人/机器人的待跟进 issue，供飞书秘书同步提醒
        state.setdefault("user_todo_watermark", None)
        state.setdefault("user_assigned_since", "")
        self.run = run
        self.sleep: Callable[[float], None] = time.sleep
        self.me: str = ""
        self.labels_seen: dict[str, list[str]] = {}  # 本轮查到的发起 issue 标签，省一次查询
        self.project_cache = GP.ProjectActivityCache()
        self.repo_resolver = GP.RepoResolver(
            self.host,
            self.projects,
            code_root=Path(config.get("code_root") or (Path.home() / "code")),
            hwf_home=HWF_HOME,
            local_config_path=Path(config.get("local_config_path") or LOCAL_CONFIG_PATH),
            run=run,
            log_fn=log,
        )
        self.auto_clone: bool = bool(config.get("auto_clone", self.dynamic_projects))
        self._user_token_spec = config.get("user_token")
        self._user_token_loaded = False
        self._user_token_value = ""
        self.jev = jev if jev is not None else JC.JevClient(
            config.get("typesafe") if "typesafe" in config else None
        )

    # ---- 外部命令 ----

    def _exec(self, argv: Sequence[str], *, cwd: str | None = None, timeout: int = CMD_TIMEOUT_S,
              env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        kw: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout, "cwd": cwd, "check": False}
        if env is not None:
            kw["env"] = dict(env)
        return self.run(list(argv), **kw)

    def api(self, path: str, method: str = "GET", *, env: Mapping[str, str] | None = None) -> Any:
        result = self._exec(["glab", "api", "--hostname", self.host, "-X", method, path], env=env)
        if result.returncode != 0:
            raise InboxError(f"glab api {method} {path} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _user_token(self) -> str:
        """按需读取本人只读 GitLab token，仅用于 GET 拉取指派/提及本人的 issue，绝不改本人待办状态。"""
        if self._user_token_loaded:
            return self._user_token_value
        self._user_token_loaded = True
        if self._user_token_spec is not None:
            self._user_token_value = str(self._user_token_spec).strip()
            return self._user_token_value
        if not self.dynamic_projects or self.run is not subprocess.run:
            return ""
        self._user_token_value = GP.read_keychain_token(GP.default_user_token_spec(HWF_HOME))
        return self._user_token_value

    def is_project_allowed(self, project: str) -> bool:
        if not project:
            return False
        if not self.dynamic_projects:
            return project in self.projects
        return self.project_cache.is_active(project, self.api)

    def ensure_repo(self, project: str) -> str:
        """解析本地代码仓；动态全项目模式下按需自动 clone 到 ~/code/<仓库名> 并写回本机配置。"""
        if not self.dynamic_projects:
            return self.projects.get(project, "")
        repo = self.projects.get(project, "")
        if repo and Path(repo).exists():
            return repo
        existing = self.repo_resolver.find_existing(project)
        if existing:
            self.projects[project] = existing
            return existing
        if repo:
            return repo
        if not self.auto_clone:
            return ""
        cloned = self.repo_resolver.ensure(project)
        if cloned:
            self.projects[project] = cloned
        return cloned

    def primary_allowed_user(self) -> str:
        return sorted(self.allowed)[0] if self.allowed else "pengjh"

    def register_watched_issue(
        self,
        *,
        url: str,
        project: str,
        project_id: Any,
        iid: int,
        title: str,
        kind: str,
        actor: str,
        event_key: str,
        status: str,
        note_url: str = "",
        excerpt: str = "",
    ) -> None:
        """登记指派或提及本人/机器人的 issue，供飞书秘书主动推送提醒与总览展示。"""
        canon_url = GP.canon_issue_url(url)
        watched: dict[str, Any] = self.state["watched_issues"]
        watched[canon_url] = {
            "url": canon_url,
            "project": project,
            "project_id": project_id,
            "iid": iid,
            "title": title,
            "kind": kind,
            "actor": actor,
            "event_key": event_key,
            "status": status,
            "note_url": note_url,
            "excerpt": excerpt[:200],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # 避免跨项目长期积累无限膨胀，最多保留最近 200 条登记记录
        for stale in list(watched)[:-200]:
            del watched[stale]

    def owner(self, repo: str, url: str) -> tuple[str | None, str | None]:
        """返回 (任务标识, 在线调度者 pane)，都可能为 None；发现失败抛错（fail closed）。"""
        info = self.owner_info(repo, url)
        return info["task"], info["pane"]

    def owner_info(self, repo: str, url: str) -> dict[str, Any]:
        result = self._exec([sys.executable, str(FIND_SCHEDULER), repo, "--owner-of", url])
        if result.returncode != 0:
            raise InboxError(f"find-scheduler exited {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def agent_state(self, pane: str) -> str:
        """只读会话进程状态；终端标题可能属于同一调度者的其他任务。"""
        result = self._exec(["herdr", "agent", "get", pane])
        if result.returncode != 0:
            return "unknown"
        agent = json.loads(result.stdout)["result"]["agent"]
        return str(agent.get("agent_status") or "unknown")

    def status_text(self, repo: str, url: str, where: Mapping[str, Any]) -> str:
        """任务进度只读目标 issue；调度者进程状态另列，不能把共享会话当成任务进度。"""
        # 复用评论入口已经解析好的项目和编号，不从调度者任务名或其他 issue 的编号猜归属。
        issue = self.api(f"projects/{where['project_id']}/issues/{where['iid']}")
        state = issue.get("state")
        if state == "closed":
            progress = "已关闭（不代表已验收或交付完成）"
        elif state == "opened":
            labels = [label for label in issue.get("labels", []) if label.startswith("状态:")]
            progress = "打开中；" + ("、".join(labels) if labels else "未设置状态标签")
        else:
            raise InboxError(f"Issue state unavailable for {url}")
        result = f"目标 issue {url}：{progress}。"
        # 进度区的三行状态由 task-record.py progress 写在描述里，也来自目标 issue，一并带上（2026-11-19 #484）。
        area = re.search(r"<!-- hwf:start.*?<!-- hwf:end -->", issue.get("description") or "", re.S)
        lines = re.findall(r"^\*\*(现在|下一步|等你处理)\*\*：(.+?)\s*$", area.group(0) if area else "", re.M)
        if lines:
            result += "\n" + "\n".join(f"- {name}：{value}" for name, value in lines) + "\n"
        info = self.owner_info(repo, url)
        if info["task"] is None:
            return result + "会话状态：没有对应的调度者记录。"
        pane = info["live_pane"]
        if pane is None:
            return result + f"会话状态：负责的调度者 `{info['scheduler']}` 已退出。"
        status = self.agent_state(pane)
        other = "（它手上还有别的任务，新消息会交给空闲调度者接手）" if info["pane"] is None else ""
        return result + f"会话状态：调度者 `{info['scheduler']}` {STATUS_WORDS.get(status, status)}{other}。"

    def rotate_oversized_idle_schedulers(self) -> list[str]:
        """接新单或轮询收尾时，为名下无任何在途任务且会话文件超过阈值的空闲调度者自动开新会话。"""
        result = self._exec(["herdr", "agent", "list"])
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            agents = json.loads(result.stdout)["result"]["agents"]
        except (ValueError, KeyError, TypeError):
            return []
        oversized: list[tuple[dict[str, Any], int]] = []
        for agent in agents:
            name = str(agent.get("name") or "")
            pane = str(agent.get("pane_id") or "")
            status = str(agent.get("agent_status") or "")
            session_file = str((agent.get("agent_session") or {}).get("value") or "")
            if not name.startswith("delivery-scheduler-") or not pane or status not in ("idle", "done") or not session_file:
                continue
            try:
                size = Path(session_file).stat().st_size
            except OSError:
                continue
            if size > MAX_IDLE_SCHEDULER_SESSION_BYTES:
                oversized.append((agent, size))
        if not oversized:
            return []
        fs = find_scheduler_module()
        repo_paths = set(self.projects.values()) | {
            str(origin.get("repo")) for origin in self.state["origins"].values() if origin.get("repo")
        }
        busy_names = {
            row.scheduler
            for repo_path in repo_paths
            for row in fs.active_rows(repo_path)
            if row.scheduler
        }
        for url, origin in self.state["origins"].items():
            repo_path = origin.get("repo")
            if repo_path:
                try:
                    owner_sched = fs.owner_of(url, repo_path, agents).get("scheduler")
                except Exception:
                    owner_sched = None
                if owner_sched:
                    busy_names.add(str(owner_sched))
        rotated: list[str] = []
        for agent, size in oversized:
            name = str(agent["name"])
            pane = str(agent["pane_id"])
            if name in busy_names:
                continue
            res = self._exec([sys.executable, str(RESET_SESSION), pane])
            if res.returncode == 0:
                rotated.append(name)
                log(f"rotated oversized idle scheduler {name} ({pane}, {size / (1024 * 1024):.1f}MB) -> {res.stdout.strip()}")
            else:
                log(f"skip rotating scheduler {name} ({pane}): {res.stderr.strip()}")
        return rotated

    def launch(self, repo: str, task: str, key: str, url: str | None = None) -> bool:
        """走 team-up 入口；key（任务标识或 issue URL）冷却期内不重复走，返回是否已发出。

        url 是发起 issue 时带上它记录的模型与思考强度，接手的新调度者用同样参数。
        """
        if time.time() - self.state["launched_at"].get(key, 0) < LAUNCH_COOLDOWN_S:
            log(f"{key}: entry launched recently, waiting for scheduler to register")
            return False
        self.rotate_oversized_idle_schedulers()
        origin = self.state["origins"].get(url or "") or {}
        result = self._exec(
            ["hwf", "run", "team-up", "--detach", "--json", "--input", f"task={task}{NOTIFY_REMINDER}",
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
        result = self._exec(["herdr", "agent", "prompt", pane, text + NOTIFY_REMINDER])
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
            "~/.hwf/PROTOCOL.md 的阅读入口读取 ~/.hwf/protocol/scheduling.md，用 herdr agent get 核对登记身份（不要改名），再按 .hwf/tmp/INDEX.md 与该任务 "
            "STATE.md 接续；在 STATE 记录新会话。结束回合或收到关闭通知前，按 ~/.hwf/protocol/delivery.md §9「完成评论与关闭前核对」「发起 issue 的关闭授权」核验剩余工作。"))
        return result.stdout

    def comment(self, where: Mapping[str, Any], body: str) -> None:
        """在 issue 下评论；where 只需 project_id 与 iid（发起 issue 记录和待办 target 都有）。"""
        self.api(f"projects/{where['project_id']}/issues/{where['iid']}/notes?body={quote(body)}", "POST")

    # ---- 投递 ----

    def deliver(self, repo: str, url: str, text: str, mode: str = CHAT) -> str | None:
        """发给负责该 issue 的调度者；没有在线负责人时把消息作为需求走入口（按 mode 决定对话还是交付）。

        返回给用户的回执（None 表示没送出，下一轮重试）。调度者在一个回合里时消息要排队到回合结束，
        可能几分钟才回复；回执只确认消息转交，不代表已执行或完成。
        """
        task, pane = self.owner(repo, url)
        if pane:
            if not self.prompt(pane, text):
                return None
            return f"已收到，已转交目标 issue {url} 的消息，等待调度者回复。"
        if not self.launch(repo, self.entry_task(text, mode), task or url, url):
            return None
        return f"已收到目标 issue {url} 的消息。目前没有在线负责人，已转交调度入口，等待接手后回复。"

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

    def handle_notify(self, url: str, repo: str, note: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
        """`/免打扰`、`/详尽`：切换本交付的通知模式；返回 False 表示下一轮重试。"""
        user = note["author"]["username"]
        command = self.words(note["body"])[0].lower()
        mode = NOTIFY_COMMANDS[command]
        label, effect = NOTIFY_LABELS[mode]
        task, _ = self.owner(repo, url)
        if task is None:
            self.comment(where, f"@{user} 这个 issue 现在没有对应的交付，开工后再发 {command}。")
            return True
        text = (f"[GitLab 收件箱] 用户在 {url}#note_{note['id']} 发出 {command}：把本交付的通知模式切换为「{label}」，"
                "写进 STATE 顶部「通知模式」（附该 note 链接），之后按 ~/.hwf/protocol/communication.md「通知模式」执行。"
                "收件箱已在 issue 回复确认，你不要再发确认评论。")
        receipt = self.deliver(repo, url, text, self.mode_of(url))
        if receipt is None:
            return False
        origin = self.state["origins"].get(url)
        if origin is not None:
            origin["notify"] = mode
            save_state(self.state)
        self.comment(where, f"@{user} 已切到{label}模式，{effect}。{receipt}")
        return True

    def words(self, body: str) -> list[str]:
        """仅供命令解析：去掉末行飞书桥尾注和 @本账号，转交消息仍保留原文。"""
        lines = body.rstrip().rsplit("\n", 1)
        if len(lines) == 2 and lines[-1].strip() in ("_（来自飞书）_", "_（来自飞书秘书）_"):
            body = lines[0]
        return [w for w in body.split() if w.lower() != f"@{self.me}".lower()]

    def is_command(self, body: str, commands: frozenset[str]) -> bool:
        """去掉 @本账号 后只剩一个指令词才算指令。"""
        words = self.words(body)
        return len(words) == 1 and words[0].lower() in commands

    def handle_go(self, url: str, repo: str, note: Mapping[str, Any], where: Mapping[str, Any],
                  *, natural_flow: str | None = None) -> bool:
        """`/go [模式] [补充说明]`、独立模式指令或 Jev 高把握识别的自然开工指令：切到交付，必要时登记发起 issue。"""
        user = note["author"]["username"]
        words = self.words(note["body"])
        if natural_flow in ("standard", "debug"):
            debug = natural_flow == "debug"
            rest = words
            extra = " ".join(words)
            trigger_label = "明确开工指令"
        else:
            rest = words[1:] if words[0].lower() in GO_COMMANDS else words
            used = 2 if len(rest) >= 2 and "".join(rest[:2]).lower() in DEBUG_WORDS else int(bool(rest) and rest[0].lower() in DEBUG_WORDS)
            debug = used > 0
            extra = " ".join(rest[used:])
            trigger_label = "/go" if words[0].lower() in GO_COMMANDS else "独立模式指令"
        origin = self.state["origins"].get(url)
        if origin is not None and self.mode_of(url) == DELIVER:
            forwarded = bool(rest) and self.deliver(repo, url, self.message(url, note, True), DELIVER) is not None
            self.comment(where, f"@{user} 这个 issue 已经在交付流程中了。" + ("补充说明已转给调度者。" if forwarded else ""))
            return True
        flow = ("用户明确要求 debug 模式：按 ~/.hwf/protocol/implementation.md「debug 模式」领取普通开发角色 dev/dev-fe/dev-be，"
                "保留本地单元测试先行和实现前核验；不跑本地浏览器/Electron 验收，develop 部署后做真实自动化验收，通过后提交 MR 到 release-<根编号>。"
                "不做复杂度判定、方案评审、整项代码评审、arch 终审或 main 合并；按此流程"
                if debug else "按 ~/.hwf/protocol/planning.md 从 §0 复杂度判定开始")
        go = (f"[GitLab 收件箱] 用户在 {url}#note_{note['id']} 发出 {trigger_label}：从对话模式切换到交付流程，"
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
            project = (
                where.get("project")
                or GP.extract_project_from_web_url(url)
                or next((p for p, r in self.projects.items() if r == repo), "")
            )
            origin = {"project": project,
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
        self.comment(where, f"@{user} 开始跑交付流程{'（debug 模式：普通开发角色保留单测，develop 部署后自动化验收，通过后提交 release MR）' if debug else ''}。{receipt}")
        return True

    def resume_stage(self, url: str, where: Mapping[str, Any]) -> str:
        """用户回复了「等你处理」的 issue：把进度标签改回进入等待前的阶段，返回给调度者的说明（没改返回空）。

        调度者常忘了在用户答复后改回（2026-11-13 #481 停在等你处理 40 分钟，秘书一直以为要用户处理），
        所以由收件箱兜底；用户的回复若只是追问、仍要他处理，调度者再改回 `状态:等你处理`。
        找不到之前的阶段（例如登记池一建就是等你处理）就不动。失败只记日志，不影响转交。
        """
        labels = where.get("labels")
        if labels is None:
            labels = self.labels_seen.get(url)
        if labels is not None and WAITING_LABEL not in labels:
            return ""
        base = f"projects/{where['project_id']}/issues/{where['iid']}"
        try:
            if labels is None:
                labels = (self.api(base) or {}).get("labels", [])
                if WAITING_LABEL not in labels:
                    return ""
            stage = previous_stage(self.api(f"{base}/resource_label_events?per_page=100") or [])
            if stage is None:
                return ""
            self.api(f"{base}?add_labels={quote(stage)}&remove_labels={quote(WAITING_LABEL)}", "PUT")
        except InboxError as exc:
            log(f"resume stage failed for {url}: {exc}")
            return ""
        self.labels_seen[url] = [l for l in labels if l != WAITING_LABEL] + [stage]
        log(f"{url}: {WAITING_LABEL} -> {stage} after user reply")
        return (f"\n[标签] 已改回「{stage}」；仍需要用户处理，改回「{WAITING_LABEL}」，根 issue 按实际阶段同步。")

    def clear_stage_labels(self, url: str, where: Mapping[str, Any], labels: Sequence[str] | None = None) -> None:
        """已关闭的发起 issue 若仍残留 `状态:*` 标签，自动调用 PUT 移除，防止看板与巡检误判。"""
        stale = [label for label in (labels or []) if label.startswith(STAGE_PREFIX)]
        if not stale:
            return
        base = f"projects/{where['project_id']}/issues/{where['iid']}"
        try:
            self.api(f"{base}?remove_labels={quote(','.join(stale))}", "PUT")
        except InboxError as exc:
            log(f"clear stage labels failed for {url}: {exc}")
            return
        self.labels_seen[url] = [label for label in (labels or []) if not label.startswith(STAGE_PREFIX)]
        log(f"{url}: cleared stale stage labels on closed issue: {', '.join(stale)}")
    def handle_note(self, url: str, repo: str, note: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
        """评论统一入口。返回 False 表示下一轮重试。

        `/status` 允许任何人查询；`/go`、自然语言开工、`/new`、`/quiet`、拍板选项仅限白名单用户（本人）。
        其他同事 `@机器人` 只会作为只读讨论/背景转交调度者，绝不触发代码修改或交付授权。
        """
        origin = self.state["origins"].get(url)
        author = note["author"]["username"]
        authorized = author in self.allowed
        if self.is_command(note["body"], STATUS_COMMANDS):
            try:
                status = self.status_text(repo, url, where)
            except InboxError as exc:
                log(f"Status query failed for {url}: {exc}")
                status = f"目标 issue {url} 的状态查询失败，无法确认当前进度；请稍后重试 /status。"
            self.comment(where, f"@{author} {status}")
            return True
        if authorized and origin is not None and origin.get("blocked_dialog") and author == origin["initiator"]:
            number = self.choice_of(note["body"])
            if number is not None:
                self.resume_stage(url, where)
                return self.handle_choice(url, origin, note, number)
        words = self.words(note["body"])
        if authorized:
            if words and (words[0].lower() in GO_COMMANDS or
                          (len(words) <= 2 and "".join(words).lower() in DEBUG_WORDS)):
                self.resume_stage(url, where)
                return self.handle_go(url, repo, note, where)
            if origin is not None and self.is_command(note["body"], RESET_COMMANDS):
                return self.handle_reset(url, origin, note)
            if self.is_command(note["body"], frozenset(NOTIFY_COMMANDS)):
                return self.handle_notify(url, repo, note, where)
            if words and not words[0].startswith("/") and self.mode_of(url) == CHAT:
                natural_flow = self._detect_natural_go_flow(" ".join(words))
                if natural_flow is not None:
                    self.resume_stage(url, where)
                    return self.handle_go(url, repo, note, where, natural_flow=natural_flow)
        resumed = self.resume_stage(url, where) if authorized else ""
        receipt = self.deliver(
            repo, url, self.message(url, note, origin is not None, authorized=authorized) + resumed, self.mode_of(url)
        )
        if receipt is None:
            return False
        self.comment(where, receipt)
        return True

    def _detect_natural_go_flow(self, clean_body: str) -> str | None:
        """仅在对话模式下判断评论是否明确下达开工指令（noul >= 0.90）；拿不准或不可用时一律保持对话模式。"""
        if self.jev is None or not clean_body.strip():
            return None
        try:
            res = self.jev.decide(
                state={"comment_body": clean_body[:2000]},
                questions={
                    "go_authorized": JC.Noul(
                        instructions=(
                            "On a GitLab issue currently in read-only discussion (CHAT) mode, does `comment_body` "
                            "explicitly tell the coding team to start implementing code or fixing the bug NOW "
                            "(including instructions like 'fix it in debug mode', 'go ahead with option A', 'start coding'), "
                            "rather than asking questions, requesting read-only investigation, or hesitating?"
                        ),
                        criteria={
                            "true": "Direct go-ahead to implement code or fix the bug now (standard or debug mode).",
                            "false": "Questions, discussion, read-only analysis request, hesitation, or no clear instruction to modify code now.",
                        },
                    ),
                    "flow": JC.Choice(
                        "If code changes are authorized, which workflow is requested?",
                        {
                            "standard": "Normal feature or engineering delivery without mentioning debug/minimal mode.",
                            "debug": "Explicitly mentions debug mode or minimal bug-fix mode.",
                        },
                    ),
                },
            )
            go_min = JG.threshold_of("inbox_natural_go_min", JEV_GO_AUTH_MIN)
            dbg_min = JG.threshold_of("inbox_debug_flow_min", JEV_DEBUG_FLOW_MIN)
            go_prob = res.noul("go_authorized")
            if go_prob < go_min:
                JG.record_gate_event(
                    "gate3_inbox_go",
                    "stay_chat",
                    rules_evaluated=["go_authorized"],
                    near_misses={"go_authorized": go_prob} if (go_min - 0.15) <= go_prob < go_min else None,
                )
                return None
            flow, _, probs = res.choice("flow")
            chosen = "debug" if flow == "debug" and probs.get("debug", 0.0) >= dbg_min else "standard"
            JG.record_gate_event(
                "gate3_inbox_go",
                f"go_{chosen}",
                rules_evaluated=["go_authorized"],
                rules_triggered={"go_authorized": go_prob},
            )
            return chosen
        except JC.JevUnavailableError:
            return None

    def message(self, url: str, note: Mapping[str, Any], origin: bool, *, authorized: bool = True) -> str:
        if origin:
            kind = "发起 issue 的回复"
        elif f"@{self.me}".lower() in note["body"].lower():
            kind = "@提及"
        else:
            kind = "本账号参与的 issue 下的回复"
        mode = "交付模式" if self.mode_of(url) == DELIVER else "对话模式：你自己不分析，派用户点名角色只读分析（没点名派 arch）并原样转贴；/go 或明确开工才交付，STATE 已是交付模式时以 STATE 为准"
        auth_note = "" if authorized else "\n[权限] 发言人不是本人，仅限只读分析或补充背景，禁止视作 /go 改代码或合并授权。"
        return (
            f"[GitLab 收件箱] {kind}（{mode}）：@{note['author']['username']} 在 {url}#note_{note['id']} 评论：\n"
            f"{note['body']}{auth_note}\n"
            "[归属] 本条只属于上方 issue，授权不用于其他 issue；按 ~/.hwf/protocol/communication.md「GitLab 收件箱」"
            "处理，独立新需求按其「独立需求与飞书话题」另建。"
        )

    def remember(self, note_id: int) -> None:
        forwarded: list[int] = self.state["forwarded"]
        forwarded.append(note_id)
        del forwarded[:-FORWARDED_CAP]

    # ---- 三类事件 ----

    def handle_assignment(self, todo: Mapping[str, Any]) -> None:
        issue, url = todo["target"], GP.canon_issue_url(todo["target_url"])
        if url in self.state["origins"] or issue.get("state") != "opened":
            self.api(f"todos/{todo['id']}/mark_as_done", "POST")  # 重复指派或已关闭：不重复发起。
            return
        if not self.dynamic_projects and self.label not in issue.get("labels", []):
            return  # 显式单仓白名单模式下保留开关标签门禁
        project = todo["project"]["path_with_namespace"]
        repo = self.ensure_repo(project)
        if not repo:
            log(f"skip assignment for {url}: failed to resolve or clone local repo for {project}")
            return
        actor = todo["author"]["username"]
        initiator = actor if actor in self.allowed else self.primary_allowed_user()
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
        # 指派只开只读对话分析，本人发 /go 或明确开工才跑交付。
        reason = f"@{actor} 指派给机器人" if self.dynamic_projects else f"@{actor} 指派并打开 {self.label}"
        task = (f"{url}（GitLab 收件箱：{reason}，发起 issue，发起人 @{initiator}；"
                "开工前只派角色做只读分析，调度者自己不分析）")
        try:
            launched = self.launch(repo, self.entry_task(task, CHAT), url, url)
        except InboxError:
            del self.state["origins"][url]
            raise
        if not launched:
            # 冷却期内：同一 issue 刚因 @提及走过入口，调度者正在路上，照常登记为发起 issue。
            log(f"{url}: entry already launched for this issue, tracking it as origin")
        if self.dynamic_projects:
            self.register_watched_issue(
                url=url, project=project, project_id=issue["project_id"], iid=issue["iid"],
                title=str(issue.get("title") or ""), kind="assigned_agent", actor=actor,
                event_key=f"todo:{todo['id']}", status="active",
            )
        save_state(self.state)
        self.api(f"todos/{todo['id']}/mark_as_done", "POST")
        if launched:
            log(f"launched team-up for {url}")

    def handle_body_mention(self, todo: Mapping[str, Any]) -> None:
        """在 issue 正文里直接 @机器人 时（无 note_id）：全项目模式下同样自动拉起只读分析并登记飞书提醒。"""
        issue = todo["target"]
        url = GP.canon_issue_url(todo["target_url"])
        if url in self.state["origins"] or issue.get("state") != "opened":
            self.api(f"todos/{todo['id']}/mark_as_done", "POST")
            return
        project = todo["project"]["path_with_namespace"]
        repo = self.ensure_repo(project)
        if not repo:
            log(f"skip body mention for {url}: failed to resolve or clone local repo for {project}")
            return
        actor = todo["author"]["username"]
        initiator = actor if actor in self.allowed else self.primary_allowed_user()
        model, thinking, error = self.scheduler_params(issue.get("description", ""))
        if error:
            if url not in self.state["bad_params_notified"]:
                self.comment(issue, f"@{initiator} {error}改好 issue 描述里的 `/model`、`/thinking` 后会自动开始。")
                self.state["bad_params_notified"].append(url)
                save_state(self.state)
            return
        notes = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes?sort=desc&per_page=1")
        self.state["origins"][url] = {
            "project": project, "project_id": issue["project_id"], "iid": issue["iid"],
            "repo": repo, "initiator": initiator, "mode": CHAT, "model": model, "thinking": thinking,
            "last_note_id": notes[0]["id"] if notes else 0,
        }
        task = (f"{url}（GitLab 收件箱：@{actor} 在正文 @机器人，发起 issue，发起人 @{initiator}；"
                "开工前只派角色做只读分析，调度者自己不分析）")
        try:
            launched = self.launch(repo, self.entry_task(task, CHAT), url, url)
        except InboxError:
            del self.state["origins"][url]
            raise
        self.register_watched_issue(
            url=url, project=project, project_id=issue["project_id"], iid=issue["iid"],
            title=str(issue.get("title") or ""), kind="mentioned_agent", actor=actor,
            event_key=f"todo:{todo['id']}", status="active",
        )
        save_state(self.state)
        self.api(f"todos/{todo['id']}/mark_as_done", "POST")
        if launched:
            log(f"launched team-up for body mention {url}")

    def handle_mention(self, todo: Mapping[str, Any]) -> None:
        match = NOTE_RE.search(todo["target_url"])
        if not match:
            if self.dynamic_projects:
                self.handle_body_mention(todo)
            else:
                self.api(f"todos/{todo['id']}/mark_as_done", "POST")  # 写在正文里的 @，交付流程会读正文。
            return
        note_id = int(match.group(1))
        issue = todo["target"]
        url = GP.canon_issue_url(todo["target_url"])
        project = todo["project"]["path_with_namespace"]
        actor = todo["author"]["username"]
        is_closed = issue.get("state") == "closed"
        is_status_query = self.is_command(str(todo.get("body") or ""), STATUS_COMMANDS)
        if is_closed and url not in self.state["origins"] and not is_status_query:
            # 已关闭的历史旧单不再拉仓库、不在旧单留言，也不写入 watched_issues 误发飞书「已自动接手」提醒。
            log(f"skip mention on closed issue: {url}#note_{note_id}")
            self.api(f"todos/{todo['id']}/mark_as_done", "POST")
            return
        if note_id not in self.state["forwarded"]:
            note = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes/{note_id}")
            note_is_status = self.is_command(str(note.get("body") or ""), STATUS_COMMANDS)
            if is_closed and url not in self.state["origins"] and not note_is_status:
                log(f"skip mention on closed issue: {url}#note_{note_id}")
                self.api(f"todos/{todo['id']}/mark_as_done", "POST")
                return
            repo = self.ensure_repo(project)
            if not repo:
                log(f"skip mention for {url}#note_{note_id}: failed to resolve or clone local repo for {project}")
                return
            where = dict(issue)
            where.setdefault("project", project)
            if not self.handle_note(url, repo, note, where):
                return
            self.remember(note_id)
            if not is_closed:
                self.follow(url, project, issue["iid"])
                if self.dynamic_projects and not note_is_status:
                    self.register_watched_issue(
                        url=url, project=project, project_id=issue["project_id"], iid=issue["iid"],
                        title=str(issue.get("title") or ""), kind="mentioned_agent", actor=actor,
                        event_key=f"todo:{todo['id']}", status="active",
                        note_url=f"{url}#note_{note_id}", excerpt=str(note.get("body") or ""),
                    )
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

        每个项目查询上一轮以来有更新的参与 issue（本账号建的 + 用户 @ 过的），再取其中的新评论。
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
        if not self.dynamic_projects:
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
                    if url in self.state["followed"] and self._should_evict_followed_issue(url, issue, now):
                        self.state["followed"].pop(url, None)
                        self.state["watched_issues"].pop(GP.canon_issue_url(url), None)
                        log(f"evicted closed followed issue: {url}")
        else:
            seen: set[str] = set()
            issues = list(self.api(f"issues?scope=all&updated_after={after}&author_username={self.me}&per_page=100") or [])
            by_proj: dict[str, list[int]] = {}
            for meta in self.state["followed"].values():
                if isinstance(meta, dict) and meta.get("project") and meta.get("iid") is not None:
                    by_proj.setdefault(str(meta["project"]), []).append(int(meta["iid"]))
            for project, iids in by_proj.items():
                if not self.is_project_allowed(project):
                    continue
                base = f"projects/{quote(project, safe='')}/issues?scope=all&updated_after={after}&per_page=100"
                issues += self.api(base + "".join(f"&iids[]={iid}" for iid in sorted(set(iids)))) or []
            for issue in issues:
                url = GP.canon_issue_url(str(issue.get("web_url") or ""))
                if not url or url in seen or url in self.state["origins"]:
                    continue
                project = GP.extract_project_from_web_url(url)
                if not self.is_project_allowed(project):
                    continue
                seen.add(url)
                if not self.poll_followed_issue(url, "", issue, floor, project=project):
                    return
                if url in self.state["followed"] and self._should_evict_followed_issue(url, issue, now):
                    self.state["followed"].pop(url, None)
                    self.state["watched_issues"].pop(GP.canon_issue_url(url), None)
                    log(f"evicted closed followed issue: {url}")
        self.prune_closed_followed(now)
        self.state["follow_since"] = now
        save_state(self.state)

    def _should_evict_followed_issue(self, url: str, issue: Mapping[str, Any], now: float) -> bool:
        """已关闭满 7 天的跟进单移出 followed；刚关闭不足 7 天的保留以承接尾随评论。"""
        entry = self.state["followed"].get(url)
        if not isinstance(entry, dict):
            return False
        if issue.get("state") != "closed":
            entry.pop("closed_since", None)
            return False
        closed_raw = issue.get("closed_at") or issue.get("updated_at")
        closed_ts: float | None = None
        if isinstance(closed_raw, str) and closed_raw.strip():
            try:
                closed_ts = parse_time(closed_raw)
            except ValueError:
                closed_ts = None
        if closed_ts is None:
            closed_ts = float(entry.setdefault("closed_since", now))
        return (now - closed_ts) >= FOLLOWED_CLOSED_RETENTION_S

    def prune_closed_followed(self, now: float) -> list[str]:
        """按小时低频巡检久未更新的 followed 列表，把关闭已满 7 天的老工单清理掉。"""
        if not self.state["followed"]:
            return []
        last_pruned = self.state.get("followed_pruned_at")
        if last_pruned is None:
            self.state["followed_pruned_at"] = now
            return []
        if (now - float(last_pruned)) < FOLLOWED_PRUNE_INTERVAL_S:
            return []
        evicted: list[str] = []
        followed_projects = {
            str(meta["project"])
            for meta in self.state["followed"].values()
            if isinstance(meta, dict) and meta.get("project")
        }
        projects_to_check = sorted(followed_projects) if self.dynamic_projects else list(self.projects)
        for project in projects_to_check:
            entries = {
                int(meta["iid"]): url
                for url, meta in list(self.state["followed"].items())
                if isinstance(meta, dict) and meta.get("project") == project and meta.get("iid") is not None
            }
            if not entries:
                continue
            query = (
                f"projects/{quote(project, safe='')}/issues?state=closed&per_page=100"
                + "".join(f"&iids[]={iid}" for iid in sorted(entries))
            )
            closed_issues = self.api(query)
            if not isinstance(closed_issues, list):
                continue
            for issue in closed_issues:
                raw_url = issue.get("web_url")
                url = GP.canon_issue_url(raw_url) if raw_url else entries.get(int(issue.get("iid") or -1))
                if url and url in self.state["followed"] and self._should_evict_followed_issue(url, issue, now):
                    self.state["followed"].pop(url, None)
                    self.state["watched_issues"].pop(GP.canon_issue_url(url), None)
                    evicted.append(url)
                    log(f"pruned stale closed followed issue: {url}")
        self.state["followed_pruned_at"] = now
        return evicted

    def poll_followed_issue(
        self, url: str, repo: str, issue: Mapping[str, Any], floor: float, *, project: str = ""
    ) -> bool:
        notes = self.api(f"projects/{issue['project_id']}/issues/{issue['iid']}/notes?sort=desc&per_page=100")
        fresh = sorted((n for n in notes
                        if not n.get("system") and n["author"]["username"] in self.allowed
                        and n["id"] not in self.state["forwarded"] and parse_time(n["created_at"]) >= floor),
                       key=lambda n: n["id"])
        if not fresh:
            return True
        if not repo and project:
            repo = self.ensure_repo(project)
            if not repo:
                log(f"skip followed issue {url}: failed to resolve or clone local repo for {project}")
                return False
        where = dict(issue)
        if project:
            where.setdefault("project", project)
        for note in fresh:
            if not self.handle_note(url, repo, note, where):
                return False
            self.remember(note["id"])
            save_state(self.state)
        return True

    def track_reopened(self) -> None:
        """把被用户重新打开的发起 issue 接回跟踪（关闭时移到 closed_origins 保留水位）。"""
        if not self.dynamic_projects:
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
            return
        by_proj: dict[str, list[int]] = {}
        for url, origin in self.state["closed_origins"].items():
            if url in self.state["origins"] or not isinstance(origin, dict):
                continue
            proj = origin.get("project") or GP.extract_project_from_web_url(url)
            iid = origin.get("iid")
            if proj and isinstance(iid, int):
                by_proj.setdefault(proj, []).append(iid)
        for project, iids in by_proj.items():
            if not self.is_project_allowed(project):
                continue
            issues = self.api(
                f"projects/{quote(project, safe='')}/issues?state=opened&per_page=100"
                + "".join(f"&iids[]={iid}" for iid in sorted(set(iids)))
            ) or []
            for issue in issues:
                url = GP.canon_issue_url(str(issue.get("web_url") or ""))
                if not url or url in self.state["origins"]:
                    continue
                origin = self.state["closed_origins"].pop(url, None)
                if origin is None:
                    continue
                self.state["origins"][url] = origin
                save_state(self.state)
                log(f"origin reopened by user: {url}")

    def poll_origin(self, url: str, origin: dict[str, Any]) -> None:
        base = f"projects/{origin['project_id']}/issues/{origin['iid']}"
        issue = self.api(base)
        self.labels_seen[url] = list(issue.get("labels") or [])
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
            self.clear_stage_labels(url, origin, issue.get("labels"))
            # 关闭是平台状态，不是交付结论；但若仍处于只读对话模式且无在途交付产物，用户关单即视为结束咨询/取消，不追问打扰。
            _, pane = self.owner(origin["repo"], url)
            if pane:
                if origin.get("mode", DELIVER) == CHAT:
                    self.prompt(
                        pane,
                        f"[GitLab 收件箱] 检测到发起 issue 已关闭 {url}（收件箱记录为对话模式）。先核对本任务 STATE.md、分支与 worktree："
                        "若尚未收到开工授权、未进入交付流程且无代码改动或未合并分支，用户关闭发起 issue 即视为取消本次只读咨询，"
                        f"直接运行 wrapup.py --status \"cancelled（对话模式未开工，用户已关闭 issue）\" --no-issue-note 静默释放成员并更新 INDEX，"
                        "禁止在 issue 发追问评论、禁止发待办 ntfy；"
                        "仅当 STATE.md 已切入交付流程且确有未完成交付工作时，才按 ~/.hwf/protocol/delivery.md §9 核验缺项并处理。",
                    )
                else:
                    self.prompt(pane, f"[GitLab 收件箱] 检测到发起 issue 已关闭 {url}。先按 ~/.hwf/protocol/delivery.md "
                                      "§9「完成评论与关闭前核对」「发起 issue 的关闭授权」核验交付证据和剩余工作；关闭事件本身不代表验收或交付完成。"
                                      "仍有未完成项且用户未明确取消时，在原 issue 说明缺项，请用户重开继续或明确取消，"
                                      "INDEX 保持 blocked，保留成员与 worktree；测试服务仍按每轮规则清理。"
                                      "只有确认交付完成或用户明确取消后，才执行 §9「成员收尾与收尾回执」。")
            closed: dict[str, Any] = self.state["closed_origins"]
            closed[url] = self.state["origins"].pop(url)  # 保留水位：用户重开后接着处理关闭后的评论。
            self.state["watched_issues"].pop(GP.canon_issue_url(url), None)
            for stale in list(closed)[:-CLOSED_CAP]:
                del closed[stale]
            save_state(self.state)
            log(f"origin closed: {url}")
            return
        self.watch_blocked(url, origin)

    def watch_blocked(self, url: str, origin: dict[str, Any]) -> None:
        """调度者卡在审批/提问界面时，它自己无法发评论，由收件箱在发起 issue @发起人。

        读得出单选题就贴出题目和编号选项，发起人回数字即可远程作答；题目换了（同一次卡住里出了下一题）再贴一次。
        读不出就只提醒一次去电脑上处理。恢复后清除，下次卡住再提醒。
        """
        _, pane = self.owner(origin["repo"], url)
        status = self.agent_state(pane) if pane else ""
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
        if not pane or self.agent_state(pane) != "blocked":
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
        still = self.agent_state(pane) == "blocked" and (parse_dialog(self.read_screen(pane)) or {}).get(
            "fingerprint") == dialog["fingerprint"]
        if still:
            self.comment(origin, f"@{user} 已按第 {number} 项「{option['label']}」，但界面还停在这道题上，"
                                 "可能没生效，请到电脑上看看。")
            return True
        origin.pop("blocked_dialog", None)
        save_state(self.state)
        self.comment(origin, f"@{user} 已替你选了第 {number} 项：{option['label']}。")
        return True

    # ---- 协议更新 ----

    def protocol_rev(self) -> str:
        result = self._exec(["git", "-C", str(HWF_HOME), "log", "-1", "--format=%H", "--", *PROTOCOL_PATHS])
        return result.stdout.strip() if result.returncode == 0 else ""

    def busy_schedulers(self) -> list[str]:
        """名下有未收尾交付、仍在线的调度者 pane。"""
        fs = find_scheduler_module()
        repo_paths = set(self.projects.values()) | {
            str(origin.get("repo")) for origin in self.state["origins"].values() if origin.get("repo")
        }
        names = {row.scheduler for repo in repo_paths for row in fs.active_rows(repo) if row.scheduler}
        if not names:
            return []
        result = self._exec(["herdr", "agent", "list"])
        if result.returncode != 0:
            raise InboxError(f"herdr agent list failed: {result.stderr.strip()}")
        agents = json.loads(result.stdout)["result"]["agents"]
        return sorted(a["pane_id"] for a in agents if a.get("name") in names and a.get("pane_id"))

    def notify_protocol_change(self) -> None:
        """入口或主题有新提交时提醒在跑的调度者重读改动。
        继续沿用提交 SHA 水位，旧 state 无需迁移；查询或投递失败时保留旧版本供下一轮重试。"""
        rev = self.protocol_rev()
        old = self.state.get("protocol_rev")
        if not rev or rev == old:
            return
        if old:
            try:
                panes = self.busy_schedulers()
            except Exception as exc:  # 提醒是附带的，任何失败都不能拖垮这一轮收件箱
                log(f"protocol change notice skipped this round: {exc!r}")
                return
            text = (f"[GitLab 收件箱] ~/.hwf 的协议、成员 skill 或角色配置有更新（{old[:8]} → {rev[:8]}），你启动时读的是旧版。"
                    f"当前这一步告一段落后，运行 `git -C ~/.hwf diff {old} {rev} -- {' '.join(PROTOCOL_PATHS)}` 看改了什么，"
                    "之后按新规则继续本交付（涉及 herdr/ 角色变更时，下次派该角色前运行 member-start.py 切换到最新配置；STATE 需补字段一并补上）。这条提醒不用发评论。")
            delivered = True
            for pane in panes:
                if not self.prompt(pane, text):
                    delivered = False
            if not delivered:
                return
            log(f"protocol {old[:8]} → {rev[:8]}: notified {panes}")
        self.state["protocol_rev"] = rev
        save_state(self.state)
    def poll_user_items(self) -> None:
        """只读拉取指派或 @ 本人的 issue，仅登记到 state['watched_issues'] 推飞书提醒，不 clone 也不启动调度者。"""
        user_token = self._user_token()
        if not user_token:
            return
        env = {**os.environ, "GITLAB_TOKEN": user_token}
        changed = False
        try:
            todos = self.api("todos?state=pending&type=Issue&per_page=100", env=env) or []
        except InboxError as exc:
            log(f"poll user todos failed: {exc}")
            todos = []
        watermark = self.state.get("user_todo_watermark")
        ids = [int(t["id"]) for t in todos if isinstance(t, dict) and isinstance(t.get("id"), int)]
        if watermark is None:
            self.state["user_todo_watermark"] = max(ids, default=0)
            changed = True
        else:
            max_seen = int(watermark)
            for item in sorted((t for t in todos if isinstance(t, dict) and isinstance(t.get("id"), int)),
                               key=lambda t: int(t["id"])):
                tid = int(item["id"])
                if tid <= watermark:
                    continue
                max_seen = max(max_seen, tid)
                action = item.get("action_name", "")
                if action not in ("assigned", "mentioned", "directly_addressed"):
                    continue
                actor = ((item.get("author") or {}).get("username") or "").strip()
                if actor == self.me:
                    continue
                target = item.get("target") or {}
                if target.get("state") and target.get("state") != "opened":
                    continue
                raw_url = str(target.get("web_url") or item.get("target_url") or "")
                url = GP.canon_issue_url(raw_url)
                if not url or url in self.state["origins"]:
                    continue
                project = ((item.get("project") or {}).get("path_with_namespace")
                           or GP.extract_project_from_web_url(url))
                if not self.is_project_allowed(project):
                    continue
                iid = target.get("iid")
                if not isinstance(iid, int):
                    continue
                kind = "assigned_user" if action == "assigned" else "mentioned_user"
                note_url = str(item.get("target_url") or url) if action != "assigned" else ""
                self.register_watched_issue(
                    url=url, project=project, project_id=target.get("project_id", ""), iid=iid,
                    title=str(target.get("title") or ""), kind=kind, actor=actor,
                    event_key=f"user-todo:{tid}", status="standby",
                    note_url=note_url, excerpt=str(item.get("body") or ""),
                )
                changed = True
            if max_seen != watermark:
                self.state["user_todo_watermark"] = max_seen
                changed = True

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assigned_since = str(self.state.get("user_assigned_since") or "")
        if not assigned_since:
            self.state["user_assigned_since"] = now_iso
            changed = True
        else:
            ok_all = True
            for user in sorted(self.allowed):
                try:
                    issues = self.api(
                        f"issues?scope=all&state=opened&assignee_username={quote(user)}"
                        f"&updated_after={quote(assigned_since)}&per_page=100",
                        env=env,
                    ) or []
                except InboxError as exc:
                    log(f"poll user assigned issues failed for {user}: {exc}")
                    ok_all = False
                    continue
                for issue in issues:
                    if not isinstance(issue, dict) or issue.get("state") != "opened":
                        continue
                    url = GP.canon_issue_url(str(issue.get("web_url") or ""))
                    if not url or url in self.state["origins"] or url in self.state["watched_issues"]:
                        continue
                    project = GP.extract_project_from_web_url(url)
                    if not self.is_project_allowed(project):
                        continue
                    author = ((issue.get("author") or {}).get("username") or "").strip()
                    if author == self.me:
                        continue
                    iid = issue.get("iid")
                    if not isinstance(iid, int):
                        continue
                    self.register_watched_issue(
                        url=url, project=project, project_id=issue.get("project_id", ""), iid=iid,
                        title=str(issue.get("title") or ""), kind="assigned_user", actor=author or user,
                        event_key=f"user-assigned:{url}", status="standby",
                    )
                    changed = True
            if ok_all:
                self.state["user_assigned_since"] = now_iso
                changed = True
        if changed:
            save_state(self.state)

    # ---- 一轮 ----

    def poll(self) -> None:
        if not self.me:
            self.me = self.api("user")["username"]
        self.notify_protocol_change()
        # 先接回重开的发起 issue：同一轮里的 @ 待办（如 `/new @本账号`）才会按发起 issue 处理。
        self.track_reopened()
        todos = self.api("todos?state=pending&type=Issue&per_page=100")
        for todo in todos:
            actor = todo["author"]["username"]
            project = todo["project"]["path_with_namespace"]
            if self.dynamic_projects:
                # 全项目模式：任何非机器人同事指派/@机器人都允许自动拉起只读分析，但权限敏感指令仍在 handle_note 限制仅本人。
                relevant = actor != self.me and self.is_project_allowed(project)
            else:
                relevant = actor in self.allowed and project in self.projects
            if not relevant:
                continue  # 非范围项目或自指派：不处理也不标记，留给人看。
            action = todo["action_name"]
            if action == "assigned":
                self.handle_assignment(todo)
            elif action in ("mentioned", "directly_addressed"):
                self.handle_mention(todo)
        for url, origin in list(self.state["origins"].items()):
            self.poll_origin(url, origin)
        self.poll_followed()
        self.poll_user_items()
        self.rotate_oversized_idle_schedulers()


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


def previous_stage(events: Sequence[Mapping[str, Any]]) -> str | None:
    """按标签历史找进入最近一次「等你处理」之前的进度阶段；之前没有别的进度标签就返回 None。"""
    last_other: str | None = None
    before_wait: str | None = None
    for event in sorted(events, key=lambda e: (e.get("created_at") or "", e.get("id") or 0)):
        name = ((event.get("label") or {}).get("name")) or ""
        if not name.startswith(STAGE_PREFIX) or event.get("action") != "add":
            continue
        if name == WAITING_LABEL:
            before_wait = last_other
        else:
            last_other = name
    return before_wait

_FIND_SCHEDULER: Any = None


def find_scheduler_module() -> Any:
    global _FIND_SCHEDULER
    if _FIND_SCHEDULER is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("find_scheduler", Path(__file__).with_name("find-scheduler.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("find_scheduler", module)  # dataclass 要从 sys.modules 找到所在模块
        spec.loader.exec_module(module)
        _FIND_SCHEDULER = module
    return _FIND_SCHEDULER


def parse_time(text: str) -> float:
    """GitLab 时间（如 2026-10-27T09:00:00.123Z 或带 +08:00）转时间戳。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def load_config(shared_path: Path = CONFIG_PATH, local_path: Path = LOCAL_CONFIG_PATH) -> dict[str, Any] | None:
    """合并入库配置与本机配置；shared 未配置 projects 时启用全项目动态监听。"""
    shared = load_json(shared_path, None)
    if shared is None:
        return None
    local = load_json(local_path, {})
    paths = dict(local.get("projects", {}))
    merged = {**shared, **{k: v for k, v in local.items() if k != "projects"}}
    merged["local_projects"] = dict(paths)
    if "projects" not in shared or shared.get("projects") in (None, []):
        merged["dynamic_projects"] = True
        merged["projects"] = dict(paths)
        return merged
    wanted = shared.get("projects", [])
    if isinstance(wanted, dict):  # 旧格式：入库文件里直接写了路径
        paths = {**wanted, **paths}
        wanted = list(wanted)
    merged["dynamic_projects"] = False
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


def watched_scripts(base_dir: Path | None = None) -> tuple[Path, ...]:
    root = base_dir or Path(__file__).resolve().parent
    return tuple(
        root / name
        for name in (
            "gitlab-inbox.py", "gitlab_projects.py", "find-scheduler.py", "proactive.py",
            "retrospect.py", "jev_client.py", "jev_gates.py", "poll_backoff.py",
        )
    )


def snapshot_mtimes(paths: Sequence[Path]) -> dict[Path, int]:
    return {p: p.stat().st_mtime_ns for p in paths if p.exists()}


def reloadable_changes(baseline: Mapping[Path, int], paths: Sequence[Path]) -> list[Path]:
    """检测常驻进程依赖的本地脚本是否已更新；存在语法错误或半截写入时暂缓重载。"""
    current = snapshot_mtimes(paths)
    changed = [p for p in paths if baseline.get(p) != current.get(p)]
    if not changed:
        return []
    for p in paths:
        if p.exists():
            try:
                compile(p.read_bytes(), str(p), "exec")
            except (OSError, SyntaxError) as error:
                log(f"skip hot-reload due to invalid syntax in {p.name}: {error}")
                return []
    return changed


MAX_POLL_BACKOFF_S = PB.MAX_POLL_BACKOFF_S
FAILURE_HEARTBEAT_EVERY = PB.FAILURE_HEARTBEAT_EVERY
compute_poll_backoff = PB.compute_poll_backoff
PollFailureTracker = PB.PollFailureTracker


def scout_busy(scout: Any) -> bool:
    thread = getattr(scout, "_thread", None)
    return bool(thread is not None and thread.is_alive())


def main(argv: Sequence[str] | None = None) -> int:
    global _EVENT_LOG_PATH
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single polling round")
    args = parser.parse_args(argv)
    if os.environ.get("HERDR_ENV") != "1":
        print("gitlab-inbox must run inside a Herdr-managed pane", file=sys.stderr)
        return 1
    watched = watched_scripts()
    baseline = snapshot_mtimes(watched)
    config = load_config()
    if config is None:
        print(f"missing config {CONFIG_PATH}", file=sys.stderr)
        return 1
    if not args.once:
        _EVENT_LOG_PATH = STATE_PATH.parent / "gitlab-inbox-events.log"
    inbox = Inbox(config, load_json(STATE_PATH, {}))
    scout = start_scout(config) if not args.once else None
    retro = start_retrospect(config) if not args.once else None
    tracker = PollFailureTracker()
    base_interval = int(config.get("interval_seconds", 60))
    while True:
        poll_ok = False
        try:
            inbox.poll()
            poll_ok = True
            recovered = tracker.record_success()
            if recovered:
                log(recovered)
        except (InboxError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            # 单轮失败（网络、glab、herdr）不退出常驻进程；--once 时如实返回失败。
            notice = tracker.record_failure(error)
            if notice:
                log(notice)
            if args.once:
                return 1
        if args.once:
            return 0
        if not scout_busy(scout) and not scout_busy(retro):
            changed = reloadable_changes(baseline, watched)
            if changed:
                names = ", ".join(p.name for p in changed)
                log(f"reloading gitlab-inbox after code update: {names}")
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
        if poll_ok:
            if scout is not None:
                scout.tick_in_background()  # 主动接活（observe 档）仅在收件箱连通轮次后台触发
            if retro is not None:
                retro.tick_in_background()  # 周期复盘（周二/周五 12:00）在连通轮次后台触发
        time.sleep(compute_poll_backoff(base_interval, tracker.consecutive_failures))


def start_scout(config: Mapping[str, Any]) -> Any:
    """加载同目录的 proactive.py（主动接活）；没有规则文件或加载失败就不启用，收件箱照常工作。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("proactive", Path(__file__).resolve().parent / "proactive.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not module.RULES_PATH.exists():
            log(f"proactive off: no {module.RULES_PATH.name}")
            return None
        scout_config = dict(config)
        if config.get("dynamic_projects"):
            scout_config["projects"] = []
            scout_config.setdefault("local_projects", dict(config.get("projects") or {}))
        return module.Scout(scout_config)
    except Exception as error:  # 主动接活起不来不能影响收件箱
        log(f"proactive off: {error!r}")
        return None


def start_retrospect(config: Mapping[str, Any]) -> Any:
    """加载同目录的 retrospect.py（周二/周五 12:00 周期复盘）；加载失败不影响收件箱。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("retrospect", Path(__file__).resolve().parent / "retrospect.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not dict(config.get("retrospect") or {}).get("enabled", True):
            log("retrospect off: disabled in config")
            return None
        return module.Retrospect(config)
    except Exception as error:
        log(f"retrospect off: {error!r}")
        return None


if __name__ == "__main__":
    sys.exit(main())
