"""Named runs: one repo, several Warp runs, each in its own folder."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import orchestrator  # noqa: E402
import runs  # noqa: E402
import scan  # noqa: E402
import state_commit  # noqa: E402
import ticket_state  # noqa: E402


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=warp@example.com", "-c", "user.name=Warp Dev", "-c", "maintenance.auto=false", *args],
        capture_output=True,
        text=True,
    )


def ticket(tid, status="queued", **extra):
    row = {
        "id": tid,
        "status": status,
        "summary": "ship %s" % tid,
        "size": "M",
        "module": "app",
        "hours": 1,
        "tokens": 1200,
        "minutes": 7,
        "attempts": 1,
        "locks": ["src/%s" % tid],
        "usage": {"cost": 0.5, "tokensIn": 1000, "tokensOut": 200},
        "pr": {"url": None},
    }
    row.update(extra)
    return row


CONFIG = """stateDir: .warp
model: claude-sonnet-5-5-high
runner: local
launch: worktree
worktreeRoot: .warp/worktrees
maxAgents: 6
slackChannel: "warp"
scanFolder: "HOS/UI/Admin"
apiKey: sk-should-not-be-copied
"""


class RepoCase(unittest.TestCase):
    """A repo with a bare origin, and a way to make a second checkout of it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.repo = self.clone_dir("one")
        git(self.repo, "init", "-b", "main")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / "README.md").write_text("hello\n")
        (self.repo / ".gitignore").write_text(".warp/config.yaml\n.warp/worktrees/\n")
        git(self.repo, "add", "-A")
        self.assertEqual(git(self.repo, "commit", "-m", "init").returncode, 0)
        self.assertEqual(git(self.repo, "push", "-u", "origin", "main").returncode, 0)

    def clone_dir(self, name):
        path = self.tmp / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def second_checkout(self, name="two"):
        path = self.tmp / name
        done = subprocess.run(["git", "clone", "-q", str(self.origin), str(path)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        git(path, "config", "user.email", "warp@example.com")
        git(path, "config", "user.name", "Warp Dev")
        return path

    def write_beam(self, repo, tickets, **extra):
        warp = repo / ".warp"
        warp.mkdir(parents=True, exist_ok=True)
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [{"key": "G0", "members": list(tickets)[:1], "status": "pending"}],
            "runState": "running",
            "paused": False,
            "config": {"baseBranch": "main", "runner": "local", "maxAgents": 4},
        }
        body.update(extra)
        (warp / "beam.json").write_text(json.dumps(body, indent=2) + "\n")
        (warp / "journal.jsonl").write_text(json.dumps({"type": "running"}) + "\n")

    def write_config(self, repo, text=CONFIG, run=None):
        warp = repo / ".warp"
        warp.mkdir(parents=True, exist_ok=True)
        body = text + ("run: %s\n" % run if run else "")
        (warp / "config.yaml").write_text(body)

    def show(self, ref, rel):
        done = git(self.repo, "show", "%s:%s" % (ref, rel))
        return done.stdout if done.returncode == 0 else None

    def run_published(self, repo, name, tickets, state="running", **extra):
        """Make `repo` a checkout of run `name` and publish it to origin."""
        self.write_config(repo, run=name)
        self.write_beam(repo, tickets, runState=state, **extra)
        (repo / ".warp" / "scan.json").write_text(json.dumps({"folder": "HOS/UI/Admin", "scannedAt": "2026-10-12T12:00:00Z", "root": str(repo)}))
        code = state_commit.publish_base(repo, ".warp/beam.json", force=True)
        self.assertEqual(code, 0)


class NameTests(unittest.TestCase):
    def test_valid_names(self):
        for name in ("core", "admin", "ui", "admin-2", "a1"):
            self.assertTrue(runs.valid_name(name)[0], name)

    def test_invalid_names(self):
        for name in ("", "Core", "has space", "-x", "g5", "G5", "runs", "all", "x" * 40, "a/b"):
            self.assertFalse(runs.valid_name(name)[0], name)

    def test_git_rel_maps_only_a_named_run(self):
        self.assertEqual(runs.git_rel("", ".warp/beam.json"), ".warp/beam.json")
        self.assertEqual(runs.git_rel("admin", ".warp/beam.json"), ".warp/runs/admin/beam.json")
        self.assertEqual(runs.git_rel("admin", ".warp/tickets/T-1/state.json"), ".warp/runs/admin/tickets/T-1/state.json")
        self.assertEqual(runs.git_rel("admin", "src/app.py"), "src/app.py")

    def test_branch_names_carry_the_run(self):
        t = {"id": "P-1", "jiraKey": "WAR-1"}
        self.assertEqual(orchestrator.branch_name(t, {}), "warp/P-1-WAR-1")
        self.assertEqual(orchestrator.branch_name(t, {"run": "admin"}), "warp/admin/P-1-WAR-1")
        self.assertEqual(orchestrator.branch_name({"id": "P-2"}, {"run": "ui"}), "warp/ui/P-2")
        self.assertEqual(orchestrator.branch_name({"id": "P-3", "branch": "keep/me"}, {"run": "ui"}), "keep/me")


class ConfigTextTests(unittest.TestCase):
    def test_set_and_read_key(self):
        text = "a: 1\n# run: old\nrun: core\n"
        self.assertEqual(runs.read_key(text, "run"), "core")
        self.assertEqual(runs.set_key(text, "run", "admin"), "a: 1\n# run: old\nrun: admin\n")
        self.assertEqual(runs.set_key("a: 1\n", "run", "ui"), "a: 1\nrun: ui\n")
        self.assertEqual(runs.read_key('scanFolder: "HOS/UI"\n', "scanFolder"), "HOS/UI")
        self.assertEqual(runs.read_key('scanFolder: ""\n', "scanFolder"), "")
        self.assertIsNone(runs.read_key("a: 1\n", "scanFolder"))

    def test_mirror_drops_machine_keys_and_secrets(self):
        out = runs.mirror_config(CONFIG + "run: admin\nslackWebhook: https://hooks.slack.com/services/T/B/x\n")
        self.assertIn("maxAgents: 6", out)
        self.assertIn('scanFolder: "HOS/UI/Admin"', out)
        for gone in ("runner:", "launch:", "worktreeRoot:", "stateDir:", "run:", "apiKey", "hooks.slack.com"):
            self.assertNotIn(gone, out)

    def test_overlay_keeps_machine_keys_here(self):
        mirror = runs.mirror_config(CONFIG)
        local = "runner: cloud\nmaxAgents: 18\nscanFolder: \"\"\n"
        text, changed, skipped = runs.overlay_config(local, mirror)
        self.assertIn("runner: cloud", text)
        self.assertIn("maxAgents: 6", text)
        self.assertIn('scanFolder: "HOS/UI/Admin"', text)
        self.assertIn("maxAgents", changed)
        self.assertEqual(skipped, [])


class CurrentRunTests(RepoCase):
    def test_precedence(self):
        self.assertEqual(runs.current(self.repo), "")
        self.write_config(self.repo, run="admin")
        self.assertEqual(runs.current(self.repo), "admin")
        self.assertEqual(runs.current(self.repo, explicit="ui"), "ui")
        with mock.patch.dict(os.environ, {"WARP_RUN": "core"}):
            self.assertEqual(runs.current(self.repo), "core")
        self.assertEqual(runs.current(self.repo, branch="warp/ui/P-1-WAR-1", explicit=""), "admin")

    def test_branch_finds_the_run_when_there_is_no_config(self):
        other = self.second_checkout()
        self.assertEqual(runs.current(other, branch="warp/ui/P-1-WAR-1"), "ui")
        self.assertEqual(runs.current(other, branch="warp/P-1-WAR-1"), "")
        git(other, "checkout", "-q", "-b", "warp/admin/P-9")
        self.assertEqual(runs.current(other), "admin")

    def test_beam_names_its_run(self):
        self.assertEqual(runs.current(self.repo, {"run": "ui"}), "ui")


class PublishTests(RepoCase):
    def test_named_run_publishes_to_its_own_folder_only(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1", "merged"), "P-2": ticket("P-2")})
        git(self.repo, "fetch", "-q", "origin")
        tree = git(self.repo, "ls-tree", "-r", "--name-only", "origin/main").stdout.split()
        folder = [p for p in tree if p.startswith(".warp/")]
        self.assertTrue(folder)
        self.assertTrue(all(p.startswith(".warp/runs/admin/") for p in folder), folder)
        for name in ("beam.json", "journal.jsonl", "scan.json", "stats.json", "config.yaml"):
            self.assertIn(".warp/runs/admin/%s" % name, tree)
        self.assertNotIn(".warp/beam.json", tree)
        self.assertNotIn(".warp/config.yaml", tree)

    def test_published_config_has_no_machine_keys_or_secrets(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        text = self.show("HEAD", ".warp/runs/admin/config.yaml")
        self.assertIn("maxAgents: 6", text)
        self.assertIn('scanFolder: "HOS/UI/Admin"', text)
        for gone in ("runner", "worktreeRoot", "apiKey", "sk-should"):
            self.assertNotIn(gone, text)

    def test_stats_keep_the_numbers_the_beam_strips(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1", "merged"), "P-2": ticket("P-2")})
        beam = json.loads(self.show("HEAD", ".warp/runs/admin/beam.json"))
        self.assertEqual(beam["run"], "admin")
        self.assertNotIn("tokens", beam["tickets"]["P-1"])
        stats = json.loads(self.show("HEAD", ".warp/runs/admin/stats.json"))
        self.assertEqual(stats["totals"]["tokens"], 2400)
        self.assertEqual(stats["totals"]["cost"], 1.0)
        self.assertEqual(stats["tickets"]["P-1"]["status"], "merged")
        self.assertEqual(stats["byStatus"], {"merged": 1, "queued": 1})

    def test_scan_root_path_is_not_published(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        scan_text = self.show("HEAD", ".warp/runs/admin/scan.json")
        self.assertNotIn(str(self.repo), scan_text)
        self.assertEqual(json.loads(scan_text)["folder"], "HOS/UI/Admin")

    def test_ticket_state_and_log_are_published_with_the_run(self):
        self.write_config(self.repo, run="admin")
        self.write_beam(self.repo, {"P-1": ticket("P-1", "merged")})
        folder = self.repo / ".warp" / "tickets" / "P-1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": "P-1", "state": "merged"}))
        (folder / "log.jsonl").write_text(json.dumps({"event": "merged"}) + "\n")
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json", force=True), 0)
        self.assertIn("merged", self.show("HEAD", ".warp/runs/admin/tickets/P-1/state.json"))
        self.assertIn("merged", self.show("HEAD", ".warp/runs/admin/tickets/P-1/log.jsonl"))

    def test_unnamed_run_still_publishes_at_the_old_paths(self):
        self.write_config(self.repo)
        self.write_beam(self.repo, {"P-1": ticket("P-1")})
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json"), 0)
        tree = git(self.repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        self.assertIn(".warp/beam.json", tree)
        self.assertFalse([p for p in tree if p.startswith(".warp/runs/")])

    def test_two_runs_never_touch_each_others_files(self):
        self.run_published(self.repo, "core", {"C-1": ticket("C-1", "merged")})
        other = self.second_checkout()
        git(other, "pull", "-q", "origin", "main")
        self.run_published(other, "admin", {"A-1": ticket("A-1")})
        git(self.repo, "pull", "-q", "origin", "main")
        core = json.loads(self.show("HEAD", ".warp/runs/core/beam.json"))
        admin = json.loads(self.show("HEAD", ".warp/runs/admin/beam.json"))
        self.assertEqual(list(core["tickets"]), ["C-1"])
        self.assertEqual(list(admin["tickets"]), ["A-1"])
        self.assertEqual(runs.list_runs(self.repo, "HEAD"), ["admin", "core"])

    def test_publish_is_skipped_until_something_changes(self):
        self.write_config(self.repo, run="admin")
        self.write_beam(self.repo, {"P-1": ticket("P-1")})
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json"), 0)
        first = git(self.repo, "rev-parse", "HEAD").stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json"), 0)
        self.assertIn("is current", buf.getvalue())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout, first)
        self.write_beam(self.repo, {"P-1": ticket("P-1", "merged")})
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json"), 0)
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD").stdout, first)

    def test_live_files_are_kept_out_of_git_status(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        status = git(self.repo, "status", "--porcelain").stdout
        self.assertNotIn(".warp/beam.json", status)
        self.assertNotIn("runs/admin", status)

    def test_publish_when_another_branch_is_checked_out(self):
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.write_config(self.repo, run="admin")
        self.write_beam(self.repo, {"P-1": ticket("P-1")})
        self.assertEqual(state_commit.publish_base(self.repo, ".warp/beam.json", force=True), 0)
        self.assertIsNotNone(self.show("main", ".warp/runs/admin/beam.json"))
        self.assertIsNone(self.show("feature", ".warp/runs/admin/beam.json"))


class TakeUpTests(RepoCase):
    def test_new_name_is_a_new_run(self):
        self.write_config(self.repo)
        code, lines = runs.take_up(self.repo, "admin")
        self.assertEqual(code, 0)
        self.assertIn("run: admin is new.", lines[0])
        self.assertIn("next: /warp-scan [folder]", lines)
        self.assertEqual(runs.read_run(self.repo), "admin")

    def test_bad_name_is_refused(self):
        code, lines = runs.take_up(self.repo, "G3")
        self.assertEqual(code, 2)
        self.assertIn("looks like a gate", lines[0])

    def test_existing_run_prints_the_notice_and_restores_state_and_config(self):
        self.run_published(
            self.repo,
            "admin",
            {"P-1": ticket("P-1", "merged"), "P-2": ticket("P-2", "coding"), "P-3": ticket("P-3"), "P-4": ticket("P-4")},
            state="stopped",
        )
        fresh = self.second_checkout()
        self.write_config(fresh, "runner: cloud\nmaxAgents: 18\nscanFolder: \"\"\n")
        code, lines = runs.take_up(fresh, "admin")
        text = "\n".join(lines)
        self.assertEqual(code, 0, text)
        self.assertIn("run: admin already exists on origin/main. You are taking up a previous run.", text)
        self.assertIn(".warp/runs/admin/config.yaml, last saved", text)
        self.assertRegex(text, r"last saved just now by \S+")
        self.assertIn("folder: HOS/UI/Admin", text)
        self.assertIn("scan: 2026-10-12, 4 tickets, 1 gates", text)
        self.assertIn("state: stopped, parent last seen", text)
        self.assertIn("progress: 1 merged, 1 open, 2 queued", text)
        self.assertIn("next: /warp-start", text)
        beam = json.loads((fresh / ".warp" / "beam.json").read_text())
        self.assertEqual(sorted(beam["tickets"]), ["P-1", "P-2", "P-3", "P-4"])
        self.assertEqual(beam["run"], "admin")
        cfg = (fresh / ".warp" / "config.yaml").read_text()
        self.assertIn("run: admin", cfg)
        self.assertIn("runner: cloud", cfg)
        self.assertIn("maxAgents: 6", cfg)
        self.assertIn('scanFolder: "HOS/UI/Admin"', cfg)
        self.assertEqual(json.loads((fresh / ".warp" / "scan.json").read_text())["root"], str(fresh))

    def test_a_running_run_warns_that_another_agent_may_still_run_it(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")}, state="running")
        fresh = self.second_checkout()
        code, lines = runs.take_up(fresh, "admin")
        self.assertEqual(code, 0)
        self.assertTrue([l for l in lines if l.startswith("warn: the last parent saved state")], lines)

    def test_an_old_checkpoint_is_not_called_live(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")}, state="running")
        fresh = runs.summary(self.repo, "HEAD", "admin")
        self.assertTrue(fresh["live"])
        later = runs.summary(self.repo, "HEAD", "admin", now=datetime.now(timezone.utc) + timedelta(hours=3))
        self.assertFalse(later["live"])
        self.assertGreater(later["seenMinutes"], runs.LIVE_MINUTES)

    def test_dry_run_writes_nothing(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        fresh = self.second_checkout()
        code, lines = runs.take_up(fresh, "admin", dry=True)
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing written", lines)
        self.assertFalse((fresh / ".warp" / "beam.json").exists())
        self.assertEqual(runs.read_run(fresh), "")

    def test_this_checkout_runs_one_run(self):
        self.write_config(self.repo, run="ui")
        code, lines = runs.take_up(self.repo, "admin")
        self.assertEqual(code, 2)
        self.assertIn("this checkout runs ui", lines[0])
        self.assertEqual(runs.read_run(self.repo), "ui")

    def test_switch_keeps_the_old_files(self):
        self.write_config(self.repo, run="ui")
        self.write_beam(self.repo, {"U-1": ticket("U-1")})
        code, lines = runs.take_up(self.repo, "admin", switch=True)
        self.assertEqual(code, 0, lines)
        self.assertEqual(runs.read_run(self.repo), "admin")
        self.assertTrue((self.repo / ".warp" / "previous-ui" / "beam.json").is_file())
        self.assertFalse((self.repo / ".warp" / "beam.json").exists())

    def test_an_unnamed_run_with_tickets_needs_adopt(self):
        self.write_config(self.repo)
        self.write_beam(self.repo, {"P-1": ticket("P-1")})
        code, lines = runs.take_up(self.repo, "admin")
        self.assertEqual(code, 2)
        self.assertIn("unnamed run with 1 tickets", lines[0])
        code, lines = runs.take_up(self.repo, "admin", adopt=True)
        self.assertEqual(code, 0, lines)
        self.assertEqual(runs.read_run(self.repo), "admin")
        beam = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertEqual(beam["run"], "admin")
        self.assertEqual(sorted(beam["tickets"]), ["P-1"])

    def test_adopt_will_not_overwrite_a_published_run(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        other = self.second_checkout()
        self.write_config(other)
        self.write_beam(other, {"X-1": ticket("X-1")})
        code, lines = runs.take_up(other, "admin", adopt=True)
        self.assertEqual(code, 2)
        self.assertIn("would overwrite", lines[0])

    def test_same_run_with_live_state_keeps_it(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1")})
        code, lines = runs.take_up(self.repo, "admin")
        self.assertEqual(code, 0)
        self.assertTrue([l for l in lines if "already has admin's live state" in l], lines)


class StartNeedsScanTests(RepoCase):
    def test_named_run_without_tickets_cannot_start(self):
        self.write_config(self.repo, run="admin")
        beam = self.repo / ".warp" / "beam.json"
        self.assertIn("admin has no scan", runs.start_blocker(self.repo, beam))
        self.write_beam(self.repo, {})
        self.assertIn("admin has no scan", runs.start_blocker(self.repo, beam))
        self.write_beam(self.repo, {"P-1": ticket("P-1")})
        self.assertIsNone(runs.start_blocker(self.repo, beam))

    def test_unnamed_run_is_not_blocked_here(self):
        self.write_config(self.repo)
        self.assertIsNone(runs.start_blocker(self.repo, self.repo / ".warp" / "beam.json"))


class ScanFolderTests(RepoCase):
    def make_plan(self, folder, ids):
        d = self.repo / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "schedule.json").write_text(
            json.dumps({"tickets": [{"id": i, "summary": "s %s" % i, "deps": [], "locks": ["src/%s" % i], "size": "S"} for i in ids], "gates": []})
        )

    def scan_cli(self, *args):
        buf = io.StringIO()
        code = 0
        argv = ["scan.py", "scan", "--root", str(self.repo), "--out", str(self.repo / ".warp" / "beam.json"), *args]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            try:
                scan.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    buf.write(e.code)
        return code, buf.getvalue()

    def test_folder_is_saved_reused_and_cleared(self):
        self.write_config(self.repo, run="admin")
        self.make_plan("HOS/UI/Admin", ["A-1", "A-2"])
        self.make_plan("HOS/Core", ["C-1"])
        code, out = self.scan_cli("--folder", "HOS/UI/Admin")
        self.assertEqual(code, 0, out)
        self.assertEqual(runs.read_scan_folder(self.repo), "HOS/UI/Admin")
        code, out = self.scan_cli()
        self.assertEqual(code, 0, out)
        self.assertIn("folder: HOS/UI/Admin (saved by an earlier scan)", out)
        beam = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertEqual(sorted(beam["tickets"]), ["A-1", "A-2"])
        code, out = self.scan_cli("--folder", "/")
        self.assertEqual(code, 0, out)
        self.assertIn("folder: was HOS/UI/Admin, now whole repo", out)
        self.assertEqual(runs.read_scan_folder(self.repo), "")
        beam = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertEqual(beam["run"], "admin")

    def test_scan_publishes_the_run(self):
        self.write_config(self.repo, run="admin")
        self.make_plan("HOS/UI/Admin", ["A-1"])
        code, out = self.scan_cli("--folder", "HOS/UI/Admin")
        self.assertEqual(code, 0, out)
        self.assertIn("A-1", self.show("HEAD", ".warp/runs/admin/beam.json"))
        self.assertIn("HOS/UI/Admin", self.show("HEAD", ".warp/runs/admin/config.yaml"))
        self.assertIsNotNone(self.show("HEAD", ".warp/runs/admin/scan.json"))

    def test_a_ticket_another_run_owns_stops_the_scan(self):
        self.run_published(self.repo, "core", {"A-2": ticket("A-2", "merged"), "C-1": ticket("C-1")})
        other = self.second_checkout()
        git(other, "pull", "-q", "origin", "main")
        self.repo = other
        self.write_config(other, run="admin")
        self.make_plan("HOS/UI/Admin", ["A-1", "A-2"])
        code, out = self.scan_cli("--folder", "HOS/UI/Admin")
        self.assertEqual(code, 4, out)
        self.assertIn("1 ticket ids are already owned by another run", out)
        self.assertIn("core: A-2", out)
        self.assertIn("refuse: nothing was written", out)
        self.assertFalse((other / ".warp" / "beam.json").exists())

    def test_skip_owned_leaves_them_out(self):
        self.run_published(self.repo, "core", {"A-2": ticket("A-2", "merged")})
        other = self.second_checkout()
        git(other, "pull", "-q", "origin", "main")
        self.repo = other
        self.write_config(other, run="admin")
        self.make_plan("HOS/UI/Admin", ["A-1", "A-2"])
        code, out = self.scan_cli("--folder", "HOS/UI/Admin", "--skip-owned")
        self.assertEqual(code, 0, out)
        self.assertIn("skipped: 1 tickets owned by another run", out)
        beam = json.loads((other / ".warp" / "beam.json").read_text())
        self.assertEqual(sorted(beam["tickets"]), ["A-1"])

    def test_an_unnamed_run_does_not_check_owners(self):
        self.run_published(self.repo, "core", {"A-1": ticket("A-1")})
        other = self.second_checkout()
        git(other, "pull", "-q", "origin", "main")
        self.repo = other
        self.write_config(other, CONFIG.replace('scanFolder: "HOS/UI/Admin"\n', ""))
        self.make_plan("plans", ["A-1"])
        code, out = self.scan_cli()
        self.assertEqual(code, 0, out)


class TicketBranchTests(RepoCase):
    def test_ticket_branch_carries_the_run_folder_and_a_run_branch_name(self):
        self.write_config(self.repo, run="admin")
        t = ticket("P-1", "claimed", agent="a1", jiraKey="WAR-1")
        self.write_beam(self.repo, {"P-1": t}, config={"baseBranch": "main", "run": "admin"})
        code = state_commit.publish_ticket(self.repo, ".warp/beam.json", "P-1", push=True)
        self.assertEqual(code, 0)
        git(self.repo, "fetch", "-q", "origin")
        branch = "origin/warp/admin/P-1-WAR-1"
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", branch).returncode, 0)
        names = git(self.repo, "ls-tree", "-r", "--name-only", branch).stdout.split()
        self.assertIn(".warp/runs/admin/beam.json", names)
        self.assertNotIn(".warp/beam.json", names)
        self.assertNotIn(".warp/runs/admin/stats.json", names)

    def test_ticket_state_push_goes_to_the_run_folder(self):
        git(self.repo, "checkout", "-q", "-b", "warp/admin/P-1")
        folder = self.repo / ".warp" / "tickets" / "P-1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": "P-1", "state": "coding"}))
        (folder / "log.jsonl").write_text(json.dumps({"event": "start"}) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ticket_state._push_dir(self.repo, "P-1", "coding")
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("committed: .warp/runs/admin/tickets/P-1", buf.getvalue())
        names = git(self.repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        self.assertIn(".warp/runs/admin/tickets/P-1/state.json", names)
        self.assertNotIn(".warp/tickets/P-1/state.json", names)

    def test_parent_reads_a_ticket_branch_from_the_run_folder(self):
        git(self.repo, "checkout", "-q", "-b", "warp/admin/P-1")
        folder = self.repo / ".warp" / "tickets" / "P-1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": "P-1", "state": "coding"}))
        (folder / "log.jsonl").write_text(json.dumps({"event": "start"}) + "\n")
        self.assertEqual(ticket_state._push_dir(self.repo, "P-1", "coding"), 0)
        git(self.repo, "checkout", "-q", "main")
        self.write_config(self.repo, run="admin")
        git(self.repo, "fetch", "-q", "origin")
        state, rows = ticket_state._default_read(self.repo, "warp/admin/P-1", "P-1")
        self.assertEqual(state["state"], "coding")
        self.assertEqual(rows[0]["event"], "start")

    def test_unnamed_ticket_branch_still_reads_the_old_path(self):
        git(self.repo, "checkout", "-q", "-b", "warp/P-1")
        folder = self.repo / ".warp" / "tickets" / "P-1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": "P-1", "state": "coding"}))
        self.assertEqual(ticket_state._push_dir(self.repo, "P-1", "coding"), 0)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "fetch", "-q", "origin")
        state, _rows = ticket_state._default_read(self.repo, "warp/P-1", "P-1")
        self.assertEqual(state["state"], "coding")


class InitCommandTests(RepoCase):
    def init(self, repo, *args):
        argv = ["install.py", "init", "--root", str(repo), "--no-allow", *args]
        import install

        buf = io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            try:
                install.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    buf.write(e.code)
        return code, buf.getvalue()

    def test_init_with_a_name_writes_it_to_the_config(self):
        code, out = self.init(self.repo, "admin")
        self.assertEqual(code, 0, out)
        self.assertIn("run: admin is new.", out)
        self.assertEqual(runs.read_run(self.repo), "admin")

    def test_init_with_a_bad_name_refuses(self):
        code, out = self.init(self.repo, "G2")
        self.assertNotEqual(code, 0)
        self.assertIn("looks like a gate", out)

    def test_init_without_a_name_lists_the_runs_the_repo_has(self):
        self.run_published(self.repo, "core", {"C-1": ticket("C-1")})
        fresh = self.second_checkout()
        code, out = self.init(fresh)
        self.assertEqual(code, 0, out)
        self.assertIn("runs on origin/main: core", out)
        self.assertIn("ask: which run is this checkout?", out)

    def test_init_restores_a_published_run(self):
        self.run_published(self.repo, "admin", {"P-1": ticket("P-1", "merged"), "P-2": ticket("P-2")}, state="stopped")
        fresh = self.second_checkout()
        code, out = self.init(fresh, "admin")
        self.assertEqual(code, 0, out)
        self.assertIn("run: admin already exists on origin/main. You are taking up a previous run.", out)
        self.assertIn("progress: 1 merged, 0 open, 1 queued", out)
        self.assertTrue((fresh / ".warp" / "beam.json").is_file())


class RunCliTests(RepoCase):
    def test_list_and_show(self):
        self.run_published(self.repo, "core", {"C-1": ticket("C-1", "merged")})
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["runs.py", "list", "--root", str(self.repo)]), redirect_stdout(buf):
            runs.main()
        self.assertIn("run: core (this checkout) state=running", buf.getvalue())
        self.assertIn("merged=1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
