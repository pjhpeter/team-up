"""通过 CLI 验证可恢复源码、真实退出码和留证失败时的阻断。"""
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

    def test_preserves_failure_and_raw_output(self):
        result = self.run_cli("import sys; print('FAIL'); print('detail', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)
        record = json.loads((self.out / "run.json").read_text())
        self.assertEqual(record["test_exit_code"], 7)
        self.assertTrue(record["source_unchanged"])
        self.assertIn(b"FAIL", (self.out / "output.log").read_bytes())
        self.assertIn(b"detail", (self.out / "output.log").read_bytes())

    def test_restores_worktree_including_staged_untracked_and_deletion(self):
        (self.repo / "source.txt").write_text("staged")
        self.git("add", "source.txt")
        (self.repo / "source.txt").write_text("actual tested bytes")
        (self.repo / "new test.py").write_text("assert True\n")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "secret.env").write_text("not source")
        (self.repo / "link").symlink_to("source.txt")
        result = self.run_cli("print('PASS')")
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = self.root / "restored"
        restored.mkdir()
        subprocess.run(["tar", "-xf", str(self.out / "source.tar"), "-C", str(restored)], check=True)
        self.assertEqual((restored / "source.txt").read_text(), "actual tested bytes")
        self.assertEqual((restored / "new test.py").read_text(), "assert True\n")
        self.assertTrue((restored / "link").is_symlink())
        self.assertFalse((restored / "deleted.txt").exists())
        self.assertFalse((restored / "secret.env").exists())
        record = json.loads((self.out / "run.json").read_text())
        self.assertEqual(record["head"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(record["source_sha256"], hashlib.sha256((self.out / "source.tar").read_bytes()).hexdigest())

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
        with tarfile.open(out / "source.tar") as archive:
            self.assertFalse(any(name.startswith("evidence/") for name in archive.getnames()))


if __name__ == "__main__":
    unittest.main()
