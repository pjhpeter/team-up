"""显式拆分话题的请求队列与发送账本；只有桥主循环可以消费。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
import uuid


class TopicError(Exception):
    pass


def issue_id(config, url):
    """只接受配置项目的完整 issue URL，不把正文引用当作拆分关系。"""
    parsed = urlsplit(url)
    prefix = f"/{config['default_project']}/-/"
    match = re.fullmatch(re.escape(prefix) + r"(?:issues|work_items)/([1-9]\d*)/?", parsed.path)
    if (parsed.scheme not in ("https", "http") or parsed.netloc != config["gitlab_host"]
            or parsed.username or parsed.password or parsed.query or parsed.fragment or not match):
        raise TopicError("只接受配置的 GitLab host/project 下的完整 issue URL（不含查询参数或锚点）")
    return int(match[1])


def request_key(config, iid):
    return hashlib.sha256(f"{config['gitlab_host']}/{config['default_project']}#{iid}".encode()).hexdigest()[:32]


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    """先落盘再替换；发送前后的账本都必须在下一次外部调用前持久化。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w") as out:
            json.dump(value, out, ensure_ascii=False, indent=1)
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def lock_bridge(state_path):
    """文件句柄须保持到进程退出，防止两个桥同时消费或覆盖运行状态。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.with_suffix(".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise TopicError("已有飞书桥运行，不能启动第二个消费者") from None
    return lock


def status(config, directory, target_url):
    iid = issue_id(config, target_url)
    key = request_key(config, iid)
    request = read_json(directory / f"{key}.request.json")
    if not request:
        return {"status": "not_registered", "target_iid": iid}
    result = read_json(directory / f"{key}.result.json", {})
    return {**request, **result}


def register(config, directory, source_url, target_url, api, now=time.time):
    source, target = issue_id(config, source_url), issue_id(config, target_url)
    if source == target:
        raise TopicError("来源和目标不能是同一个 issue")
    directory.mkdir(parents=True, exist_ok=True)
    key = request_key(config, target)
    path = directory / f"{key}.request.json"
    # 登记者只写不可变请求，绝不读取运行状态或覆盖消费者的结果。
    with (directory / "register.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_json(path)
        if existing:
            if existing["source_iid"] != source:
                raise TopicError(f"目标 #{target} 已登记来源 #{existing['source_iid']}，拒绝改绑")
            return status(config, directory, target_url)
        pid = quote(config["default_project"], safe="")
        api(f"projects/{pid}/issues/{source}")
        issue = api(f"projects/{pid}/issues/{target}")
        notes = api(f"projects/{pid}/issues/{target}/notes?sort=desc&order_by=created_at&per_page=1")
        request = {"key": key, "status": "queued", "host": config["gitlab_host"],
                   "project": config["default_project"], "source_iid": source, "target_iid": target,
                   "target_url": f"https://{config['gitlab_host']}/{config['default_project']}/-/issues/{target}",
                   "registered_at": now(), "last_note": max((n["id"] for n in notes), default=0),
                   "title": issue["title"], "closed": issue["state"] == "closed"}
        write_json(path, request)
        return request


class Topics:
    def __init__(self, bridge, directory, persist):
        self.bridge, self.directory, self.persist = bridge, directory, persist

    def matches(self, iid, chat=None):
        return [(root, t) for root, t in self.bridge.state["threads"].items()
                if t["project"] == self.bridge.project and t["iid"] == iid
                and (chat is None or t["chat_id"] == chat)]

    def consume(self):
        for path in sorted(self.directory.glob("*.request.json")):
            result_path = path.with_name(path.name.replace(".request.json", ".result.json"))
            result = read_json(result_path, {"effects": {}})
            try:
                request = read_json(path)
                if request["host"] != self.bridge.host or request["project"] != self.bridge.project:
                    raise TopicError("请求不属于当前配置的 GitLab 项目")
                if result.get("status") == "needs_attention":
                    continue
                self.finish(request, result, result_path)
            except Exception as error:
                # 超时意味着可能已经发送，不能把它当作未发送然后换一个幂等键。
                if result.get("status") != "needs_attention":
                    result["status"] = "failed"
                result["error"] = str(error)
                write_json(result_path, result)

    def effect(self, request, result, path, name, args, content):
        effects = result.setdefault("effects", {})
        entry = effects.get(name)
        if entry and entry.get("message_id"):
            return entry["message_id"]
        if not entry:
            entry = {"started_at": self.bridge.now(), "args": args, "content": content,
                     "key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"feishu-topic:{request['key']}:{name}"))}
            effects[name] = entry
            result["status"] = "sending"
            write_json(path, result)
        # 飞书只保证一小时去重，留一分钟给网络；过期的不确定发送必须人工核对。
        if self.bridge.now() - entry["started_at"] >= 3540:
            result["status"] = "needs_attention"
            raise TopicError(f"{name} 的发送结果不确定且已接近/超过一小时；已停止重发，请人工核对飞书消息和账本")
        response = self.bridge.lark(entry["args"] + ["--idempotency-key", entry["key"]],
                                    stdin=entry["content"])
        mid = (response.get("data") or {}).get("message_id")
        if not mid:
            raise TopicError(f"{name} 未返回 message_id，保留原幂等键等待恢复")
        entry["message_id"] = mid
        write_json(path, result)
        return mid

    def install_binding(self, request, result):
        root = result["root"]
        threads = self.bridge.state["threads"]
        t = threads.get(root)
        if t and (t["iid"] != request["target_iid"] or t["project"] != request["project"]
                  or t["chat_id"] != result["chat_id"]):
            raise TopicError("目标根消息已有不同绑定，拒绝覆盖")
        if not t:
            t = {"project": request["project"], "iid": request["target_iid"], "chat_id": result["chat_id"],
                 "last_note": request["last_note"], "closed": result["closed"], "bound": True,
                 "topic_pending": True}
            threads[root] = t
        t["bound"] = True
        if result.get("thread_id"):
            tid = result["thread_id"]
            old = self.bridge.state["thread_ids"].get(tid)
            if old and old != root:
                raise TopicError("目标 thread_id 已有不同绑定，拒绝覆盖")
            self.bridge.state["thread_ids"][tid] = root
            if result.get("message_app_link"):
                t["message_app_link"] = result["message_app_link"]
            t.pop("topic_pending", None)
        self.persist()

    def finish(self, request, result, path):
        if result.get("status") in ("ready", "archived"):
            self.install_binding(request, result)
            return
        if not result.get("source_root"):
            sources = self.matches(request["source_iid"])
            if not sources:
                raise TopicError("来源 issue 没有飞书绑定；请先在正确聊天关联来源 issue")
            if len(sources) != 1 or not sources[0][1].get("chat_id"):
                raise TopicError("来源 issue 有多个话题或聊天，无法确定来源；未发送任何消息")
            source_root, source = sources[0]
            targets = self.matches(request["target_iid"], source["chat_id"])
            if len(targets) > 1:
                raise TopicError("同一聊天已有多个目标话题，无法选择；未发送任何消息")
            issue = self.bridge.api(f"projects/{quote(request['project'], safe='')}/issues/{request['target_iid']}")
            result.update(source_root=source_root, chat_id=source["chat_id"], closed=issue["state"] == "closed")
            if targets:
                result.update(root=targets[0][0], reused=True)
            write_json(path, result)
        if not result.get("root"):
            title = (f"#{request['target_iid']} {request['title']}（来源 #{request['source_iid']}）"
                     f"\n{request['target_url']}")
            root = self.effect(request, result, path, "root",
                               ["im", "+messages-send", "--chat-id", result["chat_id"], "--markdown", "-"], title)
            result["root"] = root
            write_json(path, result)
        self.install_binding(request, result)
        root = result["root"]
        intro = ("这张 issue 已关闭；这里只保留归档入口，不会重开或重放旧评论。" if result["closed"] else
                 "这是独立话题；只同步这张 issue 的新评论、确认和结果。群聊回复时请 @机器人。")
        self.effect(request, result, path, "intro",
                    ["im", "+messages-reply", "--message-id", root, "--reply-in-thread", "--markdown", "-"], intro)
        if not result.get("thread_id") or not result.get("message_app_link"):
            data = self.bridge.lark(["im", "+messages-mget", "--message-ids", root, "--no-reactions"])
            items = (data.get("data") or {}).get("messages") or []
            message = next((m for m in items if m.get("message_id") == root), {})
            if message.get("thread_id"):
                result["thread_id"] = message["thread_id"]
            if message.get("message_app_link"):
                result["message_app_link"] = message["message_app_link"]
            write_json(path, result)
            self.install_binding(request, result)
            if not result.get("thread_id") or not result.get("message_app_link"):
                raise TopicError("尚未读到目标 thread_id/message_app_link；保留已发消息，稍后只重试读取")
        self.install_binding(request, result)
        notice = (f"#{request['target_iid']} 已拆为[独立话题]({result['message_app_link']})，"
                  f"后续确认与结果请到那里；本话题仍是 #{request['source_iid']}。"
                  + ("目标已关闭，新入口仅供归档查看。" if result["closed"] else ""))
        self.effect(request, result, path, "notice",
                    ["im", "+messages-reply", "--message-id", result["source_root"], "--reply-in-thread", "--markdown", "-"], notice)
        result["status"] = "archived" if result["closed"] else "ready"
        result.pop("error", None)
        write_json(path, result)


def redirect(bridge, linked, msg, iid):
    """只提示去独立话题，不在旧话题新建/改绑，也不把附件写进错误 issue。"""
    matches = [(root, t) for root, t in bridge.state["threads"].items()
               if t["project"] == bridge.project and t["iid"] == iid and t["chat_id"] == msg["chat_id"]]
    link = ""
    if len(matches) == 1:
        root, target = matches[0]
        link = target.get("message_app_link", "")
        if not link:
            data = bridge.lark(["im", "+messages-mget", "--message-ids", root, "--no-reactions"])
            link = next((m.get("message_app_link", "") for m in (data.get("data") or {}).get("messages", [])
                         if m.get("message_id") == root), "")
    destination = f"请到 [#{iid} 的独立话题]({link})继续。" if link else f"请回主聊天发「#{iid}」，再到它的独立话题继续。"
    bridge.reply(msg["message_id"], f"本话题仍关联 #{bridge.state['threads'][linked]['iid']}。{destination}"
                 "这条消息和附件未转发，请到目标话题重新发送。", f"redirect-{msg['message_id']}")


def is_registered_topic(home: Path, target_url: str) -> bool:
    """调度器只读查询显式登记；issues/work_items 两种链接使用同一请求键。"""
    parsed = urlsplit(target_url)
    match = re.fullmatch(r"/(.+)/-/(?:issues|work_items)/([1-9]\d*)/?", parsed.path)
    if not match or parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    config = {"gitlab_host": parsed.netloc, "default_project": match[1]}
    iid = int(match[2])
    path = home / "state" / "feishu-topics" / f"{request_key(config, iid)}.request.json"
    request = read_json(path, {})
    return (request.get("host") == config["gitlab_host"] and request.get("project") == config["default_project"]
            and request.get("target_iid") == iid)
