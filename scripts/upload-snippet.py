#!/usr/bin/env python3
"""把方案或已评审测试用例上传为 GitLab 项目 snippet，并回读核对；回读不一致即失败。

    python3 ~/.hwf/scripts/upload-snippet.py create --repo <仓库路径> --file <文件> \\
        --title "<issue IID> 测试用例（已评审）" --description "<issue URL>；用例评审通过轮次 <N>；通过时间 <ISO8601>"
    python3 ~/.hwf/scripts/upload-snippet.py mark --repo <仓库路径> --id <snippet ID> --file <确认版本文件> \\
        --title "<issue IID> 最终技术方案 r<N>（已确认）" --description "<issue URL>；确认时间 <ISO8601>；来源 <note 链接>"
    python3 ~/.hwf/scripts/upload-snippet.py check --repo <仓库路径> --id <snippet ID> --file <本地文件>

create：从仓库 origin 确定 GitLab 项目，以 internal 可见性新建 snippet，描述自动补 SHA-256。
mark：只改已有 snippet 的标题和描述，不改内容；先核对线上内容与 --file 一致才改。
check：只回读核对，不改任何东西（交接或关单前复核用）。
三者都回读：可见性是 internal、文件名一致、线上内容 SHA-256 与本地文件一致。
成功时 stdout 输出一行 JSON（id、web_url、file_name、sha256、visibility、title），附在 issue 评论和 STATE 里。

退出码：0 已上传且回读一致；1 上传或回读失败（stdout 一行 JSON 说明卡在哪一步）；2 参数错误。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote

VISIBILITY = "internal"

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _load_init_project() -> Any:
    spec = importlib.util.spec_from_file_location("hwf_init_project", Path(__file__).resolve().parent / "init-project.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnippetError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Uploader:
    def __init__(self, repo: Path, run: Runner = subprocess.run):
        self.run = run
        result = run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, check=False)
        if result.returncode != 0:
            raise SnippetError("origin", f"读不到 {repo} 的 origin：{result.stderr.decode(errors='replace').strip()}")
        try:
            self.host, self.project = _load_init_project().parse_remote(result.stdout.decode().strip())
        except Exception as exc:  # init-project 的 InitError 在另一个模块里，统一转成本脚本的失败
            raise SnippetError("origin", str(exc)) from exc
        self.base = f"projects/{quote(self.project, safe='')}/snippets"

    def api(self, stage: str, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> bytes:
        argv = ["glab", "api", "--hostname", self.host, "-X", method, path]
        with tempfile.TemporaryDirectory(prefix="hwf-snippet-") as tmp:
            if payload is not None:
                body = Path(tmp) / "payload.json"
                body.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                # 只给 --input 时 GitLab 回 415，必须显式声明 JSON。
                argv += ["--input", str(body), "-H", "Content-Type: application/json"]
            result = self.run(argv, capture_output=True, check=False, timeout=60)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise SnippetError(stage, f"glab api {method} {path} 失败：{detail}")
        return result.stdout

    def json_api(self, stage: str, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        raw = self.api(stage, method, path, payload)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise SnippetError(stage, f"{path} 返回的不是 JSON：{raw[:200]!r}") from exc

    def create(self, file: Path, title: str, description: str) -> dict[str, Any]:
        data = file.read_bytes()
        digest = sha256(data)
        if digest not in description:
            description = f"{description}；SHA-256 {digest}"
        payload = {
            "title": title,
            "description": description,
            "visibility": VISIBILITY,
            "files": [{"file_path": file.name, "content": data.decode("utf-8")}],
        }
        created = self.json_api("create", "POST", self.base, payload)
        if "id" not in created:
            raise SnippetError("create", f"创建返回缺少 id：{created}")
        return self.verify(int(created["id"]), file.name, digest)

    def mark(self, snippet_id: int, file: Path, title: str, description: str) -> dict[str, Any]:
        digest = sha256(file.read_bytes())
        self.verify(snippet_id, file.name, digest)  # 线上不是确认的版本就不能标记
        if digest not in description:
            description = f"{description}；SHA-256 {digest}"
        self.json_api("mark", "PUT", f"{self.base}/{snippet_id}", {"title": title, "description": description})
        return self.verify(snippet_id, file.name, digest, title=title)

    def verify(self, snippet_id: int, file_name: str, digest: str, title: Optional[str] = None) -> dict[str, Any]:
        meta = self.json_api("readback", "GET", f"{self.base}/{snippet_id}")
        problems = []
        if meta.get("visibility") != VISIBILITY:
            problems.append(f"可见性是 {meta.get('visibility')!r}，应为 {VISIBILITY}")
        names = [f.get("path") for f in meta.get("files") or []] or [meta.get("file_name")]
        if names != [file_name]:
            problems.append(f"文件是 {names}，应只有 {file_name}")
        if title is not None and meta.get("title") != title:
            problems.append(f"标题是 {meta.get('title')!r}，应为 {title!r}")
        online = ""
        if not problems:
            raw = self.api("readback", "GET", f"{self.base}/{snippet_id}/files/main/{quote(file_name, safe='')}/raw")
            online = sha256(raw)
            if online != digest:
                problems.append(f"线上内容 SHA-256 {online} 与本地 {digest} 不一致")
        if problems:
            raise SnippetError("readback", f"snippet {snippet_id}（{meta.get('web_url')}）回读不一致：" + "；".join(problems))
        return {
            "ok": True,
            "id": snippet_id,
            "web_url": meta.get("web_url"),
            "file_name": file_name,
            "sha256": digest,
            "visibility": meta.get("visibility"),
            "title": meta.get("title"),
        }


def main(argv: Optional[Sequence[str]] = None, run: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description="上传方案/测试用例为 internal snippet 并回读核对")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "mark", "check"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True, type=Path, help="本地仓库路径，用它的 origin 确定 GitLab 项目")
        p.add_argument("--file", required=True, type=Path)
        if name != "check":
            p.add_argument("--title", required=True)
            p.add_argument("--description", required=True)
        if name != "create":
            p.add_argument("--id", required=True, type=int)
    args = parser.parse_args(argv)
    if not args.file.is_file():
        parser.error(f"文件不存在：{args.file}")
    try:
        uploader = Uploader(args.repo, run=run)
        if args.command == "create":
            result = uploader.create(args.file, args.title, args.description)
        elif args.command == "mark":
            result = uploader.mark(args.id, args.file, args.title, args.description)
        else:
            result = uploader.verify(args.id, args.file.name, sha256(args.file.read_bytes()))
    except SnippetError as exc:
        print(json.dumps({"ok": False, "stage": exc.stage, "error": str(exc)}, ensure_ascii=False))
        return 1
    except UnicodeDecodeError:
        print(json.dumps({"ok": False, "stage": "create", "error": f"{args.file} 不是 UTF-8 文本"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
