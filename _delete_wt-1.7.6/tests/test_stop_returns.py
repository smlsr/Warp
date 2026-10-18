"""1.7.5: a Shuttle that stops on a problem (lock-escape, failed, alarm) has returned, like one that writes done."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import agents  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import ticket_state  # noqa: E402

STARTED = "2026-10-17T18:00:00Z"
NOW = "2026-10-17T18:05:00Z"


def ticket(tid="T-1", step="implement", **kw):
    row = {"id": tid, "status": "coding", "phase": "implementing", "alarm": None, "locks": ["src/%s" % tid], "deps": [],
           "after": [], "size": "S", "summary": tid, "pr": {}, "branch": "warp/%s" % tid, "module": "core",
           "tokens": 0, "minutes": 0, "agent": "subagent:%s" % tid, "lastSeenAt": NOW, "workerStartedAt": STARTED,
           "shuttle": {"pending": True, "step": step, "startedAt": STARTED}}
    row.update(kw)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.parent.mkdir(parents=True)

    def make(self, *tickets, **cfg):
        config = {"maxAgents": 4, "runner": "local", "maxRecoveries": 5, "jiraTransition": False}
        config.update(cfg)
        data = {"runState": "running", "paused": False, "config": config, "gates": [],
                "tickets": {t["id"]: t for t in tickets}}
        self.path.write_text(json.dumps(data))
        (self.path.parent / "agents.json").write_text(json.dumps({"agents": [
            {"id": t["agent"], "ticket": t["id"], "role": "shuttle", "started": STARTED, "state": "running", "ended": None}
            for t in tickets], "spawns": []}))
        return json.loads(self.path.read_text())

    def write(self, data, tid, state, at=NOW, **event):
        row = {"at": at, "state": state, "id": tid, "agent": "subagent:%s" % tid}
        row.update(event)
        lines = []
        ticket_state._apply_event(self.path, data, data["tickets"][tid], row, NOW, set(), lines)
        return lines


class StopTest(Base):
    def test_a_lock_escape_is_repaired_at_once_not_after_stale_minutes(self):
        data = self.make(ticket())
        lines = self.write(data, "T-1", "lock-escape", escaped=["app/x.ts"])
        self.assertIn("shuttle: returned T-1 step=implement (lock-escape)", lines)
        t = data["tickets"]["T-1"]
        self.assertTrue(t["shuttle"]["returned"])
        self.assertFalse(orchestrator.shuttle_still_out(t, data, now=NOW))
        self.assertFalse(agents.live_rows(agents.registry(data, self.path), "T-1"))  # its row is ended
        self.path.write_text(json.dumps(data))
        out = pipeline.advance(data, beam_path=self.path, now=NOW)
        self.assertFalse(any("shuttle-out T-1" in l for l in out), out)
        self.assertTrue(any(l.startswith("alarm-repair: start T-1") or l.startswith("alarm-repair: launch T-1") for l in out), out)

    def test_a_failed_step_is_restarted_now(self):
        data = self.make(ticket())
        self.write(data, "T-1", "failed", error="tests would not build")
        t = data["tickets"]["T-1"]
        self.assertTrue(t["shuttle"]["failed"])
        out = pipeline.advance(data, beam_path=self.path, now=NOW)
        self.assertEqual(t["status"], "recovering", out)
        self.assertEqual(t["recoveries"], 1)
        self.assertTrue(any(l.startswith(("start T-1", "resume T-1")) for l in out), out)

    def test_an_alarm_it_names_frees_the_slot_and_the_fixer_restarts_it_in_the_same_pass(self):
        data = self.make(ticket())
        self.write(data, "T-1", "alarm", alarm="needs-decision", error="two specs disagree")
        out = pipeline.advance(data, beam_path=self.path, now=NOW)
        self.assertIn("shuttle: T-1 returned in alarm (needs-decision) after writing alarm; its slot is free", out)
        self.assertIn("fixer: T-1 shuttle", out)
        self.assertEqual(data["tickets"]["T-1"]["shuttle"]["step"], "restart")

    def test_a_stuck_alarm_restarts(self):
        data = self.make(ticket())
        self.write(data, "T-1", "alarm", alarm="stuck")
        out = pipeline.advance(data, beam_path=self.path, now=NOW)
        self.assertEqual(data["tickets"]["T-1"]["status"], "recovering", out)

    def test_a_stop_line_from_an_earlier_or_another_shuttle_is_not_a_return(self):
        data = self.make(ticket())
        self.write(data, "T-1", "failed", at="2026-10-17T17:00:00Z")
        self.assertNotIn("returned", data["tickets"]["T-1"]["shuttle"])
        row = {"at": NOW, "state": "failed", "id": "T-1", "agent": "subagent:someone-else"}
        ticket_state._apply_event(self.path, data, data["tickets"]["T-1"], row, NOW, set(), [])
        self.assertNotIn("returned", data["tickets"]["T-1"]["shuttle"])

    def test_the_prompt_says_how_to_stop(self):
        lines = orchestrator.done_lines(ticket(), "python3 s/ticket_state.py append --id T-1 --state done --agent a1")
        self.assertTrue(lines[-1].startswith("If you cannot finish the step, write one of these as your last state instead of done"))
        self.assertIn("--state lock-escape --escaped <path> --agent a1", lines[-1])
        self.assertIn("--state failed --error", lines[-1])


if __name__ == "__main__":
    unittest.main()
