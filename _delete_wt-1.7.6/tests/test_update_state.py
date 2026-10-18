"""Insurance sync loads main, patches ticket folders, and keeps parent state."""

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

import update_state  # noqa: E402


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=warp@example.com", "-c", "user.name=Warp", "-c", "maintenance.auto=false", *args],
        capture_output=True,
        text=True,
    )


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "warp@example.com")
        git(self.repo, "config", "user.name", "Warp")
        (self.repo / "README.md").write_text("hello\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "init")
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        pushed = git(self.repo, "push", "-u", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.beam_path = self.repo / ".warp" / "beam.json"

    def ticket(self, tid, status="claimed"):
        return {
            "id": tid,
            "status": status,
            "summary": "ship %s" % tid,
            "size": "M",
            "module": "app",
            "hours": 1,
            "tokens": 0,
            "minutes": 0,
            "locks": ["src/%s" % tid],
            "agent": "shuttle-%s" % tid,
            "branch": "warp/%s" % tid,
            "claimedAt": "2026-10-06T11:00:00Z",
            "jira": {"startedAt": "2026-10-06T08:00:00Z"},
            "pr": {"url": "https://example.test/pull/7"},
        }

    def write(self, tickets, **extra):
        warp = self.repo / ".warp"
        warp.mkdir(parents=True, exist_ok=True)
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {"baseBranch": "main", "runner": "local", "maxAgents": 4},
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def test_sync_loads_main_patches_directories_and_keeps_parent_state(self):
        self.write({"T-1": self.ticket("T-1")}, marker="REMOTE-BEAM")
        git(self.repo, "add", ".warp/beam.json")
        committed = git(self.repo, "commit", "-m", "remote beam")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed = git(self.repo, "push", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        state = {
            "id": "T-1",
            "state": "coding",
            "updatedAt": "2026-10-06T12:00:00Z",
            "heartbeatAt": "2026-10-06T12:00:00Z",
        }
        git(self.repo, "checkout", "-B", "warp/T-1")
        warp = self.repo / ".warp"
        (warp / "beam.json").write_text(json.dumps({"marker": "STALE-BEAM"}) + "\n")
        folder = warp / "tickets" / "T-1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps(state) + "\n")
        git(self.repo, "add", ".warp/beam.json", ".warp/tickets")
        ticket_commit = git(self.repo, "commit", "-m", "ticket dir")
        self.assertEqual(ticket_commit.returncode, 0, ticket_commit.stderr)
        git(self.repo, "push", "-u", "origin", "warp/T-1")
        git(self.repo, "checkout", "main")
        claimed = self.ticket("T-2")
        claimed["token"] = "super-secret-token"
        self.write(
            {"T-1": self.ticket("T-1"), "T-2": claimed},
            marker="LOCAL-BEAM",
            runState="paused",
            paused=True,
            pauseReason="hold",
            token="super-secret-token",
        )
        (warp / "config.yaml").write_text("token: super-secret-token\n")
        code = update_state.update_state(self.beam_path)
        self.assertEqual(code, 0)
        shown = git(self.repo, "show", "origin/main:.warp/beam.json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        saved = json.loads(shown.stdout)
        self.assertEqual(saved["marker"], "REMOTE-BEAM")
        self.assertNotIn("STALE-BEAM", shown.stdout)
        self.assertNotIn("LOCAL-BEAM", shown.stdout)
        self.assertNotIn("super-secret-token", shown.stdout)
        self.assertEqual(saved["runState"], "paused")
        self.assertEqual(saved["pauseReason"], "hold")
        self.assertEqual(saved["tickets"]["T-1"]["status"], "coding")
        self.assertEqual(saved["tickets"]["T-2"]["status"], "claimed")
        self.assertNotIn("token", saved["tickets"]["T-2"])
        hidden = git(self.repo, "cat-file", "-e", "origin/main:.warp/config.yaml")
        self.assertNotEqual(hidden.returncode, 0)
        text = git(self.repo, "log", "-1", "--format=%s", "origin/main").stdout
        self.assertIn("Warp state", text)

    def test_pause_pushes_paused_state_before_it_returns(self):
        self.write({"T-1": self.ticket("T-1")})
        git(self.repo, "add", ".warp/beam.json")
        git(self.repo, "commit", "-m", "beam")
        git(self.repo, "push", "origin", "main")
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "scan.py"), "pause", "--beam", ".warp/beam.json", "--reason", "hold"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("paused", proc.stdout)
        self.assertIn("commit:", proc.stdout)
        shown = git(self.repo, "show", "origin/main:.warp/beam.json")
        saved = json.loads(shown.stdout)
        self.assertEqual(saved["runState"], "paused")
        self.assertEqual(saved["pauseReason"], "hold")

    def test_help_describes_the_sync(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "update_state.py"), "?"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for needle in ("--beam", "--root", ".warp/tickets/", "config.yaml", "/warp-pause", "origin"):
            self.assertIn(needle, proc.stdout, needle)
