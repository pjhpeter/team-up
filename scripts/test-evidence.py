#!/usr/bin/env python3
"""先保存源码指纹与增量工作树快照再运行测试；不经 shell，保留测试真实退出码。

快照记录全部 Git 跟踪文件及未忽略新文件的 SHA-256 清单（source.json），
但只把相对 HEAD 有改动或新增的文件打进 dirty.tar，避免每次运行复制整仓数千个未改文件。
用 `--restore <证据目录> --to <空目录>` 即可结合 HEAD 与 dirty.tar 完整还原当时被测源码。
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


def manifest_digest(manifest: dict[str, dict]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def source_files(repo: Path) -> list[str]:
    # 子模块是独立仓库，不能只存一个目录却声称保存了它的源码。
    entries = git(repo, "ls-files", "--stage", "-z").split(b"\0")
    if any(entry.startswith(b"160000 ") for entry in entries):
        raise ValueError("Submodules require separate evidence runs in a repository without gitlinks")
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({os.fsdecode(p) for p in paths.split(b"\0") if p})


def scan_manifest(repo: Path) -> dict[str, dict]:
    """扫描工作树全部受控文件的精确状态，不落盘整仓归档。"""
    manifest: dict[str, dict] = {}
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
    return manifest


def dirty_files(repo: Path, manifest: dict[str, dict]) -> list[str]:
    """找出相对 HEAD 有变化（含暂存、未暂存）或未跟踪的新文件，仅打包这些增量条目。"""
    changed: set[str] = set()
    for cmd in (
        ("diff", "--name-only", "-z", "HEAD", "--"),
        ("diff", "--cached", "--name-only", "-z", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        raw = git(repo, *cmd)
        changed.update(os.fsdecode(p) for p in raw.split(b"\0") if p)
    return sorted(name for name in changed if name in manifest and manifest[name].get("kind") != "deleted")


def write_dirty_tar(repo: Path, target: Path, names: list[str]) -> None:
    with tarfile.open(target, "w", dereference=False) as archive:
        for name in names:
            archive.add(repo / name, arcname=name, recursive=False)


def restore_snapshot(evidence: Path, target: Path, repo_override: Path | None = None) -> None:
    """将证据目录记录的被测源码完整还原到空目录（兼容旧版 source.tar 与新版 HEAD + dirty.tar）。"""
    evidence = evidence.resolve()
    target = target.resolve()
    if not evidence.is_dir():
        raise ValueError(f"Evidence directory not found: {evidence}")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError("--to must be a non-existent or empty directory")
    else:
        target.mkdir(parents=True, mode=0o700)

    legacy_tar = evidence / "source.tar"
    if legacy_tar.exists():
        subprocess.run(["tar", "-xf", str(legacy_tar), "-C", str(target)], check=True)
        return

    run_record = json.loads((evidence / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((evidence / "source.json").read_text(encoding="utf-8"))
    expected = run_record.get("source_sha256")
    if expected and manifest_digest(manifest) != expected:
        raise ValueError("Evidence source.json digest mismatch")

    repo = (repo_override or Path(run_record["repo"])).resolve()
    head = str(run_record["head"])
    with subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", head],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as git_proc:
        tar_res = subprocess.run(["tar", "-xf", "-", "-C", str(target)], stdin=git_proc.stdout, capture_output=True)
        if git_proc.stdout is not None:
            git_proc.stdout.close()
        git_err = git_proc.stderr.read().decode(errors="replace") if git_proc.stderr is not None else ""
        if git_proc.wait() != 0:
            raise ValueError(f"git archive failed for {head}: {git_err.strip()}")
        if tar_res.returncode != 0:
            raise ValueError(f"tar extract HEAD failed: {tar_res.stderr.decode(errors='replace').strip()}")

    dirty_tar_path = evidence / "dirty.tar"
    dirty_names: list[str] = []
    if dirty_tar_path.exists():
        if run_record.get("dirty_sha256") and digest(dirty_tar_path) != run_record["dirty_sha256"]:
            raise ValueError("Evidence dirty.tar digest mismatch")
        with tarfile.open(dirty_tar_path, "r") as archive:
            dirty_names = [member.name for member in archive.getmembers()]

    # 先删除工作树中已移除的文件和即将被覆盖的路径，防止普通文件与符号链接互换时解包串改目标。
    for name, meta in manifest.items():
        if meta.get("kind") == "deleted":
            victim = target / name
            if victim.exists() or victim.is_symlink():
                victim.unlink()
    for name in dirty_names:
        victim = target / name
        if victim.exists() or victim.is_symlink():
            victim.unlink()

    if dirty_names:
        subprocess.run(["tar", "-xf", str(dirty_tar_path), "-C", str(target)], check=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture source state and run one test command without a shell")
    parser.add_argument("--restore", type=Path, help="Restore captured source from an evidence directory")
    parser.add_argument("--to", type=Path, help="Empty target directory for --restore")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--phase", choices=("red", "green"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.restore is not None:
        if args.to is None:
            parser.error("--to is required with --restore")
        try:
            restore_snapshot(args.restore, args.to, args.repo)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
            print(f"Evidence restore error: {error}", file=sys.stderr)
            return EXIT_EVIDENCE_ERROR
        print(json.dumps({"restored": str(args.to.resolve())}))
        return 0

    if args.repo is None or args.out is None or args.case is None or args.phase is None:
        parser.error("--repo, --out, --case, and --phase are required when running tests")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A test command is required after --")
    record = {"schema": 2, "case": args.case, "phase": args.phase, "command": command,
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
        before = scan_manifest(repo)
        dirty = dirty_files(repo, before)
        write_dirty_tar(repo, out / "dirty.tar", dirty)
        record["source_sha256"] = manifest_digest(before)
        record["dirty_sha256"] = digest(out / "dirty.tar")
        record["dirty_count"] = len(dirty)
        (out / "source.json").write_text(json.dumps(before, ensure_ascii=True, indent=2) + "\n")
        # 打包后再扫一次清单，发现快照期间的并发编辑就不启动测试；运行期间必须独占工作树。
        checked = scan_manifest(repo)
        if before != checked or record["head"] != git(repo, "rev-parse", "HEAD").decode().strip():
            raise ValueError("Source changed while capturing evidence")
        with (out / "output.log").open("wb") as log:
            result = subprocess.run(command, cwd=repo, stdout=log, stderr=subprocess.STDOUT)
        record["test_exit_code"] = result.returncode
        record["output_sha256"] = digest(out / "output.log")
        after = scan_manifest(repo)
        record["source_unchanged"] = before == after and record["head"] == git(repo, "rev-parse", "HEAD").decode().strip()
        record["after_sha256"] = manifest_digest(after)
        if not record["source_unchanged"]:
            raise ValueError("Source changed during test execution; evidence is invalid")
        exit_code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
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
