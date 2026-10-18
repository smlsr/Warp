"""1.7.0: start and resume clean what a killed process left: orphaned .tmp files and stale git locks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fresh_start  # noqa: E402

OLD = 30 * 60


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def age(path: Path, seconds: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x")
    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


class FreshStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "Core"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".warp").mkdir()

    def test_old_warp_tmp_files_go_and_young_ones_and_worktrees_stay(self):
        old = age(self.repo / ".warp" / "beam.json.abc123.tmp", OLD)
        old_rev = age(self.repo / ".warp" / ".beam-revs" / "beam.5.k.json.gz.x.tmp", OLD)
        young = age(self.repo / ".warp" / "beam.json.def456.tmp", 30)
        code = age(self.repo / ".warp" / "worktrees" / "T-1" / "cache.tmp", OLD)
        lines = fresh_start.clean(self.repo, running=lambda: False)
        self.assertIn("start: removed 2 leftover .tmp files in .warp (older than 10m)", lines)
        self.assertFalse(old.exists() or old_rev.exists())
        self.assertTrue(young.exists() and code.exists())

    def test_warp_lock_files_are_never_touched(self):
        lock = age(self.repo / ".warp" / ".listener.lock", OLD)
        self.assertEqual(fresh_start.clean(self.repo, running=lambda: False), [])
        self.assertTrue(lock.exists())

    def test_a_stale_git_lock_goes_when_no_git_runs(self):
        index = age(self.repo / ".git" / "index.lock", OLD)
        ref = age(self.repo / ".git" / "refs" / "heads" / "main.lock", OLD)
        maint = age(self.repo / ".git" / "objects" / "maintenance.lock", OLD)
        lines = fresh_start.clean(self.repo, running=lambda: False)
        self.assertIn("start: removed stale Core/.git/index.lock (30m old, no git running)", lines)
        self.assertTrue(any("Core/.git/refs/heads/main.lock" in line for line in lines), lines)
        self.assertFalse(index.exists() or ref.exists() or maint.exists())
        git(self.repo, "status")  # git works again

    def test_a_young_lock_or_a_running_git_leaves_it(self):
        index = age(self.repo / ".git" / "index.lock", 60)
        self.assertIn("start: Core/.git/index.lock is 1m old; a git command may be using it. Left in place.",
                      fresh_start.clean(self.repo, running=lambda: False))
        age(index, OLD)
        lines = fresh_start.clean(self.repo, running=lambda: True)
        self.assertIn("start: Core/.git/index.lock is 30m old, but a git process is running. Left in place; remove it once git is done.", lines)
        self.assertIn("cannot tell whether git is running", fresh_start.clean(self.repo, running=lambda: None)[0])
        self.assertTrue(index.exists())

    def test_sibling_clones_and_dry_run(self):
        ui = self.tmp / "Core-UI"
        ui.mkdir()
        git(ui, "init", "-q", "-b", "main")
        lock = age(ui / ".git" / "index.lock", OLD)
        lines = fresh_start.clean(self.repo, [ui], dry=True, running=lambda: False)
        self.assertIn("start: would remove stale Core-UI/.git/index.lock (30m old, no git running)", lines)
        self.assertTrue(lock.exists())
        fresh_start.clean(self.repo, [ui], running=lambda: False)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
