#!/usr/bin/env python3
"""先保存源码再运行测试；不经 shell，保留测试真实退出码。

快照包含 Git 跟踪文件及未忽略的新文件，不包含 .git、依赖或被忽略的配置。
解包 source.tar 到空目录即可检查当时源码；运行环境仍须单独准备。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


EXIT_EVIDENCE_ERROR = 125


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def source_files(repo: Path) -> list[str]:
    # 子模块是独立仓库，不能只存一个目录却声称保存了它的源码。
    entries = git(repo, "ls-files", "--stage", "-z").split(b"\0")
    if any(entry.startswith(b"160000 ") for entry in entries):
        raise ValueError("Submodules require separate evidence runs in a repository without gitlinks")
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({os.fsdecode(p) for p in paths.split(b"\0") if p})


def snapshot(repo: Path, target: Path) -> dict:
    manifest = {}
    with tarfile.open(target, "w", dereference=False) as archive:
        for name in source_files(repo):
            path = repo / name
            if not path.exists() and not path.is_symlink():
                manifest[name] = {"kind": "deleted"}
                continue
            # 不通过被替换为符号链接的父目录读取仓库外的内容。
            if any(parent.is_symlink() for parent in path.parents if parent != repo and repo in parent.parents):
                raise ValueError(f"Symlink parent is not supported: {name}")
            if path.is_symlink():
                manifest[name] = {"kind": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                manifest[name] = {"kind": "file", "sha256": digest(path), "mode": path.stat().st_mode & 0o777}
            else:
                raise ValueError(f"Unsupported source entry: {name}")
            archive.add(path, arcname=name, recursive=False)
    return manifest


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture source and run one test command without a shell")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--phase", choices=("red", "green"), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A test command is required after --")
    record = {"schema": 1, "case": args.case, "phase": args.phase, "command": command,
              "started_at": now(), "test_exit_code": None, "source_unchanged": False}
    out = args.out.resolve()
    created = False
    try:
        repo = args.repo.resolve()
        top = Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
        if repo != top:
            raise ValueError("--repo must be the repository root")
        if out == repo:
            raise ValueError("Evidence directory cannot be the repository root")
        if repo in out.parents:
            ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", str(out) + "/"])
            if ignored.returncode != 0:
                raise ValueError("Evidence inside the repository must be gitignored")
        out.mkdir(parents=True, exist_ok=False, mode=0o700)
        created = True
        record.update(repo=str(repo), head=git(repo, "rev-parse", "HEAD").decode().strip())
        (out / "status.z").write_bytes(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
        (out / "staged.patch").write_bytes(git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"))
        (out / "unstaged.patch").write_bytes(git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv"))
        before = snapshot(repo, out / "source.tar")
        record["source_sha256"] = digest(out / "source.tar")
        (out / "source.json").write_text(json.dumps(before, ensure_ascii=True, indent=2) + "\n")
        # 再读一次，发现快照期间的编辑就不启动测试；运行期间必须独占工作树。
        checked = snapshot(repo, out / "checked.tar")
        (out / "checked.tar").unlink()
        if before != checked:
            raise ValueError("Source changed while capturing evidence")
        with (out / "output.log").open("wb") as log:
            result = subprocess.run(command, cwd=repo, stdout=log, stderr=subprocess.STDOUT)
        record["test_exit_code"] = result.returncode
        record["output_sha256"] = digest(out / "output.log")
        after = snapshot(repo, out / "after.tar")
        record["source_unchanged"] = before == after and record["head"] == git(repo, "rev-parse", "HEAD").decode().strip()
        record["after_sha256"] = digest(out / "after.tar")
        if not record["source_unchanged"]:
            raise ValueError("Source changed during test execution; evidence is invalid")
        exit_code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record["error"] = str(error)
        print(f"Evidence error: {error}", file=sys.stderr)
        exit_code = EXIT_EVIDENCE_ERROR
    finally:
        if created:
            record["finished_at"] = now()
            (out / "run.json").write_text(json.dumps(record, ensure_ascii=True, indent=2) + "\n")
            print(json.dumps({"record": str(out / "run.json"), "test_exit_code": record["test_exit_code"],
                              "source_unchanged": record["source_unchanged"]}))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
