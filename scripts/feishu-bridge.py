#!/usr/bin/env python3
"""飞书桥：把飞书对话和 GitLab issue 双向同步。Agent 团队仍只认 GitLab，本脚本不碰 Herdr。

    python3 ~/.hwf/scripts/feishu-bridge.py            # 常驻（建议放在一个 Herdr 窗格里）
    python3 ~/.hwf/scripts/feishu-bridge.py --check    # 只检查配置、令牌与飞书连接后退出

分工（2026-11-02 用户指示，方案 A：每人一个飞书应用，桥跑在自己电脑上）：
  飞书 → GitLab：本脚本（收消息、建 issue、把话题回复写成 issue 评论）
  GitLab → Agent：原有的 gitlab-inbox.py（不改）
  GitLab → 飞书：本脚本轮询已关联 issue 的新评论，回帖到对应飞书话题
桥只做搬运，以后推广到公司时可整体挪到一台常开的机器上。

飞书里怎么用（只认 users 白名单里的人）：
  「需求：<内容>」或「/需求 <内容>」  → 在 default_project 建 issue（打开关标签、指派给 Agent 账号），回帖 issue 链接
  「#123 <可选内容>」或贴 issue 链接   → 把这条消息的话题关联到已有 issue，内容写成评论（带 @Agent 以便转交）
  在上面两种消息的话题里回复         → 写成该 issue 的评论；/go、/status、/new 等指令原样生效
  其他顶层消息                       → 回一段用法说明（同一会话 10 分钟内只回一次）
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
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import re
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
MEDIA_DIR = HWF_HOME / "state" / "feishu-media"  # 飞书图片下载的中转目录（lark-cli 只许写到当前目录下）

CMD_TIMEOUT_S = 60
SEEN_CAP = 2000
HELP_COOLDOWN_S = 600
FEISHU_TEXT_CAP = 2500  # 超过就截断并附 GitLab 原文链接
CREATE_RE = re.compile(r"^\s*(?:/需求|/issue|需求\s*[:：])\s*", re.I)
IMAGE_KEY_RE = re.compile(r"\[Image: (img_[^\]\s]+)\]")  # 飞书把图片渲染成「[Image: img_xxx]」
IMAGE_HINT = "收到图片。要让 Agent 看这张图：在图上点「回复」，写「需求：<要做的事>」或「#编号」。"
BIND_RE = re.compile(r"^\s*(?:#(\d+)|https?://\S+?/-/(?:issues|work_items)/(\d+)\S*)\s*")
HELP_TEXT = (
    "用法：\n"
    "1. 发「需求：<要做的事>」→ 我在 GitLab 建 issue，Agent 先分析，结论会回到这条消息的话题里\n"
    "2. 发「#123 <补充>」→ 关联已有 issue，之后在话题里回复就会同步过去\n"
    "3. 在话题里回复 /go 开工、/status 看进度、/new 让调度者重开会话\n"
    "在群里每句话（包括话题里的回复）都要 @我，我才收得到"
)

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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)  # 原子替换


def cap_list(items: list, cap: int = SEEN_CAP) -> list:
    return items[-cap:]


class Bridge:
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
        self.user_token = user_token
        self.agent: str = config.get("agent_account") or self.api("user")["username"]
        state.setdefault("threads", {})      # 话题根消息 id → {project, iid, chat_id, last_note, closed}
        state.setdefault("seen", [])         # 已处理的飞书消息 id
        state.setdefault("posted_notes", []) # 桥自己发到 GitLab 的评论 id（不再搬回飞书）
        state.setdefault("chats", {})        # chat_id → 上次看到消息的时间戳（毫秒），补漏用
        state.setdefault("help_sent", {})    # chat_id → 上次发用法说明的时间戳（秒）
        state.setdefault("group_chats", [])  # 收到过群消息的 chat_id（补漏时群里只处理 @ 机器人的）

    # ---------- 外部命令 ----------
    def sh(self, cmd: list[str], stdin: Optional[str] = None, env: Optional[dict] = None,
           cwd: Optional[Path] = None) -> str:
        r = self.run(cmd, input=stdin, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, env=env, cwd=cwd)
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

    def lark(self, args: list[str], stdin: Optional[str] = None, cwd: Optional[Path] = None) -> Any:
        out = self.sh(["lark-cli", *args, "--as", "bot"], stdin=stdin, cwd=cwd)
        data = json.loads(out) if out.strip() else {}
        if isinstance(data, dict) and data.get("ok") is False:
            raise BridgeError(f"lark-cli {args[:2]} 失败：{data.get('error')}")
        return data

    def reply(self, message_id: str, markdown: str, key: Optional[str] = None) -> None:
        args = ["im", "+messages-reply", "--message-id", message_id, "--reply-in-thread", "--markdown", "-"]
        if key:
            args += ["--idempotency-key", key[:50]]
        self.lark(args, stdin=markdown)

    def http_upload(self, project: str, path: Path) -> str:
        """以用户身份把文件传到 GitLab 项目附件（glab api 不支持上传文件），返回 Markdown。"""
        if not self.user_token:
            raise BridgeError("没有用户 GitLab 令牌，不能上传图片")
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://{self.host}/api/v4/projects/{quote(project, safe='')}/uploads", data=body, method="POST",
            headers={"PRIVATE-TOKEN": self.user_token, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=CMD_TIMEOUT_S) as r:
                return json.loads(r.read())["markdown"]
        except (OSError, ValueError, KeyError) as e:
            raise BridgeError(f"上传图片失败：{e}") from e

    def images_markdown(self, msg: dict[str, Any], project: str) -> str:
        """把这条消息、以及它所回复的那条消息里的图片搬到 GitLab，返回要附在正文后的 Markdown。"""
        sources = [(msg["message_id"], IMAGE_KEY_RE.findall(msg.get("content") or ""))]
        parent = msg.get("reply_to") or ""
        if parent and parent != msg["message_id"]:
            try:
                items = (self.lark(["im", "+messages-mget", "--message-ids", parent]).get("data") or {}).get("messages")
                sources.append((parent, IMAGE_KEY_RE.findall((items or [{}])[0].get("content") or "")))
            except BridgeError as e:
                log(f"read parent {parent} failed: {e}")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        parts = []
        for mid, keys in sources:
            for key in keys:
                try:
                    data = self.lark(["im", "+messages-resources-download", "--message-id", mid, "--file-key", key,
                                      "--type", "image", "--output", key], cwd=self.media_dir)
                    path = Path((data.get("data") or {}).get("saved_path") or "")
                    if not path.is_absolute():
                        path = self.media_dir / path
                    parts.append(self.upload(project, path))
                    path.unlink(missing_ok=True)
                except BridgeError as e:
                    log(f"image {key} of {mid} failed: {e}")
                    parts.append(f"_（有一张图片没搬过来：{e}）_")
        return "\n\n".join(parts)

    # ---------- 飞书 → GitLab ----------
    def handle(self, msg: dict[str, Any]) -> None:
        """处理一条飞书消息（实时事件或补漏拉到的历史，字段已统一）。"""
        mid = msg["message_id"]
        if mid in self.state["seen"]:
            return
        self.state["seen"] = cap_list(self.state["seen"] + [mid])
        chat = msg.get("chat_id", "")
        if chat:
            self.state["chats"][chat] = max(int(msg.get("create_ms") or 0), self.state["chats"].get(chat, 0))
            if msg.get("chat_type") == "group" and chat not in self.state["group_chats"]:
                self.state["group_chats"].append(chat)
        if msg.get("sender_type") != "user":
            return  # 机器人（含自己）发的不处理，避免来回刷屏
        user = self.users.get(msg.get("sender_id", ""))
        if not user:
            self.reply(mid, "你还没有使用权限，请联系机器人的主人把你加进白名单。", f"deny-{mid}")
            return
        text = self.clean_text(msg)
        root = msg.get("root_id") or ""
        if root and root != mid and root in self.state["threads"]:
            self.follow_up(root, msg, text)
            return
        # 在没关联的话题里（例如对一张图点「回复」）发「需求：」/「#编号」也认：整个话题挂到这张 issue 上
        key = root if root and root != mid else mid
        m = BIND_RE.match(text)
        if m:
            self.bind(mid, chat, int(m.group(1) or m.group(2)), text[m.end():].strip(), user, key, msg)
            return
        m = CREATE_RE.match(text)
        if m and text[m.end():].strip():
            self.create(mid, chat, text[m.end():].strip(), user, msg.get("sender_name") or user, key, msg)
            return
        if key != mid:
            self.reply(mid, "这条话题还没关联 issue。在这里发「需求：…」新建，或发「#编号」关联已有 issue。", f"orphan-{mid}")
            return
        if IMAGE_KEY_RE.fullmatch(text.strip()):
            self.reply(mid, IMAGE_HINT, f"img-hint-{mid}")
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

    @staticmethod
    def strip_images(text: str) -> str:
        return IMAGE_KEY_RE.sub("", text).strip()

    def is_bot_mention(self, m: dict[str, Any]) -> bool:
        mid = m.get("id") or ""
        return bool(mid) and (mid == self.cfg.get("bot_open_id") or mid.startswith("cli_"))

    def note_as_user(self, project: str, iid: int, body: str) -> int:
        note = self.api(f"projects/{quote(project, safe='')}/issues/{iid}/notes", "POST",
                        {"body": body}, as_user=True)
        self.state["posted_notes"] = cap_list(self.state["posted_notes"] + [note["id"]])
        return note["id"]

    def issue_url(self, project: str, iid: int) -> str:
        return f"https://{self.host}/{project}/-/issues/{iid}"

    def create(self, mid: str, chat: str, text: str, user: str, name: str,
               key: Optional[str] = None, msg: Optional[dict[str, Any]] = None) -> None:
        images = self.images_markdown(msg, self.project) if msg else ""
        text = self.strip_images(text) or "（见图）"
        title = text.splitlines()[0].strip()
        title = title[:60] + ("…" if len(title) > 60 else "")
        agent_id = self.api(f"users?username={quote(self.agent)}")[0]["id"]
        desc = (f"{text}\n\n{images + chr(10) + chr(10) if images else ''}---\n来自飞书（{name}，{datetime.fromtimestamp(self.now()):%Y-%m-%d %H:%M}）。"
                "在飞书话题里回复会同步成这里的评论，这里的评论也会同步回飞书。")
        issue = self.api(f"projects/{quote(self.project, safe='')}/issues", "POST", {
            "title": title, "description": desc, "labels": self.label, "assignee_id": str(agent_id)},
            as_user=True)
        iid = issue["iid"]
        self.state["threads"][key or mid] = {"project": self.project, "iid": iid, "chat_id": chat,
                                             "last_note": 0, "closed": False}
        self.reply(mid, f"已建 [#{iid}]({self.issue_url(self.project, iid)})：{title}\n"
                        "Agent 会先只读分析，结论会回到这个话题；在这里回复「/go」或说「开工」才正式交付。",
                   f"created-{mid}")
        log(f"created {self.project}#{iid} from {mid} by {user}")

    def bind(self, mid: str, chat: str, iid: int, rest: str, user: str,
             key: Optional[str] = None, msg: Optional[dict[str, Any]] = None) -> None:
        pid = quote(self.project, safe="")
        try:
            issue = self.api(f"projects/{pid}/issues/{iid}")
        except BridgeError:
            self.reply(mid, f"没找到 #{iid}（只能关联 {self.project} 里的 issue）。", f"bind-miss-{mid}")
            return
        notes = self.api(f"projects/{pid}/issues/{iid}/notes?sort=desc&per_page=1")
        self.state["threads"][key or mid] = {"project": self.project, "iid": iid, "chat_id": chat,
                                             "last_note": notes[0]["id"] if notes else 0,
                                             "closed": issue["state"] == "closed", "bound": True}
        images = self.images_markdown(msg, self.project) if msg else ""
        rest = self.strip_images(rest)
        if rest or images:
            # 已有 issue 未必在收件箱跟踪范围内，@Agent 保证转交。
            body = "\n\n".join(x for x in (rest, images) if x)
            self.note_as_user(self.project, iid, f"@{self.agent} {body}\n\n_（来自飞书）_")
        self.reply(mid, f"已关联 [#{iid}]({self.issue_url(self.project, iid)})：{issue['title']}\n"
                        "之后在这个话题里回复会同步成评论，issue 里的新评论也会发到这里。", f"bound-{mid}")
        log(f"bound {mid} → {self.project}#{iid} by {user}")

    def follow_up(self, root: str, msg: dict[str, Any], text: str) -> None:
        mid = msg["message_id"]
        t = self.state["threads"][root]
        if t.get("closed"):
            self.reply(mid, "这张 issue 已关闭。要继续请发新的「需求：…」，或「#编号」关联别的 issue。", f"closed-{mid}")
            return
        # /go、/status、/new 这类指令要整条只有指令，收件箱才认得，所以不加尾注。
        if text.startswith("/"):
            body = text
        else:
            # 话题里的回复，回复对象就是话题内的消息，只搬这条自己带的图，不重复搬根消息的图
            images = self.images_markdown({**msg, "reply_to": ""}, t["project"])
            text = self.strip_images(text)
            body = "\n\n".join(x for x in (text, images) if x) + "\n\n_（来自飞书）_"
        if t.get("bound"):
            body = f"@{self.agent} {body}"
        self.note_as_user(t["project"], t["iid"], body)

    # ---------- GitLab → 飞书 ----------
    def poll_gitlab(self) -> None:
        for root, t in list(self.state["threads"].items()):
            if t.get("closed"):
                continue
            pid = quote(t["project"], safe="")
            # 从最新往前取一页，只要比水位新的，再按时间正序发；一轮间隔内不会超过 50 条。
            notes = self.api(f"projects/{pid}/issues/{t['iid']}/notes?sort=desc&order_by=created_at&per_page=50")
            for n in sorted(notes, key=lambda x: x["id"]):
                if n["id"] <= t.get("last_note", 0):
                    continue
                t["last_note"] = n["id"]
                if n.get("system") or n["id"] in self.state["posted_notes"]:
                    continue
                self.reply(root, self.render_note(t, n), f"gl-{t['iid']}-{n['id']}")
            issue = self.api(f"projects/{pid}/issues/{t['iid']}")
            if issue["state"] == "closed":
                t["closed"] = True
                self.reply(root, f"#{t['iid']} 已关闭，这个话题不再同步。", f"gl-closed-{t['iid']}-{root[-12:]}")

    def render_note(self, t: dict[str, Any], n: dict[str, Any]) -> str:
        body = (n.get("body") or "").strip()
        url = f"{self.issue_url(t['project'], t['iid'])}#note_{n['id']}"
        if len(body) > FEISHU_TEXT_CAP:
            body = body[:FEISHU_TEXT_CAP].rstrip() + f"\n\n…内容较长，全文见 [GitLab]({url})"
        who = n.get("author", {}).get("name") or n.get("author", {}).get("username", "?")
        return f"**{who}**（[#{t['iid']}]({url})）：\n{body}"

    # ---------- 补漏 ----------
    def catch_up(self) -> None:
        """重启后按聊天记录补上停机期间的消息；第一次运行只记水位，不回放旧消息。"""
        # 群聊整段记录要额外权限，读不到就跳过该会话（话题里的回复仍按下面的话题补）；一个失败不影响其他
        for chat, last_ms in list(self.state["chats"].items()):
            start = datetime.fromtimestamp(max(last_ms / 1000 - 120, 0)).astimezone().isoformat(timespec="seconds")
            try:
                data = self.lark(["im", "+chat-messages-list", "--chat-id", chat, "--order", "asc",
                                  "--no-reactions", "--start", start, "--page-all", "--page-limit", "5"])
            except BridgeError as e:
                log(f"catch-up skip chat {chat}: {e}")
                continue
            for item in (data.get("data") or {}).get("messages", []):
                msg = normalize_history(item)
                if chat in self.state["group_chats"] and not any(map(self.is_bot_mention, msg["mentions"])):
                    continue  # 群里没 @ 机器人的话不是对它说的
                self.handle(msg)
        for root, t in list(self.state["threads"].items()):
            if t.get("closed"):
                continue
            try:
                data = self.lark(["im", "+threads-messages-list", "--thread", root, "--order", "asc",
                                  "--no-reactions", "--page-all", "--page-limit", "5"])
            except BridgeError as e:
                log(f"catch-up skip thread {root}: {e}")
                continue
            for item in (data.get("data") or {}).get("messages", []):
                self.handle(normalize_history(item, default_root=root))


def normalize_event(e: dict[str, Any]) -> dict[str, Any]:
    return {"message_id": e.get("message_id") or e.get("id"), "chat_id": e.get("chat_id"),
            "chat_type": e.get("chat_type"), "sender_id": e.get("sender_id"),
            "sender_type": e.get("sender_type"), "content": e.get("content") or "",
            "mentions": e.get("mentions") or [], "root_id": e.get("root_id") or "",
            "reply_to": e.get("reply_to") or "", "message_type": e.get("message_type") or "",
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
            "message_type": m.get("msg_type") or "", "create_ms": ms}


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


def consume_events(out: "queue.Queue[dict]") -> None:
    """常驻 lark-cli event consume；stdin 保持不关（关了它会退出），挂了就退避重启。"""
    backoff = 5
    while True:
        CHILDREN[:] = [c for c in CHILDREN if c.poll() is None]
        p = subprocess.Popen(["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        CHILDREN.append(p)
        threading.Thread(target=lambda: [log(f"lark: {l.rstrip()}") for l in p.stderr], daemon=True).start()
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
    ap.add_argument("--check", action="store_true", help="检查配置、令牌与飞书机器人后退出")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
        token = keychain_token(cfg.get("gitlab_token_keychain"))
        state = load_json(STATE_PATH, {})
        bridge = Bridge(cfg, state, user_token=token)
        bot = bridge.lark(["api", "GET", "/open-apis/bot/v3/info"])
        cfg.setdefault("bot_open_id", (bot.get("data") or {}).get("open_id"))
        who = bridge.api("user", as_user=True)["username"] if token else None
    except BridgeError as e:
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
    signal.signal(signal.SIGINT, stop_children)
    events: "queue.Queue[dict]" = queue.Queue()
    threading.Thread(target=consume_events, args=(events,), daemon=True).start()
    try:
        bridge.catch_up()
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
        if time.time() >= next_poll:
            next_poll = time.time() + poll
            try:
                bridge.poll_gitlab()
            except (BridgeError, KeyError, ValueError) as err:
                log(f"poll failed: {err}")
            save_json(STATE_PATH, state)


if __name__ == "__main__":
    sys.exit(main())
