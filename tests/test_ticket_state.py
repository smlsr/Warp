"""Remote Agents report status in .warp/tickets/<id>/. The live beam is patched, not replaced."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import alarm_repair  # noqa: E402
import beam  # noqa: E402
import ticket_state  # noqa: E402

NOW = "2026-10-06T12:00:00Z"
STALE = "2026-10-06T11:00:00Z"
FRESH = "2026-10-06T11:50:00Z"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=warp@example.com", "-c", "user.name=Warp", "-c", "maintenance.auto=false", *args],
        capture_output=True,
        text=True,
    )


def run(repo, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / args[0]), *args[1:]],
        cwd=repo,
        capture_output=True,
        text=True,
    )


class GitBeam(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "init")
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        pushed = git(self.repo, "push", "-u", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.beam_path = self.repo / ".warp" / "beam.json"

    def write_beam(self, tickets, **extra):
        warp = self.repo / ".warp"
        warp.mkdir(parents=True, exist_ok=True)
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {
                "maxAgents": 4,
                "staleMinutes": 15,
                "maxRecoveries": 3,
                "runner": "cloud",
                "baseBranch": "main",
                "checkCommand": "make ci",
            },
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def ticket(self, tid="T-1", **extra):
        row = {
            "id": tid,
            "status": "coding",
            "summary": "ship %s" % tid,
            "size": "M",
            "module": "app",
            "hours": 1,
            "tokens": 0,
            "minutes": 0,
            "autoMerge": True,
            "deps": [],
            "locks": ["src/%s" % tid],
            "critical": False,
            "rankDays": 0,
            "complexity": "MEDIUM",
            "agent": "shuttle-%s" % tid,
            "branch": "warp/%s" % tid,
            "claimedAt": STALE,
            "workerStartedAt": STALE,
            "attempts": 0,
            "alarm": None,
            "pr": {"url": "https://example.test/pull/7", "bugbot": None, "ci": None},
            "jira": {"startedAt": "2026-10-06T08:00:00Z", "previousStatus": "To Do"},
            "jiraKey": "WAR-1",
        }
        row.update(extra)
        return row

    def commit_branch(self, branch, files):
        """Commit files on branch and push. Leave the orchestrator on main.

        files maps repo-relative path -> text. This is the Agent's push.
        """
        git(self.repo, "checkout", "-B", branch)
        for rel, text in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(self.repo, "add", "--", rel)
        committed = git(self.repo, "commit", "-m", "ticket status %s" % branch)
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed = git(self.repo, "push", "-u", "origin", branch)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        tip = git(self.repo, "rev-parse", branch).stdout.strip()
        git(self.repo, "checkout", "main")
        return tip

    def advance_main(self):
        other = self.tmp / "other"
        cloned = git(self.tmp, "clone", "--branch", "main", str(self.tmp / "origin.git"), str(other))
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        git(other, "config", "user.email", "warp@example.com")
        git(other, "config", "user.name", "Warp")
        (other / "merged.txt").write_text("merged on main\n")
        git(other, "add", "merged.txt")
        committed = git(other, "commit", "-m", "merge onto main")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed = git(other, "push", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

    def load(self):
        return json.loads(self.beam_path.read_text())


class RemoteDirectoryTests(GitBeam):
    def test_die_with_no_heartbeat_relaunches_once_and_ignores_the_branch_beam(self):
        row = self.ticket()
        stale = {
            "tickets": {
                "T-1": {
                    "id": "T-1",
                    "status": "merged",
                    "lastSeenAt": NOW,
                    "locks": [],
                    "marker": "STALE-BEAM",
                    "jira": {"startedAt": "1999-01-01T00:00:00Z"},
                }
            }
        }
        tip = self.commit_branch(
            "warp/T-1",
            {
                ".warp/beam.json": json.dumps(stale, indent=2) + "\n",
                "ticket-only.txt": "keep\n",
            },
        )
        self.write_beam({"T-1": row})
        self.advance_main()
        first = beam.watchdog(self.beam_path, now=NOW)
        self.assertIn("shuttle: replace T-1 agent=shuttle-T-1-r1 branch=warp/T-1", first)
        self.assertIn("herald: T-1 worker died. A new Shuttle started.", first)
        self.assertEqual(sum(1 for line in first if line.startswith("shuttle: replace")), 1)
        saved = self.load()["tickets"]["T-1"]
        self.assertEqual(saved["status"], "recovering")
        self.assertNotEqual(saved["status"], "merged")
        self.assertEqual(saved["locks"], ["src/T-1"])
        self.assertEqual(saved["jira"]["startedAt"], "2026-10-06T08:00:00Z")
        self.assertEqual(saved["branch"], "warp/T-1")
        self.assertEqual(saved["pr"]["url"], "https://example.test/pull/7")
        self.assertNotIn("marker", saved)
        self.assertNotIn("STALE-BEAM", self.beam_path.read_text())
        self.assertEqual(git(self.repo, "rev-parse", "warp/T-1").stdout.strip(), tip)
        self.assertNotEqual(git(self.repo, "cat-file", "-e", "warp/T-1:merged.txt").returncode, 0)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", tip, "warp/T-1").returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")
        second = beam.watchdog(self.beam_path, now=NOW)
        self.assertIn("shuttle: fresh T-1", second)
        self.assertNotIn("shuttle: replace", "\n".join(second))
        self.assertEqual(self.load()["tickets"]["T-1"]["recoveries"], 1)
        self.assertEqual(self.load()["tickets"]["T-1"]["status"], "recovering")

    def test_in_flight_branch_is_not_rebased_when_the_agent_is_alive(self):
        row = self.ticket(workerStartedAt=STALE, claimedAt=STALE)
        self.write_beam({"T-1": row})
        state = {
            "id": "T-1",
            "state": "coding",
            "startedAt": STALE,
            "updatedAt": FRESH,
            "heartbeatAt": FRESH,
            "pr": {"url": "https://example.test/pull/7"},
            "check": {"result": "", "name": "", "log": ""},
            "error": "",
            "alarm": "",
            "escaped": [],
        }
        log = json.dumps({"seq": 1, "at": FRESH, "id": "T-1", "state": "heartbeat", "error": ""}) + "\n"
        tip = self.commit_branch(
            "warp/T-1",
            {
                ".warp/tickets/T-1/state.json": json.dumps(state, indent=2) + "\n",
                ".warp/tickets/T-1/log.jsonl": log,
                "ticket-only.txt": "keep\n",
            },
        )
        self.advance_main()
        lines = beam.watchdog(self.beam_path, now=NOW)
        self.assertNotIn("shuttle: replace", "\n".join(lines))
        self.assertIn("shuttle: alive T-1", lines)
        self.assertEqual(self.load()["tickets"]["T-1"]["lastSeenAt"], FRESH)
        self.assertEqual(self.load()["tickets"]["T-1"]["status"], "coding")
        self.assertEqual(git(self.repo, "rev-parse", "warp/T-1").stdout.strip(), tip)
        self.assertNotEqual(git(self.repo, "cat-file", "-e", "warp/T-1:merged.txt").returncode, 0)

    def test_pushed_lock_escape_is_applied_after_the_agent_is_gone(self):
        self.write_beam({"T-1": self.ticket()})
        state = {
            "id": "T-1",
            "state": "lock-escape",
            "startedAt": STALE,
            "updatedAt": STALE,
            "heartbeatAt": "",
            "pr": {"url": ""},
            "check": {"result": "", "name": "", "log": ""},
            "error": "left the lock",
            "alarm": "lock-escape",
            "escaped": ["src/extra.py", "docs/note.md"],
        }
        log = (
            json.dumps(
                {
                    "seq": 1,
                    "at": STALE,
                    "id": "T-1",
                    "state": "lock-escape",
                    "error": "left the lock",
                    "alarm": "lock-escape",
                    "escaped": ["src/extra.py", "docs/note.md"],
                }
            )
            + "\n"
        )
        self.commit_branch(
            "warp/T-1",
            {
                ".warp/tickets/T-1/state.json": json.dumps(state, indent=2) + "\n",
                ".warp/tickets/T-1/log.jsonl": log,
            },
        )
        lines = beam.watchdog(self.beam_path, now=NOW)
        self.assertIn("herald: T-1 lock-escape", lines)
        self.assertNotIn("shuttle: replace", "\n".join(lines))
        saved = self.load()["tickets"]["T-1"]
        self.assertEqual(saved["status"], "alarm")
        self.assertEqual(saved["alarm"], "lock-escape")
        self.assertEqual(saved["escaped"], ["src/extra.py", "docs/note.md"])
        self.assertEqual(saved["jira"]["startedAt"], "2026-10-06T08:00:00Z")
        self.assertEqual(saved["locks"], ["src/T-1"])
        repair = alarm_repair.next_repair(self.beam_path, now=NOW)
        self.assertTrue(any(line.startswith("alarm-repair: start T-1") for line in repair), repair)
        widened = self.load()["tickets"]["T-1"]["locks"]
        self.assertIn("src/extra.py", widened)
        self.assertIn("docs/note.md", widened)
        again = beam.watchdog(self.beam_path, now=NOW)
        self.assertNotIn("shuttle: replace", "\n".join(again))
        self.assertNotIn("herald: T-1 lock-escape", again)

    def test_check_red_from_the_log_sends_the_ticket_back(self):
        row = self.ticket(status="review", workerStartedAt=FRESH, claimedAt=FRESH)
        self.write_beam({"T-1": row})
        state = {
            "id": "T-1",
            "state": "check-red",
            "startedAt": STALE,
            "updatedAt": NOW,
            "heartbeatAt": FRESH,
            "pr": {"url": "https://example.test/pull/7"},
            "check": {"result": "red", "name": "make ci", "log": "boom"},
            "error": "boom",
            "alarm": "",
            "escaped": [],
        }
        log = (
            json.dumps({"seq": 1, "at": FRESH, "id": "T-1", "state": "heartbeat", "error": ""})
            + "\n"
            + json.dumps(
                {"seq": 2, "at": NOW, "id": "T-1", "state": "check-red", "error": "boom", "name": "make ci"}
            )
            + "\n"
        )
        self.commit_branch(
            "warp/T-1",
            {
                ".warp/tickets/T-1/state.json": json.dumps(state, indent=2) + "\n",
                ".warp/tickets/T-1/log.jsonl": log,
            },
        )
        lines = ticket_state.observe(self.beam_path, now=NOW)
        self.assertIn("herald: T-1 check-red", lines)
        self.assertTrue(any(line.startswith("send-back T-1 fix") for line in lines), lines)
        saved = self.load()["tickets"]["T-1"]
        self.assertEqual(saved["status"], "fix")
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(saved["pr"]["check"], "red")
        self.assertEqual(saved["pr"]["checkLog"], "boom")
        again = ticket_state.observe(self.beam_path, now=NOW)
        self.assertNotIn("herald: T-1 check-red", again)
        self.assertFalse(any(line.startswith("send-back") for line in again))
        self.assertEqual(self.load()["tickets"]["T-1"]["attempts"], 1)
        saved = self.load()
        saved["tickets"]["T-1"]["status"] = "review"
        saved["tickets"]["T-1"]["attempts"] = 4
        self.beam_path.write_text(json.dumps(saved, indent=2) + "\n")
        parked_line = {
            "seq": 3,
            "at": "2026-10-06T12:05:00Z",
            "id": "T-1",
            "state": "check-red",
            "error": "boom again",
            "name": "make ci",
        }
        git(self.repo, "checkout", "warp/T-1")
        log_path = self.repo / ".warp" / "tickets" / "T-1" / "log.jsonl"
        log_path.write_text(log_path.read_text() + json.dumps(parked_line) + "\n")
        git(self.repo, "add", "--", ".warp/tickets/T-1/log.jsonl")
        committed = git(self.repo, "commit", "-m", "third red")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        git(self.repo, "push", "origin", "warp/T-1")
        git(self.repo, "checkout", "main")
        # The live beam's seen-set does not include the new line. attempts is already 4,
        # so this red is the fifth and parks. Rewrite seen by leaving the beam as edited.
        third = ticket_state.observe(self.beam_path, now="2026-10-06T12:05:00Z")
        self.assertTrue(any(line.startswith("send-back T-1 parked") for line in third), third)
        self.assertEqual(self.load()["tickets"]["T-1"]["status"], "parked")
        self.assertFalse(any(line.startswith("start ") for line in third))

    def test_check_red_from_the_provider_when_the_directory_has_no_check(self):
        row = self.ticket(status="review", workerStartedAt=FRESH, claimedAt=FRESH)
        self.write_beam({"T-1": row})
        state = {
            "id": "T-1",
            "state": "coding",
            "startedAt": STALE,
            "updatedAt": FRESH,
            "heartbeatAt": FRESH,
            "pr": {"url": "https://example.test/pull/7"},
            "check": {"result": "", "name": "", "log": ""},
            "error": "",
            "alarm": "",
            "escaped": [],
        }
        log = json.dumps({"seq": 1, "at": FRESH, "id": "T-1", "state": "heartbeat", "error": ""}) + "\n"

        def fetch(root, branch):
            return True

        def reader(root, branch, tid):
            return state, [json.loads(log)]

        checks = [
            {"name": "make ci", "conclusion": "failure", "log": "make ci failed"},
            {"name": "Bugbot", "conclusion": "failure", "log": "one finding"},
        ]

        def provider_checks(root, ticket):
            return checks

        lines = ticket_state.observe_locked(
            self.beam_path,
            now=NOW,
            fetch=fetch,
            reader=reader,
            provider_checks=provider_checks,
        )
        self.assertIn("herald: T-1 check-red", lines)
        self.assertTrue(any(line.startswith("send-back T-1 fix") for line in lines), lines)
        saved = self.load()["tickets"]["T-1"]
        self.assertEqual(saved["status"], "fix")
        self.assertEqual(saved["pr"]["check"], "red")
        self.assertEqual(saved["pr"]["bugbot"], "fail")
        self.assertEqual(saved["pr"]["rollup"], "red")
        self.assertEqual(saved["attempts"], 1)
        again = ticket_state.observe_locked(
            self.beam_path,
            now=NOW,
            fetch=fetch,
            reader=reader,
            provider_checks=provider_checks,
        )
        self.assertFalse(any(line.startswith("send-back") for line in again))
        self.assertEqual(self.load()["tickets"]["T-1"]["attempts"], 1)

    def test_events_for_other_tickets_are_ignored(self):
        one = self.ticket(status="claimed", workerStartedAt=FRESH, claimedAt=FRESH)
        two = self.ticket("T-2", status="queued", agent=None, branch=None, locks=["src/T-2"])
        two["claimedAt"] = None
        two["workerStartedAt"] = None
        self.write_beam({"T-1": one, "T-2": two})
        log = "\n".join(
            [
                json.dumps({"seq": 1, "at": FRESH, "id": "T-2", "state": "coding", "error": ""}),
                json.dumps({"seq": 2, "at": FRESH, "id": "T-1", "state": "planning", "error": ""}),
                json.dumps({"seq": 3, "at": NOW, "id": "T-1", "state": "coding", "error": ""}),
            ]
        ) + "\n"
        state = {
            "id": "T-1",
            "state": "coding",
            "startedAt": FRESH,
            "updatedAt": NOW,
            "heartbeatAt": FRESH,
            "pr": {"url": ""},
            "check": {"result": "", "name": "", "log": ""},
            "error": "",
            "alarm": "",
            "escaped": [],
        }
        self.commit_branch(
            "warp/T-1",
            {
                ".warp/tickets/T-1/state.json": json.dumps(state, indent=2) + "\n",
                ".warp/tickets/T-1/log.jsonl": log,
                ".warp/tickets/T-2/state.json": json.dumps({"id": "T-2", "state": "done"}) + "\n",
            },
        )
        lines = ticket_state.observe(self.beam_path, now=NOW)
        self.assertIn("herald: T-1 planning", lines)
        self.assertIn("herald: T-1 coding", lines)
        self.assertNotIn("herald: T-2 coding", lines)
        self.assertNotIn("herald: T-2 done", lines)
        saved = self.load()["tickets"]
        self.assertEqual(saved["T-1"]["status"], "coding")
        self.assertEqual(saved["T-2"]["status"], "queued")
        self.assertNotIn("remote", saved["T-2"])
        again = ticket_state.observe(self.beam_path, now=NOW)
        self.assertNotIn("herald:", "\n".join(again))
        self.assertEqual(self.load()["tickets"]["T-1"]["status"], "coding")

    def test_paused_and_stopped_do_not_fetch(self):
        self.write_beam({"T-1": self.ticket()}, runState="paused", paused=True)
        calls = []

        def fetch(root, branch):
            calls.append(branch)
            return True

        paused = ticket_state.observe(self.beam_path, now=NOW, fetch=fetch)
        self.assertEqual(paused, ["observe: skipped (paused)"])
        self.assertEqual(calls, [])
        self.assertEqual(beam.watchdog(self.beam_path, now=NOW), ["watchdog: skipped (paused)"])
        data = self.load()
        data["runState"] = "stopped"
        data["paused"] = False
        self.beam_path.write_text(json.dumps(data) + "\n")
        stopped = ticket_state.observe(self.beam_path, now=NOW, fetch=fetch)
        self.assertEqual(stopped, ["observe: skipped (stopped)"])
        self.assertEqual(calls, [])

    def test_append_commits_only_the_ticket_directory(self):
        self.write_beam({"T-1": self.ticket(status="claimed", workerStartedAt=NOW, claimedAt=NOW)})
        git(self.repo, "checkout", "-b", "warp/T-1")
        dirty = json.loads(self.beam_path.read_text())
        dirty["tickets"]["T-1"]["marker"] = "DIRTY-BEAM"
        self.beam_path.write_text(json.dumps(dirty, indent=2) + "\n")
        git(self.repo, "add", "--", ".warp/beam.json")
        appended = run(
            self.repo,
            "ticket_state.py",
            "append",
            "--id",
            "T-1",
            "--state",
            "planning",
            "--root",
            ".",
            "--push",
            "--agent",
            "shuttle-T-1",
        )
        self.assertEqual(appended.returncode, 0, appended.stdout + appended.stderr)
        self.assertIn("committed: .warp/tickets/T-1", appended.stdout)
        names = git(self.repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
        self.assertIn(".warp/tickets/T-1/state.json", names)
        self.assertIn(".warp/tickets/T-1/log.jsonl", names)
        self.assertNotIn(".warp/beam.json", names)
        shown = git(self.repo, "show", "HEAD:.warp/tickets/T-1/state.json")
        self.assertIn('"state": "planning"', shown.stdout)
        self.assertIn("DIRTY-BEAM", self.beam_path.read_text())
        refused = run(
            self.repo,
            "ticket_state.py",
            "append",
            "--id",
            "T-1",
            "--state",
            "coding",
            "--root",
            ".",
            "--push",
        )
        git(self.repo, "checkout", "main")
        refused_main = run(
            self.repo,
            "ticket_state.py",
            "append",
            "--id",
            "T-1",
            "--state",
            "coding",
            "--root",
            ".",
            "--push",
        )
        self.assertEqual(refused_main.returncode, 2, refused_main.stdout + refused_main.stderr)
        self.assertIn("not main", refused_main.stdout)
        self.assertEqual(refused.returncode, 0, refused.stdout + refused.stderr)


class MergeTests(unittest.TestCase):
    def test_ticket_directories_merge_and_the_beam_does_not(self):
        import provider

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "warp@example.com")
        git(repo, "config", "user.name", "Warp")
        (repo / "f.txt").write_text("base\n")
        warp = repo / ".warp"
        warp.mkdir()
        (warp / "beam.json").write_text(json.dumps({"live": True, "tickets": {}}) + "\n")
        git(repo, "add", "f.txt", ".warp/beam.json")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", "warp/T-1")
        (repo / "f.txt").write_text("base\nticket\n")
        (warp / "beam.json").write_text(json.dumps({"live": False, "marker": "STALE-BEAM"}) + "\n")
        ticket = warp / "tickets" / "T-1"
        ticket.mkdir(parents=True)
        (ticket / "state.json").write_text('{"id": "T-1", "state": "coding"}\n')
        (ticket / "log.jsonl").write_text('{"at": "2026-10-06T12:00:00Z", "state": "coding", "error": ""}\n')
        git(repo, "add", "f.txt", ".warp/beam.json", ".warp/tickets")
        git(repo, "commit", "-q", "-m", "ticket")
        git(repo, "checkout", "-q", "main")
        ok, detail = provider.merge_local(repo, "warp/T-1", "main", "merge T-1")
        self.assertTrue(ok, detail)
        merged = git(repo, "show", "main:.warp/beam.json")
        self.assertIn('"live": true', merged.stdout)
        self.assertNotIn("STALE-BEAM", merged.stdout)
        state = git(repo, "show", "main:.warp/tickets/T-1/state.json")
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertIn("coding", state.stdout)
        product = git(repo, "show", "main:f.txt")
        self.assertIn("ticket", product.stdout)

    def test_merge_commit_lands_the_live_beam_not_the_branch_copy(self):
        import provider

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "warp@example.com")
        git(repo, "config", "user.name", "Warp")
        (repo / "f.txt").write_text("base\n")
        warp = repo / ".warp"
        warp.mkdir()
        (warp / "beam.json").write_text(json.dumps({"marker": "OLD-MAIN", "tickets": {}}) + "\n")
        git(repo, "add", "f.txt", ".warp/beam.json")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", "warp/T-1")
        (repo / "f.txt").write_text("base\nticket\n")
        (warp / "beam.json").write_text(json.dumps({"marker": "STALE-BEAM", "tickets": {}}) + "\n")
        ticket = warp / "tickets" / "T-1"
        ticket.mkdir(parents=True)
        (ticket / "state.json").write_text('{"id": "T-1", "state": "coding"}\n')
        git(repo, "add", "f.txt", ".warp/beam.json", ".warp/tickets")
        git(repo, "commit", "-q", "-m", "ticket")
        git(repo, "checkout", "-q", "main")
        live = {
            "marker": "LIVE-BEAM",
            "token": "super-secret-token",
            "apiKey": "super-secret-token",
            "tickets": {"T-1": {"id": "T-1", "status": "review"}},
        }
        (warp / "beam.json").write_text(json.dumps(live, indent=2) + "\n")
        (warp / "journal.jsonl").write_text(
            json.dumps(
                {
                    "type": "note",
                    "token": "super-secret-token",
                    "msg": "ping https://hooks.slack.com/services/T00/B00/secret",
                }
            )
            + "\n"
        )
        (warp / "BOARD.md").write_text("board https://hooks.slack.com/services/T00/B00/secret\n")
        (warp / "config.yaml").write_text("token: super-secret-token\n")
        ok, detail = provider.merge_local(repo, "warp/T-1", "main", "merge T-1")
        self.assertTrue(ok, detail)
        merged = git(repo, "show", "main:.warp/beam.json")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertIn("LIVE-BEAM", merged.stdout)
        self.assertNotIn("STALE-BEAM", merged.stdout)
        self.assertNotIn("OLD-MAIN", merged.stdout)
        self.assertNotIn("super-secret-token", merged.stdout)
        journal = git(repo, "show", "main:.warp/journal.jsonl")
        self.assertEqual(journal.returncode, 0, journal.stderr)
        self.assertNotIn("super-secret-token", journal.stdout)
        self.assertIn("[redacted]", journal.stdout)
        board = git(repo, "show", "main:.warp/BOARD.md")
        self.assertIn("[redacted]", board.stdout)
        self.assertNotIn("hooks.slack.com", board.stdout)
        hidden = git(repo, "cat-file", "-e", "main:.warp/config.yaml")
        self.assertNotEqual(hidden.returncode, 0)
        product = git(repo, "show", "main:f.txt")
        self.assertIn("ticket", product.stdout)
        state = git(repo, "show", "main:.warp/tickets/T-1/state.json")
        self.assertIn("coding", state.stdout)


class FreshCheckoutTests(GitBeam):
    def test_start_and_resume_load_main_then_ticket_folders(self):
        import session_note

        row = self.ticket(status="claimed")
        self.write_beam({"T-1": row})
        data = json.loads(self.beam_path.read_text())
        data["marker"] = "LIVE-BEAM"
        data["token"] = "super-secret-token"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        warp = self.repo / ".warp"
        (warp / "config.yaml").write_text("token: super-secret-token\n")
        git(self.repo, "config", "user.email", "warp@example.com")
        git(self.repo, "config", "user.name", "Warp")
        noted = session_note.note(warp, "session-stop")
        self.assertEqual(noted, "noted")
        published = git(self.repo, "show", "origin/main:.warp/beam.json")
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn("LIVE-BEAM", published.stdout)
        self.assertNotIn("super-secret-token", published.stdout)
        hidden = git(self.repo, "cat-file", "-e", "origin/main:.warp/config.yaml")
        self.assertNotEqual(hidden.returncode, 0)
        fresh_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {
            "id": "T-1",
            "state": "coding",
            "startedAt": fresh_at,
            "updatedAt": fresh_at,
            "heartbeatAt": fresh_at,
        }
        stale = {"tickets": {"T-1": {"id": "T-1", "status": "merged", "marker": "STALE-BEAM"}}}
        self.commit_branch(
            "warp/T-1",
            {
                ".warp/tickets/T-1/state.json": json.dumps(state, indent=2) + "\n",
                ".warp/beam.json": json.dumps(stale, indent=2) + "\n",
            },
        )
        for command in ("start", "resume"):
            checkout = self.tmp / command
            cloned = git(self.tmp, "clone", "--branch", "main", str(self.tmp / "origin.git"), str(checkout))
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            git(checkout, "config", "user.email", "warp@example.com")
            git(checkout, "config", "user.name", "Warp")
            self.assertTrue((checkout / ".warp" / "beam.json").is_file(), cloned.stderr)
            (checkout / ".warp" / "beam.json").unlink()
            args = ["scan.py", command, "--beam", ".warp/beam.json"]
            if command == "start":
                args.append("--force")
            started = run(checkout, *args)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            self.assertIn("beam: loaded from origin/main", started.stdout)
            loaded = json.loads((checkout / ".warp" / "beam.json").read_text())
            self.assertEqual(loaded["marker"], "LIVE-BEAM")
            self.assertNotIn("super-secret-token", json.dumps(loaded))
            self.assertEqual(loaded["tickets"]["T-1"]["status"], "recovering")
            self.assertTrue((loaded["tickets"]["T-1"].get("shuttle") or {}).get("pending"))
            self.assertIn("shuttle: replace T-1", started.stdout)
            self.assertNotIn("STALE-BEAM", (checkout / ".warp" / "beam.json").read_text())
            self.assertNotIn("marker", loaded["tickets"]["T-1"])
