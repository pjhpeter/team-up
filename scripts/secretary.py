"""秘书：和机器人的私聊窗口就是秘书，统一转达各交付的消息、每小时巡检、回答提问、转发用户的快速回复。

问题：每个交付各有一张 issue 和一个飞书话题，同时跑的交付一多，用户要在好几个话题之间来回看。
办法：私聊窗口本来就是提需求的入口，让它同时当秘书：调度者的每条评论都马上发到私聊主窗口（发多少由协议的免打扰/详尽
在源头控制，秘书不再二次筛）；每小时巡检只发总览，有变化或异常才汇报；用户对某条转达点「回复」，就把话转成那张 issue 的评论。
私聊绑定的 issue 只在秘书主窗口通知，原话题仍可回复；群聊绑定保留群内回复，秘书同时私聊转达。

只由 feishu-bridge.py 调用（同一进程、同一份飞书连接与状态文件），不单独运行：
  开启：白名单用户第一次在私聊里说话，桥就记下这个私聊窗口，之后的转达和巡检都发到这里，不需要命令。
  私聊主窗口里（话题里的消息仍归各自的 issue）：
    对某条转达点「回复」          → 写成那张 issue 的评论（/go、/status、/免打扰 等指令原样生效）
    「需求：…」「#123 …」         → 照旧由桥建单或关联（不经过秘书）
    「进度」「汇总」「/status」    → 立刻回一份总览
    「帮助」「/help」              → 用法说明
    「帮我问 481 的调度…」         → 模型判断是要传话，以用户名义写成那张 issue 的评论，调度者回复照常转回来
    其他话                        → 当作提问，用模型按总览、各需求最近评论和最近转达回答；模型不可用时回总览
看哪些 issue：收件箱登记的发起 issue（state/gitlab-inbox.json 的 origins）加上飞书桥里还没关闭的话题。
默认转 Agent 评论；私聊绑定的 issue 也保留原话题可见的其他人评论。固定转交回执不转，实质答复不按相似度过滤。
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
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import quote

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
STATUS_WORDS = frozenset({"/status", "进度", "汇总", "总览"})
PATROL_S = 3600
IDLE_ALERT_S = 3 * 3600  # 在途交付超过这么久没有任何动静，巡检里标出来
MSG_MAP_CAP = 500
RECENT_CAP = 20
SUMMARY_CAP = 600  # 转达只放开头一段，全文给链接
# 只认 gitlab-inbox.py deliver() 的两种整条回执；调度者以「已收到」开头的实质答复（#483 note_25889）照常转
RECEIPT = re.compile(r"^(@\S+\s+)?已收到(，已转交目标 issue \S+ 的消息，等待调度者回复。"
                     r"|目标 issue \S+ 的消息。目前没有在线负责人，已转交调度入口，等待接手后回复。)$")
FOLDED = re.compile(r"<details>.*?</details>", re.S)
BARE_ISSUE = re.compile(r"(?<![\[\w/&])#(\d+)(?!\d)")  # 已在链接里的「[#12]」、网址里的不算
WAITING_LABEL = "状态:等你处理"
SHIP_LABEL = "状态:待上线"
SHIP_ASK_GRACE_S = 15 * 60  # 调度者常先发「请授权合并」再改标签，往前放宽这么久都算
LABEL_EVENT_PAGES = 5
ASK_LINE = re.compile(r"^\s*转给\s*#(\d+)\s*[:：]\s*(.+)", re.S)  # 模型要替用户传话时的格式
NOTES_PER_ISSUE = 3
NOTE_CAP = 400
STATUS_TEXT = {"working": "正在干活", "idle": "空闲", "done": "空闲", "blocked": "卡在确认界面", "unknown": "状态不明"}
ROLE_TEXT = {"arch": "架构师", "arch-lite": "方案作者", "precheck": "取证", "code-review": "代码评审", "test-writer": "写用例",
             "test": "测试", "test-herdr": "测试", "dev": "开发", "dev-fe": "前端开发", "dev-be": "后端开发"}
DEFAULT_MODEL = {"model": "jc-gpt/gpt-6-luna", "thinking": "medium", "timeout_s": 120}
SYSTEM_PROMPT = (
    "你是用户的交付秘书，替他盯着多个由 Agent 团队推进的需求。只根据给你的「任务总览」「各需求最近的评论」「最近转达」回答，"
    "不知道就说不知道，不编造进度。问怎么用（发起任务、关联 issue、群里怎么叫你等）就按「机器人用法」回答，"
    "不要让用户自己去 GitLab 建单。用中文大白话，先给结论，控制在 10 行内；需要用户动手的事写清楚去哪个 issue 做什么。"
    "用户明确要你去问、去转告、去吩咐某个需求的调度者时（例如「帮我问 481 的调度…」「告诉 478 先别合并」），"
    "只输出一行「转给 #编号：要对调度者说的话」，把用户的意思写成对调度者说的一句完整的话，不要再输出别的；"
    "用户只是问你问题时照常回答，不要替他去问。"
)

INTRO = (
    "以后我也是你的秘书，所有在跑的需求都在这个窗口跟进：\n"
    "1. 各需求调度者发的每条消息，我都马上转到这里\n"
    "2. 每小时看一遍总览，有变化或卡住了才汇报\n"
    "3. 对某条转达点「回复」，我把你的话转给那个需求；也可以发「#编号 内容」\n"
    "4. 想问某个需求的调度者，直接说「帮我问 481 的调度…」，我替你问，回复会转到这里\n"
    "5. 发「进度」看总览，发「帮助」看用法，其他话直接问我"
)


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_FS: Any = None


def _ts(value: Any) -> float:
    """GitLab 时间（2026-11-12T10:00:00.000+08:00 / …Z）转成秒；认不出就当 0。"""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def canon(url: str) -> str:
    """GitLab 同一张 issue 有 issues、work_items 两种链接（收件箱记后者、桥记前者），统一成 issues 才不会当成两个需求。"""
    return url.replace("/-/work_items/", "/-/issues/")


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
        self.state.setdefault("held", {})     # issue URL → 飞书没发出去、下一轮重发的 Agent 发言
        self.state.setdefault("recent", [])   # 最近转达的摘要，回答提问时当材料
        self.merge_link_forms()
        cfg = dict(DEFAULT_MODEL)
        cfg.update(bridge.cfg.get("secretary") or {})
        self.cfg = cfg

    def merge_link_forms(self) -> None:
        """旧状态里同一需求可能按两种链接各记了一份：合成一份，攒着的发言按评论 id 去重。"""
        issues, held, msgs = self.state["issues"], self.state["held"], self.state["msgs"]
        for url in [u for u in issues if canon(u) != u]:
            old, new = issues.pop(url), issues.get(canon(url))
            if new is None or old.get("last_note", 0) > new.get("last_note", 0):
                issues[canon(url)] = old
        for url in [u for u in held if canon(u) != u]:
            merged = held.get(canon(url), []) + held.pop(url)
            seen: set[Any] = set()
            held[canon(url)] = [e for e in sorted(merged, key=lambda e: e["id"]) if not (e["id"] in seen or seen.add(e["id"]))]
        for mid, url in list(msgs.items()):
            msgs[mid] = canon(url)

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
                return canon(msgs[key])
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
            found[canon(url)] = {"project": o["project"], "iid": int(o["iid"]), "repo": o.get("repo") or repos.get(o["project"], ""),
                                 "notify": o.get("notify", ""), "initiator": o.get("initiator", "")}
        for t in self.bridge.state.get("threads", {}).values():
            if t.get("closed"):
                continue
            url = canon(self.bridge.issue_url(t["project"], t["iid"]))
            found.setdefault(url, {"project": t["project"], "iid": int(t["iid"]), "repo": repos.get(t["project"], "")})
        return found

    # ---------- 转达 ----------
    def poll(self, forward: bool = True) -> None:
        """Agent 的每条发言都马上转到私聊（2026-11-13 用户要求：攒到巡检里只剩一行摘要，等于漏了）。"""
        if not self.chat:
            return
        issues, held = self.state["issues"], self.state["held"]
        watched = self.watched()
        for url in [u for u in issues if u not in watched]:
            # 关闭后才掉出名单：调度者常发完最后几条评论就立刻关 issue（#481），先把没转的补发完再放手。
            # 读评论失败就抛出去、记录不删，下一轮再补。
            known = issues[url]
            self.flush(url, known, held.get(url, []) + self.fresh_entries(url, known, self.notes_of(known), forward))
            del issues[url]
            held.pop(url, None)
        for url, w in watched.items():
            known = issues.get(url)
            pid = quote(w["project"], safe="")
            notes = self.notes_of(w)
            if known is None:
                issue = self.bridge.api(f"projects/{pid}/issues/{w['iid']}")
                issues[url] = {**w, "title": issue.get("title", ""), "last_note": max([n["id"] for n in notes] or [0]),
                               "last_agent_at": self.bridge.now()}
                continue  # 第一次看到只记水位，不回放旧评论
            known.update(w)
            pending = held.get(url, []) + self.fresh_entries(url, known, notes, forward)
            if pending:
                self.flush(url, known, pending)

    def forward_private_notes(self, thread: dict[str, Any], notes: list[dict[str, Any]]) -> None:
        """私聊话题与秘书共用同一进度、待发队列和消息 ID，不再各发一份。"""
        url = canon(self.bridge.issue_url(thread["project"], thread["iid"]))
        known = self.state["issues"].get(url)
        if known is None:
            pid = quote(thread["project"], safe="")
            info = self.bridge.api(f"projects/{pid}/issues/{thread['iid']}")
            known = {"project": thread["project"], "iid": thread["iid"], "title": info.get("title", ""),
                     "last_note": thread.get("last_note", 0), "last_agent_at": self.bridge.now()}
            self.state["issues"][url] = known
        # 原话题会转达其他人从 GitLab 发来的评论；合并出口不能丢掉这些内容。
        known["private_thread"] = True
        pending = self.state["held"].get(url, []) + self.fresh_entries(url, known, notes, True)
        self.flush(url, known, pending)
        thread["last_note"] = max([thread.get("last_note", 0)] + [n["id"] for n in notes])

    def flush(self, url: str, issue: Mapping[str, Any], pending: list[dict[str, Any]]) -> None:
        """逐条发出；飞书发送失败时，没发出去的留在 held 里下一轮重发（水位已推进，丢了就再也找不回来）。"""
        held = self.state["held"]
        for i, entry in enumerate(pending):
            try:
                self.forward(url, issue, entry)
            except Exception:
                held[url] = pending[i:]
                raise
        held.pop(url, None)

    def notes_of(self, w: Mapping[str, Any]) -> list[dict[str, Any]]:
        pid = quote(w["project"], safe="")
        return self.bridge.api(f"projects/{pid}/issues/{w['iid']}/notes?sort=desc&order_by=created_at&per_page=50")

    def fresh_entries(self, url: str, known: dict[str, Any], notes: list[dict[str, Any]], forward: bool) -> list[dict[str, Any]]:
        """水位之后要转的 Agent 发言，同时推进水位。"""
        fresh = []
        for n in sorted((n for n in notes if n["id"] > known["last_note"]), key=lambda n: n["id"]):
            if forward and self.should_forward(n, include_users=known.get("private_thread", False)):
                known["last_agent_at"] = self.bridge.now()
                fresh.append(n)
            known["last_note"] = n["id"]
        return [self.held_entry(url, known, n) for n in fresh]

    def held_entry(self, url: str, issue: Mapping[str, Any], n: Mapping[str, Any]) -> dict[str, Any]:
        body = FOLDED.sub("", (n.get("body") or "").strip()).strip()
        self.state["recent"] = (self.state["recent"] + [f"#{issue['iid']} {issue.get('title', '')}：{body[:200]}"])[-RECENT_CAP:]
        return {"id": n["id"], "body": body}

    def should_forward(self, n: Mapping[str, Any], include_users: bool = False) -> bool:
        author = (n.get("author") or {}).get("username")
        body = (n.get("body") or "").strip()
        return (not n.get("system") and (author == self.bridge.agent or include_users)
                and n["id"] not in self.bridge.state.get("posted_notes", []) and not RECEIPT.match(body))

    def forward(self, url: str, issue: Mapping[str, Any], entry: Mapping[str, Any]) -> None:
        link = f"{url}#note_{entry['id']}"
        body = entry["body"]
        if len(body) > SUMMARY_CAP:
            body = body[:SUMMARY_CAP].rstrip() + "…"
        head = f"**[#{issue['iid']}]({link}) {issue.get('title', '')}**"
        mid = self.say(f"{head}\n{body}", f"sec-{issue['iid']}-{entry['id']}")
        if mid:
            msgs = self.state["msgs"]
            msgs[mid] = url
            for old in list(msgs)[:-MSG_MAP_CAP]:
                del msgs[old]

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
        text = f"**每小时巡检（{time.strftime('%H:%M', time.localtime(self.bridge.now()))}）**\n" + ("\n".join(lines) or "现在没有在跑的需求。")
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
            if self.user_spoke_last(issue):
                parts.append("你已回复，等调度者接着处理")
            else:
                alerts.append("等你处理")
        if SHIP_LABEL in labels and info.get("state") == "opened" and self.ship_not_requested(issue, info):
            alerts.append("已待上线，但调度者没 @你 请求授权合并")
        owner = self.owner(issue.get("repo") or "", url)
        if owner.get("task"):
            pane = owner.get("live_pane")
            if pane:
                status = self.agent_status(pane)
                parts.append(f"调度者{STATUS_TEXT.get(status, status)}")
                busy = self.busy_members(pane)
                if busy:
                    parts.append(f"{'、'.join(busy)}正在干活")
                if status == "blocked":
                    alerts.append("调度者卡在确认界面，要到电脑上处理")
            elif not str(owner.get("status") or "").lower().startswith(find_scheduler().TERMINAL_STATUS):
                alerts.append("调度者已退出，任务还没做完")
            last = max(float(issue.get("last_agent_at") or 0), self.state_mtime(issue.get("repo") or "", owner["task"]))
            if info.get("state") == "opened" and WAITING_LABEL not in labels and self.bridge.now() - last > IDLE_ALERT_S:
                alerts.append(f"超过 {IDLE_ALERT_S // 3600} 小时没有动静")
        line = f"- [#{issue['iid']}]({url}) {issue.get('title', '')}：{'；'.join(parts)}"
        return line + ("\n  ⚠️ " + "；".join(alerts) if alerts else "")

    def user_spoke_last(self, issue: Mapping[str, Any]) -> bool:
        """最新一条有内容的评论是用户发的：球在调度者那边，标签只是还没改（收件箱会改回，这里兜底不误报）。"""
        pid = quote(issue["project"], safe="")
        notes = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}/notes?sort=desc&order_by=created_at&per_page=10")
        for n in notes:
            if n.get("system") or RECEIPT.match((n.get("body") or "").strip()):
                continue
            return (n.get("author") or {}).get("username") != self.bridge.agent
        return False

    def ship_not_requested(self, issue: Mapping[str, Any], info: Mapping[str, Any]) -> bool:
        """打上「待上线」后，调度者没有发过 @用户 的评论（请授权合并）：用户收不到通知，MR 就一直晾着。
        用户已经在后面说过话的不算（球在调度者那边）；查不到标签记录时不报，免得误报。"""
        if self.user_spoke_last(issue):
            return False
        pid = quote(issue["project"], safe="")
        added = 0.0
        for page in range(1, LABEL_EVENT_PAGES + 1):
            try:
                events = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}/resource_label_events?per_page=100&page={page}")
            except Exception:
                return False
            for e in events or []:
                if e.get("action") == "add" and (e.get("label") or {}).get("name") == SHIP_LABEL:
                    added = max(added, _ts(e.get("created_at")))
            if len(events or []) < 100:
                break
        if not added:
            return False
        mention = self.mention_re((info.get("author") or {}).get("username") or "")
        notes = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}/notes?sort=desc&order_by=created_at&per_page=50")
        for n in notes:
            if n.get("system") or (n.get("author") or {}).get("username") != self.bridge.agent:
                continue
            if _ts(n.get("created_at")) < added - SHIP_ASK_GRACE_S:
                break
            if mention and mention.search(n.get("body") or ""):
                return False
        return True

    def mention_re(self, *extra: str) -> Optional["re.Pattern[str]"]:
        people = {u for u in self.bridge.users.values() if u} | set(extra)
        people -= {"", self.bridge.agent}
        return re.compile("|".join(rf"@{re.escape(p)}(?![\w.-])" for p in sorted(people))) if people else None

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

    def busy_members(self, scheduler_pane: str) -> list[str]:
        """调度者派出去、正在干活的成员（按 state/members.json 的领取登记）；查不到就不写，不影响总览。"""
        try:
            agents = {a.get("pane_id"): a for a in json.loads(self.bridge.sh(["herdr", "agent", "list"]))["result"]["agents"]}
            members = json.loads((self.home / "state" / "members.json").read_text())
        except Exception:
            return []
        name = (agents.get(scheduler_pane) or {}).get("name")
        if not name:
            return []
        roles = {ROLE_TEXT.get(m.get("profile", ""), m.get("profile", "")) for p, m in members.items()
                 if m.get("scheduler") == name and (agents.get(p) or {}).get("agent_status") == "working"}
        return sorted(r for r in roles if r)

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
        thread = bool(msg.get("thread_id"))  # 私聊主窗口里就地回，不开话题
        target = self.target(msg)
        if target is not None:
            self.relay(target, msg, text)
            return
        if text.strip().lower() in STATUS_WORDS:
            lines = self.overview()
            self.bridge.reply(mid, "**总览**\n" + ("\n".join(lines) or "现在没有在跑的需求。"), f"sec-status-{mid}", in_thread=thread)
            return
        self.answer(msg, text)

    def relay(self, url: str, msg: Mapping[str, Any], text: str, ack: str = "") -> None:
        mid = msg["message_id"]
        thread = bool(msg.get("thread_id"))
        issue = self.state["issues"].get(url)
        project = issue["project"] if issue else self.bridge.project
        iid = issue["iid"] if issue else int(url.rsplit("/", 1)[-1])
        # 纯指令保持原样；普通引用回复与群消息共用上下文和附件收集。
        source = {**msg, "reply_to": "", "root_id": "", "context_warning": ""} if text.startswith("/") else dict(msg)
        context = self.bridge.message_context(source, project)
        text = self.bridge.strip_images(text)
        if not text and not context:
            self.bridge.reply(mid, "这条没有内容，没转。", f"sec-empty-{mid}", in_thread=thread)
            return
        body = text if text.startswith("/") and not context else \
            "\n\n".join(x for x in (text, context) if x) + "\n\n_（来自飞书秘书）_"
        self.bridge.note_as_user(project, iid, f"@{self.bridge.agent} {body}")
        title = f" {issue.get('title', '')}" if issue else ""
        self.bridge.reply(mid, f"已转给 [#{iid}]({url}){title}。{ack}", f"sec-relayed-{mid}", in_thread=thread)

    def linkify(self, text: str) -> str:
        """把正在跟进的需求的「#编号」换成 issue 链接；模型回答时常把总览里的链接丢掉。"""
        urls = {str(i["iid"]): u for u, i in self.state["issues"].items()}
        return BARE_ISSUE.sub(lambda m: f"[#{m.group(1)}]({urls[m.group(1)]})" if m.group(1) in urls else m.group(0), text)

    def recent_notes(self) -> list[str]:
        """各需求最近几条有内容的评论，回答「为什么」时常靠它（秘书启动前的评论不在最近转达里）。"""
        out = []
        for url, issue in sorted(self.state["issues"].items(), key=lambda kv: kv[1]["iid"]):
            pid = quote(issue["project"], safe="")
            try:
                notes = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}/notes?sort=desc&order_by=created_at&per_page=10")
            except Exception:
                continue
            kept = [n for n in notes if not n.get("system") and not RECEIPT.match((n.get("body") or "").strip())][:NOTES_PER_ISSUE]
            for n in reversed(kept):
                author = (n.get("author") or {}).get("username") or (n.get("author") or {}).get("name") or ""
                who = "Agent" if author == self.bridge.agent else "用户"
                body = FOLDED.sub("", (n.get("body") or "").strip()).strip()[:NOTE_CAP]
                out.append(f"- #{issue['iid']} {who}（{str(n.get('created_at') or '')[:16]}）：{body}")
        return out

    def ask_model(self, material: str) -> Optional[str]:
        cmd = ["omp", "-p", "--model", self.cfg["model"], "--thinking", self.cfg["thinking"], "--no-session",
               "--mode", "json", "--tools", "read", "--no-context-files", "--no-skills", "--no-rules",
               "--no-extensions", "--no-lsp", "--system-prompt", SYSTEM_PROMPT, material]
        try:
            r = self.bridge.run(cmd, capture_output=True, text=True, timeout=int(self.cfg["timeout_s"]),
                                check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        reply = ""
        for line in (r.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "turn_end":
                content = (ev.get("message") or {}).get("content") or []
                reply = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        return reply.strip() or None

    def answer(self, msg: Mapping[str, Any], question: str) -> None:
        """当提问回答；用户要秘书去问、去转告某个需求时，模型回「转给 #编号：…」，就以用户名义写到那张 issue。"""
        mid, thread = msg["message_id"], bool(msg.get("thread_id"))
        lines = self.overview()
        material = ("## 任务总览\n" + ("\n".join(lines) or "（没有在跑的需求）")
                    + "\n\n## 各需求最近的评论\n" + ("\n".join(self.recent_notes()) or "（无）")
                    + "\n\n## 最近转达\n" + ("\n".join(self.state["recent"]) or "（无）")
                    + f"\n\n## 机器人用法\n{getattr(self.bridge, 'help_text', '')}"
                    + f"\n\n## 用户的问题\n{question}")
        reply = self.ask_model(material)
        if reply is None:
            self.bridge.reply(mid, "这会儿没法回答，先给你当前总览：\n" + ("\n".join(lines) or "现在没有在跑的需求。"),
                              f"sec-answer-{mid}", in_thread=thread)
            return
        ask = ASK_LINE.match(reply)
        if ask:
            iid = int(ask.group(1))
            url = next((u for u, i in self.state["issues"].items() if i["iid"] == iid),
                       self.bridge.issue_url(self.bridge.project, iid))
            said = f"{ask.group(2).strip()}\n\n> 用户原话：{question}"
            self.relay(url, msg, said, ack="调度者回复后我会转到这里。")
            return
        self.bridge.reply(mid, self.linkify(reply), f"sec-answer-{mid}", in_thread=thread)
