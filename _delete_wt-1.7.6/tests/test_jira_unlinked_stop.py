"""The first claimed ticket that Jira cannot resolve releases the claim and stops the run."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def sentence(pair):
    return (
        f"Run stopped because Jira issues are not linked ({pair}). "
        "Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume."
    )


class UnlinkedStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.beam = ".warp/beam.json"

    def scan(self, tickets, jira=True):
        (self.repo / "schedule.json").write_text(json.dumps({"tickets": tickets}))
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = 'jiraProject: "WAR"\n'
        if not jira:
            text += "jiraTransition: false\n"
        (self.repo / ".warp" / "config.yaml").write_text(text)
        started = run("scan.py", "start", "--force", "--beam", self.beam, cwd=self.repo)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.beam_json()["runState"], "running")

    def beam_json(self):
        return json.loads((self.repo / self.beam).read_text())

    def claim(self, tid, agent, branch):
        r = run(
            "beam.py",
            "set",
            "--beam",
            self.beam,
            "--id",
            tid,
            "--status",
            "claimed",
            "--agent",
            agent,
            "--branch",
            branch,
            cwd=self.repo,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def record(self, tid, result, error=None):
        args = [
            "jira_sync.py",
            "record",
            "--beam",
            self.beam,
            "--id",
            tid,
            "--event",
            "claim",
            "--result",
            result,
        ]
        if error:
            args.extend(["--error", error])
        return run(*args, cwd=self.repo)

    def test_first_ticket_not_found_stops_and_releases_the_claim(self):
        self.scan(
            [
                {"id": "WV-01", "summary": "First", "size": "S", "jiraKey": "WAR-1"},
                {"id": "WV-02", "summary": "Second", "size": "S", "jiraKey": "WAR-2", "deps": ["WV-01"]},
            ]
        )
        self.claim("WV-01", "shuttle-WV-01", "warp/WV-01")
        missed = self.record("WV-01", "not-found", "Jira issue WAR-1 was not found")
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        pair = "WV-01 / WAR-1"
        text = sentence(pair)
        self.assertIn(text, missed.stdout)
        self.assertEqual(missed.stdout.count(text), 1)
        self.assertIn("jira: STOP tickets are not linked to Jira (WV-01 / WAR-1)", missed.stdout)
        self.assertNotIn("The claim was not affected", missed.stdout)
        self.assertNotIn("The ticket was not stopped", missed.stdout)
        self.assertNotIn("Jira not updated", missed.stdout)

        beam = self.beam_json()
        ticket = beam["tickets"]["WV-01"]
        self.assertEqual(ticket["status"], "queued")
        self.assertIsNone(ticket["agent"])
        self.assertIsNone(ticket["branch"])
        self.assertNotIn("startedAt", ticket["jira"])
        self.assertNotIn("previousStatus", ticket["jira"])
        self.assertEqual(ticket["jira"]["lastAttempt"]["result"], "not-found")
        self.assertIsNone(ticket["pr"]["url"])
        self.assertEqual(beam["runState"], "stopped")
        self.assertTrue(beam["paused"])
        self.assertEqual(beam["pauseReason"], f"tickets are not linked to Jira ({pair})")
        self.assertTrue(beam.get("stoppedAt"))
        todo = json.loads((self.repo / ".warp" / "jira-todo.json").read_text())
        self.assertEqual(todo["tickets"], [])

        journal = (self.repo / ".warp" / "journal.jsonl").read_text()
        self.assertIn("session-stop", journal)
        self.assertIn('"type":"stopped"', journal)

        status = (self.repo / ".warp" / "STATUS.md").read_text()
        self.assertIn("runState=stopped", status)
        self.assertIn("## Working now\n\nNone.", status)
        self.assertIn("**WV-01** queued", status)

        board = (self.repo / ".warp" / "BOARD.md").read_text()
        self.assertIn("paused=True", board)
        self.assertIn("## In flight\n\nNone.", board)
        self.assertNotIn("shuttle-WV-01", board)
        self.assertNotIn("warp/WV-01", board)

        payload = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertEqual(payload["kind"], "jira-unlinked")
        self.assertIn(text, payload["text"])
        self.assertNotIn("claim was not affected", payload["text"].casefold())
        self.assertNotIn("was not stopped", payload["text"].casefold())
        self.assertIn(text, (self.repo / ".warp" / "outbox.md").read_text())

        ready = run("beam.py", "ready", "--beam", self.beam, cwd=self.repo)
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertIn("(none)", ready.stdout)
        self.assertNotIn("WV-01", ready.stdout)
        self.assertNotIn("WV-02", ready.stdout)

    def test_first_ticket_failed_error_not_found_stops_the_same_way(self):
        self.scan([{"id": "WV-01", "summary": "First", "size": "S", "jiraKey": "WAR-1"}])
        self.claim("WV-01", "shuttle-WV-01", "warp/WV-01")
        missed = self.record("WV-01", "failed", "Jira issue WAR-1 was not found")
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        self.assertIn(sentence("WV-01 / WAR-1"), missed.stdout)
        self.assertNotIn("The claim was not affected", missed.stdout)
        ticket = self.beam_json()["tickets"]["WV-01"]
        self.assertEqual(ticket["status"], "queued")
        self.assertIsNone(ticket["agent"])
        self.assertIsNone(ticket["branch"])
        self.assertEqual(self.beam_json()["runState"], "stopped")

    def test_first_ticket_unresolved_key_stops_and_releases_the_claim(self):
        self.scan([{"id": "WV-01", "summary": "First", "size": "S"}])
        self.claim("WV-01", "shuttle-WV-01", "warp/WV-01")
        results = self.repo / "results.json"
        results.write_text(json.dumps({"WV-01": []}))
        missed = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "results.json", cwd=self.repo)
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        text = sentence("WV-01 / unresolved")
        self.assertIn(text, missed.stdout)
        self.assertNotIn("The claim was not affected", missed.stdout)
        self.assertNotIn("The ticket was not stopped", missed.stdout)
        beam = self.beam_json()
        ticket = beam["tickets"]["WV-01"]
        self.assertEqual(ticket["status"], "queued")
        self.assertIsNone(ticket["agent"])
        self.assertIsNone(ticket["branch"])
        self.assertNotIn("startedAt", ticket.get("jira") or {})
        self.assertIsNone(ticket.get("jiraKey"))
        self.assertEqual(beam["runState"], "stopped")
        self.assertTrue(beam["paused"])
        self.assertEqual(beam["pauseReason"], "tickets are not linked to Jira (WV-01 / unresolved)")
        payload = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertEqual(payload["kind"], "jira-unlinked")
        self.assertIn(text, payload["text"])
        ready = run("beam.py", "ready", "--beam", self.beam, cwd=self.repo)
        self.assertIn("(none)", ready.stdout)

    def test_second_ticket_not_found_after_one_success_does_not_stop(self):
        self.scan(
            [
                {"id": "WV-01", "summary": "First", "size": "S", "jiraKey": "WAR-1"},
                {"id": "WV-02", "summary": "Second", "size": "S", "jiraKey": "WAR-2"},
            ]
        )
        self.claim("WV-01", "shuttle-WV-01", "warp/WV-01")
        moved = self.record("WV-01", "moved")
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertTrue(self.beam_json()["tickets"]["WV-01"]["jira"].get("startedAt"))
        self.claim("WV-02", "shuttle-WV-02", "warp/WV-02")
        missed = self.record("WV-02", "not-found", "Jira issue WAR-2 was not found")
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        self.assertIn("The claim was not affected", missed.stdout)
        self.assertNotIn("Run stopped because", missed.stdout)
        self.assertNotIn("jira: STOP", missed.stdout)
        beam = self.beam_json()
        ticket = beam["tickets"]["WV-02"]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["agent"], "shuttle-WV-02")
        self.assertEqual(ticket["branch"], "warp/WV-02")
        self.assertEqual(beam["runState"], "running")
        self.assertFalse(beam["paused"])
        payload = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertEqual(payload["kind"], "jira-failed")
        self.assertIn("The ticket was not stopped", payload["text"])
        self.assertNotIn("Run stopped because Jira issues are not linked", payload["text"])

    def test_jira_disabled_does_not_stop(self):
        self.scan(
            [{"id": "WV-01", "summary": "First", "size": "S", "jiraKey": "WAR-1"}],
            jira=False,
        )
        self.claim("WV-01", "shuttle-WV-01", "warp/WV-01")
        missed = self.record("WV-01", "not-found", "Jira issue WAR-1 was not found")
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        self.assertIn("The claim was not affected", missed.stdout)
        self.assertNotIn("Run stopped because", missed.stdout)
        self.assertNotIn("jira: STOP", missed.stdout)
        beam = self.beam_json()
        ticket = beam["tickets"]["WV-01"]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["agent"], "shuttle-WV-01")
        self.assertEqual(ticket["branch"], "warp/WV-01")
        self.assertEqual(beam["runState"], "running")
        self.assertFalse(beam["paused"])


if __name__ == "__main__":
    unittest.main()
