"""1.7.24: the case-collision hooks install idempotently, refuse a colliding commit, and the shell tests pass."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import githooks  # noqa: E402


def git(cwd, *args, check=True):
    done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True)
    if check:
        assert done.returncode == 0, (args, done.stderr)
    return done


class ShellTests(unittest.TestCase):
    def test_hook_script(self):
        done = subprocess.run(["bash", str(HERE / "test_no_case_collisions.sh")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_fix_script(self):
        done = subprocess.run(["bash", str(HERE / "test_fix_gates_case.sh")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        git(self.tmp, "init", "-q", "-b", "main")
        git(self.tmp, "config", "core.ignoreCase", "false")
        (self.tmp / "Makefile").write_text(".PHONY: ci\nci:\n\t./scripts/ci/local.sh\n")
        hooks = self.tmp / ".git" / "hooks"
        (hooks / "pre-push").write_text("#!/bin/sh\necho mine\nexit 0\n")

    def test_install_twice_is_one_block_and_keeps_the_users_hook(self):
        githooks.install(self.tmp)
        githooks.install(self.tmp)
        pre_push = (self.tmp / ".git" / "hooks" / "pre-push").read_text()
        pre_commit = (self.tmp / ".git" / "hooks" / "pre-commit").read_text()
        self.assertEqual(pre_push.count(githooks.BEGIN), 1)
        self.assertEqual(pre_commit.count(githooks.BEGIN), 1)
        self.assertTrue(pre_push.startswith("#!/bin/sh\n" + githooks.BEGIN), "before the user's exit 0")
        self.assertIn("echo mine", pre_push)
        make = (self.tmp / "Makefile").read_text()
        self.assertEqual(make.count("fix-gates:"), 1)
        self.assertIn("\tbash .cursor/plugins/warp/scripts/fix_gates_case.sh --all", make)
        self.assertTrue((self.tmp / ".warp" / "hooks" / "no-case-collisions.sh").is_file())
        self.assertIn(".backup/", (self.tmp / ".git" / "info" / "exclude").read_text())

    def test_the_installed_hook_refuses_a_gates_twin(self):
        githooks.install(self.tmp)
        spec = self.tmp / "docs" / "spec"
        spec.mkdir(parents=True)
        (spec / "GATES.md").write_text("# Gates\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "base")
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.tmp, input="x\n", capture_output=True,
                              text=True).stdout.strip()
        git(self.tmp, "update-index", "--add", "--cacheinfo", "100644,%s,docs/spec/gates.md" % blob)
        done = git(self.tmp, "commit", "-m", "x", check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("docs/spec/GATES.md  <->  docs/spec/gates.md", done.stderr)
        self.assertIn("make fix-gates", done.stderr)
        self.assertEqual(git(self.tmp, "commit", "-qm", "x", "--no-verify", check=False).returncode, 0, "--no-verify is the escape hatch")

    def test_not_a_checkout(self):
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        self.assertEqual(githooks.install(plain)[0][0], "skip")


if __name__ == "__main__":
    unittest.main()
