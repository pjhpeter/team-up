"""通过 CLI 验证增量留证可恢复完整源码、真实退出码、旧版兼容和留证失败时的阻断。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/test-evidence.py"


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "clean.txt").write_text("untouched committed file")
        (self.repo / "source.txt").write_text("original")
        (self.repo / "deleted.txt").write_text("remove me")
        (self.repo / ".gitignore").write_text("secret.env\nevidence/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.out = self.root / "run"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args])

    def run_cli(self, code, out=None):
        return subprocess.run([
            sys.executable, str(SCRIPT), "--repo", str(self.repo),
            "--out", str(out or self.out), "--case", "11", "--phase", "red",
            "--", sys.executable, "-c", code,
        ], capture_output=True, text=True)

    def restore_cli(self, evidence, target):
        return subprocess.run([
            sys.executable, str(SCRIPT), "--restore", str(evidence), "--to", str(target),
        ], capture_output=True, text=True)

    def test_preserves_failure_and_raw_output_without_full_repo_tars(self):
        result = self.run_cli("import sys; print('FAIL'); print('detail', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)
        record = json.loads((self.out / "run.json").read_text())
        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["test_exit_code"], 7)
        self.assertTrue(record["source_unchanged"])
        self.assertFalse((self.out / "source.tar").exists())
        self.assertFalse((self.out / "after.tar").exists())
        self.assertTrue((self.out / "dirty.tar").exists())
        self.assertIn(b"FAIL", (self.out / "output.log").read_bytes())
        self.assertIn(b"detail", (self.out / "output.log").read_bytes())

    def test_restores_worktree_including_clean_staged_untracked_and_deletion(self):
        (self.repo / "source.txt").write_text("staged")
        self.git("add", "source.txt")
        (self.repo / "source.txt").write_text("actual tested bytes")
        (self.repo / "new test.py").write_text("assert True\n")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "secret.env").write_text("not source")
        (self.repo / "link").symlink_to("source.txt")
        result = self.run_cli("print('PASS')")
        self.assertEqual(result.returncode, 0, result.stderr)

        # 未改动的 clean.txt 不应打进 dirty.tar，但还原后必须完整存在。
        with tarfile.open(self.out / "dirty.tar") as archive:
            self.assertNotIn("clean.txt", archive.getnames())
            self.assertCountEqual(archive.getnames(), ["link", "new test.py", "source.txt"])

        restored = self.root / "restored"
        res = self.restore_cli(self.out, restored)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((restored / "clean.txt").read_text(), "untouched committed file")
        self.assertEqual((restored / "source.txt").read_text(), "actual tested bytes")
        self.assertEqual((restored / "new test.py").read_text(), "assert True\n")
        self.assertTrue((restored / "link").is_symlink())
        self.assertFalse((restored / "deleted.txt").exists())
        self.assertFalse((restored / "secret.env").exists())

        # 非空目标目录拒绝覆盖，避免误写工作树。
        refused = self.restore_cli(self.out, restored)
        self.assertEqual(refused.returncode, 125)

        record = json.loads((self.out / "run.json").read_text())
        self.assertEqual(record["head"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(record["dirty_sha256"], hashlib.sha256((self.out / "dirty.tar").read_bytes()).hexdigest())

    def test_legacy_source_tar_still_restores(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        with tarfile.open(legacy / "source.tar", "w") as archive:
            archive.add(self.repo / "clean.txt", arcname="clean.txt")
        restored = self.root / "legacy-restored"
        res = self.restore_cli(legacy, restored)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((restored / "clean.txt").read_text(), "untouched committed file")

    def test_source_mutation_invalidates_success(self):
        result = self.run_cli("from pathlib import Path; Path('source.txt').write_text('changed')")
        self.assertEqual(result.returncode, 125, result.stderr)
        record = json.loads((self.out / "run.json").read_text())
        self.assertEqual(record["test_exit_code"], 0)
        self.assertFalse(record["source_unchanged"])

    def test_existing_evidence_is_not_overwritten_or_executed(self):
        self.out.mkdir()
        (self.out / "keep").write_text("original")
        result = self.run_cli("from pathlib import Path; Path('executed').touch()")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "executed").exists())
        self.assertEqual((self.out / "keep").read_text(), "original")

    def test_unignored_output_inside_repo_is_rejected(self):
        result = self.run_cli("from pathlib import Path; Path('executed').touch()", self.repo / "runs")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "executed").exists())

    def test_ignored_evidence_directory_does_not_contaminate_snapshot(self):
        out = self.repo / "evidence" / "one"
        result = self.run_cli("print('PASS')", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((out / "source.json").read_text())
        self.assertFalse(any(name.startswith("evidence/") for name in manifest))
        with tarfile.open(out / "dirty.tar") as archive:
            self.assertFalse(any(name.startswith("evidence/") for name in archive.getnames()))


if __name__ == "__main__":
    unittest.main()
