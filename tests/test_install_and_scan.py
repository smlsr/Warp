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
        self.assertIn('slackChannel: "warp"', cfg)
        self.assertIn('teamsChannel: "warp"', cfg)
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
        self.assertIn('slackChannel: "warp"', text)
        self.assertIn('teamsChannel: "keep-me"', text)

    def test_channel_is_shared_not_per_repo(self):
        run(INSTALL, "init", "--root", ".", cwd=self.repo)
        cfg = (self.repo / ".warp/config.yaml").read_text()
        self.assertNotIn("my-repo", cfg)

    def test_custom_channel_override_and_existing_kept(self):
        run(INSTALL, "init", "--root", ".", "--channel", "eng-builds", cwd=self.repo)
        cfg = (self.repo / ".warp/config.yaml").read_text()
        self.assertIn('slackChannel: "eng-builds"', cfg)
        self.assertIn('teamsChannel: "eng-builds"', cfg)
        run(INSTALL, "init", "--root", ".", cwd=self.repo)
        self.assertIn('slackChannel: "eng-builds"', (self.repo / ".warp/config.yaml").read_text())

    def test_invalid_channel_override_rejected(self):
        for bad in ('bad"name', "has space", "dot.name", "", "x" * 81):
            r = run(INSTALL, "init", "--root", ".", "--channel", bad, cwd=self.repo)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(list(self.repo.iterdir()), [])

    def test_channel_override_is_lowercased(self):
        r = run(INSTALL, "init", "--root", ".", "--channel", "#Eng-Builds", cwd=self.repo)
        self.assertIn("normalized to 'eng-builds'", r.stdout)
        self.assertIn('slackChannel: "eng-builds"', (self.repo / ".warp/config.yaml").read_text())

    def test_old_Warp_default_is_migrated(self):
        (self.repo / ".warp").mkdir()
        cfg = self.repo / ".warp/config.yaml"
        cfg.write_text('slackChannel: "Warp"\nteamsChannel: "Warp"\n')
        run(INSTALL, "init", "--root", ".", cwd=self.repo)
        text = cfg.read_text()
        self.assertIn('slackChannel: "warp"', text)
        self.assertIn('teamsChannel: "warp"', text)

    def test_custom_uppercase_slack_channel_warned_not_changed(self):
        (self.repo / ".warp").mkdir()
        cfg = self.repo / ".warp/config.yaml"
        cfg.write_text('slackChannel: "Eng-Team"\nteamsChannel: "Eng Team"\n')
        r = run(INSTALL, "init", "--root", ".", cwd=self.repo)
        self.assertIn("[warn] slackChannel 'Eng-Team' has uppercase", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(cfg.read_text(), 'slackChannel: "Eng-Team"\nteamsChannel: "Eng Team"\n')

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


def fmt_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import herald_fmt

    return herald_fmt


def payload(repo):
    return json.loads((repo / ".warp/notify-post.json").read_text())


class NotifyTests(Base):
    def setUp(self):
        super().setUp()
        git(self.repo, "init", "-q", "-b", "feat/x")
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/Demo-App.git")
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
        self.assertIn("Channel: warp", p["text"])
        self.assertEqual({t["messenger"] for t in p["targets"]}, {"slack", "teams"})

    def test_header_names_repo_and_workspace_in_every_view(self):
        self.init()
        p = payload(self.repo)
        head = "Warp | Demo-App / My Repo.v2"
        self.assertEqual(p["header"], head)
        self.assertTrue(p["text"].startswith(head))
        self.assertEqual(p["slack"]["blocks"][0]["text"]["text"], head)
        self.assertEqual(p["slack"]["blocks"][0]["type"], "header")
        self.assertIn(head, p["slack"]["text"])
        self.assertTrue(p["teams"]["markdown"].startswith("**Warp | Demo-App / My Repo.v2**".replace("_", "\\_")))
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        self.assertEqual(payload(self.repo)["header"], head)

    def test_slack_view_uses_mrkdwn_links_teams_uses_markdown(self):
        self.init()
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        p = payload(self.repo)
        url = "https://github.com/acme/Demo-App/blob/feat/x/spec/CURSOR_PLAN.md"
        section = p["slack"]["blocks"][1]["text"]["text"]
        self.assertIn(f"<{url}|Plan used: spec/CURSOR_PLAN.md>", section)
        self.assertIn("*Tickets:* 7", section)
        self.assertIn(f"]({url})", p["teams"]["markdown"])
        self.assertIn("**Tickets:** 7", p["teams"]["markdown"])

    def test_project_from_config_then_jira_then_folder(self):
        self.init()
        self.set_cfg(jiraProject="HOS")
        fmt = fmt_module()
        self.assertEqual(fmt.header(self.repo), "Warp | Demo-App / HOS")
        self.set_cfg(projectName="Ops Platform")
        self.assertEqual(fmt.header(self.repo), "Warp | Demo-App / Ops Platform")

    def test_header_collapses_when_project_equals_repo(self):
        other = self.tmp / "Demo-App"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "remote", "add", "origin", "git@github.com:acme/Demo-App.git")
        self.assertEqual(fmt_module().header(other), "Warp | Demo-App")

    def test_special_characters_are_escaped(self):
        fmt = fmt_module()
        out = fmt.render("Warp | a_b / c*d", "T<1>&", facts=[("k", "x < y & z")])
        self.assertIn("T&lt;1&gt;&amp;", out["slack"]["blocks"][1]["text"]["text"])
        self.assertIn("a\\_b", out["teams"]["markdown"])

    def test_formatter_cli_for_other_herald_messages(self):
        r = run(
            ROOT / "scripts" / "herald_fmt.py", "--title", "Alarm L-01", "--fact", "reason=bugbot-failed",
            "--link", "PR=https://host/pr/1", "--footer", "warp:retry L-01", cwd=self.repo,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["text"].startswith("Warp | "))
        self.assertIn("<https://host/pr/1|PR>", out["slack"]["blocks"][1]["text"]["text"])

    def test_status_post_uses_the_same_header(self):
        self.init()
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        r = run(ROOT / "scripts" / "status_post.py", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = json.loads((self.repo / ".warp/status-post.json").read_text())
        self.assertTrue(body["text"].startswith("Warp | Demo-App / My Repo.v2"))
        self.assertIn("slack", body)
        self.assertIn("teams", body)

    def test_invite_uses_git_email_and_skips_bots(self):
        git(self.repo, "config", "user.email", "dev@acme.com")
        r = self.init()
        self.assertEqual(payload(self.repo)["invite"], "dev@acme.com")
        self.assertIn("add dev@acme.com to the channel", r.stdout)
        self.assertIn("Do not create the channel", r.stdout)
        shutil.rmtree(self.repo / ".warp")
        for bad in ("cursoragent@cursor.com", "1+me@users.noreply.github.com"):
            git(self.repo, "config", "user.email", bad)
            shutil.rmtree(self.repo / ".warp", ignore_errors=True)
            r = self.init()
            self.assertIsNone(payload(self.repo)["invite"])
            self.assertEqual(r.returncode, 0)
            self.assertIn("nobody to add to the channel", r.stdout)

    def test_slack_uppercase_lowercased_when_posting_teams_kept(self):
        self.init()
        self.set_cfg(slackChannel="Eng-Team", teamsChannel="Eng Team")
        run(SCAN, "scan", "--root", ".", cwd=self.repo)
        p = payload(self.repo)
        chans = {t["messenger"]: t["channel"] for t in p["targets"]}
        self.assertEqual(chans, {"slack": "eng-team", "teams": "Eng Team"})
        self.assertTrue(any("lowercase" in n for n in p["notes"]))

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
        self.assertIn("Scan finished", (self.repo / ".warp/outbox.md").read_text())

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
        self.assertIn("Scan finished", (self.repo / ".warp/outbox.md").read_text())


if __name__ == "__main__":
    unittest.main()
