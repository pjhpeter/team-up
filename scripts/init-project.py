#!/usr/bin/env python3
"""把一个 GitLab 仓库接入 Herdr 交付流程（可重复运行，已有的不会重建或覆盖）。

    python3 ~/.hwf/scripts/init-project.py <仓库路径>                     # 只看要做什么，不改任何东西
    python3 ~/.hwf/scripts/init-project.py <仓库路径> --apply             # 真的去做
    python3 ~/.hwf/scripts/init-project.py <仓库路径> --apply \\
        --smoke "国内=https://develop.example.com" --smoke "国际=https://app.develop.example.com"

做的事（缺什么补什么）：
  GitLab：9 个标签、登记池 issue、需求看板（6 列进度）；
  收件箱：在入库的 ~/.hwf/gitlab-inbox.config.json 登记项目，在本机 gitlab-inbox.local.json 写仓库路径（重启收件箱生效）；
  仓库：docs/agents/herdr-project.yaml、docs/agents/issue-tracker.md、docs/agents/triage-labels.md、
        .gitignore 加 .hwf/tmp/、AGENTS.md 加交付流程段落、没有 CLAUDE.md 时建一个引用 AGENTS.md。
仓库里已存在的文件只检查不覆盖；GitLab 上多出来的旧标签只列出、不删。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Optional
from urllib.parse import quote

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
TEMPLATES = HWF_HOME / "templates/project"
INBOX_CONFIG = HWF_HOME / "gitlab-inbox.config.json"

REGISTRY_PREFIX = "登记池："
REGISTRY_TITLE = "登记池：与需求无关的问题（只记录，不排期）"
REGISTRY_BODY = (
    "这张 issue 长期开着，专门记录**与当前需求无关**的问题（既有缺陷、测试债、别的模块的问题）。\n\n"
    "- 每个问题一条评论，只记录、不排期、不派人。\n"
    "- 想做哪一条，就在那条评论下回复「这个要做」，会开一张新的需求 issue。\n\n"
    "规则见 `~/.hwf/protocol/planning.md`「issue 数量与进度标签」的登记池规则。"
)
BOARD_NAME = "需求看板"
STATUS_LABELS = ["状态:等你处理", "状态:待开始", "状态:方案中", "状态:开发中", "状态:测试中", "状态:待上线"]
LABELS = [
    ("需求", "#1F75CB", "类型：根 issue，一个需求最上层的 issue（含 spec）。关闭 = 已上线或已作废"),
    ("子任务", "#6699CC", "类型：根 issue 下要并行开发才拆出的子 issue"),
    ("状态:等你处理", "#E24329", "进度：需要你拍板、执行、澄清或决定做不做"),
    ("状态:待开始", "#9E9E9E", "进度：已排好，等人开工"),
    ("状态:方案中", "#8E44AD", "进度：写方案、方案/用例评审"),
    ("状态:开发中", "#E67E22", "进度：开发与代码评审"),
    ("状态:测试中", "#F1C40F", "进度：自动化测试、合入测试分支、冒烟"),
    ("状态:待上线", "#108548", "进度：代码已在 release 分支，等合上线分支"),
    ("herdr-auto", "#6699CC", "打开后，把 issue 指派给 {me} 即自动启动本地 Herdr 交付团队"),
]

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class InitError(Exception):
    pass


def parse_remote(url: str) -> tuple[str, str]:
    """git@host:group/repo.git 或 https://host/group/repo.git → (host, group/repo)。"""
    m = re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", url) or re.match(
        r"^https?://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url
    )
    if not m:
        raise InitError(f"看不懂 origin 地址：{url}")
    return m.group(1), m.group(2)


class Init:
    def __init__(self, repo: Path, apply: bool, smoke: list[tuple[str, str]],
                 test_branch: Optional[str], run: Runner = subprocess.run,
                 inbox_config: Path = INBOX_CONFIG, templates: Path = TEMPLATES):
        self.repo, self.apply, self.smoke, self.want_test = repo, apply, smoke, test_branch
        self.run, self.inbox_config, self.templates = run, inbox_config, templates
        self.lines: list[str] = []
        self.todo = 0
        remote = self.sh(["git", "remote", "get-url", "origin"]).strip()
        self.host, self.project = parse_remote(remote)
        self.pid = quote(self.project, safe="")

    # ---------- 基础 ----------
    def sh(self, cmd: list[str]) -> str:
        r = self.run(cmd, cwd=str(self.repo), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise InitError(f"{' '.join(cmd[:4])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def api(self, path: str, method: str = "GET", fields: Optional[dict[str, str]] = None,
            ok404: bool = False) -> Any:
        cmd = ["glab", "api", "--hostname", self.host, "-X", method, path]
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        r = self.run(cmd, cwd=str(self.repo), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            if ok404 and "404" in (r.stderr + r.stdout):
                return None
            raise InitError(f"GitLab {method} {path} 失败：{(r.stderr or r.stdout).strip()[:300]}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def api_all(self, path: str) -> list[Any]:
        out: list[Any] = []
        sep = "&" if "?" in path else "?"
        page = 1
        while True:
            chunk = self.api(f"{path}{sep}per_page=100&page={page}")
            out += chunk
            if len(chunk) < 100:
                return out
            page += 1

    def have(self, what: str) -> None:
        self.lines.append(f"[已有]   {what}")

    def do(self, what: str, action: Callable[[], Any]) -> Any:
        self.todo += 1
        if not self.apply:
            self.lines.append(f"[将新建] {what}")
            return None
        result = action()
        self.lines.append(f"[已新建] {what}")
        return result

    def warn(self, what: str) -> None:
        self.lines.append(f"[注意]   {what}")

    # ---------- GitLab ----------
    def labels(self) -> dict[str, int]:
        me = self.api("user")["username"]
        existing = {l["name"]: l["id"] for l in self.api_all(f"projects/{self.pid}/labels")}
        wanted = {name for name, _, _ in LABELS}
        for name, color, desc in LABELS:
            if name in existing:
                self.have(f"标签 {name}")
                continue
            made = self.do(f"标签 {name}", lambda n=name, c=color, d=desc: self.api(
                f"projects/{self.pid}/labels", "POST",
                {"name": n, "color": c, "description": d.format(me=me)}))
            if made:
                existing[name] = made["id"]
        extra = sorted(set(existing) - wanted)
        if extra:
            self.warn(f"项目里还有 {len(extra)} 个流程不用的标签（没动）：{'、'.join(extra)}")
        return existing

    def registry(self) -> Optional[int]:
        found = [i for i in self.api_all(
            f"projects/{self.pid}/issues?state=opened&in=title&search={quote(REGISTRY_PREFIX)}")
            if i["title"].startswith(REGISTRY_PREFIX)]
        if found:
            iid = min(i["iid"] for i in found)
            self.have(f"登记池 #{iid}")
            if len(found) > 1:
                self.warn(f"有 {len(found)} 张登记池，按最早的 #{iid}；其余请合并后关掉")
            return iid
        made = self.do("登记池 issue", lambda: self.api(f"projects/{self.pid}/issues", "POST", {
            "title": REGISTRY_TITLE, "description": REGISTRY_BODY, "labels": "状态:等你处理"}))
        return made["iid"] if made else None

    def board(self, label_ids: dict[str, int]) -> Optional[int]:
        boards = self.api(f"projects/{self.pid}/boards")
        board = next((b for b in boards if b["name"] == BOARD_NAME), None)
        if board:
            self.have(f"{BOARD_NAME}（编号 {board['id']}）")
        else:
            board = self.do(BOARD_NAME, lambda: self.api(
                f"projects/{self.pid}/boards", "POST", {"name": BOARD_NAME}))
            if not board:
                self.lines.append(f"[将新建] {BOARD_NAME}的 6 列：{' → '.join(STATUS_LABELS)}")
                self.todo += 1
                return None
            board.setdefault("lists", [])
        cols = {l["label"]["name"] for l in board.get("lists", []) if l.get("label")}
        for name in STATUS_LABELS:
            if name in cols:
                continue
            if name not in label_ids:
                self.lines.append(f"[将新建] 看板列 {name}（等标签建好）")
                self.todo += 1
                continue
            self.do(f"看板列 {name}", lambda n=name: self.api(
                f"projects/{self.pid}/boards/{board['id']}/lists", "POST", {"label_id": str(label_ids[n])}))
        if cols >= set(STATUS_LABELS):
            self.have("看板 6 列进度")
        return board["id"]

    def branches(self) -> tuple[Optional[str], str]:
        main = self.api(f"projects/{self.pid}")["default_branch"]
        test = self.want_test or "develop"
        if self.api(f"projects/{self.pid}/repository/branches/{quote(test, safe='')}", ok404=True) is None:
            self.warn(f"没有测试分支 {test}：docs/agents/herdr-project.yaml 里写 null，交付到冒烟这步会先问你")
            return None, main
        return test, main

    # ---------- 本机 ----------
    def inbox(self) -> None:
        """入库的 gitlab-inbox.config.json 登记项目；本机的 gitlab-inbox.local.json 写仓库路径。"""
        cfg_path = self.inbox_config
        local_path = cfg_path.with_name("gitlab-inbox.local.json")
        if not cfg_path.exists():
            self.warn(f"没有收件箱配置 {cfg_path}，跳过登记")
            return
        cfg = json.loads(cfg_path.read_text())
        if cfg.get("host") != self.host:
            self.warn(f"收件箱只盯 {cfg.get('host')}，本项目在 {self.host}，不能登记")
            return
        local = json.loads(local_path.read_text()) if local_path.exists() else {}

        def dump(path: Path, data: Any) -> None:
            with open(path, "w") as f:  # 原地写回（不 mv 覆盖）
                f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

        shared_projects = cfg.get("projects")
        if shared_projects in (None, []):
            self.have("收件箱已开启全项目动态监听（无需修改 gitlab-inbox.config.json）")
        elif self.project in shared_projects:
            self.have(f"收件箱登记 {self.project}（入库配置）")
        else:
            def add_shared() -> None:
                cfg["projects"] = sorted(set(shared_projects) | {self.project})
                dump(cfg_path, cfg)
            self.do(f"收件箱登记 {self.project}（改的是入库的 gitlab-inbox.config.json，记得在 ~/.hwf 提交 MR）", add_shared)

        if local.get("projects", {}).get(self.project) == str(self.repo):
            self.have(f"收件箱本机路径 {self.project} → {self.repo}")
        else:
            def add_local() -> None:
                local.setdefault("projects", {})[self.project] = str(self.repo)
                dump(local_path, local)
            self.do(f"收件箱本机路径 {self.project} → {self.repo}（要重启收件箱 pane 才生效）", add_local)

    # ---------- 仓库文件 ----------
    def render(self, name: str, **values: str) -> str:
        text = (self.templates / name).read_text()
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", v)
        return text

    def write_new(self, rel: str, text: str) -> bool:
        path = self.repo / rel
        if path.exists():
            self.have(rel)
            return False

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.do(rel, write)
        return True

    def project_yaml(self, registry: Optional[int], board: Optional[int], test: Optional[str], main: str) -> None:
        smoke = "\n".join(f"  - name: {n}\n    url: {u}" for n, u in self.smoke) or "  []"
        text = self.render("project.yaml", HOST=self.host, PROJECT=self.project,
                           REGISTRY=str(registry) if registry else "null",
                           BOARD=str(board) if board else "null",
                           TEST_BRANCH=test or "null", MAIN_BRANCH=main, SMOKE=smoke)
        if self.write_new("docs/agents/herdr-project.yaml", text):
            if not self.smoke:
                self.warn("没给冒烟地址：交付到冒烟这步会先问你，之后可手动补进 docs/agents/herdr-project.yaml 的 smoke")
            return
        current = (self.repo / "docs/agents/herdr-project.yaml").read_text()
        for key, val in (("registry_issue", registry), ("board_id", board)):
            m = re.search(rf"^\s*{key}:\s*(\S+)", current, re.M)
            if val and (not m or m.group(1) != str(val)):
                self.warn(f"docs/agents/herdr-project.yaml 的 {key} 是 {m.group(1) if m else '空'}，GitLab 上实际是 {val}，请手改")

    def repo_files(self) -> None:
        values = {"HOST": self.host, "PROJECT": self.project}
        self.write_new("docs/agents/issue-tracker.md", self.render("issue-tracker.md", **values))
        self.write_new("docs/agents/triage-labels.md", self.render("triage-labels.md", **values))

        gi = self.repo / ".gitignore"
        lines = gi.read_text().splitlines() if gi.exists() else []
        if any(l.strip().rstrip("/") in (".hwf/tmp", "/.hwf/tmp", ".hwf") for l in lines):
            self.have(".gitignore 忽略 .hwf/tmp/")
        else:
            self.do(".gitignore 加一行 .hwf/tmp/", lambda: gi.write_text(
                "\n".join(lines + [".hwf/tmp/"]) + "\n"))

        agents = self.repo / "AGENTS.md"
        body = agents.read_text() if agents.exists() else ""
        if "Herdr Issue 交付流程" in body:
            self.have("AGENTS.md 交付流程段落")
        else:
            snippet = (self.templates / "agents-snippet.md").read_text()
            new = (body.rstrip() + "\n\n## Agent skills\n\n" if body else "# AGENTS\n\n## Agent skills\n\n") + snippet
            self.do("AGENTS.md 末尾加「Agent skills」交付流程段落", lambda: agents.write_text(new))

        claude = self.repo / "CLAUDE.md"
        if not claude.exists():
            self.do("CLAUDE.md（只有一行 @AGENTS.md，让 Claude 也读到）",
                    lambda: claude.write_text("@AGENTS.md\n"))
        elif "AGENTS.md" in claude.read_text():
            self.have("CLAUDE.md 引用 AGENTS.md")
        else:
            self.warn("CLAUDE.md 没引用 AGENTS.md，Claude 读不到交付规则；请加一行 @AGENTS.md")

    # ---------- 总流程 ----------
    def main(self) -> int:
        self.lines.append(f"项目 {self.host}/{self.project}（仓库 {self.repo}）"
                          + ("" if self.apply else "，只看不改；确认后加 --apply"))
        label_ids = self.labels()
        registry = self.registry()
        board = self.board(label_ids)
        test, main = self.branches()
        self.inbox()
        self.project_yaml(registry, board, test, main)
        self.repo_files()
        if board:
            self.lines.append(f"需求看板：https://{self.host}/{self.project}/-/boards/{board}?label_name[]=需求")
        if registry:
            self.lines.append(f"登记池：https://{self.host}/{self.project}/-/issues/{registry}")
        verb = "已完成" if self.apply else "待做"
        self.lines.append(f"{verb} {self.todo} 项" + ("；仓库里新建/改动的文件记得提交" if self.apply and self.todo else ""))
        print("\n".join(self.lines))
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repo", help="本地仓库路径")
    p.add_argument("--apply", action="store_true", help="真的执行；不加只列出要做的事")
    p.add_argument("--smoke", action="append", default=[], metavar="名称=URL", help="冒烟地址，可写多次")
    p.add_argument("--test-branch", default=None, help="测试分支名，默认 develop")
    a = p.parse_args(argv)
    smoke = []
    for s in a.smoke:
        name, sep, url = s.partition("=")
        if not sep or not url.startswith("http"):
            print(f"--smoke 要写成 名称=URL：{s}", file=sys.stderr)
            return 2
        smoke.append((name.strip(), url.strip()))
    repo = Path(a.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"{repo} 不是 git 仓库根目录", file=sys.stderr)
        return 2
    try:
        return Init(repo, a.apply, smoke, a.test_branch).main()
    except InitError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
