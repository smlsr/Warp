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
        self.assertIn('slackChannel: "warp-my-repo-v2"', cfg)
        self.assertIn('teamsChannel: "warp-my-repo-v2"', cfg)
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
        self.assertIn('slackChannel: "warp-my-repo-v2"', text)
        self.assertIn('teamsChannel: "keep-me"', text)

    def test_channel_is_valid_and_limited(self):
        long = self.tmp / ("Very Long.Repo Name!" + "x" * 80)
        long.mkdir()
        run(INSTALL, "init", "--root", ".", cwd=long)
        cfg = (long / ".warp/config.yaml").read_text()
        import re

        names = re.findall(r'^(?:slack|teams)Channel: "([^"]*)"', cfg, re.M)
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertRegex(n, r"^warp-[a-z0-9_-]+$")
            self.assertLessEqual(len(n), 50)
        self.assertTrue(names[0].startswith("warp-very-long-repo-name-x"))

    def test_invalid_channel_override_rejected(self):
        r = run(INSTALL, "init", "--root", ".", "--channel", "warp.bad", cwd=self.repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.repo.iterdir()), [])

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


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True, capture_output=True
    )


def payload(repo):
    return json.loads((repo / ".warp/notify-post.json").read_text())


class NotifyTests(Base):
    def setUp(self):
        super().setUp()
        git(self.repo, "init", "-q", "-b", "feat/x")
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/Demo-App.git")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "x")
        (self.repo / "spec").mkdir()
        shutil.copy(PLAN, self.repo / "spec/CURSOR_PLAN.md")

    def init(self):
        return run(INSTALL, "init", "--root", ".", cwd=self.repo)

    def set_cfg(self, **kv):
        import re

        f = self.repo / ".warp/config.yaml"
        text = f.read_text()
        for k, v in kv.items():
            text = re.sub(rf"^{k}:.*$", f'{k}: "{v}"', text, flags=re.M)
        f.write_text(text)

    def test_init_message(self):
        r = self.init()
        self.assertIn("herald: post", r.stdout)
        p = payload(self.repo)
        self.assertEqual(p["action"], "post")
        self.assertIn("Cursor repo Demo-App was initialized with Warp", p["text"])
        self.assertIn("Channel: warp-demo-app", p["text"])
        self.assertEqual({t["messenger"] for t in p["targets"]}, {"slack", "teams"})

    def test_second_init_sends_nothing_new(self):
        self.init()
        (self.repo / ".warp/notify-post.json").unlink()
        r = self.init()
        self.assertNotIn("herald", r.stdout)
        self.assertFalse((self.repo / ".warp/notify-post.json").exists())

    def test_scan_message_has_links(self):
        self.init()
        r = run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        p = payload(self.repo)
        self.assertEqual(p["kind"], "scan")
        self.assertIn("Tickets: 7", p["text"])
        self.assertIn("https://github.com/acme/Demo-App/blob/feat/x/spec/CURSOR_PLAN.md", p["text"])

    def test_relative_paths_without_remote(self):
        git(self.repo, "remote", "remove", "origin")
        self.init()
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        text = payload(self.repo)["text"]
        self.assertIn("spec/CURSOR_PLAN.md", text)
        self.assertNotIn("http", text)

    def test_messenger_respected(self):
        self.init()
        self.set_cfg(messenger="slack")
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual([t["messenger"] for t in payload(self.repo)["targets"]], ["slack"])

    def test_no_channel_goes_to_outbox_and_scan_succeeds(self):
        self.init()
        self.set_cfg(slackChannel="", teamsChannel="")
        r = run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(payload(self.repo)["action"], "outbox")
        self.assertIn("slackChannel is empty", r.stdout)
        self.assertIn("Warp scan finished", (self.repo / ".warp/outbox.md").read_text())

    def test_no_config_at_all_is_soft(self):
        r = run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(payload(self.repo)["action"], "outbox")

    def test_quiet_posts_nothing(self):
        self.init()
        self.set_cfg(notify="quiet")
        r = run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(payload(self.repo)["action"], "skip")
        self.assertFalse((self.repo / ".warp/outbox.md").exists())
        self.assertIn("quiet", r.stdout)

    def test_failed_scan_posts_nothing(self):
        (self.repo / "spec/CURSOR_PLAN.md").unlink()
        r = run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.repo / ".warp/notify-post.json").exists())

    def test_outbox_subcommand(self):
        self.init()
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        r = run(ROOT / "scripts" / "notify.py", "outbox", "--root", ".", cwd=self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Warp scan finished", (self.repo / ".warp/outbox.md").read_text())


if __name__ == "__main__":
    unittest.main()
