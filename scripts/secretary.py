"""秘书：和机器人的私聊窗口就是秘书，统一转达各交付的消息、每小时巡检、回答提问、转发用户的快速回复。

问题：每个交付各有一张 issue 和一个飞书话题，同时跑的交付一多，过程存档全刷到私聊会把真正要拍板的消息淹没。
办法：私聊窗口同时当秘书：免打扰模式下只把需要用户决定、实质答复和整项交卷马上发到私聊主窗口（过程存档与步骤汇总留 GitLab，发 /详尽 则每条都转），巡检有变化或异常才汇报。
自然消息由一次模型决策识别新需求、已有任务转达、问答或追问，代码核验目标来源后由桥执行，避免背景被误作授权。
私聊绑定的 issue 只在秘书主窗口通知，原话题仍可回复；群聊绑定保留群内回复，秘书同时私聊转达。

只由 feishu-bridge.py 调用（同一进程、同一份飞书连接与状态文件），不单独运行：
  开启：白名单用户第一次在私聊里说话，桥就记下这个私聊窗口，之后的转达和巡检都发到这里，不需要命令。
  自然消息（包括对转达的回复、名称指代、进度提问）统一走 classify_intent：
    create / relay / answer / clarify → 桥分别负责建单、原文转达、只读答复或追问。
    mode 只表达本次授权；引用、附件和历史不能让普通查询升级成开工指令。
    结构或目标校验失败返回 None，由桥告知用户尚未执行，不回退旧语义入口。
  明确的斜杠指令和帮助入口由桥处理；模型 runner 与消息转达继续共用秘书现有实现。
看哪些 issue：收件箱登记的发起 issue（state/gitlab-inbox.json 的 origins）加上飞书桥里还没关闭的话题。
免打扰下 Agent 评论按 hwf-kind 标记与已知过程回执过滤，只推待办/风险/实质答复/整项交付完成；私聊绑定的 issue 也保留其他人评论。详尽模式除固定转交回执外全转。
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
PATROL_S = 3600
IDLE_ALERT_S = 3 * 3600  # 在途交付超过这么久没有任何动静，巡检里标出来
REVIEW_WIP_LIMIT = 3  # 同时等待用户拍板或确认上线的任务达到此阈值时，在总览与巡检顶部发出审核过载提醒
MSG_MAP_CAP = 500
RECENT_CAP = 20
SUMMARY_CAP = 600  # 转达只放开头一段，全文给链接
# 只认 gitlab-inbox.py deliver() 的两种整条回执；调度者以「已收到」开头的实质答复（#483 note_25889）照常转
RECEIPT = re.compile(r"^(@\S+\s+)?已收到(，已转交目标 issue \S+ 的消息，等待调度者回复。"
                     r"|目标 issue \S+ 的消息。目前没有在线负责人，已转交调度入口，等待接手后回复。)$")
# task-record.py note 附在评论末尾的类别标签；飞书不隐藏 HTML 注释，转发和拼摘要前必须剥掉。
KIND_TAG = re.compile(r"\s*<!--\s*hwf-kind:([a-z-]+)\s*-->\s*")
QUIET_FORWARD_KINDS = frozenset({"todo", "risk", "answer", "delivery-done"})
# 免打扰下无 hwf-kind 标签时的已知非待办噪音（收件箱纯命令确认回执、正常 wrapup 收尾回执、步骤汇总/存档备案/中途阶段播报）。
INBOX_ACK = re.compile(
    r"^(@\S+\s+)?(?:"
    r"开始跑交付流程[^\n]*?。已收到(?:，已转交目标 issue \S+|目标 issue \S+)[^\n]*"
    r"|已切到(?:免打扰|详尽)模式，[^\n]*"
    r"|已清空调度者上下文（新会话\s+`[^`]+`），它会按任务记录接着做。"
    r")$"
)
QUIET_ARCHIVE_NOTE = re.compile(
    r"^(?:#+\s*|\*\*)?(?:"
    r"步骤\s*\d+[a-zA-Z]?\s*完成(?:[:：\s*]|$)"
    r"|待定清单(?:\s|$)"
    r"|\d{4}-\d{2}-\d{2}\s*·\s*来自\s*#\d+"
    r"|(?:已评审(?:验收|测试)?用例|测试用例（已评审）|\d+\s*最终技术方案[^\n]*（已确认）)"
    r"|(?:根\s*issue\s*)?汇总分支(?:基线|创建|记录)"
    r"|本轮[^\n]{1,40}通过，整项需求尚未完成"
    r"|(?:代码|方案|用例)评审通过(?:\s|$|[：:，。*])"
    r"|(?:国内|国际|海外|测试环境)?冒烟(?:首轮|第\s*[0-9一二三四五六七八九十]+\s*轮)"
    r")"
)
USER_ACTION_HINT = re.compile(r"需要你决定|等你确认|待你确认|请确认|请回复|请授权|请合并|请测试|请处理|选一个")
STATE_VERBOSE_RE = re.compile(r"^- 通知模式：详尽", re.M)
FOLDED = re.compile(r"<details>.*?</details>", re.S)
BARE_ISSUE = re.compile(r"(?<![\[\w/&])#(\d+)(?!\d)")  # 已在链接里的「[#12]」、网址里的不算
WAITING_LABEL = "状态:等你处理"
SHIP_LABEL = "状态:待上线"
SHIP_ASK_GRACE_S = 15 * 60  # 调度者常先发「请授权合并」再改标签，往前放宽这么久都算
LABEL_EVENT_PAGES = 5
NOTES_PER_ISSUE = 3
NOTE_CAP = 400
STATUS_TEXT = {"working": "正在干活", "idle": "空闲", "done": "空闲", "blocked": "卡在确认界面", "unknown": "状态不明"}
ROLE_TEXT = {"arch": "架构师", "arch-lite": "方案作者", "precheck": "取证", "code-review": "代码评审", "test-writer": "写用例",
             "test": "测试", "test-herdr": "测试", "dev": "开发", "dev-fe": "前端开发", "dev-be": "后端开发"}
DEFAULT_MODEL = {"model": "jc-gpt/gpt-6-luna", "thinking": "medium", "timeout_s": 120}

INTRO = (
    "以后我也是你的秘书，所有在跑的需求都在这个窗口跟进：\n"
    "1. 需要你拍板、答复你的问题和整项交卷的消息，我马上转到这里（默认免打扰，步骤汇总与过程存档留 GitLab；发 /详尽 则每条都转）\n"
    "2. 每小时看一遍总览，有变化或卡住了才汇报\n"
    "3. 对某条转达点「回复」，我把你的话转给那个需求；也可以发「#编号 内容」\n"
    "4. 想问某个需求的调度者，直接说「帮我问 481 的调度…」，我替你问，回复会转到这里\n"
    "5. 发「进度」看总览，发「帮助」看用法，其他话直接问我"
)


def strip_kind_tag(text: str) -> tuple[str, Optional[str]]:
    """剥离评论里的 hwf-kind HTML 注释标记，避免飞书原样渲染出原始注释标签。"""
    match = KIND_TAG.search(text)
    return KIND_TAG.sub("", text).strip(), (match.group(1) if match else None)


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_FS: Any = None
_INTENT = _load("hwf_task_intent", "task_intent.py")
_JC = _load("jev_client", "jev_client.py")
_JG = _load("jev_gates", "jev_gates.py")

JEV_RELAY_PROB_MIN = 0.80
JEV_AUTH_NOW_MIN = 0.85
JEV_QUIET_FORWARD_MIN = 0.50


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
        bridge_cfg = getattr(bridge, "cfg", None) or {}
        self.jev = getattr(bridge, "jev", None) or _JC.JevClient(
            bridge_cfg.get("typesafe") if "typesafe" in bridge_cfg else None
        )
        self.intent_error: str | None = None

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
    def known_tasks(self) -> dict[str, dict[str, Any]]:
        """目标只取本项目登记，不能因为其他项目恰好有同号任务就转过去。"""
        project = getattr(self.bridge, "project", "")
        if not project:
            return {}
        return {canon(url): issue for url, issue in {**self.watched(), **self.state["issues"]}.items()
                if issue.get("project") == project
                and canon(url) == canon(self.bridge.issue_url(project, issue["iid"]))}

    def single_task_url(self, text: str) -> Optional[str]:
        """只解析单一当前项目链接；引用作者和任务存在性由调用方另行验证。"""
        links = re.findall(r"https?://[^\s<>()\[\]\"，。；！？]+", text)
        task_links = {canon(url.split("#", 1)[0].split("?", 1)[0]).rstrip(".,;")
                      for url in links if "/-/issues/" in url or "/-/work_items/" in url}
        if len(task_links) != 1:
            return None
        url = next(iter(task_links))
        number = url.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[1-9][0-9]*", number):
            return None
        if url != canon(self.bridge.issue_url(self.bridge.project, int(number))):
            return None
        # 回答里还提到另一任务（即使没有链接）也不能偷偷选第一张。
        if any(m.group(1) != number for m in BARE_ISSUE.finditer(text)):
            return None
        return url

    def remember_reply(self, message_id: Optional[str], text: str) -> None:
        url = self.single_task_url(text)
        if not message_id or url not in self.known_tasks():
            return
        self.state["msgs"][message_id] = url
        for old in list(self.state["msgs"])[:-MSG_MAP_CAP]:
            del self.state["msgs"][old]

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
        """需要用户处理的 Agent 发言马上转到私聊；免打扰下过程存档只留 GitLab，详尽模式下全部转达。"""
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
        # 同步收件箱记录的通知模式与仓库路径，避免 poll_gitlab 先于 secretary.poll 运行时漏认 /详尽。
        if url in (watched := self.watched()):
            known.update(watched[url])
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

    def track(self, url: str, issue: Mapping[str, Any], last_note: int | None = None) -> dict[str, Any]:
        """登记要跟进的任务；已有记录保留水位，不能被整张 GitLab issue 覆盖。"""
        known = self.state["issues"].get(url)
        if known is None:
            known = {"project": issue["project"], "iid": int(issue["iid"]), "title": issue.get("title", ""),
                     "last_agent_at": self.bridge.now()}
            if last_note is not None:
                known["last_note"] = last_note
            self.state["issues"][url] = known
        return known

    def notes_of(self, w: Mapping[str, Any]) -> list[dict[str, Any]]:
        pid = quote(w["project"], safe="")
        return self.bridge.api(f"projects/{pid}/issues/{w['iid']}/notes?sort=desc&order_by=created_at&per_page=50")

    def is_verbose(self, issue: Mapping[str, Any]) -> bool:
        """优先看收件箱登记的 notify；未显式写 quiet 时兜底读本机 STATE.md，兼容用户在终端里直接切详尽模式。"""
        notify = str(issue.get("notify") or "").strip().lower()
        if notify == "verbose":
            return True
        if notify == "quiet":
            return False
        repo, project, iid = issue.get("repo") or "", issue.get("project") or "", issue.get("iid")
        if repo and project and iid:
            state_file = Path(str(repo)) / ".hwf" / "tmp" / f"{str(project).replace('/', '-')}-{iid}" / "STATE.md"
            try:
                return bool(STATE_VERBOSE_RE.search(state_file.read_text(encoding="utf-8")))
            except OSError:
                return False
        return False

    def fresh_entries(self, url: str, known: dict[str, Any], notes: list[dict[str, Any]], forward: bool) -> list[dict[str, Any]]:
        """水位之后要转的发言，同时推进水位；免打扰被拦下的过程评论仍刷新活跃时间，避免误报静默超时。"""
        if "last_note" not in known:
            # 只登记了身份、还没有水位：按第一次看到处理，不回放旧评论。
            known["last_note"] = max([n["id"] for n in notes] or [0])
            return []
        fresh = []
        verbose = self.is_verbose(known)
        for n in sorted((n for n in notes if n["id"] > known["last_note"]), key=lambda n: n["id"]):
            clean_body, _ = strip_kind_tag(n.get("body") or "")
            if not n.get("system") and (n.get("author") or {}).get("username") == self.bridge.agent and not RECEIPT.match(clean_body):
                known["last_agent_at"] = self.bridge.now()
            if forward and self.should_forward(n, include_users=known.get("private_thread", False), verbose=verbose):
                fresh.append(n)
            known["last_note"] = n["id"]
        return [self.held_entry(url, known, n) for n in fresh]

    def held_entry(self, url: str, issue: Mapping[str, Any], n: Mapping[str, Any]) -> dict[str, Any]:
        clean_body, _ = strip_kind_tag(n.get("body") or "")
        body = FOLDED.sub("", clean_body).strip()
        self.state["recent"] = (self.state["recent"] + [f"#{issue['iid']} {issue.get('title', '')}：{body[:200]}"])[-RECENT_CAP:]
        return {"id": n["id"], "body": body}

    def should_forward(self, n: Mapping[str, Any], include_users: bool = False, verbose: bool = False) -> bool:
        if n.get("system") or n["id"] in self.bridge.state.get("posted_notes", []):
            return False
        author = (n.get("author") or {}).get("username")
        if author != self.bridge.agent:
            return include_users
        body, kind = strip_kind_tag(n.get("body") or "")
        if RECEIPT.match(body):
            return False
        if verbose:
            return True
        if kind is not None:
            return kind in QUIET_FORWARD_KINDS
        if INBOX_ACK.match(body):
            return False
        if body.startswith("**收尾回执**") and "**残留**" not in body:
            return False
        if body.startswith("**收尾回执**"):
            return True
        summary = FOLDED.sub("", body).strip()
        if QUIET_ARCHIVE_NOTE.match(summary) and not USER_ACTION_HINT.search(summary):
            return False
        if USER_ACTION_HINT.search(summary):
            return True
        mention = self.mention_re()
        if mention and mention.search(summary):
            return True
        return self._jev_needs_user_attention(summary)

    def _jev_needs_user_attention(self, summary: str) -> bool:
        """无 hwf-kind 标签且未命中固定归档模板的 Agent 评论，用 Jev 区分待办/实质答复与内部进度噪音；不可用时默认放行。"""
        if not summary or self.jev is None:
            return True
        try:
            res = self.jev.decide(
                state={"agent_comment": summary[:4000]},
                questions={
                    "attention": _JC.Noul(
                        instructions=(
                            "In quiet notification mode, should `agent_comment` be pushed immediately to the user's "
                            "private chat instead of staying silently archived in GitLab?"
                        ),
                        criteria={
                            "true": (
                                "Requires a user decision, confirmation, authorization, credential/action, reports a "
                                "blocker/risk/residual issue, answers a question the user asked, or announces final "
                                "delivery/readiness to ship."
                            ),
                            "false": (
                                "Routine internal progress update, intermediate step completion, test/review pass "
                                "note while overall work continues, or archival artifact link requiring no user action."
                            ),
                        },
                    )
                },
            )
            prob = res.noul("attention")
            cutoff = _JG.threshold_of("secretary_quiet_attention_min", JEV_QUIET_FORWARD_MIN, self.home)
            forward = prob >= cutoff
            _JG.record_gate_event(
                "gate2_quiet_filter",
                "forward" if forward else "archive",
                rules_evaluated=["attention"],
                rules_triggered={"attention": prob} if forward else None,
                near_misses={"attention": prob} if (cutoff - 0.12) <= prob < cutoff else None,
            )
            return forward
        except _JC.JevUnavailableError:
            return True

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
    @staticmethod
    def pending_review_count(lines: list[str]) -> int:
        """统计总览里当前真正等待用户拍板、操作或确认上线的任务数。"""
        count = 0
        for line in lines:
            if "你已回复，等调度者接着处理" in line:
                continue
            if (
                "等你处理" in line
                or "卡在确认界面" in line
                or "已待上线，但调度者没 @你" in line
                or "状态:待上线" in line
            ):
                count += 1
        return count

    @classmethod
    def review_load_banner(cls, lines: list[str]) -> str:
        """同时待审任务达到阈值时给出批处理提醒，保护用户注意力且不跳过任何把关红线。"""
        pending = cls.pending_review_count(lines)
        if pending < REVIEW_WIP_LIMIT:
            return ""
        return (
            f"⚠️ 当前有 {pending} 个需求同时等你拍板或确认上线（达到审核提醒阈值 {REVIEW_WIP_LIMIT}），"
            "建议先集中批处理完再开新任务。"
        )

    def patrol(self, force: bool = False) -> Optional[str]:
        """到点（或 force）就看一遍；内容和上次发的不同才发。返回发出的文本。"""
        if not self.chat:
            return None
        if not force and self.bridge.now() - float(self.state.get("last_patrol") or 0) < PATROL_S:
            return None
        self.state["last_patrol"] = self.bridge.now()
        lines = self.overview()
        banner = self.review_load_banner(lines)
        body_lines = ([banner, *lines] if banner else lines)
        digest = "\n".join(body_lines)
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

    def relay_body(self, msg: Mapping[str, Any], text: str, *, mode: str = "analyze",
                   context: str | None = None, project: str | None = None) -> str:
        if context is None:
            # 纯命令不继承引用指令；附件仍归目标项目。
            source = {**msg, "reply_to": "", "root_id": "", "context_warning": ""} if text.startswith("/") else dict(msg)
            context = self.bridge.message_context(source, project or self.bridge.project,
                                                  self.bridge.take_pending_images(msg.get("chat_id", "")))
        text = self.bridge.strip_images(text)
        if not text and not context:
            return ""
        body = text if text.startswith("/") and not context else \
            "\n\n".join(x for x in (text, context) if x) + "\n\n_（来自飞书秘书）_"
        return self.bridge.execution_body(body, mode)

    def relay(self, url: str, msg: Mapping[str, Any], text: str, ack: str = "", *, mode: str = "analyze") -> None:
        mid = msg["message_id"]
        thread = bool(msg.get("thread_id"))
        issue = self.state["issues"].get(url)
        project = issue["project"] if issue else self.bridge.project
        iid = issue["iid"] if issue else int(url.rsplit("/", 1)[-1])
        body = self.relay_body(msg, text, mode=mode, project=project)
        if not body:
            self.bridge.reply(mid, "这条没有内容，没转。", f"sec-empty-{mid}", in_thread=thread)
            return
        self.bridge.note_as_user(project, iid, f"@{self.bridge.agent} {body}")
        title = f" {issue.get('title', '')}" if issue else ""
        self.bridge.reply(mid, f"已转给 [#{iid}]({url}){title}。{ack}", f"sec-relayed-{mid}", in_thread=thread)

    def linkify(self, text: str) -> str:
        """把正在跟进的需求的「#编号」换成 issue 链接；模型回答时常把总览里的链接丢掉。"""
        urls = {str(i["iid"]): u for u, i in self.state["issues"].items()}
        return BARE_ISSUE.sub(lambda m: f"[#{m.group(1)}]({urls[m.group(1)]})" if m.group(1) in urls else m.group(0), text)

    def recent_notes(self, *, candidates: list[dict] | None = None) -> list[str]:
        """只读调用方范围内的最近评论，避免群聊分类材料混入其他会话任务。"""
        out = []
        issues = self.state["issues"].values() if candidates is None else candidates
        for issue in sorted(issues, key=lambda item: item["iid"]):
            pid = quote(issue["project"], safe="")
            try:
                notes = self.bridge.api(f"projects/{pid}/issues/{issue['iid']}/notes?sort=desc&order_by=created_at&per_page=10")
            except Exception:
                continue
            kept = [n for n in notes if not n.get("system") and not RECEIPT.match(strip_kind_tag(n.get("body") or "")[0])][:NOTES_PER_ISSUE]
            for n in reversed(kept):
                author = (n.get("author") or {}).get("username") or (n.get("author") or {}).get("name") or ""
                who = "Agent" if author == self.bridge.agent else "用户"
                clean_body, _ = strip_kind_tag(n.get("body") or "")
                body = FOLDED.sub("", clean_body).strip()[:NOTE_CAP]
                out.append(f"- #{issue['iid']} {who}（{str(n.get('created_at') or '')[:16]}）：{body}")
        return out

    def classify_intent(self, text: str, context: str = "", *, candidates: list[dict] | None = None,
                        linked_issue: int | None = None, recent: list[dict] | None = None) -> dict | None:
        """高频明确单任务转交优先由 Jev 毫秒级判定并经 validate_intent 核验；建单/总览/追问/拆批回退 omp。"""
        self.intent_error = None
        # 显式空候选是调用方限制范围，不能回退到秘书的全量登记。
        candidates = list(self.known_tasks().values()) if candidates is None else candidates
        recent = [] if recent is None else recent
        identities = [{key: item[key] for key in ("iid", "title", "project") if key in item}
                      for item in candidates]
        fast = self._fast_relay_with_jev(text, context, candidates=candidates, identities=identities,
                                         linked_issue=linked_issue, recent=recent)
        if fast is not None:
            return fast
        material = json.dumps({"current_message": text, "context": context, "candidates": identities,
                               "linked_issue": linked_issue, "recent": recent}, ensure_ascii=False)
        response = self.ask_model(material)
        if response is None:
            self.intent_error = self.intent_error or "no_response"
            return None
        try:
            intent = json.loads(response)
        except ValueError:
            self.intent_error = "invalid_json"
            return None
        result = _INTENT.validate_intent(intent, text, candidates, linked_issue, recent)
        self.intent_error = "invalid_intent" if result is None else None
        return result

    def _fast_relay_with_jev(
        self,
        text: str,
        context: str,
        *,
        candidates: list[dict],
        identities: list[dict],
        linked_issue: int | None,
        recent: list[dict],
    ) -> dict | None:
        if self.jev is None or not text.strip():
            return None
        # 多轮澄清拼接或分段批量续发涉及历史拆句，交由 omp 完整处理。
        if "同一次待澄清请求的用户原话：\n" in text or "本次转告的逐项状态" in context or "引用的任务无法核实" in context:
            return None

        target_options: dict[str, str] = {}
        for item in identities:
            iid = item.get("iid")
            if type(iid) is int and iid > 0:
                target_options[str(iid)] = f"Existing task #{iid}: {item.get('title') or ''}"
        if type(linked_issue) is int and linked_issue > 0 and str(linked_issue) not in target_options:
            target_options[str(linked_issue)] = f"Directly replied-to task #{linked_issue}"
        if not target_options:
            return None
        target_options["ambiguous"] = "Multiple tasks match or the reference is ambiguous."
        target_options["none"] = "No specific existing task (brand-new request, general overview/help, or casual chat)."

        state_obj = {
            "current_message": text,
            "linked_issue": linked_issue,
            "recent_tasks": [{"iid": r.get("iid"), "title": r.get("title", "")} for r in recent],
            "candidate_tasks": identities,
            "context_excerpt": context[:2000],
        }
        questions = {
            "action": _JC.Choice(
                "Classify what the user wants to do in `current_message`.",
                {
                    "relay": (
                        "Send a reply, instruction, pause/resume/constraint, progress/reason question, or go-ahead "
                        "to ONE specific existing task (questions about a specific task MUST be `relay` to its "
                        "scheduler, never answered by the secretary)."
                    ),
                    "batch": "Send instructions to TWO OR MORE distinct existing tasks in one message.",
                    "create": "Propose or claim a brand-new feature, bug report, or investigation not tied to an existing task.",
                    "overview_or_help": "Ask about overall status across all tasks, bot usage, or general chat with no target task.",
                    "clarify": "Target task or whether this is new vs existing work cannot be determined without asking the user.",
                },
            ),
            "target": _JC.Choice(
                (
                    "Which single existing task does `current_message` refer to? Priority: (1) explicit issue number "
                    "or unique task title in `current_message` (numbers used as counts/quantities like '484 samples' "
                    "are NOT issue numbers); (2) `linked_issue` when replying without naming another task; (3) the "
                    "sole task in `recent_tasks` when `current_message` refers to 'that one just now' and "
                    "`recent_tasks` has length 1."
                ),
                target_options,
            ),
            "mode": _JC.Choice(
                "Determine execution mode strictly from `current_message` ITSELF (never from `context_excerpt`).",
                {
                    "analyze": "Question, discussion, answer to a choice, pause/stop, continue testing, constraint, or read-only analysis.",
                    "deliver": "`current_message` explicitly commands starting feature implementation or code changes right now.",
                    "debug": "`current_message` explicitly commands fixing or debugging a bug right now.",
                },
            ),
            "auth_now": _JC.Noul(
                "Does `current_message` ITSELF (ignoring `context_excerpt`) explicitly authorize starting code changes or bug fixing right now?",
                {
                    "true": "Clear immediate implementation/fix command in `current_message` (e.g. '开始实现', '修一下这个', '按A改代码').",
                    "false": "Asking progress/why, pausing, continuing verification/testing, discussing, or no explicit coding command.",
                },
            ),
        }
        try:
            res = self.jev.decide(state_obj, questions)
            act, _, act_probs = res.choice("action")
            tgt, _, tgt_probs = res.choice("target")
            mode, mode_conf, _ = res.choice("mode")
            auth_p = res.noul("auth_now")
        except _JC.JevUnavailableError:
            return None

        relay_min = _JG.threshold_of("secretary_fast_relay_min", JEV_RELAY_PROB_MIN, self.home)
        auth_min = _JG.threshold_of("secretary_auth_now_min", JEV_AUTH_NOW_MIN, self.home)
        relay_prob = act_probs.get("relay", 0.0)
        if act != "relay" or relay_prob < relay_min:
            _JG.record_gate_event(
                "gate2_secretary_relay",
                "fallback_omp",
                rules_evaluated=["fast_relay"],
                near_misses={"fast_relay": relay_prob} if (relay_min - 0.15) <= relay_prob < relay_min else None,
                meta={"action": act},
            )
            return None
        tgt_prob = tgt_probs.get(tgt, 0.0)
        if not tgt.isdigit() or tgt_prob < relay_min:
            _JG.record_gate_event(
                "gate2_secretary_relay",
                "fallback_omp",
                rules_evaluated=["fast_relay"],
                near_misses={"target": tgt_prob} if tgt.isdigit() and (relay_min - 0.15) <= tgt_prob < relay_min else None,
                meta={"action": "target_uncertain"},
            )
            return None
        if mode in ("deliver", "debug") and (auth_p < auth_min or mode_conf < 0.70):
            mode = "analyze"
        built = _INTENT.build_verified_relay_intent(text, int(tgt), mode, candidates, linked_issue, recent)
        _JG.record_gate_event(
            "gate2_secretary_relay",
            "fast_relay" if built is not None else "rejected_by_validator",
            rules_evaluated=["fast_relay"],
            rules_triggered={"fast_relay": min(relay_prob, tgt_prob)} if built is not None else None,
            meta={"mode": mode},
        )
        return built

    def ask_model(self, material: str) -> Optional[str]:
        self.intent_error = None
        # 沿用秘书既有的只读工具权限：当前模型通道在 --no-tools 下会返回空响应。
        # 分类提示禁止工具，下面还会拒绝任何实际使用过工具的分类结果。
        tools = ["--tools", "read"]
        cmd = ["omp", "-p", "--model", self.cfg["model"], "--thinking", self.cfg["thinking"], "--no-session",
               "--mode", "json", *tools, "--no-context-files", "--no-skills", "--no-rules",
               "--no-extensions", "--no-lsp", "--system-prompt",
               _INTENT.INTENT_PROMPT, material]
        try:
            r = self.bridge.run(cmd, capture_output=True, text=True, timeout=int(self.cfg["timeout_s"]),
                                check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.intent_error = "timeout"
            return None
        except OSError:
            self.intent_error = "start_error"
            return None
        except subprocess.SubprocessError:
            self.intent_error = "process_error"
            return None
        if r.returncode != 0:
            self.intent_error = "process_exit"
            return None
        reply = ""
        for line in (r.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                self.intent_error = "invalid_event"
                return None
            if isinstance(ev, dict) and ev.get("type") in (
                "tool_execution_start", "tool_execution_end",
            ):
                self.intent_error = "tool_used"
                return None
            if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
                self.intent_error = "invalid_event"
                return None
            if ev["type"] != "turn_end":
                continue
            message = ev.get("message")
            if not isinstance(message, dict):
                self.intent_error = "invalid_event"
                return None
            if message.get("stopReason") not in (None, "stop"):
                self.intent_error = "interrupted_turn"
                return None
            content = message.get("content")
            if not isinstance(content, list) or any(
                    not isinstance(c, dict) or not isinstance(c.get("type"), str) for c in content):
                self.intent_error = "invalid_event"
                return None
            if any(c.get("type") in {"toolCall", "tool_use", "tool_call"} for c in content):
                self.intent_error = "tool_used"
                return None
            texts = [c.get("text") for c in content if c.get("type") == "text"]
            if any(not isinstance(text, str) for text in texts):
                self.intent_error = "invalid_event"
                return None
            reply = "".join(texts)
        if not reply.strip():
            self.intent_error = "empty_response"
            return None
        return reply.strip()

