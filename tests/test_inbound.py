"""One channel listener: parse, ack, proceed, and a single beam slot."""

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
import beam  # noqa: E402
import inbound  # noqa: E402
import scan  # noqa: E402


def ticket(tid, **extra):
    row = {
        "id": tid,
        "status": "awaiting_approval",
        "summary": "ship %s" % tid,
        "size": "L",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": False,
        "deps": [],
        "locks": ["src/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "complexity": "HIGH",
        "agent": "shuttle-1",
        "branch": "warp/%s" % tid,
        "attempts": 2,
        "alarm": None,
        "pr": {
            "url": "https://github.com/acme/app/pull/7",
            "bugbot": "pass",
            "ci": "green",
            "sha": "head111",
        },
        "jira": {"status": "QA Ready", "qaReadyAt": "2026-01-01T00:00:00Z"},
        "jiraKey": extra.pop("jiraKey", "WAR-%s" % tid.rsplit("-", 1)[-1]),
        "jiraKeySource": "manual",
        "jiraMapping": "mapped",
    }
    row.update(extra)
    if "pr" in extra:
        row["pr"] = {
            "url": "https://github.com/acme/app/pull/7",
            "bugbot": "pass",
            "ci": "green",
            "sha": "head111",
            **extra["pr"],
        }
    return row


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        warp = self.repo / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"
        (warp / "config.yaml").write_text('jiraProject: "WAR"\nmessenger: slack\nslackChannel: warp\n')
        self.write_beam(
            {
                "XV-01": ticket("XV-01", jiraKey="WAR-1"),
                "XV-02": ticket("XV-02", jiraKey="WAR-2"),
            }
        )

    def write_beam(self, tickets, **extra):
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {"maxAgents": 18, "jiraProject": "WAR", "jiraDoneOnManualMerge": True},
            "program": {"criticalPath": []},
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def journal_types(self):
        path = self.repo / ".warp" / "journal.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line)["type"] for line in path.read_text().splitlines() if line.strip()]

    def test_parse_known_and_malformed(self):
        proceed = inbound.parse("warp:proceed XV-01")
        self.assertEqual(proceed["verb"], "proceed")
        self.assertEqual(proceed["token"], "XV-01")
        self.assertTrue(proceed["ok"])
        self.assertEqual(inbound.parse("WARP:PROCEED war-1")["token"], "war-1")
        self.assertEqual(inbound.parse("  warp:pause  hold overnight")["verb"], "pause")
        self.assertEqual(inbound.parse("warp:resume")["command"], "warp:resume")
        self.assertEqual(inbound.parse("warp:stop")["verb"], "stop")
        self.assertEqual(inbound.parse("warp:start")["verb"], "start")
        self.assertEqual(inbound.parse("warp:status")["verb"], "status")
        self.assertEqual(inbound.parse("warp:retry XV-02")["token"], "XV-02")
        self.assertIsNone(inbound.parse("hello from shawn"))
        missing = inbound.parse("warp:proceed")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["command"], "warp:proceed")
        unknown = inbound.parse("warp:hold XV-01")
        self.assertFalse(unknown["ok"])
        self.assertIn("warp:hold", unknown["command"])

    def test_ack_text_for_each_command(self):
        data = self.beam()
        cfg = {"jiraDoneOnManualMerge": True, "jiraDoneStatus": "Done", "jiraQaReadyStatus": "QA Ready", "bugbotRequired": True, "bugbotManual": True}

        def ack(text):
            return inbound.plan_command(data, inbound.parse(text), cfg)["ack"]

        self.assertEqual(ack("warp:proceed XV-01"), "Received warp:proceed XV-01. Merging and moving Jira to Done.")
        self.assertEqual(ack("warp:proceed WAR-1"), "Received warp:proceed XV-01. Merging and moving Jira to Done.")
        self.assertEqual(
            ack("warp:proceed XV-99"),
            "Received warp:proceed XV-99. No ticket is awaiting approval with that id. Nothing was merged.",
        )
        self.assertEqual(ack("warp:pause"), "Received warp:pause. Pausing the run and stopping the listener.")
        self.assertEqual(ack("warp:resume"), "Received warp:resume. Resuming the run.")
        self.assertEqual(ack("warp:stop"), "Received warp:stop. Stopping the run and the listener.")
        self.assertEqual(ack("warp:start"), "Received warp:start. Starting the run.")
        self.assertEqual(ack("warp:status"), "Received warp:status. Posting the digest.")
        unknown = ack("warp:hold")
        self.assertIn("Not understood: warp:hold.", unknown)
        self.assertIn("Accepted forms: %s." % inbound.ACCEPTED_FORMS, unknown)
        malformed = ack("warp:proceed")
        self.assertTrue(malformed.startswith("Not understood: warp:proceed."))
        self.assertIn("warp:retry <id>", malformed)

    def test_proceed_acks_then_merges_only_the_waiting_ticket(self):
        result = inbound.handle(self.beam_path, "warp:proceed XV-01", by="shawn")
        self.assertEqual(result["ack"], "Received warp:proceed XV-01. Merging and moving Jira to Done.")
        self.assertEqual(result["ticket"], "XV-01")
        self.assertIn("action: proceed XV-01", result["detail"])
        self.assertIn("Merge now", result["detail"])
        saved = self.beam()
        self.assertEqual(saved["tickets"]["XV-01"]["pr"]["proceededBy"], "shawn")
        self.assertEqual(saved["tickets"]["XV-01"]["status"], "awaiting_approval")
        other = saved["tickets"]["XV-02"]
        self.assertEqual(other["status"], "awaiting_approval")
        self.assertNotIn("proceededBy", other.get("pr") or {})
        ack_file = json.loads((self.repo / ".warp" / "inbound-ack.json").read_text())
        self.assertIn(result["ack"], ack_file["text"])
        self.assertEqual(ack_file["ack"], result["ack"])
        types = self.journal_types()
        self.assertLess(types.index("ack"), types.index("proceed"))

    def test_proceed_jira_key_and_bad_id_merges_nothing_else(self):
        result = inbound.handle(self.beam_path, "warp:proceed WAR-2", by="shawn")
        self.assertEqual(result["ack"], "Received warp:proceed XV-02. Merging and moving Jira to Done.")
        self.assertEqual(self.beam()["tickets"]["XV-02"]["pr"]["proceededBy"], "shawn")
        self.assertNotIn("proceededBy", self.beam()["tickets"]["XV-01"].get("pr") or {})

        self.write_beam(
            {
                "XV-01": ticket("XV-01", jiraKey="WAR-1"),
                "XV-02": ticket("XV-02", jiraKey="WAR-2"),
            }
        )
        before = self.beam()["tickets"]
        bad = inbound.handle(self.beam_path, "warp:proceed XV-99", by="shawn")
        self.assertEqual(
            bad["ack"],
            "Received warp:proceed XV-99. No ticket is awaiting approval with that id. Nothing was merged.",
        )
        self.assertEqual(bad["action"], "none")
        after = self.beam()["tickets"]
        self.assertEqual(before["XV-01"]["status"], after["XV-01"]["status"])
        self.assertEqual(before["XV-02"]["status"], after["XV-02"]["status"])
        self.assertNotIn("proceededBy", after["XV-01"].get("pr") or {})
        self.assertNotIn("proceededBy", after["XV-02"].get("pr") or {})
        self.assertIn(bad["ack"], (self.repo / ".warp" / "inbound-ack.json").read_text())

        self.write_beam({"XV-01": ticket("XV-01", status="coding", pr={"bugbot": None, "ci": None}, jiraKey="WAR-1")})
        coding = inbound.handle(self.beam_path, "warp:proceed XV-01")
        self.assertIn("Nothing was merged.", coding["ack"])
        self.assertEqual(self.beam()["tickets"]["XV-01"]["status"], "coding")

    def test_retry_status_and_run_control_ack(self):
        self.write_beam(
            {
                "XV-01": ticket("XV-01", status="alarm", alarm="bugbot-failed", attempts=3, jiraKey="WAR-1"),
                "XV-02": ticket("XV-02", jiraKey="WAR-2"),
            }
        )
        retried = inbound.handle(self.beam_path, "warp:retry XV-01")
        self.assertEqual(retried["ack"], "Received warp:retry XV-01. Requeueing XV-01.")
        saved = self.beam()["tickets"]["XV-01"]
        self.assertEqual(saved["status"], "queued")
        self.assertIsNone(saved["alarm"])
        self.assertEqual(saved["attempts"], 3)
        self.assertEqual(self.beam()["tickets"]["XV-02"]["status"], "awaiting_approval")

        missed = inbound.handle(self.beam_path, "warp:retry XV-99")
        self.assertIn("Nothing was requeued.", missed["ack"])
        self.assertEqual(self.beam()["tickets"]["XV-02"]["status"], "awaiting_approval")

        status = inbound.handle(self.beam_path, "warp:status")
        self.assertEqual(status["ack"], "Received warp:status. Posting the digest.")
        self.assertTrue((self.repo / ".warp" / "status-post.json").is_file())

        paused = inbound.handle(self.beam_path, "warp:pause")
        self.assertEqual(paused["ack"], "Received warp:pause. Pausing the run and stopping the listener.")
        self.assertEqual(self.beam()["runState"], "paused")
        self.assertEqual(inbound.status_line(self.beam()), "listener: stopped")

    def test_one_listener_is_idempotent_and_stops_with_the_run(self):
        first = inbound.claim(self.beam_path, "listener-a", pid="10")
        self.assertEqual(first, "listener: started listener-a")
        second = inbound.claim(self.beam_path, "listener-b", pid="11")
        self.assertEqual(second, "listener: already running listener-a")
        saved = self.beam()["listener"]
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["agentId"], "listener-a")
        self.assertEqual(saved["pid"], "10")
        self.assertNotIn("listener", self.beam()["tickets"]["XV-01"])
        self.assertNotIn("listener", self.beam()["tickets"]["XV-02"])

        same = inbound.claim(self.beam_path, "listener-a", pid="10")
        self.assertEqual(same, "listener: already running listener-a")

        scan.set_run(self.beam_path, "paused", "hold")
        self.assertEqual(self.beam()["runState"], "paused")
        self.assertEqual(self.beam()["listener"]["state"], "stopped")
        self.assertEqual(inbound.claim(self.beam_path, "listener-c"), "listener: started listener-c")

        beam.cmd_pause(self.beam_path, True, "button")
        self.assertEqual(self.beam()["listener"]["state"], "stopped")
        scan.set_run(self.beam_path, "running", None)
        self.assertEqual(self.beam()["listener"]["state"], "stopped")
        inbound.claim(self.beam_path, "listener-d")
        scan.set_run(self.beam_path, "stopped", "end")
        self.assertEqual(self.beam()["runState"], "stopped")
        self.assertEqual(self.beam()["listener"]["state"], "stopped")
        self.assertIn("listener-stop", self.journal_types())

    def test_drain_acks_a_queued_command_once(self):
        self.assertIn("queued", inbound.enqueue(self.beam_path, "warp:status", "shawn", "100.1", "slack"))
        self.assertIn("duplicate", inbound.enqueue(self.beam_path, "warp:status", "shawn", "100.1", "slack"))
        results = inbound.drain(self.beam_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ack"], "Received warp:status. Posting the digest.")
        self.assertEqual(inbound.drain(self.beam_path), [])

    def test_help_mentions_the_slot(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "inbound.py"), "?"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for needle in ("--beam", "--text", "--by", "--agent-id", "--pid", "--message-id", "--source", "claim", "release", "already running"):
            self.assertIn(needle, proc.stdout)


class ListenerInstructionTests(unittest.TestCase):
    def test_one_listener_not_one_per_ticket(self):
        skill = (ROOT / "skills" / "warp-listen" / "SKILL.md").read_text()
        agent = (ROOT / "agents" / "listener.md").read_text()
        start = (ROOT / "commands" / "warp-start.md").read_text()
        resume = (ROOT / "commands" / "warp-resume.md").read_text()
        pause = (ROOT / "commands" / "warp-pause.md").read_text()
        stop = (ROOT / "commands" / "warp-stop.md").read_text()
        for text in (skill, agent):
            folded = text.lower()
            self.assertIn("one listener for the beam", folded)
            self.assertIn("not one per awaiting_approval ticket", folded)
            self.assertIn("not one per shuttle", folded)
            self.assertIn("not one per reed", folded)
            self.assertIn("must not keep reading while paused or stopped", folded)
        self.assertIn("listener: already running", skill)
        self.assertIn("do not launch a second", skill.lower())
        for text in (start, resume):
            self.assertIn("warp-listen", text)
            self.assertIn("inbound.py claim", text)
            self.assertIn("do not launch a second", text.lower())
            self.assertIn("listener: already running", text)
        for text in (pause, stop):
            self.assertIn("inbound.py release", text)
            self.assertIn("must not keep reading while paused or stopped", text)
        self.assertNotIn("do not end the turn while awaiting approval", skill)
        self.assertNotIn("one reader per", skill)
