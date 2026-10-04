"""Run with: python3 -m unittest discover -s tests"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.py"
SCAN = ROOT / "scripts" / "scan.py"
PLAN = ROOT / "examples" / "CURSOR_PLAN.sample.md"


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(script), *args], cwd=cwd, capture_output=True, text=True
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "My Repo.v2"
        self.repo.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)


class InitTests(Base):
    def test_init_is_idempotent(self):
        (self.repo / ".gitignore").write_text("dist/")
        r = run(INSTALL, "init", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = (self.repo / ".warp/config.yaml").read_text()
        self.assertIn('slackChannel: "warp.my-repo-v2"', cfg)
        self.assertIn('teamsChannel: "warp.my-repo-v2"', cfg)
        self.assertTrue((self.repo / ".cursor/plugins/warp/.cursor-plugin/plugin.json").exists())
        gi = (self.repo / ".gitignore").read_text()
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        r = run(INSTALL, "init", "--root", ".", cwd=self.repo)
        self.assertIn("Nothing changed", r.stdout)
        self.assertEqual(gi, (self.repo / ".gitignore").read_text())
        self.assertEqual(gi.count(".warp/"), 1)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_existing_config_only_default_channels_change(self):
        (self.repo / ".warp").mkdir()
        cfg = self.repo / ".warp/config.yaml"
        cfg.write_text('maxAgents: 3\nslackChannel: ""\nteamsChannel: "keep-me"\n')
        run(INSTALL, "init", "--root", ".", cwd=self.repo)
        text = cfg.read_text()
        self.assertIn("maxAgents: 3", text)
        self.assertIn('slackChannel: "warp.my-repo-v2"', text)
        self.assertIn('teamsChannel: "keep-me"', text)

    def test_dry_run_writes_nothing(self):
        run(INSTALL, "init", "--root", ".", "--dry-run", cwd=self.repo)
        self.assertEqual(list(self.repo.iterdir()), [])


class UninstallTests(Base):
    def setUp(self):
        super().setUp()
        (self.repo / ".gitignore").write_text("dist/\n")
        run(INSTALL, "init", "--root", ".", cwd=self.repo)

    def test_requires_yes(self):
        r = run(INSTALL, "uninstall", "--root", ".", "--remove-gitignore", cwd=self.repo)
        self.assertIn("Will remove", r.stdout)
        self.assertIn("Nothing deleted", r.stdout)
        self.assertTrue((self.repo / ".warp").exists())
        self.assertTrue((self.repo / ".cursor/plugins/warp").exists())

    def test_remove_and_reinstall(self):
        run(INSTALL, "uninstall", "--root", ".", "--remove-gitignore", "--yes", cwd=self.repo)
        self.assertFalse((self.repo / ".warp").exists())
        self.assertFalse((self.repo / ".cursor").exists())
        self.assertEqual((self.repo / ".gitignore").read_text(), "dist/\n")
        r = run(INSTALL, "init", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.repo / ".warp/config.yaml").exists())

    def test_gitignore_kept_without_flag(self):
        run(INSTALL, "uninstall", "--root", ".", "--yes", cwd=self.repo)
        self.assertIn(".warp/", (self.repo / ".gitignore").read_text())

    def test_hand_written_entry_not_removed(self):
        (self.repo / ".gitignore").write_text(".warp/\n")
        run(INSTALL, "uninstall", "--root", ".", "--remove-gitignore", "--yes", cwd=self.repo)
        self.assertEqual((self.repo / ".gitignore").read_text(), ".warp/\n")


class ScanFolderTests(Base):
    def setUp(self):
        super().setUp()
        for d in ("a/spec", "b/spec", "c/spec"):
            (self.repo / d).mkdir(parents=True)
        for d in ("a/spec", "b/spec"):
            shutil.copy(PLAN, self.repo / d / "CURSOR_PLAN.md")

    def scan(self, *args):
        return run(SCAN, "scan", "--root", ".", *args, cwd=self.repo)

    def test_no_folder_reports_multiple(self):
        r = self.scan()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("plans found in 2 folders: a/spec, b/spec", r.stdout)

    def test_ambiguous_name(self):
        r = self.scan("--folder", "spec")
        self.assertEqual(r.returncode, 3)
        self.assertIn("a/spec", r.stdout)
        self.assertIn("c/spec  no plan files", r.stdout)
        self.assertFalse((self.repo / ".warp/beam.json").exists())

    def test_path_scopes_scan(self):
        r = self.scan("--folder", "b/spec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        found = json.loads((self.repo / ".warp/scan.json").read_text())
        self.assertEqual(found["folder"], "b/spec")
        self.assertEqual(found["plans"], ["b/spec/CURSOR_PLAN.md"])

    def test_single_plan_bearing_match_wins(self):
        shutil.rmtree(self.repo / "b")
        r = self.scan("--folder", "spec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("only a/spec has plan files", r.stdout)

    def test_missing_and_outside(self):
        self.assertNotEqual(self.scan("--folder", "nope").returncode, 0)
        self.assertNotEqual(self.scan("--folder", str(self.tmp)).returncode, 0)

    def test_empty_folder(self):
        self.assertEqual(self.scan("--folder", "c").returncode, 2)


if __name__ == "__main__":
    unittest.main()
