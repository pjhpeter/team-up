#!/usr/bin/env python3
"""飞书桥：把飞书对话和 GitLab issue 双向同步。Agent 团队仍只认 GitLab，本脚本不碰 Herdr。

    python3 ~/.hwf/scripts/feishu-bridge.py            # 常驻（建议放在一个 Herdr 窗格里）
    python3 ~/.hwf/scripts/feishu-bridge.py --check    # 只检查配置、令牌与飞书连接后退出

分工（2026-11-02 用户指示，方案 A：每人一个飞书应用，桥跑在自己电脑上）：
  飞书 → GitLab：本脚本（收消息、建 issue、把话题回复写成 issue 评论）
  GitLab → Agent：原有的 gitlab-inbox.py（不改）
  GitLab → 飞书：轮询新评论；私聊统一由秘书通知，群聊保留原话题回复
桥负责识别入口与搬运；实际分析、交付仍由 GitLab 收件箱处理。

飞书里怎么用（只认 users 白名单里的人）：
  直接说「希望增加…」「帮我修这个 bug」→ 新功能默认先分析，明确修复请求走 /go debug；缺信息时先追问
  「需求：<内容>」或「/需求 <内容>」  → 在 default_project 建 issue（打开关标签、指派给 Agent 账号），回帖 issue 链接
  「#123 <可选内容>」或贴 issue 链接   → 把这条消息的话题关联到已有 issue，内容写成评论（带 @Agent 以便转交）
  在上面两种消息的话题里回复         → 写成该 issue 的评论；/go、/status、/new、/免打扰、/详尽 等指令原样生效
  私聊里对秘书转达的消息点「回复」   → 写成那个需求 issue 的评论（私聊窗口就是秘书，见 secretary.py）
  私聊里的其他话                     → 「进度」回总览，「帮助」回用法；讨论只回答，不建单或开工
  群里自然说需求或回复话题           → 同样识别意图，但每条都必须 @机器人；引用前文只是背景，不算执行授权
发到 GitLab 的内容用用户自己的 GitLab 令牌（钥匙串里取），这样收件箱才认得是用户本人在说话。

依赖：lark-cli（首次使用按引导建好机器人应用，只用机器人身份）、glab。
接入步骤见 Wiki「飞书接入」。配置：~/.hwf/feishu-bridge.local.json（本机，不入库；open_id 每个应用不同，所以整份放本机），例：
  {
    "default_project": "frontend/web-v1",
    "users": {"ou_xxx": "pengjh"},
    "gitlab_token_keychain": {"service": "hwf-feishu-bridge", "account": "pengjh"}
  }
  可选：gitlab_host（默认取 gitlab-inbox.config.json 的 host）、agent_account（默认 glab 当前账号）、
        label（默认 herdr-auto）、poll_seconds（默认 15）。
状态：~/.hwf/state/feishu-bridge.json。
恢复：启动、消息连接恢复后自动补读；运行时每分钟查漏。只查上次读完之后的消息，按消息 ID 去重。
电脑休眠期间不能实时收信，醒来后补收；补收会照常执行积压指令。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import html
import json
import os
from pathlib import Path
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote
import urllib.request
import uuid

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import gitlab_projects as GP
import poll_backoff as PB

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
CONFIG_PATH = HWF_HOME / "feishu-bridge.local.json"
INBOX_CONFIG_PATH = HWF_HOME / "gitlab-inbox.config.json"
STATE_PATH = HWF_HOME / "state" / "feishu-bridge.json"
MEDIA_DIR = HWF_HOME / "state" / "feishu-media"  # 飞书图片/文件下载的中转目录（lark-cli 只许写到当前目录下）

CMD_TIMEOUT_S = 60
TRANSFER_TIMEOUT_S = 300  # 下载/上传附件可能是几十 MB 的文件，单独放宽
SEEN_CAP = 2000
HISTORY_POLL_S = 60
HISTORY_OVERLAP_MS = 120_000  # 历史接口时间只精确到分钟，重读边界以免漏掉同一分钟的消息
STALE_THREAD_DAYS = 14
STALE_THREAD_S = STALE_THREAD_DAYS * 86400
POLL_BACKOFF_STEPS = (30, 60, 180)
HISTORY_BACKOFF_STEPS = (60, 120, 180)
HELP_COOLDOWN_S = 600
NOTE_RETRIES = 3  # 一条评论连续发送失败几次后改发 GitLab 链接
FEISHU_TEXT_CAP = 2500  # 超过就截断并附 GitLab 原文链接
CONTEXT_TEXT_CAP = 6000  # 建单时带上的话题前文总字数上限，超出只留最近的
# 秘书或话题同步搬到飞书的 GitLab 评论，首行带该评论链接。
RELAYED_NOTE_HEAD = re.compile(r"^[^\n]*?\((https?://[^\s()]+/-/(?:issues|work_items)/\d+#note_\d+)\)")
FOLDED = re.compile(r"<details>.*?</details>", re.S)  # 评论里折叠的全文
KIND_TAG = re.compile(r"\s*<!--\s*hwf-kind:([a-z-]+)\s*-->\s*")  # task-record 附的类别注释，飞书会原样显示故渲染前剥离
CREATE_RE = re.compile(r"^\s*(?:/需求|/issue|需求\s*[:：])\s*", re.I)
# lark-cli 把消息里的附件渲染成这些标记（见 lark-im 文档 messages-resources-download）：
#   图片：实时事件「[Image: img_xxx]」，历史记录「![Image](img_xxx)」→ 下载用 --type image
#   文件/视频/音频：「<file key="file_xxx" name="a.zip"/>」「<video key=… name=…/>」「<audio key=… duration=…/>」→ --type file
#   文件夹：「<folder key=… name=…>」里展开一层子文件（子项也是 <file …/>），文件夹标签本身只去掉不下载
IMAGE_KEY_RE = re.compile(r"\[Image: (img_[^\]\s]+)\]|!\[Image\]\((img_[^)\s]+)\)")
FILE_TAG_RE = re.compile(r"<(?:file|video|audio)\b[^>]*?\bkey=\"([^\"]+)\"[^>]*?/?>")
NAME_ATTR_RE = re.compile(r"\bname=\"([^\"]*)\"")
FOLDER_TAG_RE = re.compile(r"</?folder\b[^>]*>")
PENDING_IMAGE_S = 600  # 单独发的图片/文件在这段时间内会自动带进下一条「需求：」/「#编号」
IMAGE_HINT = "收到附件。10 分钟内接着说要做什么，或发「#编号」关联已有 issue，我会把附件一起带上。"
# 链接尾巴只吃 URL 里的 ASCII 字符，免得把紧跟的「，处理这个…」吞掉；同时支持 #123、group/repo#123 与完整 issue 链接
BIND_RE = re.compile(
    r"^\s*(?:#(\d+)|([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)#(\d+)|https?://\S+?/-/(?:issues|work_items)/(\d+)(?:[/?#][!-~]*)?)[\s,，。:：;；]*"
)
QUALIFIED_ISSUE_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)#([1-9][0-9]*)(?![0-9])")
# 中文可紧贴任务编号；数字只用于查候选，不代表已获转告授权。
ISSUE_NUMBER_RE = re.compile(r"(?<![0-9A-Za-z_./])[1-9][0-9]*(?![0-9])")
FAILED_TEXT = "这条没处理成功，请重发。"
# 白名单外的人找机器人时随机回一句（2026-11-09 用户指定前两句，其余为配套编写）
DENY_TEXTS = (
    "给钱我主人，我帮你干活",
    "我是牛马，阿巴阿巴阿巴.......",
    "牛马正在吃草，有事请找我主人",
    "牛马今日工位已满，请找我主人排队",
    "我是有主的牛马，不接私活",
    "想使唤我？先问问我主人同不同意",
    "本牛马只吃主人喂的草",
    "你 @ 我也没用，我的缰绳在主人手里",
    "对不起，你不在我的饲料名单上",
    "牛马已读，但牛马不回（除非主人发话）",
)
HELP_TEXT = (
    "用法：\n"
    "1. 直接说要做什么（也支持「需求：<要做的事>」）→ 新需求默认先分析；明确说「帮我修这个 bug」会进入修复流程\n"
    "2. 发「#123 <补充>」→ 关联已有 issue，之后在话题里回复就会同步过去\n"
    "3. 在话题里说「开始修复」或「开工」即可交付，也支持 /go、/status 看进度、/new 重开会话\n"
    "4. 嫌消息多就回复 /免打扰（只在步骤完成、交付完成和要你处理时发），想看每一步就回复 /详尽\n"
    "5. 私聊窗口就是秘书：各需求的进度都会转到这里，对转达点「回复」就能回给那个需求，发「进度」看总览，其他话直接问\n"
    "在群里每句话（包括话题里的回复）都要 @我，我才收得到"
)

HELP_WORDS = frozenset({"帮助", "用法", "/help", "help"})

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Uploader = Callable[[str, Path], str]  # (project, 本地文件) → 可直接放进评论的 Markdown


# 飞书 IM 机器人已被移出群聊（230002）或群已解散（230019）属于永久不可达错误，不能与普通无权限（230027）混同重试。
RETIRED_CHAT_ERROR_RE = re.compile(
    r"""(?ix)
    ["']code["']\s*:\s*(?:230002|230019)\b
    | can\s+not\s+be\s+out\s+of\s+the\s+chat
    | chat\s+is\s+disbanded
    | chat\s+does\s+not\s+exist
    """
)


class BridgeError(Exception):
    pass


def is_retired_chat_error(error: BaseException) -> bool:
    return bool(RETIRED_CHAT_ERROR_RE.search(str(error)))


_EVENT_LOG_PATH: Optional[Path] = None
_EVENT_LOG_MAX_BYTES = 2 * 1024 * 1024


def append_event_log(path: Optional[Path], line: str) -> None:
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


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as out:
        json.dump(data, out, ensure_ascii=False, indent=1)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)  # 原子替换
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cap_list(items: list, cap: int = SEEN_CAP) -> list:
    return items[-cap:]


def find_attachments(text: str) -> list[dict[str, str]]:
    """按出现顺序列出消息里的附件：{key, kind: image|file, name}（图片没有文件名）。"""
    found: list[tuple[int, dict[str, str]]] = []
    for m in IMAGE_KEY_RE.finditer(text):
        found.append((m.start(), {"key": m.group(1) or m.group(2), "kind": "image", "name": ""}))
    for m in FILE_TAG_RE.finditer(text):
        name = NAME_ATTR_RE.search(m.group(0))
        found.append((m.start(), {"key": m.group(1), "kind": "file",
                                  "name": html.unescape(name.group(1)) if name else ""}))
    return [a for _, a in sorted(found, key=lambda x: x[0])]


def strip_attachments(text: str) -> str:
    return FOLDER_TAG_RE.sub("", FILE_TAG_RE.sub("", IMAGE_KEY_RE.sub("", text))).strip()


def safe_filename(name: str, fallback: str) -> str:
    """飞书里的文件名可能带路径分隔符或以点开头，落盘前只保留最后一段。"""
    name = name.replace("\\", "/").split("/")[-1].strip().lstrip(".")
    return name or fallback


class Bridge:
    help_text = HELP_TEXT  # 秘书回答「怎么用」时当材料

    def __init__(self, config: dict[str, Any], state: dict[str, Any], run: Runner = subprocess.run,
                 user_token: str = "", now: Callable[[], float] = time.time,
                 upload: Optional[Uploader] = None, media_dir: Path = MEDIA_DIR,
                 persist: Optional[Callable[[], None]] = None):
        self.cfg, self.state, self.run, self.now = config, state, run, now
        self.upload = upload or self.http_upload
        self.persist = persist or (lambda: None)
        self.relay_summary: Optional[str] = None
        self.media_dir = media_dir
        self.host: str = config["gitlab_host"]
        self.project: str = config["default_project"]
        self.label: str = config.get("label", "herdr-auto")
        self.users: dict[str, str] = dict(config["users"])
        self.user_token = user_token
        self.agent: str = config.get("agent_account") or self.api("user")["username"]
        self.project_cache = GP.ProjectActivityCache()
        # 默认项目直接预标为活跃，避免单仓日常轮询多发 GET projects/<pid>。
        self.project_cache.mark(self.project, True)
        state.setdefault("threads", {})      # 话题根消息 id → {project, iid, chat_id, last_note, closed}
        state.setdefault("seen", [])         # 已处理的飞书消息 id
        state.setdefault("posted_notes", []) # 桥自己发到 GitLab 的评论 id（不再搬回飞书）
        state.setdefault("chats", {})        # chat_id → 上次看到消息的时间戳（毫秒）
        # 实时收到新消息不代表之前的历史已读完，两种进度必须分开。
        state.setdefault("history_cursors", {f"chat:{chat}": ms for chat, ms in state["chats"].items()})
        state.setdefault("history_seen", {})  # chat_id → 尚在补读窗口内的消息 ID/时间，不能被 SEEN_CAP 淘汰
        state.setdefault("help_sent", {})    # chat_id → 上次发用法说明的时间戳（秒）
        state.setdefault("group_chats", [])  # 收到过群消息的 chat_id（补漏时群里只处理 @ 机器人的）
        state.setdefault("thread_ids", {})   # 飞书话题 thread_id → threads 的键（话题里任何一条消息都带 thread_id）
        state.setdefault("pending_images", {})  # chat_id → [{mid, key, kind, name, ts}] 单独发、还没用上的图片/文件
        state.setdefault("pending_intents", {})  # 会话/用户/话题隔离的待补充需求，保留原话而非模型摘要
        state.setdefault("recent_targets", {})  # 只记用户实际交互的目标，后台播报不改变焦点
        self._secretary: Any = None
        self._bot_app_id: Optional[str] = None  # 只缓存当前 CLI 身份，不能把任意 cli_ 作者当成本机器人。
        self.delivered: Optional[dict[str, Any]] = None  # 处理当前这条消息时已代用户写进评论的 issue（project、iid）
        self.history_requested = threading.Event()
        self.next_history_poll = 0.0
        self._history_tracker = PB.PollFailureTracker(heartbeat_every=0, label="catch-up")

    @property
    def secretary(self) -> Any:
        """秘书（私聊窗口，见 secretary.py）；按需创建，只读命令（--split-topic 等）不会碰到它。"""
        if self._secretary is None:
            self._secretary = secretary_module().Secretary(self)
        return self._secretary

    def is_project_allowed(self, project: str) -> bool:
        """校验 GitLab 项目是否机器人有权限且未归档；默认项目免查。"""
        if not project:
            return False
        if project == self.project:
            return True
        return self.project_cache.is_active(project, self.api)

    def issue_ref(self, project: str, iid: int) -> str:
        return GP.format_issue_ref(project, iid, self.project)

    # ---------- 外部命令 ----------
    def sh(self, cmd: list[str], stdin: Optional[str] = None, env: Optional[dict] = None,
           cwd: Optional[Path] = None, timeout: int = CMD_TIMEOUT_S) -> str:
        # 卡住或起不来和返回非 0 一样算一次普通失败：记日志、下一轮再试，不能让常驻进程退出
        try:
            r = self.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise BridgeError(f"{' '.join(cmd[:4])} 超时（{timeout} 秒没返回）") from e
        except OSError as e:
            raise BridgeError(f"{' '.join(cmd[:4])} 启动失败：{e}") from e
        if r.returncode != 0:
            raise BridgeError(f"{' '.join(cmd[:4])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def api(self, path: str, method: str = "GET", fields: Optional[dict[str, str]] = None,
            as_user: bool = False) -> Any:
        cmd = ["glab", "api", "--hostname", self.host, "-X", method, path]
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        env = None
        if as_user:
            if not self.user_token:
                raise BridgeError("没有用户 GitLab 令牌，不能代用户发言")
            env = {**os.environ, "GITLAB_TOKEN": self.user_token}
        out = self.sh(cmd, env=env)
        return json.loads(out) if out.strip() else None

    def lark(self, args: list[str], stdin: Optional[str] = None, cwd: Optional[Path] = None,
             timeout: int = CMD_TIMEOUT_S) -> Any:
        out = self.sh(["lark-cli", *args, "--as", "bot"], stdin=stdin, cwd=cwd, timeout=timeout)
        data = json.loads(out) if out.strip() else {}
        if isinstance(data, dict) and data.get("ok") is False:
            raise BridgeError(f"lark-cli {args[:2]} 失败：{data.get('error')}")
        return data

    def reply(self, message_id: str, markdown: str, key: Optional[str] = None, in_thread: bool = True) -> Optional[str]:
        args = ["im", "+messages-reply", "--message-id", message_id, "--markdown", "-"]
        if in_thread:
            args.insert(4, "--reply-in-thread")
        if key:
            args += ["--idempotency-key", key[:50]]
        return (self.lark(args, stdin=markdown).get("data") or {}).get("message_id")

    def http_upload(self, project: str, path: Path) -> str:
        """以用户身份把文件传到 GitLab 项目附件（glab api 不支持上传文件），返回 Markdown。"""
        if not self.user_token:
            raise BridgeError("没有用户 GitLab 令牌，不能上传附件")
        boundary = uuid.uuid4().hex
        filename = path.name.replace('"', "'")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://{self.host}/api/v4/projects/{quote(project, safe='')}/uploads", data=body, method="POST",
            headers={"PRIVATE-TOKEN": self.user_token, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=TRANSFER_TIMEOUT_S) as r:
                return json.loads(r.read())["markdown"]
        except (OSError, ValueError, KeyError) as e:
            raise BridgeError(f"上传附件失败：{e}") from e

    def take_pending_images(self, chat: str) -> list[tuple[str, list[dict[str, str]]]]:
        """取出这个会话里 10 分钟内单独发的附件（图片或文件）。旧状态里只有图片，没有 kind。"""
        fresh = [p for p in self.state["pending_images"].pop(chat, []) if self.now() - p["ts"] <= PENDING_IMAGE_S]
        return [(p["mid"], [{"key": p["key"], "kind": p.get("kind", "image"), "name": p.get("name", "")}])
                for p in fresh]

    def recover_reply(self, msg: dict[str, Any]) -> None:
        """CLI 的紧凑消息会丢引用字段；只对已授权的指令补读原生关系，不改话题归属。"""
        if msg.get("reply_to"):
            return
        mid = msg["message_id"]
        try:
            data = self.lark(["api", "GET", f"/open-apis/im/v1/messages/{mid}"])
            item = next((m for m in (data.get("data") or {}).get("items", []) if m.get("message_id") == mid), None)
            if item is None:
                raise BridgeError("message metadata unavailable")
            msg["reply_to"] = item.get("parent_id") or ""
            msg["root_id"] = item.get("root_id") or msg.get("root_id") or ""
        except BridgeError as e:
            log(f"read reply metadata {mid} failed: {e}")
            msg["context_warning"] = "_（未能读取飞书引用关系，可能缺少上下文。）_"

    def recover_task_reply(self, msg: dict[str, Any]) -> Optional[str]:
        """恢复可信引用的目标，不替用户决定回答、转交还是另起需求。"""
        quoted = msg.get("reply_to") or ""
        if (not quoted or quoted == msg["message_id"]) and not msg.get("context_warning"):
            return None
        try:
            if not quoted:
                raise BridgeError("reply relationship unavailable")
            items = (self.lark(["im", "+messages-mget", "--message-ids", quoted,
                                "--no-reactions"]).get("data") or {}).get("messages", [])
            item = next((m for m in items if m.get("message_id") == quoted), None)
            if item is None or item.get("deleted"):
                raise BridgeError("quoted message unavailable")
            if item.get("chat_id") and item["chat_id"] != msg.get("chat_id"):
                raise BridgeError("quoted message belongs to another chat")
            content = self.clean_text(normalize_history(item))
            sender = item.get("sender") or {}
            # 普通引用仍只是背景，不改变原有讨论/建单入口。
            if not re.search(r"/-/(?:issues|work_items)/", content):
                if re.search(r"(?<![\w/])#[1-9][0-9]*\b", content):
                    msg["unresolved_task_reference"] = True
                return None
            own_bot = False
            if sender.get("sender_type") in ("app", "bot"):
                configured = self.cfg.get("bot_open_id")
                if configured:
                    own_bot = configured in (sender.get("id"), sender.get("open_bot_id"))
                else:
                    if self._bot_app_id is None:
                        identity = self.lark(["whoami"])
                        if identity.get("identity") != "bot" or identity.get("available") is not True \
                                or not identity.get("appId"):
                            raise BridgeError("current bot identity unavailable")
                        self._bot_app_id = identity["appId"]
                    own_bot = sender.get("id") == self._bot_app_id
            target = self.secretary.single_task_url(content) if own_bot else None
            if target:
                project, iid = topics_module().parse_issue_url(self.cfg, target)
                if not self.is_project_allowed(project):
                    raise BridgeError("quoted project archived or inaccessible")
                issue = self.api(f"projects/{quote(project, safe='')}/issues/{iid}")
                if not issue or issue.get("iid") != iid:
                    raise BridgeError("quoted task unavailable")
        except (BridgeError, topics_module().TopicError) as e:
            log(f"recover quoted task {quoted} failed: {e}")
            target = None
        if target:
            return target
        msg["unresolved_task_reference"] = True
        return None

    def message_context(self, msg: dict[str, Any], project: str,
                        extra: Optional[list[tuple[str, list[dict[str, str]]]]] = None,
                        *, include_thread: bool = False, exclude_related: tuple[str, ...] = (),
                        upload: bool = True, full: bool = False, done: Optional[set[str]] = None,
                        covered: Optional[set[str]] = None, history_text: bool = True) -> str:
        """收集背景；分类阶段只读文字和附件标记，确定任务后才搬附件。

        full 时话题前文不截断（建单用）；covered 收集已写进前文的消息 ID；
        history_text=False 时调用方自己写追问原话，这里只补它们各自的背景。"""
        context, context_files, seen = self.thread_context(msg, full=full) if include_thread else ("", [], set())
        done = set() if done is None else done
        if covered is not None:
            covered.update(seen)
        sources = [(msg["message_id"], find_attachments(msg.get("content") or ""))] + (extra or []) + context_files
        parts = [p for p in (context, msg.get("context_warning")) if p]
        for previous in msg.get("intent_history", []):
            if previous["message_id"] in seen:
                # 已在话题前文里；再展开会让前文重复、同一张图重复上传
                continue
            if history_text:
                parts.append("**此前的用户原话**：\n\n" + self.clean_text(previous))
            previous_context = self.message_context(previous, project, include_thread=True, upload=upload,
                                                    full=full, done=done, covered=covered)
            if previous_context:
                parts.append(previous_context)
        seen.update((msg["message_id"], *exclude_related))
        for related in dict.fromkeys((msg.get("reply_to") or "", msg.get("root_id") or "")):
            if not related or related in seen:
                continue
            seen.add(related)
            try:
                items = (self.lark(["im", "+messages-mget", "--message-ids", related, "--no-reactions"]).get("data") or {}).get("messages", [])
                item = next((m for m in items if m.get("message_id") == related), None)
                if item is None or item.get("deleted"):
                    raise BridgeError("quoted message unavailable")
                raw = self.clean_text(normalize_history(item))
                sources.append((related, find_attachments(raw)))
                body = strip_attachments(raw).strip()
                relayed = RELAYED_NOTE_HEAD.match(body) if (item.get("sender") or {}).get("sender_type") in ("app", "bot") else None
                if relayed:
                    # 机器人搬来的 GitLab 评论，接收方能直接打开原评论，附全文只会重复。
                    parts.append(f"**引用的消息**：{relayed.group(1)}")
                elif body:
                    if len(body) > CONTEXT_TEXT_CAP:
                        body = body[:CONTEXT_TEXT_CAP] + "\n（引用内容较长，后文省略。）"
                    parts.append("**引用的消息**（从飞书原样搬来）：\n\n" +
                                 "\n".join("> " + line if line else ">" for line in body.splitlines()))
            except BridgeError as e:
                log(f"read quoted message {related} failed: {e}")
                parts.append(f"_（未能读取引用消息 {related}，请补充这段上下文。）_")
        for mid, items in sources:
            for a in items:
                if a["key"] in done:
                    continue
                done.add(a["key"])
                parts.append(self.transfer(project, mid, a) if upload else
                             ("[图片]" if a["kind"] == "image" else f"[文件 {a['name'] or '未命名'}]"))
        return "\n\n".join(parts)

    def transfer(self, project: str, mid: str, a: dict[str, str]) -> str:
        """下载一个飞书附件再传到 GitLab；失败时返回一句说明，不影响建单/评论。"""
        # 每个附件一个子目录，文件按原名落盘，GitLab 里显示的就是原文件名
        workdir = self.media_dir / a["key"]
        try:
            return self.upload(project, self.download(mid, a, workdir, TRANSFER_TIMEOUT_S))
        except BridgeError as e:
            log(f"{a['kind']} {a['key']} of {mid} failed: {e}")
            what = "张图片" if a["kind"] == "image" else f"个文件{'「' + a['name'] + '」' if a['name'] else ''}"
            return f"_（有一{what}没搬过来：{e}）_"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def download(self, mid: str, a: dict[str, str], workdir: Path, timeout: int) -> Path:
        """把一个飞书附件下载到 workdir（lark-cli 只许写到当前目录下），返回落盘路径。"""
        workdir.mkdir(parents=True, exist_ok=True)
        out = a["key"] if a["kind"] == "image" else safe_filename(a["name"], a["key"])
        data = self.lark(["im", "+messages-resources-download", "--message-id", mid, "--file-key", a["key"],
                          "--type", a["kind"], "--output", out], cwd=workdir, timeout=timeout)
        path = Path((data.get("data") or {}).get("saved_path") or "")
        return path if path.is_absolute() else workdir / path

    # ---------- 飞书 → GitLab ----------
    def handle(self, msg: dict[str, Any]) -> None:
        """处理一条飞书消息（实时事件或补漏拉到的历史，字段已统一）。"""
        mid, chat = msg["message_id"], msg.get("chat_id", "")
        pending_seen = self.state["history_seen"].setdefault(chat, {})
        already_seen = mid in self.state["seen"] or mid in pending_seen
        pending_seen[mid] = int(msg.get("create_ms") or self.now() * 1000)
        if already_seen:
            return
        self.state["seen"] = cap_list(self.state["seen"] + [mid])
        pending = list(self.state["pending_images"].get(msg.get("chat_id", ""), []))
        self.delivered = None
        self.relay_summary = None
        try:
            self.dispatch(msg)
        except Exception as e:  # 消息已记为处理过，不会自动重试，无论什么错都要让发消息的人知道
            log(f"handle {mid} failed: {e!r}")
            self.report_failure(msg, pending)

    def report_failure(self, msg: dict[str, Any], pending: list[dict[str, Any]]) -> None:
        """告诉发消息的人结果。话题已记录或评论已写进 issue 说明事情办成了，只是回帖失败，这时重发会重复。"""
        mid, chat = msg["message_id"], msg.get("chat_id", "")
        t = self.state["threads"].get(mid) or self.delivered
        if not t:  # 没办成：把单独发的附件放回去，重发时还能带上
            if pending:
                self.state["pending_images"][chat] = pending
            else:
                self.state["pending_images"].pop(chat, None)
        if msg.get("sender_type") != "user" or msg.get("sender_id") not in self.users:
            return
        text = f"[#{t['iid']}]({self.issue_url(t['project'], t['iid'])}) 已收到这条消息，只是回执没发出去，不用重发。" if t else FAILED_TEXT
        if t and t.get("execution_pending"):
            text = (f"[#{t['iid']}]({self.issue_url(t['project'], t['iid'])}) 已建单，但开工指令未发送成功。"
                    "不要重发需求；请在这条消息下回复「开始修复」或「开工」。")
        if self.relay_summary:
            text = self.relay_summary
        # 主聊天区的引用回复也带 root_id（被引用那条），但不是话题；只有真在话题里才用话题回，否则会凭空开出一个话题
        in_thread = bool(mid in self.state["threads"] or msg.get("thread_id")
                         or msg.get("root_id") in self.state["threads"])
        try:
            self.reply(mid, text, f"failed-{mid}", in_thread=in_thread)
        except (BridgeError, ValueError) as e:
            log(f"tell failure of {mid} failed: {e}")

    def dispatch(self, msg: dict[str, Any]) -> None:
        mid = msg["message_id"]
        chat = msg.get("chat_id", "")
        if chat:
            self.state["history_cursors"].setdefault(
                f"chat:{chat}", self.state["chats"].get(chat, int(msg.get("create_ms") or self.now() * 1000)))
            self.state["chats"][chat] = max(int(msg.get("create_ms") or 0), self.state["chats"].get(chat, 0))
            if msg.get("chat_type") == "group" and chat not in self.state["group_chats"]:
                self.state["group_chats"].append(chat)
        if msg.get("sender_type") != "user":
            return  # 机器人（含自己）发的不处理，避免来回刷屏
        mentioned = any(map(self.is_bot_mention, msg.get("mentions") or []))
        user = self.users.get(msg.get("sender_id", ""))
        if msg.get("chat_type") == "group" and not mentioned:
            # 应用开了「获取群组中所有消息」权限后，群里每句话都会推过来；没 @ 机器人的不是对它说的，
            # 一律不回（否则会给同事回「没有使用权限」或用法说明，在群里刷屏）。2026-11-08
            return
        if not user:
            self.reply(mid, random.choice(DENY_TEXTS), f"deny-{mid}")
            return
        self.recover_reply(msg)
        text = self.clean_text(msg)
        private = msg.get("chat_type") != "group" and chat not in self.state["group_chats"]
        if private:
            self.secretary.learn(chat)
        root, tid = msg.get("root_id") or "", msg.get("thread_id") or ""
        linked = self.state["thread_ids"].get(tid) if tid else None
        if not linked and root and root != mid and root in self.state["threads"]:
            linked = root
        if linked and self.state["threads"][linked].get("chat_id") != chat:
            linked = None
        target = self.secretary.target(msg) if private else None
        if target:
            try:
                t_proj, _ = topics_module().parse_issue_url(self.cfg, target)
                if not self.is_project_allowed(t_proj):
                    target = None
            except topics_module().TopicError:
                target = None
        if text.startswith("/") and (linked or target):
            if linked:
                self.follow_up(linked, msg, text)
            else:
                self.secretary.relay(target, msg, text)
            return
        # 只有开头明确选另一个 issue 才分流；普通正文提到编号仍是当前话题的回复。
        command_text = self.strip_images(text)
        c = CREATE_RE.match(command_text)
        candidate = command_text[c.end():] if c else command_text
        m = BIND_RE.match(candidate)
        if m and not c and (self.pending_keys(msg) or len(set(ISSUE_NUMBER_RE.findall(command_text))) > 1):
            # 补充编号和多目标句子都须完整理解，不能静默只绑定第一个。
            self.natural_entry(msg, text, user, linked=linked, target=target)
            return
        bind_project: Optional[str] = None
        bind_iid: Optional[int] = None
        if m:
            if m.group(4):
                url = re.match(r"https?://[^\s,，。;；]+", candidate.strip()).group(0)
                try:
                    bind_project, bind_iid = topics_module().parse_issue_url(self.cfg, url)
                except topics_module().TopicError:
                    self.reply(mid, "该链接不是当前 GitLab 实例的合法 issue 链接；本话题绑定不变，这条消息未转发。", f"wrong-project-{mid}")
                    return
                if not self.is_project_allowed(bind_project):
                    self.reply(mid, f"项目 {bind_project} 已归档或机器人没有权限；本话题绑定不变，这条消息未转发。", f"wrong-project-{mid}")
                    return
            elif m.group(2) and m.group(3):
                bind_project, bind_iid = m.group(2), int(m.group(3))
                if not self.is_project_allowed(bind_project):
                    self.reply(mid, f"项目 {bind_project} 已归档或机器人没有权限；本话题绑定不变，这条消息未转发。", f"wrong-project-{mid}")
                    return
            else:
                bind_iid = int(m.group(1))
            if linked:
                cur_t = self.state["threads"][linked]
                eff_proj = bind_project or cur_t["project"]
                if (eff_proj, bind_iid) != (cur_t["project"], cur_t["iid"]):
                    topics_module().redirect(self, linked, msg, bind_iid, project=bind_project or self.project)
                    return
        # 引用回复（主对话里的「回复 xxx」）不算话题，按普通消息处理；真在话题里但还没关联的，发「需求：」也认
        # 「需求：」后面紧跟已有 issue 的链接或 #编号，是让 Agent 处理那张单，关联它而不是新建
        # 图文消息可能先放附件；命令只看文字，上传仍从原始 msg 提取附件。
        if m and bind_iid is not None:
            if linked:
                self.follow_up(linked, msg, text)
                self.remember_target(msg, self.state["threads"][linked])
            else:
                self.bind(mid, chat, bind_iid, m.string[m.end():].strip(), user, msg, project=bind_project)
            return
        if c and (command_text[c.end():].strip() or find_attachments(text)):
            self.create(mid, chat, command_text[c.end():].strip(), user, msg.get("sender_name") or user, msg)
            return
        # 未关联话题也能直接说需求，不再要求命令前缀。
        attachments = find_attachments(text)
        if attachments and not self.strip_images(text) and not (linked or target):
            # 单独发图片/文件：记下来等下一条需求带上；提示用引用回复，不开话题，免得多出一个空话题
            self.state["pending_images"].setdefault(chat, []).extend(
                {"mid": mid, "ts": self.now(), **a} for a in attachments)
            self.reply(mid, IMAGE_HINT, f"img-hint-{mid}", in_thread=False)
            return
        if not c and command_text.strip().lower() not in HELP_WORDS:
            target = target or (None if linked else self.recover_task_reply(msg))
            if text.startswith("/"):
                if target:
                    self.secretary.relay(target, msg, text)
                else:
                    self.reply(mid, "请先用 #编号 选择任务，再发送这条指令。", f"command-target-{mid}",
                               in_thread=bool(msg.get("thread_id")))
                return
            self.natural_entry(msg, text, user, linked=linked, target=target)
            return
        if private:
            self.reply(mid, HELP_TEXT, f"help-{mid}", in_thread=False)
            return
        last = self.state["help_sent"].get(chat, 0)
        if self.now() - last >= HELP_COOLDOWN_S:
            self.state["help_sent"][chat] = self.now()
            self.reply(mid, HELP_TEXT, f"help-{mid}")

    @staticmethod
    def intent_scope(msg: dict[str, Any]) -> str:
        return json.dumps([msg.get("chat_id", ""), msg.get("sender_id"), msg.get("thread_id") or "main"])

    def recent_targets(self, msg: dict[str, Any]) -> list[dict]:
        scopes = self.state["recent_targets"]
        for key in list(scopes):
            scopes[key] = [item for item in scopes[key] if self.now() - item["ts"] <= 1800]
            if not scopes[key]:
                del scopes[key]
        return scopes.get(self.intent_scope(msg), [])

    def remember_target(self, msg: dict[str, Any], issue: dict) -> None:
        recent = self.recent_targets(msg)
        key = self.intent_scope(msg)
        proj = str(issue.get("project") or self.project)
        self.state["recent_targets"].pop(key, None)
        self.state["recent_targets"][key] = (
            [item for item in recent if (item.get("project") or self.project, item["iid"]) != (proj, issue["iid"])]
            + [{"iid": issue["iid"], "title": issue.get("title", ""),
                "project": proj, "ts": self.now(),
                "time": datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")}]
        )[-20:]
        for old in list(self.state["recent_targets"])[:-200]:
            del self.state["recent_targets"][old]

    def intent_candidates(
        self,
        msg: dict[str, Any],
        text: str,
        linked_issue: Optional[int],
        linked_project: Optional[str] = None,
    ) -> list[dict]:
        """编号仅扩大只读候选，不等于转交许可；群候选绝不从私聊全量登记取。"""
        group = msg.get("chat_type") == "group" or msg.get("chat_id") in self.state["group_chats"]
        known: dict[tuple[str, int], dict] = {}
        if not group:
            for item in self.secretary.known_tasks().values():
                proj = str(item.get("project") or self.project)
                known[(proj, int(item["iid"]))] = dict(item, project=proj)
        for item in self.state["threads"].values():
            if item.get("closed"):
                continue  # 已关闭话题默认不进路由候选；若用户显式点名编号或回复原消息，下方 explicit_refs 会再查回。
            proj = str(item.get("project") or self.project)
            if item.get("chat_id") == msg.get("chat_id") and self.is_project_allowed(proj):
                known[(proj, int(item["iid"]))] = dict(item, project=proj)
        for item in self.recent_targets(msg):
            proj = str(item.get("project") or self.project)
            if self.is_project_allowed(proj):
                known.setdefault((proj, int(item["iid"])), dict(item, project=proj))

        # 解析跨项目 URL 与 group/repo#iid 显式引用
        explicit_refs: set[tuple[str, int]] = set()
        for url in re.findall(r"https?://[^\s<>，。；]+", text):
            if "/-/issues/" in url or "/-/work_items/" in url:
                try:
                    proj, iid = topics_module().parse_issue_url(self.cfg, url)
                except topics_module().TopicError:
                    continue
                if self.is_project_allowed(proj):
                    explicit_refs.add((proj, iid))
        for m in QUALIFIED_ISSUE_RE.finditer(text):
            proj, iid = m.group(1), int(m.group(2))
            if self.is_project_allowed(proj):
                explicit_refs.add((proj, iid))
        if linked_issue:
            explicit_refs.add((linked_project or self.project, linked_issue))

        # 裸数字只在尚未以跨项目显式引用命中时，补查默认项目
        qualified_iids = {iid for _, iid in explicit_refs}
        for n in ISSUE_NUMBER_RE.findall(text):
            iid = int(n)
            if iid not in qualified_iids:
                explicit_refs.add((self.project, iid))

        for proj, iid in explicit_refs:
            try:
                issue = self.api(f"projects/{quote(proj, safe='')}/issues/{iid}")
            except BridgeError:
                continue
            if issue and issue.get("iid") == iid:
                known[(proj, iid)] = {**issue, "project": proj}
        return [dict(item, iid=iid, project=proj) for (proj, iid), item in known.items()]

    def resolve_relay_project(
        self,
        iid: int,
        action_item: dict[str, Any],
        candidates: list[dict],
        linked_project: Optional[str],
        linked_issue: Optional[int],
    ) -> Optional[str]:
        """根据引用来源与候选任务确定唯一目标项目；跨项目同号无法唯一确定时返回 None。"""
        ref = action_item.get("reference")
        ev = str(action_item.get("evidence") or "")
        if ref == "linked" and linked_project and linked_issue == iid:
            return linked_project
        for url in re.findall(r"https?://[^\s<>()\[\]\"，。；！？]+", ev):
            if "/-/issues/" in url or "/-/work_items/" in url:
                try:
                    proj, url_iid = topics_module().parse_issue_url(self.cfg, url)
                    if url_iid == iid:
                        return proj
                except topics_module().TopicError:
                    pass
        for m in QUALIFIED_ISSUE_RE.finditer(ev):
            if int(m.group(2)) == iid:
                return m.group(1)
        matched_projects = sorted({str(c.get("project") or self.project) for c in candidates if c.get("iid") == iid})
        if len(matched_projects) == 1:
            return matched_projects[0]
        if len(matched_projects) > 1:
            return None
        return linked_project if linked_issue == iid and linked_project else self.project

    def classify_message(self, msg: dict[str, Any], text: str, *, candidates: list[dict],
                         linked_issue: Optional[int] = None, recent: list[dict]) -> Optional[dict]:
        context = self.message_context(msg, self.project, include_thread=True, upload=False)
        if msg.get("intent_question"):
            context += "\n\n机器人上次追问（仅作背景）：\n" + msg["intent_question"]
        if msg.get("unresolved_task_reference"):
            context += "\n引用的任务无法核实；不能把它当作新需求，需追问目标。"
        if msg.get("relay_plan"):
            context += "\n\n本次转告的逐项状态（sent 已成功，不要重复；其余仍待处理）：\n" + json.dumps(
                msg["relay_plan"], ensure_ascii=False)
        context += "\n\n## 机器人用法\n" + HELP_TEXT
        context += "\n\n## 候选任务最近评论\n" + "\n".join(self.secretary.recent_notes(candidates=candidates))
        return self.secretary.classify_intent(text, context, candidates=candidates,
                                             linked_issue=linked_issue, recent=recent)

    def pending_keys(self, msg: dict[str, Any]) -> list[str]:
        """按存量记录的范围匹配，旧版引用 ID 作键也不影响重启后承接。"""
        scoped, direct = [], []
        for key, item in self.state["pending_intents"].items():
            if item["chat"] != msg.get("chat_id", "") or item["sender"] != msg.get("sender_id"):
                continue
            if (item.get("thread") or "") == (msg.get("thread_id") or ""):
                scoped.append(key)
            original_ids = {message["message_id"] for message in item["messages"]}
            if msg.get("reply_to") and msg["reply_to"] in item["reply_ids"] \
                    and msg["reply_to"] not in original_ids:
                direct.append(key)
        return direct or scoped

    def save_pending_intent(self, key: str, msg: dict, previous: Optional[dict], *,
                            linked_issue: Optional[int], status: str, question: str = "",
                            linked_project: Optional[str] = None) -> dict:
        item = {**(previous or {}), "chat": msg.get("chat_id", ""), "sender": msg.get("sender_id"),
                "thread": msg.get("thread_id") or "", "linked_issue": linked_issue,
                "linked_project": linked_project or (previous.get("linked_project") if previous else None),
                "status": status, "question": question,
                "messages": (previous["messages"] if previous else []) + [dict(msg)],
                "reply_ids": list(previous["reply_ids"] if previous else [])}
        self.state["pending_intents"][key] = item
        return item

    def natural_entry(self, msg: dict[str, Any], text: str, user: str, *,
                      linked: Optional[str] = None, target: Optional[str] = None) -> None:
        mid, chat = msg["message_id"], msg.get("chat_id", "")
        pending = self.state["pending_intents"]
        key = self.intent_scope(msg)
        matches = self.pending_keys(msg)
        if len(matches) > 1:
            self.reply(mid, "这里还有多项未完成请求，请直接回复你想继续的那条消息。尚未新建或转交。",
                       f"pending-choice-{mid}", in_thread=bool(msg.get("thread_id")))
            return
        if matches:
            key = matches[0]
        previous = pending.get(key)
        source = {**msg, "intent_history": previous["messages"], "intent_question": previous["question"],
                  "relay_plan": previous.get("relay_plan", [])} if previous else dict(msg)
        request = text
        if previous:
            request = ("同一次待澄清请求的用户原话：\n"
                       + "\n\n".join(self.clean_text(item) for item in previous["messages"])
                       + "\n\n本次用户补充（若改变或撤销原意，以本次为准）：\n" + text)
        linked_project: Optional[str] = None
        linked_issue: Optional[int] = None
        if linked:
            linked_project = str(self.state["threads"][linked].get("project") or self.project)
            linked_issue = int(self.state["threads"][linked]["iid"])
        elif target:
            linked_project, linked_issue = topics_module().parse_issue_url(self.cfg, target)
        if linked_issue is None and previous:
            linked_issue = previous.get("linked_issue")
            linked_project = previous.get("linked_project")
        in_thread = bool(msg.get("thread_id"))
        for url in re.findall(r"https?://[^\s<>，。；]+", text):
            if "/-/issues/" in url or "/-/work_items/" in url:
                try:
                    u_proj, _ = topics_module().parse_issue_url(self.cfg, url)
                except topics_module().TopicError:
                    self.reply(mid, "该链接不是当前 GitLab 实例的合法 issue 链接，这条消息未转发。", f"wrong-project-{mid}", in_thread=in_thread)
                    return
                if not self.is_project_allowed(u_proj):
                    self.reply(mid, f"项目 {u_proj} 已归档或机器人没有权限，这条消息未转发。", f"wrong-project-{mid}", in_thread=in_thread)
                    return
        candidates = self.intent_candidates(msg, request, linked_issue, linked_project)
        recent = self.recent_targets(msg)
        intent = self.classify_message(source, request, candidates=candidates, linked_issue=linked_issue, recent=recent)
        if intent is None:
            # 内部故障不等于用户没说清，也不能擅自转给引用里的另一张任务。
            reason = self.secretary.intent_error or "invalid_decision"
            log(f"intent failed mid={mid} reason={reason}")
            item = self.save_pending_intent(key, msg, previous, linked_issue=linked_issue,
                                            linked_project=linked_project, status="failed")
            self.persist()
            reply_id = self.reply(mid, "这次消息处理失败，尚未新建或启动任务，也未新增转告。原请求已保留，请回复“重试”。",
                                  f"intent-failed-{mid}", in_thread=in_thread)
            if reply_id:
                item["reply_ids"].append(reply_id)
            return
        if intent["action"] == "create" and source.get("unresolved_task_reference"):
            intent = {"action": "clarify", "issue": None, "mode": "analyze",
                      "reply": "没能确认引用对应哪张任务，尚未新建或转交。请写明任务编号或链接。"}
        if intent["action"] == "batch" or intent["action"] == "relay" and previous and previous.get("relay_plan"):
            if intent["action"] == "batch":
                actions = [
                    {**act, "project": self.resolve_relay_project(act["issue"], act, candidates, linked_project, linked_issue) or self.project}
                    for act in intent["actions"]
                ]
            else:
                old = next((item for item in previous["relay_plan"] if item["issue"] == intent["issue"]), None)
                directive = (old["text"] + "\n\n用户补充（以本次为准）：\n" + text) if old else text
                proj = (old.get("project") if old else None) or self.resolve_relay_project(
                    intent["issue"], intent, candidates, linked_project, linked_issue) or self.project
                actions = [{**intent, "text": directive, "project": proj}]
            item = self.save_pending_intent(key, msg, previous, linked_issue=linked_issue,
                                            linked_project=linked_project, status="partial")
            self.deliver_relay_plan(key, source, actions, item)
            return
        if intent["action"] == "answer" and (intent["issue"] or linked_issue):
            # 已确定任务的提问由该任务调度者回答：秘书只原文转交，不凭材料代答。
            intent = {**intent, "action": "relay", "issue": intent["issue"] or linked_issue, "mode": "analyze"}
        action, mode, iid = intent["action"], intent["mode"], intent["issue"]
        if action in ("answer", "clarify"):
            if action == "clarify":
                item = self.save_pending_intent(key, msg, previous, linked_issue=linked_issue,
                                                linked_project=linked_project, status="clarify", question=intent["reply"])
                self.persist()
            reply_text = self.secretary.linkify(intent["reply"])
            reply_id = self.reply(mid, reply_text, f"intent-{mid}", in_thread=in_thread)
            if action == "clarify":
                if reply_id:
                    item["reply_ids"].append(reply_id)
            else:
                pending.pop(key, None)
            return
        try:
            if action == "relay":
                target_project = self.resolve_relay_project(iid, intent, candidates, linked_project, linked_issue)
                if not target_project:
                    projs = "、".join(sorted({str(c.get("project") or self.project) for c in candidates if c.get("iid") == iid}))
                    q = f"多个项目都有 #{iid}（{projs}），请写明 `项目路径#{iid}` 或贴出完整链接。"
                    item = self.save_pending_intent(key, msg, previous, linked_issue=linked_issue,
                                                    linked_project=linked_project, status="clarify", question=q)
                    self.persist()
                    reply_id = self.reply(mid, q, f"intent-ambig-{mid}", in_thread=in_thread)
                    if reply_id:
                        item["reply_ids"].append(reply_id)
                    return
                issue = self.api(f"projects/{quote(target_project, safe='')}/issues/{iid}")
                if issue.get("state") == "closed":
                    self.reply(mid, "这张任务已关闭，未转交。请明确要继续处理的未关闭任务，或提出独立新需求。",
                               f"closed-{mid}", in_thread=in_thread)
                    return
                if linked and (target_project, iid) == (self.state["threads"][linked]["project"], self.state["threads"][linked]["iid"]):
                    self.follow_up(linked, source, request, mode=mode)
                else:
                    self.secretary.relay(self.issue_url(target_project, iid), source, request, mode=mode)
            else:
                # request 是给模型看的拼接稿；正文只放用户原话
                self.create(mid, chat, text, user, msg.get("sender_name") or user, source, mode=mode,
                            title=intent["reply"], earlier=tuple((previous or {}).get("messages", [])))
        finally:
            # 写入成功即收束追问；回执失败不能让下一句重复建单或转交。
            delivered = self.state["threads"].get(mid) or self.delivered
            if delivered:
                pending.pop(key, None)
                d_proj = str(delivered.get("project") or self.project)
                issue = next(
                    (item for item in candidates if item["iid"] == delivered["iid"] and str(item.get("project") or self.project) == d_proj),
                    {"iid": delivered["iid"], "project": d_proj, "title": self.strip_images(text)[:60]},
                )
                self.remember_target(msg, issue)

    def deliver_relay_plan(self, key: str, source: dict, actions: list[dict], pending: dict) -> None:
        plan = pending.setdefault("relay_plan", [])
        by_issue = {(item.get("project") or self.project, item["issue"]): item for item in plan}
        for action in actions:
            proj = str(action.get("project") or self.project)
            iid = action["issue"]
            old = by_issue.get((proj, iid))
            if old and old["status"] in ("sent", "sending", "unknown"):
                continue
            if old:
                old.update(text=action["text"], mode=action["mode"])
            else:
                item = {"project": proj, "issue": iid, "text": action["text"], "mode": action["mode"], "status": "pending"}
                plan.append(item)
                by_issue[(proj, iid)] = item
        pending.setdefault("origin", source["message_id"])
        self.persist()
        # 附件只上传一次；续接使用已保存背景，不重复搬运。
        if "context" not in pending:
            first_proj = str(plan[0].get("project") or self.project) if plan else self.project
            pending["context"] = self.message_context(source, first_proj,
                                                       self.take_pending_images(source.get("chat_id", "")))
            self.persist()
        selected = {(str(item.get("project") or self.project), item["issue"]) for item in actions}
        for item in plan:
            proj = str(item.get("project") or self.project)
            iid = item["issue"]
            marker = f"<!-- hwf-relay:{pending['origin']}:{iid} -->"
            if item["status"] in ("sending", "unknown"):
                # 写入超时可能已成功；只核对，不盲目重发。
                item["status"] = "unknown"
                try:
                    for page in range(1, 6):
                        notes = self.api(f"projects/{quote(proj, safe='')}/issues/{iid}/notes"
                                         f"?sort=desc&per_page=100&page={page}")
                        found = next((n for n in notes if marker in (n.get("body") or "")), None)
                        if found:
                            item.update(status="sent", note_id=found["id"])
                            self.state["posted_notes"] = cap_list(self.state["posted_notes"] + [found["id"]])
                            break
                        if len(notes) < 100:
                            break
                except (BridgeError, ValueError):
                    log(f"relay reconciliation failed mid={pending['origin']} issue={proj}#{iid}")
                self.persist()
            if item["status"] != "pending" or (proj, iid) not in selected:
                continue
            try:
                issue = self.api(f"projects/{quote(proj, safe='')}/issues/{iid}")
                if issue.get("state") == "closed":
                    item["error"] = "closed"
                    continue
                body = self.secretary.relay_body(source, item["text"], mode=item["mode"],
                                                  project=proj, context=pending["context"])
                if not body:
                    item["error"] = "empty"
                    continue
                item["status"] = "sending"
                self.persist()
                note_id = self.note_as_user(proj, iid, f"@{self.agent} {body}\n\n{marker}")
                item.update(status="sent", note_id=note_id)
                self.remember_target(source, {**issue, "project": proj})
                self.secretary.track(self.issue_url(proj, iid), {**issue, "project": proj},
                                     last_note=note_id)
            except (BridgeError, ValueError):
                if item["status"] == "sending":
                    item["status"] = "unknown"
                item["error"] = "delivery_failed"
                log(f"relay failed mid={pending['origin']} issue={proj}#{iid} status={item['status']}")
            finally:
                self.persist()
        lines = []
        for item in plan:
            proj = str(item.get("project") or self.project)
            iid = item["issue"]
            label = f"[{self.issue_ref(proj, iid)}]({self.issue_url(proj, iid)})"
            result = ("已转交" if item["status"] == "sent" else
                      "发送结果待核实，请先查看任务评论，不要重复发送" if item["status"] == "unknown" else
                      "任务已关闭，未转交" if item.get("error") == "closed" else "未转交")
            lines.append(f"{label}：{result}。")
        if any(item["status"] == "pending" for item in plan):
            lines.append("未完成项已保留，处理后可回复“重试未完成项”；已成功的不会重复发送。")
        if all(item["status"] == "sent" for item in plan):
            self.state["pending_intents"].pop(key, None)
        self.persist()
        self.relay_summary = "\n".join(lines)
        reply_id = self.reply(source["message_id"], self.relay_summary,
                              f"relay-plan-{source['message_id']}", in_thread=bool(source.get("thread_id")))
        if reply_id and key in self.state["pending_intents"]:
            pending["reply_ids"].append(reply_id)

    def clean_text(self, msg: dict[str, Any]) -> str:
        # 事件里的 content 已渲染成「@名字」，原始接口里是「@_user_1」占位符，两种都要处理；
        # 机器人在 mentions 里可能是 open_id，也可能是应用 ID（cli_ 开头）
        text = msg.get("content") or ""
        for m in msg.get("mentions") or []:
            key, name = m.get("key") or "", m.get("name") or ""
            if self.is_bot_mention(m):
                for token in (key, f"@{name}" if name else ""):
                    if token:
                        text = text.replace(token, "")
            elif key:
                text = text.replace(key, f"@{name}")
        return text.strip()

    @staticmethod
    def strip_images(text: str) -> str:
        return strip_attachments(text)

    def is_bot_mention(self, m: dict[str, Any]) -> bool:
        mid = m.get("id") or ""
        return bool(mid) and (mid == self.cfg.get("bot_open_id") or mid.startswith("cli_"))

    def note_as_user(self, project: str, iid: int, body: str) -> int:
        note = self.api(f"projects/{quote(project, safe='')}/issues/{iid}/notes", "POST",
                        {"body": body}, as_user=True)
        self.state["posted_notes"] = cap_list(self.state["posted_notes"] + [note["id"]])
        self.delivered = {"project": project, "iid": iid}
        return note["id"]

    def issue_url(self, project: str, iid: int) -> str:
        return f"https://{self.host}/{project}/-/issues/{iid}"

    def link_thread(self, mid: str, key: str) -> None:
        """回帖后这条消息就在一个话题里了，记下 thread_id，之后话题里的消息都按它找到 issue。"""
        try:
            items = (self.lark(["im", "+messages-mget", "--message-ids", mid]).get("data") or {}).get("messages")
        except BridgeError as e:
            log(f"read thread of {mid} failed: {e}")
            return
        tid = (items or [{}])[0].get("thread_id")
        if tid:
            self.state["thread_ids"][tid] = key

    def thread_context(self, msg: dict[str, Any], *, full: bool = False
                       ) -> tuple[str, list[tuple[str, list[dict[str, str]]]], set[str]]:
        """收集当前指令之前的话题前文，返回已收集的消息 ID，避免与直接引用重复。"""
        tid = msg.get("thread_id") or ""
        if not tid:
            return "", [], set()
        try:
            data = self.lark(["im", "+threads-messages-list", "--thread", tid, "--order", "asc",
                              "--no-reactions", "--page-all", "--page-limit", "20" if full else "4"])
        except BridgeError as e:
            log(f"read thread context {tid} failed: {e}")
            return "", [], set()
        lines: list[tuple[str, str]] = []
        attachments: list[tuple[str, list[dict[str, str]]]] = []
        seen: set[str] = set()
        for item in (data.get("data") or {}).get("messages", []):
            m = normalize_history(item)
            if m["message_id"] == msg["message_id"]:
                break
            if m["sender_type"] != "user":
                continue
            raw = self.clean_text(m)
            found = find_attachments(raw)
            if found:
                attachments.append((m["message_id"], found))
            marks = ["[图片]" if a["kind"] == "image" else f"[文件 {a['name'] or '未命名'}]" for a in found]
            body = " ".join([strip_attachments(raw), *marks]).strip()
            if not body:
                continue
            when = (item.get("create_time") or "").strip()
            head = f"**{m['sender_name'] or m['sender_id'] or '?'}**" + (f"（{when}）" if when else "")
            lines.append((m["message_id"], "\n".join(f"> {l}" if l else ">" for l in f"{head}：{body}".splitlines())))
        if not lines:
            return "", attachments, seen
        kept: list[str] = []
        total = 0
        for mid, line in reversed(lines):
            total += len(line)
            if kept and not full and total > CONTEXT_TEXT_CAP:
                kept.append("> …更早的消息省略")
                break
            kept.append(line)
            # 只有实际保留的前文才去重；被截掉的旧消息仍可能是用户本次明确引用的对象。
            seen.add(mid)
        return "**话题里的前文**（从飞书原样搬来）：\n\n" + "\n>\n".join(reversed(kept)), attachments, seen

    def create(self, mid: str, chat: str, text: str, user: str, name: str,
               msg: Optional[dict[str, Any]] = None, *, mode: str = "analyze", title: str = "",
               earlier: tuple[dict[str, Any], ...] = ()) -> None:
        # issue 正文要带上触发句之前的全部对话，不能沿用分类时的字数截断
        covered: set[str] = set()
        context = self.message_context(msg, self.project, self.take_pending_images(chat), include_thread=True,
                                       full=True, covered=covered, history_text=False) if msg else ""
        said = [self.clean_text(m) for m in earlier if m["message_id"] not in covered]
        text = "\n\n".join(x for x in (*map(self.strip_images, said), self.strip_images(text)) if x.strip()) \
            or "（见附件）"
        title = (title.strip() or text.splitlines()[0]).strip()
        title = title[:60] + ("…" if len(title) > 60 else "")
        agent_id = self.api(f"users?username={quote(self.agent)}")[0]["id"]
        desc = (f"{text}\n\n{context + chr(10) + chr(10) if context else ''}"
                f"---\n来自飞书（{name}，{datetime.fromtimestamp(self.now()):%Y-%m-%d %H:%M}）。"
                "在飞书话题里回复会同步成这里的评论，这里的评论也会同步回飞书。")
        issue = self.api(f"projects/{quote(self.project, safe='')}/issues", "POST", {
            "title": title, "description": desc, "labels": self.label, "assignee_id": str(agent_id)},
            as_user=True)
        iid = issue["iid"]
        self.state["threads"][mid] = {"project": self.project, "iid": iid, "chat_id": chat,
                                      "title": title, "last_note": 0, "closed": False,
                                      "last_active_at": int(self.now())}
        if msg:
            self.remember_target(msg, {"iid": iid, "title": title})
        if mode in ("deliver", "debug"):
            # 先记录建单，再发已有开工协议；开工失败与回执失败必须分开，避免重复建单。
            self.state["threads"][mid]["execution_pending"] = mode
            self.note_as_user(self.project, iid, self.execution_body(text, mode))
            self.state["threads"][mid].pop("execution_pending")
        status = ("已发送修复开工指令（debug），后续进展会回到这里。" if mode == "debug" else
                  "已发送交付开工指令，后续进展会回到这里。" if mode == "deliver" else
                  "Agent 会先只读分析；在这里说「开始修复」或「开工」即可正式交付。")
        self.reply(mid, f"已建 [#{iid}]({self.issue_url(self.project, iid)})：{title}\n"
                        f"{status}",
                   f"created-{mid}")
        self.link_thread(mid, mid)
        log(f"created {self.project}#{iid} from {mid} by {user}")

    def bind(self, mid: str, chat: str, iid: int, rest: str, user: str,
             msg: Optional[dict[str, Any]] = None, *, mode: str = "analyze",
             project: Optional[str] = None) -> None:
        target_project = project or self.project
        issue: Optional[dict[str, Any]] = None
        if project is not None:
            if not self.is_project_allowed(target_project):
                self.reply(mid, f"项目 {target_project} 已归档或机器人没有权限，无法关联。", f"bind-miss-{mid}")
                return
            try:
                issue = self.api(f"projects/{quote(target_project, safe='')}/issues/{iid}")
            except BridgeError:
                issue = None
        else:
            # 未指定项目名时：先查默认项目；查不到再看秘书已登记的跨项目任务是否唯一命中该编号。
            try:
                issue = self.api(f"projects/{quote(target_project, safe='')}/issues/{iid}")
            except BridgeError:
                matches = [
                    str(item["project"])
                    for item in self.secretary.known_tasks().values()
                    if item.get("iid") == iid and item.get("project") and item["project"] != self.project
                ]
                unique_projects = sorted(set(matches))
                if len(unique_projects) == 1:
                    target_project = unique_projects[0]
                    try:
                        issue = self.api(f"projects/{quote(target_project, safe='')}/issues/{iid}")
                    except BridgeError:
                        issue = None
                elif len(unique_projects) > 1:
                    projs = "、".join(unique_projects)
                    self.reply(mid, f"多个项目都有 #{iid}（{projs}），请用「项目路径#{iid}」或贴完整链接关联。", f"bind-ambig-{mid}")
                    return
        if not issue:
            ref = self.issue_ref(target_project, iid)
            self.reply(mid, f"没找到 {ref}（如需关联其他项目，请发「项目路径#{iid}」或贴完整 issue 链接）。", f"bind-miss-{mid}")
            return
        pid = quote(target_project, safe="")
        notes = self.api(f"projects/{pid}/issues/{iid}/notes?sort=desc&per_page=1")
        context = self.message_context(msg, target_project, self.take_pending_images(chat), include_thread=True) if msg else ""
        rest = self.strip_images(rest)
        if rest or context:
            # 已有 issue 未必在收件箱跟踪范围内，@Agent 保证转交。
            body = "\n\n".join(x for x in (rest, context) if x)
            body = self.execution_body(body, mode)
            self.note_as_user(target_project, iid, f"@{self.agent} {body}\n\n_（来自飞书）_")
        # 评论发出去才算关联成功：失败时要用户重发，不能留一个半截的话题
        self.state["threads"][mid] = {"project": target_project, "iid": iid, "chat_id": chat,
                                      "last_note": notes[0]["id"] if notes else 0,
                                      "title": issue["title"], "closed": issue["state"] == "closed", "bound": True,
                                      "last_active_at": int(self.now())}
        if msg:
            self.remember_target(msg, {**issue, "project": target_project})
        ref = self.issue_ref(target_project, iid)
        self.reply(mid, f"已关联 [{ref}]({self.issue_url(target_project, iid)})：{issue['title']}\n"
                        "之后在这个话题里回复会同步成评论，issue 里的新评论也会发到这里。", f"bound-{mid}")
        self.link_thread(mid, mid)
        log(f"bound {mid} → {target_project}#{iid} by {user}")

    @staticmethod
    def execution_body(text: str, mode: str) -> str:
        """沿用收件箱 /go 协议；原文保留，分类摘要绝不能替代授权。"""
        if mode not in ("deliver", "debug"):
            return text
        command = "/go debug" if mode == "debug" else "/go"
        return f"{command}\n\n用户原话：{text}"

    def follow_up(self, root: str, msg: dict[str, Any], text: str, *, mode: str = "analyze") -> None:
        mid = msg["message_id"]
        t = self.state["threads"][root]
        if t.get("closed"):
            if t.get("archived_stale"):
                issue = self.api(f"projects/{quote(t['project'], safe='')}/issues/{t['iid']}")
                if issue.get("state") != "closed":
                    # 仅因久未活跃停止后台轮询的话题，用户重新发言且工单仍开着时自动恢复同步。
                    t["closed"] = False
                    t.pop("archived_stale", None)
            if t.get("closed"):
                self.reply(mid, "这张 issue 已关闭，这里只保留归档。要继续请回主聊天说新的需求，或发「#编号」进入别的独立话题。", f"closed-{mid}")
                return
        # /go、/status、/new 这类指令要整条只有指令，收件箱才认得，所以不加尾注。
        if text.startswith("/"):
            body = text
        else:
            # 根消息已在 issue 中；仅补充本条明确回复的其他消息，不重复搬根附件。
            context = self.message_context(msg, t["project"], exclude_related=(root, msg.get("root_id") or ""))
            text = self.strip_images(text)
            body = "\n\n".join(x for x in (text, context) if x) + "\n\n_（来自飞书）_"
            body = self.execution_body(body, mode)
        if t.get("bound"):
            body = f"@{self.agent} {body}"
        self.note_as_user(t["project"], t["iid"], body)
        t["last_active_at"] = int(self.now())
        if mode in ("deliver", "debug"):
            t.pop("execution_pending", None)

    @staticmethod
    def _parse_gitlab_iso(raw: Any) -> Optional[float]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    # ---------- GitLab → 飞书 ----------
    def poll_gitlab(self) -> None:
        now = self.now()
        for root, t in list(self.state["threads"].items()):
            if t.get("closed") or t.get("topic_pending"):
                continue
            pid = quote(t["project"], safe="")
            # 请求登记后可能停机很久，逐页追到水位，不能只保留最新 50 条。
            notes, page = [], 1
            while True:
                batch = self.api(f"projects/{pid}/issues/{t['iid']}/notes?sort=desc&order_by=created_at&per_page=50&page={page}")
                notes.extend(n for n in batch if n["id"] > t.get("last_note", 0))
                if len(batch) < 50 or any(n["id"] <= t.get("last_note", 0) for n in batch):
                    break
                page += 1
            private = t["chat_id"] == self.secretary.chat and t["chat_id"] not in self.state["group_chats"]
            if private:
                self.secretary.forward_private_notes(t, notes)
            if notes:
                t["last_active_at"] = int(now)
            for n in sorted(notes, key=lambda x: x["id"]):
                if n["id"] <= t.get("last_note", 0):
                    continue
                if not n.get("system") and n["id"] not in self.state["posted_notes"]:
                    if not self.forward_note(root, t, n):
                        break  # 发送失败：水位停在这条之前，下一轮按原顺序重试
                t["last_note"] = n["id"]
                t.pop("fails", None)
            issue = self.api(f"projects/{pid}/issues/{t['iid']}")
            if issue["state"] == "closed":
                t["closed"] = True
                self.state["history_cursors"].pop(f"thread:{root}", None)
                if not private:
                    self.reply(root, f"#{t['iid']} 已关闭，这个话题不再同步。", f"gl-closed-{t['iid']}-{root[-12:]}")
                continue
            updated_ts = self._parse_gitlab_iso(issue.get("updated_at"))
            last_active = float(t.get("last_active_at") or updated_ts or 0)
            if last_active <= 0:
                t["last_active_at"] = int(now)
                continue
            if updated_ts is not None and updated_ts > last_active:
                last_active = updated_ts
                t["last_active_at"] = int(updated_ts)
            if not notes and (now - last_active) >= STALE_THREAD_S:
                t["closed"] = True
                t["archived_stale"] = True
                self.state["history_cursors"].pop(f"thread:{root}", None)
                log(f"archived stale thread {root} ({t['project']}#{t['iid']}) after {STALE_THREAD_DAYS}d inactivity")

    def retire_chat(self, chat: str, reason: str) -> None:
        """机器人已被移出群聊或群已解散时，摘除补读记录并关闭关联话题，避免每分钟重复刷 230002。"""
        cursors = self.state["history_cursors"]
        self.state["chats"].pop(chat, None)
        if chat in self.state["group_chats"]:
            self.state["group_chats"] = [c for c in self.state["group_chats"] if c != chat]
        cursors.pop(f"chat:{chat}", None)
        self.state["history_seen"].pop(chat, None)
        self.state["help_sent"].pop(chat, None)
        self.state["pending_images"].pop(chat, None)
        for root, thread in self.state["threads"].items():
            if thread.get("chat_id") == chat:
                thread["closed"] = True
                cursors.pop(f"thread:{root}", None)
        flat_reason = " ".join(reason.split())
        log(f"retired unreachable chat {chat}: {flat_reason[:240]}")
        self.persist()

    def forward_note(self, root: str, t: dict[str, Any], n: dict[str, Any]) -> bool:
        """把一条 GitLab 评论发到飞书话题；成功（或已放弃）返回 True。幂等键保证重试不会重复发。"""
        key = f"gl-{t['iid']}-{n['id']}"
        try:
            self.reply(root, self.render_note(t, n), key)
            return True
        except BridgeError as e:
            if is_retired_chat_error(e):
                t["closed"] = True
                if t.get("chat_id"):
                    self.retire_chat(str(t["chat_id"]), str(e))
                else:
                    log(f"closed unreachable thread {root}: {' '.join(str(e).split())[:240]}")
                    self.persist()
                return True
            t["fails"] = t.get("fails", 0) + 1
            log(f"forward #{t['iid']} note {n['id']} failed ({t['fails']}/{NOTE_RETRIES}): {e}")
            if t["fails"] < NOTE_RETRIES:
                return False
        # 连续失败多次（多半是内容飞书不收），改发一句带链接的短消息，不卡住后面的评论
        url = f"{self.issue_url(t['project'], t['iid'])}#note_{n['id']}"
        try:
            self.reply(root, f"#{t['iid']} 有一条新评论没能搬过来，请到 [GitLab]({url}) 查看。", f"{key}-link")
        except BridgeError as e:
            log(f"forward #{t['iid']} note {n['id']} link failed: {e}")
            return False
        return True

    def render_note(self, t: dict[str, Any], n: dict[str, Any]) -> str:
        body = KIND_TAG.sub("", (n.get("body") or "")).strip()
        url = f"{self.issue_url(t['project'], t['iid'])}#note_{n['id']}"
        # 飞书不认折叠标签，会把全文连同标签一起显示；这里只留摘要，全文给链接
        body = FOLDED.sub(f"[查看完整内容]({url})", body)
        if len(body) > FEISHU_TEXT_CAP:
            body = body[:FEISHU_TEXT_CAP].rstrip() + f"\n\n…内容较长，全文见 [GitLab]({url})"
        who = n.get("author", {}).get("name") or n.get("author", {}).get("username", "?")
        ref = self.issue_ref(t["project"], t["iid"])
        return f"**{who}**（[{ref}]({url})）：\n{body}"

    # ---------- 补漏 ----------
    def poll_history(self) -> None:
        """只由主循环调用；连接线程只发通知，不与实时消息并发写状态或发评论。"""
        if not self.history_requested.is_set() and self.now() < self.next_history_poll:
            return
        self.history_requested.clear()  # 补读期间再重连，通知留给下一轮，不能在结束时清掉
        try:
            self.catch_up()
        except BridgeError as error:
            if PB.is_transient_network_error(error):
                notice = self._history_tracker.record_failure(error)
                if notice:
                    log(notice)
                wait_s = PB.compute_step_backoff(
                    HISTORY_POLL_S, self._history_tracker.consecutive_failures, HISTORY_BACKOFF_STEPS
                )
                self.next_history_poll = self.now() + wait_s
                return
            self.next_history_poll = self.now() + HISTORY_POLL_S
            raise
        recovered = self._history_tracker.record_success()
        if recovered:
            log(recovered)
        self.next_history_poll = self.now() + HISTORY_POLL_S

    def catch_up(self) -> None:
        """增量补读已知聊天和未关闭话题；单聊/单话题权限失败跳过，瞬时断网立即中止本轮避免连刷几十条。"""
        until_ms = int(self.now()) * 1000
        cursors = self.state["history_cursors"]
        for chat, last_ms in list(self.state["chats"].items()):
            key = f"chat:{chat}"
            since_ms = max(cursors.setdefault(key, last_ms) - HISTORY_OVERLAP_MS, 0) // 60_000 * 60_000
            start = datetime.fromtimestamp(since_ms / 1000).astimezone().isoformat(timespec="seconds")
            end = datetime.fromtimestamp(until_ms / 1000).astimezone().isoformat(timespec="seconds")
            try:
                self.read_history(["im", "+chat-messages-list", "--chat-id", chat, "--order", "asc",
                                   "--start", start, "--end", end], chat)
            except (BridgeError, KeyError, ValueError) as e:
                if is_retired_chat_error(e):
                    self.retire_chat(chat, str(e))
                elif isinstance(e, BridgeError) and PB.is_transient_network_error(e):
                    raise
                else:
                    log(f"catch-up skip chat {chat}: {e}")
                continue
            cursors[key] = until_ms

        # 话题接口不支持时间过滤：从最新一页向前读，遇到已覆盖的时间边界就停。
        roots = [(root, t["chat_id"]) for root, t in self.state["threads"].items() if not t.get("closed")]
        for root, chat in roots:
            key = f"thread:{root}"
            since_ms = max(cursors.get(key, 0) - HISTORY_OVERLAP_MS, 0) // 60_000 * 60_000
            try:
                self.read_history(["im", "+threads-messages-list", "--thread", root, "--order", "desc"],
                                  chat, root, since_ms)
            except (BridgeError, KeyError, ValueError) as e:
                if is_retired_chat_error(e):
                    if chat:
                        self.retire_chat(chat, str(e))
                    else:
                        self.state["threads"][root]["closed"] = True
                        cursors.pop(key, None)
                        log(f"closed unreachable thread {root}: {' '.join(str(e).split())[:240]}")
                        self.persist()
                elif isinstance(e, BridgeError) and PB.is_transient_network_error(e):
                    raise
                else:
                    log(f"catch-up skip thread {root}: {e}")
                continue
            cursors[key] = until_ms

        for chat, seen in self.state["history_seen"].items():
            covered = [cursors.get(f"chat:{chat}", 0)]
            covered.extend(cursors.get(f"thread:{root}", 0) for root, cid in roots if cid == chat)
            cutoff = (min(covered) - HISTORY_OVERLAP_MS) // 60_000 * 60_000
            self.state["history_seen"][chat] = {mid: ms for mid, ms in seen.items() if ms >= cutoff}

    def read_history(self, args: list[str], chat: str, root: str = "", since_ms: int = 0) -> None:
        """逐页读取，不把分页上限当作读完；话题倒序拉取、正序处理，保持指令先后顺序。"""
        token = ""
        used_tokens: set[str] = set()
        replies: list[dict[str, Any]] = []
        while True:
            page_args = args + ["--no-reactions", "--page-size", "50"]
            if token:
                page_args += ["--page-token", token]
            data = self.lark(page_args).get("data") or {}
            reached_boundary = False
            for item in data.get("messages", []):
                msg = normalize_history(item, default_root=root)
                msg["chat_id"] = chat
                if root and 0 < msg["create_ms"] < since_ms:
                    reached_boundary = True
                    continue
                if chat in self.state["group_chats"]:
                    msg["chat_type"] = "group"
                if root:
                    replies.append(msg)
                else:
                    self.handle(msg)
            if reached_boundary or not data.get("has_more"):
                break
            token = data.get("page_token") or ""
            if not token or token in used_tokens:
                raise BridgeError("history pagination did not advance")
            used_tokens.add(token)
        for msg in reversed(replies):
            self.handle(msg)


_SECRETARY: Any = None


def secretary_module() -> Any:
    global _SECRETARY
    if _SECRETARY is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("secretary", Path(__file__).resolve().parent / "secretary.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("secretary", module)
        spec.loader.exec_module(module)
        _SECRETARY = module
    return _SECRETARY


_TOPICS: Any = None


def topics_module() -> Any:
    """测试用 importlib 按路径加载桥时，也从脚本同目录加载话题队列。"""
    global _TOPICS
    if _TOPICS is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("feishu_topics", Path(__file__).resolve().parent / "feishu_topics.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TOPICS = module
    return _TOPICS


def normalize_event(e: dict[str, Any]) -> dict[str, Any]:
    return {"message_id": e.get("message_id") or e.get("id"), "chat_id": e.get("chat_id"),
            "chat_type": e.get("chat_type"), "sender_id": e.get("sender_id"),
            "sender_name": e.get("sender_name") or "",
            "sender_type": e.get("sender_type"), "content": e.get("content") or "",
            "mentions": e.get("mentions") or [], "root_id": e.get("root_id") or "",
            "reply_to": e.get("reply_to") or e.get("parent_id") or "", "message_type": e.get("message_type") or "",
            "thread_id": e.get("thread_id") or "",
            "create_ms": int(e.get("create_time") or 0)}


def normalize_history(m: dict[str, Any], default_root: str = "") -> dict[str, Any]:
    sender = m.get("sender") or {}
    ct = m.get("create_time") or ""
    try:
        ms = int(datetime.strptime(ct, "%Y-%m-%d %H:%M").timestamp() * 1000)
    except ValueError:
        ms = int(ct) if str(ct).isdigit() else 0
    root = default_root if default_root and m.get("message_id") != default_root else (m.get("root_id") or "")
    return {"message_id": m.get("message_id"), "chat_id": m.get("chat_id"),
            "sender_id": sender.get("id"), "sender_name": sender.get("name"),
            "sender_type": "user" if sender.get("sender_type") == "user" else "bot",
            "content": m.get("content") or "", "mentions": m.get("mentions") or [],
            "root_id": root, "reply_to": m.get("parent_id") or m.get("reply_to") or "",
            "message_type": m.get("msg_type") or "", "thread_id": m.get("thread_id") or "", "create_ms": ms}


# ---------- 运行 ----------
def keychain_token(spec: Optional[dict[str, str]]) -> str:
    if os.environ.get("HWF_GITLAB_USER_TOKEN"):
        return os.environ["HWF_GITLAB_USER_TOKEN"]
    if not spec:
        return ""
    r = subprocess.run(["security", "find-generic-password", "-s", spec["service"], "-a", spec["account"], "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def load_config() -> dict[str, Any]:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        raise BridgeError(f"缺少配置 {CONFIG_PATH}")
    cfg.setdefault("gitlab_host", (load_json(INBOX_CONFIG_PATH, {}) or {}).get("host"))
    for key in ("gitlab_host", "default_project", "users"):
        if not cfg.get(key):
            raise BridgeError(f"配置缺少 {key}")
    return cfg


CHILDREN: list[subprocess.Popen] = []


def terminate_children() -> None:
    """温和关掉 lark-cli（SIGTERM，让它清理服务端订阅），供退出信号和热重载复用。"""
    for p in CHILDREN:
        if p.poll() is None:
            p.terminate()
    for p in CHILDREN:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def stop_children(*_: Any) -> None:
    terminate_children()
    sys.exit(0)


def watched_scripts(base_dir: Optional[Path] = None) -> tuple[Path, ...]:
    root = base_dir or Path(__file__).resolve().parent
    return tuple(root / name for name in (
        "feishu-bridge.py", "gitlab_projects.py", "secretary.py", "task_intent.py", "feishu_topics.py",
        "jev_client.py", "jev_gates.py", "poll_backoff.py", "role_config.py",
    ))


def snapshot_mtimes(paths: tuple[Path, ...]) -> dict[Path, int]:
    return {p: p.stat().st_mtime_ns for p in paths if p.exists()}


def reloadable_changes(baseline: dict[Path, int], paths: tuple[Path, ...]) -> list[Path]:
    """检测桥及其依赖模块是否有落盘更新；遇到语法错误暂缓重载，避免重启即崩。"""
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


def read_event_status(lines: Any, history_requested: threading.Event) -> None:
    """连接恢复只安排补读；实际补读仍在主线程执行，与消息处理共用去重状态。"""
    for line in lines:
        status = line.strip()
        log(f"lark: {status}")
        if (status == "[source] feishu-websocket: connected"
                or status == "[event] ready event_key=im.message.receive_v1"):
            history_requested.set()


def consume_events(out: "queue.Queue[dict]", history_requested: threading.Event) -> None:
    """常驻 lark-cli event consume；stdin 保持不关（关了它会退出），挂了就退避重启。"""
    backoff = 5
    while True:
        CHILDREN[:] = [c for c in CHILDREN if c.poll() is None]
        p = subprocess.Popen(["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        CHILDREN.append(p)
        threading.Thread(target=read_event_status, args=(p.stderr, history_requested), daemon=True).start()
        for line in p.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line:
                try:
                    out.put(json.loads(line))
                    backoff = 5
                except ValueError:
                    log(f"bad event line: {line[:200]}")
        log(f"lark-cli event consume exited rc={p.wait()}, restart in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


def main(argv: Optional[list[str]] = None) -> int:
    global _EVENT_LOG_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="检查配置、令牌与飞书机器人后退出")
    mode.add_argument("--split-topic", nargs=2, metavar=("SOURCE_ISSUE_URL", "TARGET_ISSUE_URL"), help="登记独立话题请求，不直接发送")
    mode.add_argument("--topic-status", metavar="TARGET_ISSUE_URL", help="只读查询话题请求与结果")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
        topics = topics_module()
        directory = STATE_PATH.parent / "feishu-topics"
        if args.topic_status or args.split_topic:
            try:
                if args.topic_status:
                    result = topics.status(cfg, directory, args.topic_status)
                else:
                    # 只读 GitLab 获取登记水位，不读写运行中的 feishu-bridge.json。
                    client = Bridge(cfg, {})
                    result = topics.register(cfg, directory, *args.split_topic, client.api)
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["status"] in ("queued", "sending", "ready", "archived") else 1
            except (topics.TopicError, BridgeError, OSError, ValueError) as error:
                print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
                return 1
        # --check 不消费队列；常驻桥必须先拿锁，再读取运行状态。
        consumer_lock = topics.lock_bridge(STATE_PATH) if not args.check else None
        token = keychain_token(cfg.get("gitlab_token_keychain"))
        state = load_json(STATE_PATH, {})
        bridge = Bridge(cfg, state, user_token=token, persist=lambda: save_json(STATE_PATH, state))
        bot = bridge.lark(["api", "GET", "/open-apis/bot/v3/info"])
        cfg.setdefault("bot_open_id", (bot.get("data") or {}).get("open_id"))
        who = bridge.api("user", as_user=True)["username"] if token else None
    except (BridgeError, topics_module().TopicError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    print(f"GitLab {cfg['gitlab_host']}，新 issue 建在 {cfg['default_project']}，指派给 {bridge.agent}；"
          f"代你发言的账号：{who or '（未配置令牌）'}；机器人：{(bot.get('data') or {}).get('app_name')}")
    if who and who not in bridge.users.values():
        print(f"注意：令牌账号 {who} 不在 users 白名单的 GitLab 账号里", file=sys.stderr)
    if args.check:
        return 0 if who else 1
    if not who:
        print("没有用户 GitLab 令牌，见脚本开头说明", file=sys.stderr)
        return 1
    _EVENT_LOG_PATH = STATE_PATH.parent / "feishu-bridge-events.log"
    signal.signal(signal.SIGTERM, stop_children)
    topic_queue = topics.Topics(bridge, directory, lambda: save_json(STATE_PATH, state))
    topic_queue.consume()
    signal.signal(signal.SIGINT, stop_children)
    events: "queue.Queue[dict]" = queue.Queue()
    threading.Thread(target=consume_events, args=(events, bridge.history_requested), daemon=True).start()
    # 搬附件用的中转目录正常情况下用完即删；上次被强杀可能留下半截文件，启动时清空
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    try:
        bridge.poll_history()
    except BridgeError as e:
        log(f"catch-up failed: {e}")
    save_json(STATE_PATH, state)
    watched = watched_scripts()
    baseline = snapshot_mtimes(watched)
    poll = int(cfg.get("poll_seconds", 15))
    next_poll = 0.0
    poll_tracker = PB.PollFailureTracker(heartbeat_every=0, label="poll")
    while True:
        try:
            e = events.get(timeout=1)
            bridge.handle(normalize_event(e))
            save_json(STATE_PATH, state)
        except queue.Empty:
            pass
        except (BridgeError, KeyError, ValueError) as err:
            log(f"handle failed: {err}")
            save_json(STATE_PATH, state)
        try:
            bridge.poll_history()
        except (BridgeError, KeyError, ValueError) as err:
            log(f"catch-up failed: {err}")
        if time.time() >= next_poll:
            poll_err: Optional[BaseException] = None
            try:
                topic_queue.consume()
                bridge.poll_gitlab()
            except (BridgeError, KeyError, ValueError) as err:
                poll_err = err
            # GitLab 已经瞬时断网时不再紧接着跑秘书轮询，避免同一轮打出双份网络报错。
            if poll_err is None or not PB.is_transient_network_error(poll_err):
                try:
                    bridge.secretary.poll()
                    bridge.secretary.patrol()
                except Exception as err:  # 秘书出错不能拖垮桥本身
                    if poll_err is None and PB.is_transient_network_error(err):
                        poll_err = err
                    else:
                        log(f"secretary failed: {err!r}")
            if poll_err is not None:
                notice = poll_tracker.record_failure(poll_err)
                if notice:
                    log(notice)
                wait_s = (
                    PB.compute_step_backoff(poll, poll_tracker.consecutive_failures, POLL_BACKOFF_STEPS)
                    if PB.is_transient_network_error(poll_err)
                    else poll
                )
                next_poll = time.time() + wait_s
            else:
                recovered = poll_tracker.record_success()
                if recovered:
                    log(recovered)
                next_poll = time.time() + poll
            save_json(STATE_PATH, state)
            if events.empty():
                changed = reloadable_changes(baseline, watched)
                if changed:
                    names = ", ".join(p.name for p in changed)
                    log(f"reloading feishu-bridge after code update: {names}")
                    terminate_children()
                    if consumer_lock is not None:
                        consumer_lock.close()
                    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
