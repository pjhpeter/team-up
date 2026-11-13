"""秘书：和机器人的私聊窗口就是秘书，统一转达各交付的消息、每小时巡检、回答提问、转发用户的快速回复。

问题：每个交付各有一张 issue 和一个飞书话题，同时跑的交付一多，用户要在好几个话题之间来回看。
办法：私聊窗口本来就是提需求的入口，让它同时当秘书：所有交付的 Agent 评论都同时发到私聊主窗口；没有新消息时
每小时巡检一次，有变化或异常才汇报；用户对某条转达点「回复」，就把话转成那张 issue 的评论。各交付自己的
话题照常保留（默认免打扰），想看某个需求的完整经过时去话题里看。

只由 feishu-bridge.py 调用（同一进程、同一份飞书连接与状态文件），不单独运行：
  开启：白名单用户第一次在私聊里说话，桥就记下这个私聊窗口，之后的转达和巡检都发到这里，不需要命令。
  私聊主窗口里（话题里的消息仍归各自的 issue）：
    对某条转达点「回复」          → 写成那张 issue 的评论（/go、/status、/免打扰 等指令原样生效）
    「需求：…」「#123 …」         → 照旧由桥建单或关联（不经过秘书）
    「进度」「汇总」「/status」    → 立刻回一份总览
    「帮助」「/help」              → 用法说明
    其他话                        → 当作提问，用模型按当前总览和最近转达回答；模型不可用时回总览
看哪些 issue：收件箱登记的发起 issue（state/gitlab-inbox.json 的 origins）加上飞书桥里还没关闭的话题。
只转 Agent 账号发的评论；收件箱的「已收到」回执不转，免得用户每回一句就多一条。
巡检只读：GitLab GET、find-scheduler --owner-of、herdr agent get、本机 INDEX/STATE 修改时间。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
STATUS_WORDS = frozenset({"/status", "进度", "汇总", "总览"})
PATROL_S = 3600
IDLE_ALERT_S = 3 * 3600  # 在途交付超过这么久没有任何动静，巡检里标出来
MSG_MAP_CAP = 500
RECENT_CAP = 20
SUMMARY_CAP = 600  # 转达只放开头一段，全文给链接
RECEIPT = re.compile(r"^(@\S+\s+)?已收到")  # 收件箱回执，不转达
FOLDED = re.compile(r"<details>.*?</details>", re.S)
WAITING_LABEL = "状态:等你处理"
STATUS_TEXT = {"working": "正在干活", "idle": "空闲", "done": "空闲", "blocked": "卡在确认界面", "unknown": "状态不明"}
DEFAULT_MODEL = {"model": "jc-gpt/gpt-6-luna", "thinking": "medium", "timeout_s": 120}
SYSTEM_PROMPT = (
    "你是用户的交付秘书，替他盯着多个由 Agent 团队推进的需求。只根据给你的「任务总览」和「最近转达」回答，"
    "不知道就说不知道，不编造进度。用中文大白话，先给结论，控制在 10 行内；需要用户动手的事写清楚去哪个 issue 做什么。"
)

INTRO = (
    "以后我也是你的秘书，所有在跑的需求都在这个窗口跟进：\n"
    "1. 各需求的 Agent 一发言，我就转到这里\n"
    "2. 没有新消息时每小时看一次，有变化或卡住了才汇报\n"
    "3. 对某条转达点「回复」，我把你的话转给那个需求；也可以发「#编号 内容」\n"
    "4. 发「进度」看总览，发「帮助」看用法，其他话直接问我"
)


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_FS: Any = None


def find_scheduler() -> Any:
    global _FS
    if _FS is None:
        _FS = _load("find_scheduler", "find-scheduler.py")
    return _FS


class Secretary:
    def __init__(self, bridge: Any, home: Path = HWF_HOME):
        self.bridge, self.home = bridge, home
        self.state: dict[str, Any] = bridge.state.setdefault("secretary", {})
        self.state.setdefault("issues", {})   # issue URL → {project, iid, repo, title, last_note, last_agent_at}
        self.state.setdefault("msgs", {})     # 私聊里的转达消息 id → issue URL（快速回复用）
        self.state.setdefault("recent", [])   # 最近转达的摘要，回答提问时当材料
        cfg = dict(DEFAULT_MODEL)
        cfg.update(bridge.cfg.get("secretary") or {})
        self.cfg = cfg

    # ---------- 私聊窗口 ----------
    @property
    def chat(self) -> str:
        return str(self.state.get("chat_id") or "")

    def learn(self, chat: str) -> None:
        """白名单用户在私聊里说话时调用：记下（或换成）这个私聊窗口；第一次记下时发一段说明。"""
        if not chat or chat == self.chat:
            return
        first = not self.chat
        self.state.update(chat_id=chat, last_patrol=self.bridge.now(), last_digest="")
        if first:
            self.say(INTRO, f"sec-intro-{chat}")

    def target(self, msg: Mapping[str, Any]) -> Optional[str]:
        """对某条转达点了「回复」（引用回复或在它下面开的话题）就返回那个需求的 issue URL。"""
        msgs = self.state["msgs"]
        for key in (msg.get("reply_to"), msg.get("root_id")):
            if key and key in msgs:
                return msgs[key]
        return None

    def is_relay(self, msg: Mapping[str, Any]) -> bool:
        return msg.get("chat_id") == self.chat and self.target(msg) is not None

    def say(self, markdown: str, key: str) -> str:
        data = self.bridge.lark(["im", "+messages-send", "--chat-id", self.chat, "--markdown", "-",
                                 "--idempotency-key", key[:50]], stdin=markdown)
        return str((data.get("data") or {}).get("message_id") or "")

    # ---------- 看哪些 issue ----------
    def repos(self) -> dict[str, str]:
        local = self.home / "gitlab-inbox.local.json"
        try:
            return dict((json.loads(local.read_text()) or {}).get("projects") or {})
        except (OSError, ValueError):
            return {}

    def watched(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        repos = self.repos()
        try:
            origins = json.loads((self.home / "state" / "gitlab-inbox.json").read_text()).get("origins", {})
        except (OSError, ValueError):
            origins = {}
        for url, o in origins.items():
            found[url] = {"project": o["project"], "iid": int(o["iid"]), "repo": o.get("repo") or repos.get(o["project"], "")}
        for t in self.bridge.state.get("threads", {}).values():
            if t.get("closed"):
                continue
            url = self.bridge.issue_url(t["project"], t["iid"])
            found.setdefault(url, {"project": t["project"], "iid": int(t["iid"]), "repo": repos.get(t["project"], "")})
        return found

    # ---------- 转达 ----------
    def poll(self, forward: bool = True) -> None:
        if not self.chat:
            return
        issues = self.state["issues"]
        watched = self.watched()
        for url in [u for u in issues if u not in watched]:
            del issues[url]
        for url, w in watched.items():
            known = issues.get(url)
            pid = quote(w["project"], safe="")
            notes = self.bridge.api(f"projects/{pid}/issues/{w['iid']}/notes?sort=desc&order_by=created_at&per_page=50")
            if known is None:
                issue = self.bridge.api(f"projects/{pid}/issues/{w['iid']}")
                issues[url] = {**w, "title": issue.get("title", ""), "last_note": max([n["id"] for n in notes] or [0]),
                               "last_agent_at": self.bridge.now()}
                continue  # 第一次看到只记水位，不回放旧评论
            known.update(w)
            for n in sorted((n for n in notes if n["id"] > known["last_note"]), key=lambda n: n["id"]):
                if forward and self.should_forward(n):
                    known["last_agent_at"] = self.bridge.now()
                    self.forward(url, known, n)
                known["last_note"] = n["id"]

    def should_forward(self, n: Mapping[str, Any]) -> bool:
        author = (n.get("author") or {}).get("username")
        body = (n.get("body") or "").strip()
        return (not n.get("system") and author == self.bridge.agent
                and n["id"] not in self.bridge.state.get("posted_notes", []) and not RECEIPT.match(body))

    def forward(self, url: str, issue: Mapping[str, Any], n: Mapping[str, Any]) -> None:
        link = f"{url}#note_{n['id']}"
        body = FOLDED.sub("", (n.get("body") or "").strip()).strip()
        if len(body) > SUMMARY_CAP:
            body = body[:SUMMARY_CAP].rstrip() + "…"
        head = f"**[#{issue['iid']}]({link}) {issue.get('title', '')}**"
        mid = self.say(f"{head}\n{body}", f"sec-{issue['iid']}-{n['id']}")
        if mid:
            msgs = self.state["msgs"]
            msgs[mid] = url
            for old in list(msgs)[:-MSG_MAP_CAP]:
                del msgs[old]
        self.state["recent"] = (self.state["recent"] + [f"#{issue['iid']} {issue.get('title', '')}：{body[:200]}"])[-RECENT_CAP:]

    # ---------- 巡检 ----------
    def patrol(self, force: bool = False) -> Optional[str]:
        """到点（或 force）就看一遍；内容和上次发的不同才发。返回发出的文本。"""
        if not self.chat:
            return None
        if not force and self.bridge.now() - float(self.state.get("last_patrol") or 0) < PATROL_S:
            return None
        self.state["last_patrol"] = self.bridge.now()
        lines = self.overview()
        digest = "\n".join(lines)
        if digest == self.state.get("last_digest"):
            return None
        self.state["last_digest"] = digest
        text = f"**每小时巡检（{time.strftime('%H:%M', time.localtime(self.bridge.now()))}）**\n" + (digest or "现在没有在跑的需求。")
        self.say(text, f"sec-patrol-{int(self.bridge.now())}")
        return text

    def overview(self) -> list[str]:
        lines = []
        for url, issue in sorted(self.state["issues"].items(), key=lambda kv: kv[1]["iid"]):
            try:
                lines.append(self.task_line(url, issue))
            except Exception as e:  # 一个需求查不到不影响其他
                lines.append(f"- [#{issue['iid']}]({url}) {issue.get('title', '')}：这次没查到（{str(e)[:80]}）")
        return lines

    def task_line(self, url: str, issue: Mapping[str, Any]) -> str:
        pid = quote(issue["project"], safe="")
        info = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}")
        labels = [l for l in info.get("labels", []) if l.startswith("状态:")]
        parts = ["、".join(labels) or ("已关闭" if info.get("state") == "closed" else "没有进度标签")]
        alerts = []
        if WAITING_LABEL in labels:
            alerts.append("等你处理")
        owner = self.owner(issue.get("repo") or "", url)
        if owner.get("task"):
            pane = owner.get("live_pane")
            if pane:
                status = self.agent_status(pane)
                parts.append(f"调度者{STATUS_TEXT.get(status, status)}")
                if status == "blocked":
                    alerts.append("调度者卡在确认界面，要到电脑上处理")
            elif not str(owner.get("status") or "").lower().startswith(find_scheduler().TERMINAL_STATUS):
                alerts.append("调度者已退出，任务还没做完")
            last = max(float(issue.get("last_agent_at") or 0), self.state_mtime(issue.get("repo") or "", owner["task"]))
            if info.get("state") == "opened" and WAITING_LABEL not in labels and self.bridge.now() - last > IDLE_ALERT_S:
                alerts.append(f"超过 {IDLE_ALERT_S // 3600} 小时没有动静")
        line = f"- [#{issue['iid']}]({url}) {issue.get('title', '')}：{'；'.join(parts)}"
        return line + ("\n  ⚠️ " + "；".join(alerts) if alerts else "")

    def owner(self, repo: str, url: str) -> dict[str, Any]:
        if not repo:
            return {}
        out = self.bridge.sh([sys.executable, str(Path(__file__).with_name("find-scheduler.py")), repo, "--owner-of", url])
        return json.loads(out)

    def agent_status(self, pane: str) -> str:
        try:
            out = self.bridge.sh(["herdr", "agent", "get", pane])
            return str(json.loads(out)["result"]["agent"].get("agent_status") or "unknown")
        except Exception:
            return "unknown"

    @staticmethod
    def state_mtime(repo: str, task: str) -> float:
        path = Path(repo) / ".hwf" / "tmp" / task / "STATE.md"
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    # ---------- 用户在私聊里对秘书说话 ----------
    def handle(self, msg: Mapping[str, Any], text: str) -> None:
        """私聊主窗口里桥没认出的话：对转达的「回复」转给需求，「进度」回总览，其余当提问。"""
        mid = msg["message_id"]
        target = self.target(msg)
        if target is not None:
            self.relay(target, msg, text)
            return
        if text.strip().lower() in STATUS_WORDS:
            lines = self.overview()
            self.bridge.reply(mid, "**总览**\n" + ("\n".join(lines) or "现在没有在跑的需求。"), f"sec-status-{mid}")
            return
        self.bridge.reply(mid, self.answer(text), f"sec-answer-{mid}")

    def relay(self, url: str, msg: Mapping[str, Any], text: str) -> None:
        mid = msg["message_id"]
        issue = self.state["issues"].get(url)
        project = issue["project"] if issue else self.bridge.project
        iid = issue["iid"] if issue else int(url.rsplit("/", 1)[-1])
        images = self.bridge.images_markdown({**msg, "reply_to": "", "root_id": ""}, project)
        text = self.bridge.strip_images(text)
        if not text and not images:
            self.bridge.reply(mid, "这条没有内容，没转。", f"sec-empty-{mid}")
            return
        # 指令要整条只有指令，收件箱才认得，所以不加尾注
        body = text if text.startswith("/") and not images else \
            "\n\n".join(x for x in (text, images) if x) + "\n\n_（来自飞书秘书）_"
        self.bridge.note_as_user(project, iid, f"@{self.bridge.agent} {body}")
        title = f" {issue.get('title', '')}" if issue else ""
        self.bridge.reply(mid, f"已转给 [#{iid}]({url}){title}。", f"sec-relayed-{mid}")

    def answer(self, question: str) -> str:
        lines = self.overview()
        material = ("## 任务总览\n" + ("\n".join(lines) or "（没有在跑的需求）") + "\n\n## 最近转达\n"
                    + ("\n".join(self.state["recent"]) or "（无）") + f"\n\n## 用户的问题\n{question}")
        cmd = ["omp", "-p", "--model", self.cfg["model"], "--thinking", self.cfg["thinking"], "--no-session",
               "--mode", "json", "--tools", "read", "--no-context-files", "--no-skills", "--no-rules",
               "--no-extensions", "--no-lsp", "--system-prompt", SYSTEM_PROMPT, material]
        try:
            r = self.bridge.run(cmd, capture_output=True, text=True, timeout=int(self.cfg["timeout_s"]),
                                check=False, stdin=subprocess.DEVNULL)
            reply = ""
            for line in (r.stdout or "").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "turn_end":
                    content = (ev.get("message") or {}).get("content") or []
                    reply = "".join(c.get("text", "") for c in content if c.get("type") == "text")
            if reply.strip():
                return reply.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return "这会儿没法回答，先给你当前总览：\n" + ("\n".join(lines) or "现在没有在跑的需求。")
