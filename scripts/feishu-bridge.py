#!/usr/bin/env python3
"""飞书桥：把飞书对话和 GitLab issue 双向同步。Agent 团队仍只认 GitLab，本脚本不碰 Herdr。

    python3 ~/.hwf/scripts/feishu-bridge.py            # 常驻（建议放在一个 Herdr 窗格里）
    python3 ~/.hwf/scripts/feishu-bridge.py --check    # 只检查配置、令牌与飞书连接后退出

分工（2026-11-02 用户指示，方案 A：每人一个飞书应用，桥跑在自己电脑上）：
  飞书 → GitLab：本脚本（收消息、建 issue、把话题回复写成 issue 评论）
  GitLab → Agent：原有的 gitlab-inbox.py（不改）
  GitLab → 飞书：轮询新评论；私聊统一由秘书通知，群聊保留原话题回复
桥只做搬运，以后推广到公司时可整体挪到一台常开的机器上。

飞书里怎么用（只认 users 白名单里的人）：
  「需求：<内容>」或「/需求 <内容>」  → 在 default_project 建 issue（打开关标签、指派给 Agent 账号），回帖 issue 链接
  「#123 <可选内容>」或贴 issue 链接   → 把这条消息的话题关联到已有 issue，内容写成评论（带 @Agent 以便转交）
  在上面两种消息的话题里回复         → 写成该 issue 的评论；/go、/status、/new、/免打扰、/详尽 等指令原样生效
  私聊里对秘书转达的消息点「回复」   → 写成那个需求 issue 的评论（私聊窗口就是秘书，见 secretary.py）
  私聊里的其他话                     → 交给秘书：「进度」回总览，「帮助」回用法，其余当提问回答
  群里的其他顶层消息                 → 回一段用法说明（同一会话 10 分钟内只回一次）
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
HELP_COOLDOWN_S = 600
NOTE_RETRIES = 3  # 一条评论连续发送失败几次后改发 GitLab 链接
FEISHU_TEXT_CAP = 2500  # 超过就截断并附 GitLab 原文链接
CONTEXT_TEXT_CAP = 6000  # 建单时带上的话题前文总字数上限，超出只留最近的
FOLDED = re.compile(r"<details>.*?</details>", re.S)  # 评论里折叠的全文
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
IMAGE_HINT = "收到附件。10 分钟内接着发「需求：<要做的事>」或「#编号」，我会把它一起带上。"
# 链接尾巴只吃 URL 里的 ASCII 字符，免得把紧跟的「，处理这个…」吞掉
BIND_RE = re.compile(r"^\s*(?:#(\d+)|https?://\S+?/-/(?:issues|work_items)/(\d+)(?:[/?#][!-~]*)?)[\s,，。:：;；]*")
FAILED_TEXT = "这条没处理成功，请重发。"
OBSERVE_IMAGES_PER_MESSAGE = 4  # 观察群一条消息最多下载几张图给模型看
OBSERVE_DOWNLOAD_TIMEOUT_S = 60  # 观察是顺带做的，单张图下载超时比搬到 GitLab 短，免得拖慢 @ 指令的处理
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
    "1. 发「需求：<要做的事>」→ 我在 GitLab 建 issue，Agent 先分析，结论会回到这条消息的话题里\n"
    "2. 发「#123 <补充>」→ 关联已有 issue，之后在话题里回复就会同步过去\n"
    "3. 在话题里回复 /go 开工、/status 看进度、/new 让调度者重开会话\n"
    "4. 嫌消息多就回复 /免打扰（只在步骤完成、交付完成和要你处理时发），想看每一步就回复 /详尽\n"
    "5. 私聊窗口就是秘书：各需求的进度都会转到这里，对转达点「回复」就能回给那个需求，发「进度」看总览，其他话直接问\n"
    "在群里每句话（包括话题里的回复）都要 @我，我才收得到"
)

HELP_WORDS = frozenset({"帮助", "用法", "/help", "help"})

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Uploader = Callable[[str, Path], str]  # (project, 本地文件) → 可直接放进评论的 Markdown


class BridgeError(Exception):
    pass


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


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
                 upload: Optional[Uploader] = None, media_dir: Path = MEDIA_DIR):
        self.cfg, self.state, self.run, self.now = config, state, run, now
        self.upload = upload or self.http_upload
        self.media_dir = media_dir
        self.host: str = config["gitlab_host"]
        self.project: str = config["default_project"]
        self.label: str = config.get("label", "herdr-auto")
        self.users: dict[str, str] = dict(config["users"])
        # 主动接活的观察群：chat_id → 群名（备注）。这些群里的消息写进本机观察队列，由收件箱里的 proactive 判断。
        self.observe_chats: dict[str, str] = dict(config.get("observe_chats") or {})
        self.user_token = user_token
        self.agent: str = config.get("agent_account") or self.api("user")["username"]
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
        self._secretary: Any = None
        self.delivered: Optional[dict[str, Any]] = None  # 处理当前这条消息时已代用户写进评论的 issue（project、iid）
        self.history_requested = threading.Event()
        self.next_history_poll = 0.0

    @property
    def secretary(self) -> Any:
        """秘书（私聊窗口，见 secretary.py）；按需创建，只读命令（--split-topic 等）不会碰到它。"""
        if self._secretary is None:
            self._secretary = secretary_module().Secretary(self)
        return self._secretary

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

    def images_markdown(self, msg: dict[str, Any], project: str,
                        extra: Optional[list[tuple[str, list[dict[str, str]]]]] = None) -> str:
        """把这条消息、它所回复的消息、话题根消息、以及之前单独发的图片和文件搬到 GitLab，返回要附在正文后的 Markdown。"""
        sources = [(msg["message_id"], find_attachments(msg.get("content") or ""))] + (extra or [])
        # 在文件下面「回复话题」时 root_id 是文件那条；引用回复时 reply_to 是被引用那条，两个都要看
        for related in dict.fromkeys((msg.get("reply_to") or "", msg.get("root_id") or "")):
            if not related or related == msg["message_id"]:
                continue
            try:
                items = (self.lark(["im", "+messages-mget", "--message-ids", related]).get("data") or {}).get("messages")
                sources.append((related, find_attachments((items or [{}])[0].get("content") or "")))
            except BridgeError as e:
                log(f"read related {related} failed: {e}")
        parts, done = [], set()
        for mid, items in sources:
            for a in items:
                if a["key"] in done:
                    continue
                done.add(a["key"])
                parts.append(self.transfer(project, mid, a))
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
        text = f"[#{t['iid']}]({self.issue_url(t['project'], t['iid'])}) 已经弄好了，不用重发。" if t else FAILED_TEXT
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
        if msg.get("chat_type") == "group" and chat in self.observe_chats and not (mentioned and user):
            # 观察群：白名单的人 @ 机器人是明确指令，照常处理；其余消息（含同事 @ 机器人）交给主动接活去判断
            self.observe(msg)
        if msg.get("chat_type") == "group" and not mentioned:
            # 应用开了「获取群组中所有消息」权限后，群里每句话都会推过来；没 @ 机器人的不是对它说的，
            # 一律不回（否则会给同事回「没有使用权限」或用法说明，在群里刷屏）。2026-11-08
            return
        if not user:
            self.reply(mid, random.choice(DENY_TEXTS), f"deny-{mid}")
            return
        text = self.clean_text(msg)
        private = msg.get("chat_type") != "group" and chat not in self.state["group_chats"]
        if private:
            self.secretary.learn(chat)
            if self.secretary.is_relay(msg):
                self.secretary.handle(msg, text)
                return
        root, tid = msg.get("root_id") or "", msg.get("thread_id") or ""
        linked = self.state["thread_ids"].get(tid) if tid else None
        if not linked and root and root != mid and root in self.state["threads"]:
            linked = root
        # 只有开头明确选另一个 issue 才分流；普通正文提到编号仍是当前话题的回复。
        command_text = self.strip_images(text)
        c = CREATE_RE.match(command_text)
        candidate = command_text[c.end():] if c else command_text
        m = BIND_RE.match(candidate)
        if linked and m:
            iid = int(m.group(1) or m.group(2))
            if m.group(2):
                url = re.match(r"https?://[^\s,，。;；]+", candidate.strip()).group(0)
                try:
                    iid = topics_module().issue_id(self.cfg, url)
                except topics_module().TopicError:
                    self.reply(mid, "该链接不属于当前配置的项目；本话题绑定不变，这条消息未转发。", f"wrong-project-{mid}")
                    return
            if iid != self.state["threads"][linked]["iid"]:
                topics_module().redirect(self, linked, msg, iid)
                return
        if linked:
            self.follow_up(linked, msg, text)
            return
        # 引用回复（主对话里的「回复 xxx」）不算话题，按普通消息处理；真在话题里但还没关联的，发「需求：」也认
        # 「需求：」后面紧跟已有 issue 的链接或 #编号，是让 Agent 处理那张单，关联它而不是新建
        # 图文消息可能先放附件；命令只看文字，上传仍从原始 msg 提取附件。
        if m:
            self.bind(mid, chat, int(m.group(1) or m.group(2)), m.string[m.end():].strip(), user, msg)
            return
        if c and (command_text[c.end():].strip() or find_attachments(text)):
            self.create(mid, chat, command_text[c.end():].strip(), user, msg.get("sender_name") or user, msg)
            return
        if tid:
            self.reply(mid, "这条话题还没关联 issue。在这里发「需求：…」新建，或发「#编号」关联已有 issue。", f"orphan-{mid}")
            return
        attachments = find_attachments(text)
        if attachments and not self.strip_images(text):
            # 单独发图片/文件：记下来等下一条需求带上；提示用引用回复，不开话题，免得多出一个空话题
            self.state["pending_images"].setdefault(chat, []).extend(
                {"mid": mid, "ts": self.now(), **a} for a in attachments)
            self.reply(mid, IMAGE_HINT, f"img-hint-{mid}", in_thread=False)
            return
        if private and not c and self.strip_images(text).strip().lower() not in HELP_WORDS:
            self.secretary.handle(msg, text)
            return
        if private:
            self.reply(mid, HELP_TEXT, f"help-{mid}", in_thread=False)
            return
        last = self.state["help_sent"].get(chat, 0)
        if self.now() - last >= HELP_COOLDOWN_S:
            self.state["help_sent"][chat] = self.now()
            self.reply(mid, HELP_TEXT, f"help-{mid}")

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

    def observe(self, msg: dict[str, Any]) -> None:
        """观察群里的一条消息进主动接活队列：只写本机文件，不回复、不碰 GitLab；写失败只记日志。"""
        text = self.clean_text(msg)
        attachments = find_attachments(text)
        marks = ["[图片]" if a["kind"] == "image" else f"[文件 {a['name'] or '未命名'}]" for a in attachments]
        body = " ".join([strip_attachments(text).strip(), *marks]).strip()
        # 图片下载到本机给模型看（2026-11-08 用户同意）；文件、视频只写名字不下载。7 天后由 proactive 清理
        images: list[str] = []
        media = proactive_module().Files().media / msg["message_id"]
        for a in [a for a in attachments if a["kind"] == "image"][:OBSERVE_IMAGES_PER_MESSAGE]:
            try:
                images.append(str(self.download(msg["message_id"], a, media, OBSERVE_DOWNLOAD_TIMEOUT_S)))
            except BridgeError as e:
                log(f"observe image {a['key']} of {msg['message_id']} failed: {e}")
        chat = msg.get("chat_id", "")
        quote = ""
        quoted = msg.get("reply_to") or msg.get("root_id") or ""
        if quoted and quoted != msg["message_id"] and not msg.get("thread_id"):
            # 主聊天区的引用回复：把被引用那句带上，否则「同上」「这个也是」之类的话判不了；话题里本来就有上下文
            try:
                items = (self.lark(["im", "+messages-mget", "--message-ids", quoted]).get("data") or {}).get("messages")
                raw = (items or [{}])[0].get("content") or ""
                quote = (strip_attachments(raw).strip() + (" [附件]" if find_attachments(raw) else ""))[:300]
            except BridgeError as e:
                log(f"read quoted {quoted} failed: {e}")
        try:
            proactive_module().append_feishu_queue({
                "mid": msg["message_id"], "chat": chat, "chat_name": self.observe_chats.get(chat, ""),
                "root": msg.get("root_id") or "", "thread": msg.get("thread_id") or "",
                "sender": msg.get("sender_id") or "", "sender_name": msg.get("sender_name") or "",
                "text": body, "quote": quote, "images": images, "ms": int(msg.get("create_ms") or 0)})
        except OSError as e:
            log(f"observe {msg['message_id']} failed: {e}")

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

    def thread_context(self, msg: dict[str, Any]) -> tuple[str, list[tuple[str, list[dict[str, str]]]]]:
        """在别人的话题里发「需求：」时，把这条之前的人类发言原样带上，否则 Agent 只看得到「处理这个问题」。
        返回（引用块 Markdown，前文里的附件）；读不到就返回空，不影响建单。"""
        tid = msg.get("thread_id") or ""
        if not tid:
            return "", []  # 主聊天区的引用回复没有话题，被引用那条的附件已由 images_markdown 处理
        try:
            data = self.lark(["im", "+threads-messages-list", "--thread", tid, "--order", "asc",
                              "--no-reactions", "--page-all", "--page-limit", "4"])
        except BridgeError as e:
            log(f"read thread context {tid} failed: {e}")
            return "", []
        lines: list[str] = []
        attachments: list[tuple[str, list[dict[str, str]]]] = []
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
            lines.append("\n".join(f"> {l}" if l else ">" for l in f"{head}：{body}".splitlines()))
        if not lines:
            return "", attachments
        kept: list[str] = []
        total = 0
        for line in reversed(lines):
            total += len(line)
            if kept and total > CONTEXT_TEXT_CAP:
                kept.append("> …更早的消息省略")
                break
            kept.append(line)
        return "**话题里的前文**（从飞书原样搬来）：\n\n" + "\n>\n".join(reversed(kept)), attachments

    def create(self, mid: str, chat: str, text: str, user: str, name: str,
               msg: Optional[dict[str, Any]] = None) -> None:
        context, context_files = self.thread_context(msg) if msg else ("", [])
        images = self.images_markdown(msg, self.project, self.take_pending_images(chat) + context_files) if msg else ""
        text = self.strip_images(text) or "（见附件）"
        title = text.splitlines()[0].strip()
        title = title[:60] + ("…" if len(title) > 60 else "")
        agent_id = self.api(f"users?username={quote(self.agent)}")[0]["id"]
        desc = (f"{text}\n\n{context + chr(10) + chr(10) if context else ''}"
                f"{images + chr(10) + chr(10) if images else ''}---\n来自飞书（{name}，{datetime.fromtimestamp(self.now()):%Y-%m-%d %H:%M}）。"
                "在飞书话题里回复会同步成这里的评论，这里的评论也会同步回飞书。")
        issue = self.api(f"projects/{quote(self.project, safe='')}/issues", "POST", {
            "title": title, "description": desc, "labels": self.label, "assignee_id": str(agent_id)},
            as_user=True)
        iid = issue["iid"]
        self.state["threads"][mid] = {"project": self.project, "iid": iid, "chat_id": chat,
                                      "last_note": 0, "closed": False}
        self.reply(mid, f"已建 [#{iid}]({self.issue_url(self.project, iid)})：{title}\n"
                        "Agent 会先只读分析，结论会回到这个话题；在这里回复「/go」或说「开工」才正式交付。",
                   f"created-{mid}")
        self.link_thread(mid, mid)
        log(f"created {self.project}#{iid} from {mid} by {user}")

    def bind(self, mid: str, chat: str, iid: int, rest: str, user: str,
             msg: Optional[dict[str, Any]] = None) -> None:
        pid = quote(self.project, safe="")
        try:
            issue = self.api(f"projects/{pid}/issues/{iid}")
        except BridgeError:
            self.reply(mid, f"没找到 #{iid}（只能关联 {self.project} 里的 issue）。", f"bind-miss-{mid}")
            return
        notes = self.api(f"projects/{pid}/issues/{iid}/notes?sort=desc&per_page=1")
        images = self.images_markdown(msg, self.project, self.take_pending_images(chat)) if msg else ""
        rest = self.strip_images(rest)
        if rest or images:
            # 已有 issue 未必在收件箱跟踪范围内，@Agent 保证转交。
            body = "\n\n".join(x for x in (rest, images) if x)
            self.note_as_user(self.project, iid, f"@{self.agent} {body}\n\n_（来自飞书）_")
        # 评论发出去才算关联成功：失败时要用户重发，不能留一个半截的话题
        self.state["threads"][mid] = {"project": self.project, "iid": iid, "chat_id": chat,
                                      "last_note": notes[0]["id"] if notes else 0,
                                      "closed": issue["state"] == "closed", "bound": True}
        self.reply(mid, f"已关联 [#{iid}]({self.issue_url(self.project, iid)})：{issue['title']}\n"
                        "之后在这个话题里回复会同步成评论，issue 里的新评论也会发到这里。", f"bound-{mid}")
        self.link_thread(mid, mid)
        log(f"bound {mid} → {self.project}#{iid} by {user}")

    def follow_up(self, root: str, msg: dict[str, Any], text: str) -> None:
        mid = msg["message_id"]
        t = self.state["threads"][root]
        if t.get("closed"):
            self.reply(mid, "这张 issue 已关闭，这里只保留归档。要继续请回主聊天发新的「需求：…」，或「#编号」进入别的独立话题。", f"closed-{mid}")
            return
        # /go、/status、/new 这类指令要整条只有指令，收件箱才认得，所以不加尾注。
        if text.startswith("/"):
            body = text
        else:
            # 话题里的回复，回复对象就是话题内的消息，只搬这条自己带的附件，不重复搬根消息的
            images = self.images_markdown({**msg, "reply_to": "", "root_id": ""}, t["project"])
            text = self.strip_images(text)
            body = "\n\n".join(x for x in (text, images) if x) + "\n\n_（来自飞书）_"
        if t.get("bound"):
            body = f"@{self.agent} {body}"
        self.note_as_user(t["project"], t["iid"], body)

    # ---------- GitLab → 飞书 ----------
    def poll_gitlab(self) -> None:
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
                if not private:
                    self.reply(root, f"#{t['iid']} 已关闭，这个话题不再同步。", f"gl-closed-{t['iid']}-{root[-12:]}")

    def forward_note(self, root: str, t: dict[str, Any], n: dict[str, Any]) -> bool:
        """把一条 GitLab 评论发到飞书话题；成功（或已放弃）返回 True。幂等键保证重试不会重复发。"""
        key = f"gl-{t['iid']}-{n['id']}"
        try:
            self.reply(root, self.render_note(t, n), key)
            return True
        except BridgeError as e:
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
        body = (n.get("body") or "").strip()
        url = f"{self.issue_url(t['project'], t['iid'])}#note_{n['id']}"
        # 飞书不认折叠标签，会把全文连同标签一起显示；这里只留摘要，全文给链接
        body = FOLDED.sub(f"[查看完整内容]({url})", body)
        if len(body) > FEISHU_TEXT_CAP:
            body = body[:FEISHU_TEXT_CAP].rstrip() + f"\n\n…内容较长，全文见 [GitLab]({url})"
        who = n.get("author", {}).get("name") or n.get("author", {}).get("username", "?")
        return f"**{who}**（[#{t['iid']}]({url})）：\n{body}"

    # ---------- 补漏 ----------
    def poll_history(self) -> None:
        """只由主循环调用；连接线程只发通知，不与实时消息并发写状态或发评论。"""
        if not self.history_requested.is_set() and self.now() < self.next_history_poll:
            return
        self.history_requested.clear()  # 补读期间再重连，通知留给下一轮，不能在结束时清掉
        try:
            self.catch_up()
        finally:
            self.next_history_poll = self.now() + HISTORY_POLL_S

    def catch_up(self) -> None:
        """增量补读已知聊天和未关闭话题；一个来源失败不推进它的进度，也不挡住其他来源。"""
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


_PROACTIVE: Any = None


def proactive_module() -> Any:
    """按需加载同目录的 proactive.py（复用它的队列写入，队列格式只在一处定义）。"""
    global _PROACTIVE
    if _PROACTIVE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("proactive", Path(__file__).resolve().parent / "proactive.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PROACTIVE = module
    return _PROACTIVE


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
            "reply_to": e.get("reply_to") or "", "message_type": e.get("message_type") or "",
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


def stop_children(*_: Any) -> None:
    """桥退出时温和关掉 lark-cli（SIGTERM，让它清理服务端订阅），避免留下孤儿进程。"""
    for p in CHILDREN:
        if p.poll() is None:
            p.terminate()
    for p in CHILDREN:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


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
        bridge = Bridge(cfg, state, user_token=token)
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
    poll = int(cfg.get("poll_seconds", 15))
    next_poll = 0.0
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
            next_poll = time.time() + poll
            try:
                topic_queue.consume()
                bridge.poll_gitlab()
            except (BridgeError, KeyError, ValueError) as err:
                log(f"poll failed: {err}")
            try:
                bridge.secretary.poll()
                bridge.secretary.patrol()
            except Exception as err:  # 秘书出错不能拖垮桥本身
                log(f"secretary failed: {err!r}")
            save_json(STATE_PATH, state)


if __name__ == "__main__":
    sys.exit(main())
