#!/usr/bin/env python3
"""关闭某个交付 worktree 里还在监听端口的本地测试服务，并回读端口确认空闲。

    python3 ~/.hwf/scripts/stop-test-services.py <worktree>... [--port N]... [--dry-run]

exit 0 = 指定 worktree 下已无监听进程、待回读端口全部空闲；exit 1 = 仍有残留（明细写 stdout）。

为什么需要：协议要求 test 测完自己关服务，但 2026-10-25 用户反馈交付跑完服务仍常驻。成员用
`nohup … &` 或 omp 命名服务起的 vite 不随 pane 关闭退出，调度者只看报告里的“已关闭”核不出来。
本脚本按“进程 cwd 在交付 worktree 内 + 正在监听 TCP 端口”认领服务，不依赖成员登记：
- 并行 issue 各用独立 worktree（PROTOCOL「并行」），cwd 归属即 issue 归属，不会误关别人的服务；
- 只沿父进程链向上收拢 node/pnpm/npm 等包装进程（`pnpm exec vite` 会留两层 pnpm），
  遇到 shell、agent、omp broker 即停，不碰成员 pane 本身；
- agent 自身（omp/codex/claude/pi/herdr）即使 cwd 在 worktree 内也不关。
测试浏览器（Chrome for Testing 等）带调试端口，cwd 在 worktree 内时同样会被认领关闭。
--port 只用于回读（例如 STATE 里登记的端口）；worktree 登记的 dev-server-port 会自动加入回读。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterable, Sequence

AGENT_NAMES = frozenset({"omp", "codex", "claude", "pi", "herdr"})
WRAPPER_NAMES = frozenset({"node", "bun", "pnpm", "npm", "npx", "yarn", "corepack"})
TERM_WAIT_S = 5.0


def run(argv: Sequence[str]) -> str:
    # lsof 查不到任何结果时返回 1，属于正常情况，不当作错误。
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=15, check=False).stdout


def listening() -> dict[int, set[int]]:
    """pid → 正在监听的 TCP 端口。"""
    result: dict[int, set[int]] = {}
    pid = 0
    for line in run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"]).splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                result.setdefault(pid, set()).add(int(port))
    return result


def cwds(pids: Iterable[int]) -> dict[int, str]:
    pids = sorted(set(pids))
    if not pids:
        return {}
    result: dict[int, str] = {}
    pid = 0
    for line in run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fpn"]).splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            result[pid] = os.path.realpath(line[1:])
    return result


def process_table() -> dict[int, tuple[int, str, str]]:
    """pid → (ppid, 可执行文件名, 完整命令)。"""
    table: dict[int, tuple[int, str, str]] = {}
    for line in run(["ps", "-Ao", "pid=,ppid=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()), "")
    for line in run(["ps", "-Ao", "pid=,command="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and int(parts[0]) in table:
            ppid, name, _ = table[int(parts[0])]
            table[int(parts[0])] = (ppid, name, parts[1])
    return table


def under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def registered_port(worktree: str) -> int | None:
    """仓库 scripts/dev/worktree-port.mjs 把非主工作树的端口登记在 <git-dir>/dev-server-port。"""
    git_dir = run(["git", "-C", worktree, "rev-parse", "--absolute-git-dir"]).strip()
    port_file = Path(git_dir) / "dev-server-port" if git_dir else None
    if port_file and port_file.is_file() and port_file.read_text().strip().isdigit():
        return int(port_file.read_text().strip())
    return None


def descendants(roots: set[int], table: dict[int, tuple[int, str, str]]) -> set[int]:
    found = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _, _) in table.items():
            if ppid in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def select(roots: Sequence[str]) -> tuple[dict[int, set[int]], set[int], dict[int, tuple[int, str, str]]]:
    """返回 (要关的监听进程 → 端口, 要发信号的完整进程集合, 进程表)。"""
    listeners = listening()
    table = process_table()
    cwd_of = cwds(listeners)
    targets = {pid: ports for pid, ports in listeners.items()
               if pid in cwd_of and under(cwd_of[pid], roots)
               and table.get(pid, (0, "", ""))[1] not in AGENT_NAMES}
    tops: set[int] = set()
    for pid in targets:
        top = pid
        while True:
            ppid, _, _ = table.get(top, (0, "", ""))
            parent = table.get(ppid)
            if ppid <= 1 or parent is None or parent[1] not in WRAPPER_NAMES:
                break
            parent_cwd = cwds([ppid]).get(ppid, "")
            if not under(parent_cwd, roots):
                break
            top = ppid
        tops.add(top)
    return targets, descendants(tops, table), table


def signal_all(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def alive(pids: Iterable[int]) -> set[int]:
    result = set()
    for pid in pids:
        try:
            os.kill(pid, 0)
            result.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            result.add(pid)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("worktrees", nargs="+", help="交付 worktree 绝对路径（STATE 中 created_by 为自己的）")
    parser.add_argument("--port", type=int, action="append", default=[], help="额外回读的端口，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只列出将关闭的进程")
    args = parser.parse_args(argv)

    roots = []
    for raw in args.worktrees:
        path = os.path.realpath(raw)
        if not os.path.isdir(path):
            print(f"skip: {raw} is not a directory")
            continue
        roots.append(path)
    ports = set(args.port)
    for root in roots:
        port = registered_port(root)
        if port:
            ports.add(port)

    targets, victims, table = select(roots) if roots else ({}, set(), {})
    for pid in sorted(targets):
        print(f"{'would stop' if args.dry_run else 'stop'}: pid={pid} ports={sorted(targets[pid])} "
              f"cmd={table.get(pid, (0, '', ''))[2][:160]}")
        ports.update(targets[pid])
    if args.dry_run:
        print(f"dry-run: {len(victims)} process(es) in {len(targets)} service tree(s)")
        return 0

    signal_all(victims, signal.SIGTERM)
    deadline = time.time() + TERM_WAIT_S
    while alive(victims) and time.time() < deadline:
        time.sleep(0.2)
    stubborn = alive(victims)
    if stubborn:
        print(f"SIGKILL: {sorted(stubborn)}")
        signal_all(stubborn, signal.SIGKILL)
        time.sleep(0.5)

    # 回读：worktree 内不得再有监听进程，登记/指定端口必须空闲。
    leftover, _, _ = select(roots) if roots else ({}, set(), {})
    busy = {port: pid for pid, owned in listening().items() for port in owned if port in ports}
    for pid, owned in sorted(leftover.items()):
        print(f"RESIDUE: pid={pid} ports={sorted(owned)} still listening inside worktree")
    for port in sorted(ports):
        if port in busy:
            print(f"RESIDUE: port {port} busy (pid={busy[port]})")
        else:
            print(f"port {port} free")
    if not ports and not leftover:
        print("no listening service inside worktree(s)")
    return 1 if leftover or busy else 0


if __name__ == "__main__":
    sys.exit(main())
