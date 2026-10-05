"""Run with: python3 -m unittest discover -s tests"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check_version  # noqa: E402
import version  # noqa: E402

INSTALL = SCRIPTS / "install.py"
SCAN = SCRIPTS / "scan.py"
BUMP = SCRIPTS / "bump_version.py"
CHECK = SCRIPTS / "check_version.py"
VERSION_PY = SCRIPTS / "version.py"
PLAN = ROOT / "examples" / "CURSOR_PLAN.sample.md"
VER = (ROOT / "VERSION").read_text().strip()


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(script), *args], cwd=cwd, capture_output=True, text=True
    )


class RepoVersionTests(unittest.TestCase):
    def test_version_files_and_changelog_agree(self):
        self.assertEqual(check_version.problems(ROOT), [])
        self.assertEqual(version.read_version_file(ROOT), VER)
        self.assertEqual(version.read_manifest_version(ROOT), VER)
        self.assertIn(f"## {VER}", (ROOT / "CHANGELOG.md").read_text())

    def test_disagreement_and_missing_changelog_fail(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".cursor-plugin").mkdir(parents=True)
        (tmp / "VERSION").write_text("1.3.1\n")
        (tmp / ".cursor-plugin" / "plugin.json").write_text('{"version": "1.3.0"}\n')
        (tmp / "CHANGELOG.md").write_text("# Changelog\n\n## 1.3.0\n\n- old\n")
        found = check_version.problems(tmp)
        self.assertTrue(any("plugin.json" in item for item in found))
        self.assertTrue(any("CHANGELOG" in item for item in found))

    def test_bump_patch_updates_every_place(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".cursor-plugin").mkdir(parents=True)
        (tmp / "VERSION").write_text("1.3.1\n")
        (tmp / ".cursor-plugin" / "plugin.json").write_text('{\n  "name": "warp",\n  "version": "1.3.1"\n}\n')
        (tmp / "CHANGELOG.md").write_text("# Changelog\n\n## 1.3.1\n\n- current\n")
        proc = run(BUMP, "--root", str(tmp), "--note", "A test bump.", cwd=tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((tmp / "VERSION").read_text().strip(), "1.3.2")
        self.assertEqual(json.loads((tmp / ".cursor-plugin" / "plugin.json").read_text())["version"], "1.3.2")
        text = (tmp / "CHANGELOG.md").read_text()
        self.assertTrue(text.startswith("# Changelog\n\n## 1.3.2\n"))
        self.assertIn("A test bump.", text)
        self.assertIn("## 1.3.1", text)
        self.assertEqual(check_version.problems(tmp), [])
        minor = run(BUMP, "--root", str(tmp), "--minor", cwd=tmp)
        self.assertEqual(minor.returncode, 0, minor.stderr)
        self.assertEqual((tmp / "VERSION").read_text().strip(), "1.4.0")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_prints_source_when_nothing_is_installed(self):
        proc = run(VERSION_PY, "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Warp v{VER}", proc.stdout)
        self.assertIn("installed: none", proc.stdout)
        self.assertIn(f"source: {VER}", proc.stdout)

    def test_init_records_version_and_messages_include_it(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/Demo.git"], cwd=self.repo, check=True)
        (self.repo / "spec").mkdir()
        shutil.copy(PLAN, self.repo / "spec" / "CURSOR_PLAN.md")
        proc = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn(f"recorded Warp v{VER} in .warp/version", proc.stdout)
        self.assertEqual((self.repo / ".warp" / "version").read_text().strip(), VER)
        payload = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertIn(f"Warp v{VER}", payload["text"])
        self.assertTrue(payload["text"].startswith("Warp | "))
        scan = run(SCAN, "scan", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
        scanned = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertIn(f"Warp v{VER}", scanned["text"])
        status = run(SCAN, "status", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        self.assertIn(f"Warp v{VER}", status.stdout)
        self.assertIn(f"installed: {VER}", status.stdout)
        self.assertIn(f"Warp v{VER}", (self.repo / ".warp" / "STATUS.md").read_text())
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        again = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("Nothing changed", again.stdout)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_old_project_copy_asks_for_reinstall(self):
        proc = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        plug = self.repo / ".cursor" / "plugins" / "warp"
        (plug / "VERSION").write_text("1.3.0\n")
        manifest = plug / ".cursor-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = "1.3.0"
        manifest.write_text(json.dumps(data, indent=2) + "\n")
        again = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertIn(f"plugin is v1.3.0, repo copy is v{VER}: run /warp-uninstall then /warp-init", again.stdout)
        self.assertEqual((self.repo / ".warp" / "version").read_text().strip(), "1.3.0")
        shown = run(VERSION_PY, "--root", str(self.repo), cwd=self.repo)
        self.assertIn(f"plugin is v1.3.0, repo copy is v{VER}: run /warp-uninstall then /warp-init", shown.stdout)
        self.assertEqual(data["version"], "1.3.0")


class AgainstMainTests(unittest.TestCase):
    def test_this_version_is_newer_than_main(self):
        ref = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"], cwd=ROOT, capture_output=True, text=True
        )
        if ref.returncode != 0:
            self.skipTest("origin/main is not available")
        self.assertEqual(check_version.against(ROOT, "origin/main"), [])
        proc = run(CHECK, "--against", "origin/main", cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
