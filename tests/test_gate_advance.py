"""A pending gate turns green when every member is merged or done."""

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

NOW = "2026-10-06T12:00:00Z"
MEMBERS = ["P-002", "P-003", "P-004", "P-018"]


def plus(stamp, minutes):
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(tid, status="merged", **extra):
    row = {
        "id": tid,
        "status": status,
        "summary": "ship %s" % tid,
        "size": "S",
        "complexity": "LOW",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": True,
        "deps": extra.pop("deps", []),
        "locks": ["src/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "agent": None,
        "branch": "warp/%s" % tid,
        "attempts": 0,
        "alarm": None,
        "pr": {},
        "jira": {},
    }
    row.update(extra)
    return row


class GateAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        warp = self.repo / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"

    def write(self, tickets, gates, **extra):
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": gates,
            "runState": "running",
            "paused": False,
            "baseGreen": True,
            "config": {"maxAgents": 4, "alarmRepairMinutes": 15, "runner": "cloud"},
            "program": {"criticalPath": []},
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def load(self):
        return json.loads(self.beam_path.read_text())

    def gate(self, key):
        return next(g for g in self.load()["gates"] if g["key"] == key)

    def shawn(self, member_status=None, run_state="running"):
        """G0 green. G1 pending. P-019 is blocked only by G1."""
        statuses = {tid: "merged" for tid in MEMBERS}
        if member_status:
            statuses.update(member_status)
        tickets = {
            tid: ticket(tid, status=statuses[tid], recoveries=2 if tid == "P-018" else 0) for tid in MEMBERS
        }
        tickets["P-019"] = ticket("P-019", status="queued", deps=["P-002"])
        gates = [
            {
                "key": "G0",
                "name": "foundation",
                "blocking": True,
                "members": [],
                "checks": [],
                "status": "green",
                "evidence": "already green",
                "greenAt": "2026-10-01T00:00:00Z",
            },
            {
                "key": "G1",
                "name": "wave-1",
                "blocking": True,
                "members": list(MEMBERS),
                "checks": ["review"],
                "status": "pending",
                "evidence": None,
                "greenAt": None,
            },
        ]
        self.write(tickets, gates, runState=run_state, paused=run_state == "paused")

    def lines(self, now=NOW):
        return alarm_repair.next_repair(self.beam_path, now=now)

    def starts(self, lines):
        return [line for line in lines if line.startswith("start ")]

    def test_all_members_merged_clears_the_gate_and_readies_the_dependent(self):
        self.shawn()
        self.assertEqual(beam.ready(self.load()), [])
        got = self.lines()
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", got)
        self.assertEqual(len(self.starts(got)), 1)
        self.assertTrue(self.starts(got)[0].startswith("start P-019 "))
        g1 = self.gate("G1")
        self.assertEqual(g1["status"], "green")
        self.assertEqual(g1["evidence"], "members merged: P-002, P-003, P-004, P-018")
        self.assertEqual(g1["greenAt"], NOW)
        self.assertEqual(self.gate("G0")["status"], "green")
        self.assertEqual(self.gate("G0")["evidence"], "already green")
        self.assertEqual([t["id"] for t in beam.ready(self.load())], ["P-019"])
        board = (self.beam_path.parent / "BOARD.md").read_text()
        self.assertIn("| G1 wave-1 | green | P-002, P-003, P-004, P-018 |", board)
        journal = (self.beam_path.parent / "journal.jsonl").read_text()
        self.assertIn('"type":"gate"', journal)
        self.assertIn("P-018", journal)

    def test_done_member_counts_as_merged(self):
        self.shawn({"P-003": "done"})
        got = self.lines()
        self.assertEqual(self.gate("G1")["status"], "green")
        self.assertTrue(self.starts(got))

    def test_one_unmerged_member_leaves_the_gate_pending(self):
        self.shawn({"P-004": "queued"})
        lines, data = beam.refresh_pending_gates(self.beam_path, now=NOW, run_dispatch=True)
        self.assertEqual(lines, [])
        self.assertEqual(self.gate("G1")["status"], "pending")
        self.assertIsNone(self.gate("G1")["evidence"])
        ready_ids = [t["id"] for t in beam.ready(data)]
        self.assertIn("P-004", ready_ids)
        self.assertNotIn("P-019", ready_ids)
        for status in ("claimed", "coding", "awaiting_approval", "parked", "merging", "skipped", "blocked"):
            self.shawn({"P-004": status})
            flipped = beam.advance_pending_gates(self.load(), NOW)
            self.assertEqual(flipped, [], status)
            self.assertEqual(self.gate("G1")["status"], "pending")

    def test_alarmed_member_leaves_the_gate_pending(self):
        self.shawn({"P-018": "alarm"})
        data = self.load()
        data["tickets"]["P-018"]["alarm"] = "worker-died"
        data["tickets"]["P-018"]["recoveries"] = 3
        self.beam_path.write_text(json.dumps(data) + "\n")
        self.assertEqual(beam.ready(self.load()), [])
        got = self.lines()
        self.assertEqual(self.gate("G1")["status"], "pending")
        self.assertFalse(self.starts(got))
        self.assertNotIn("herald: G1 pending cleared. Members merged. Tick ran.", got)
        self.assertEqual(beam.ready(self.load()), [])

    def test_second_pass_does_not_double_dispatch(self):
        self.shawn()
        first = self.lines()
        self.assertEqual(len(self.starts(first)), 1)
        green_at = self.gate("G1")["greenAt"]
        immediate = self.lines()
        self.assertIn("alarm-repair: wait", immediate)
        self.assertEqual(self.starts(immediate), [])
        second = self.lines(now=plus(NOW, 15))
        self.assertEqual(self.starts(second), [])
        self.assertNotIn("herald: G1 pending cleared. Members merged. Tick ran.", second)
        self.assertEqual(self.gate("G1")["status"], "green")
        self.assertEqual(self.gate("G1")["greenAt"], green_at)
        self.assertEqual([t["id"] for t in beam.ready(self.load())], ["P-019"])

    def test_tick_recomputes_without_waiting_for_the_pass(self):
        self.shawn()
        data = self.load()
        data["alarmRepair"] = {"checkedAt": NOW, "tried": [], "passOpen": False}
        self.beam_path.write_text(json.dumps(data) + "\n")
        self.assertIn("alarm-repair: wait", self.lines())
        self.assertEqual(self.gate("G1")["status"], "pending")
        lines, refreshed = beam.refresh_pending_gates(self.beam_path, now=NOW, run_dispatch=True)
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", lines)
        self.assertEqual(len(self.starts(lines)), 1)
        self.assertEqual([t["id"] for t in beam.ready(refreshed)], ["P-019"])
        again, _data = beam.refresh_pending_gates(self.beam_path, now=NOW, run_dispatch=True)
        self.assertEqual(self.starts(again), [])
        self.assertEqual(again, [])

    def test_ready_cli_dispatches_once(self):
        self.shawn()
        first = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "beam.py"), "ready", "--beam", str(self.beam_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", first.stdout)
        self.assertIn("start P-019 ", first.stdout)
        self.assertIn("P-019\t", first.stdout)
        second = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "beam.py"), "ready", "--beam", str(self.beam_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("start ", second.stdout)
        self.assertNotIn("herald:", second.stdout)
        self.assertIn("P-019\t", second.stdout)

    def test_green_and_red_are_left_alone(self):
        self.shawn()
        data = self.load()
        data["gates"][1]["status"] = "green"
        data["gates"][1]["evidence"] = "manual checks"
        data["gates"][1]["greenAt"] = "2026-10-02T00:00:00Z"
        data["gates"].append(
            {
                "key": "G2",
                "name": "wave-2",
                "blocking": True,
                "members": ["P-002"],
                "checks": [],
                "status": "red",
                "evidence": "ci red",
                "greenAt": None,
            }
        )
        # P-019 depends on P-002, so the red G2 still blocks it. Ready stays empty.
        self.beam_path.write_text(json.dumps(data) + "\n")
        got = self.lines()
        self.assertEqual(self.starts(got), [])
        self.assertEqual(self.gate("G1")["status"], "green")
        self.assertEqual(self.gate("G1")["evidence"], "manual checks")
        self.assertEqual(self.gate("G1")["greenAt"], "2026-10-02T00:00:00Z")
        self.assertEqual(self.gate("G2")["status"], "red")

    def test_every_ticket_merged_clears_a_pending_gate_and_dispatches(self):
        tickets = {tid: ticket(tid, status="merged") for tid in MEMBERS}
        self.write(
            tickets,
            [
                {
                    "key": "G1",
                    "name": "wave-1",
                    "blocking": True,
                    "members": list(MEMBERS),
                    "checks": ["review"],
                    "status": "pending",
                    "evidence": None,
                    "greenAt": None,
                }
            ],
        )
        self.assertEqual(beam.ready(self.load()), [])
        got = self.lines()
        self.assertEqual(self.gate("G1")["status"], "green")
        self.assertEqual(self.gate("G1")["evidence"], "members merged: P-002, P-003, P-004, P-018")
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", got)
        self.assertNotIn("_clearedCondition", self.gate("G1"))
        again = self.lines(now=plus(NOW, 15))
        self.assertNotIn("herald: G1 pending cleared. Members merged. Tick ran.", again)
        self.assertEqual(self.starts(again), [])

    def test_stale_red_clears_when_members_are_merged(self):
        self.shawn()
        data = self.load()
        data["gates"][1]["status"] = "red"
        data["gates"][1]["evidence"] = None
        self.beam_path.write_text(json.dumps(data) + "\n")
        got = self.lines()
        self.assertEqual(self.gate("G1")["status"], "green")
        self.assertIn("herald: G1 red cleared. Members merged. Tick ran.", got)
        self.assertEqual([t["id"] for t in beam.ready(self.load())], ["P-019"])
        self.assertTrue(self.starts(got)[0].startswith("start P-019 "))

    def test_red_check_on_a_merged_member_is_not_cleared(self):
        self.shawn()
        data = self.load()
        data["tickets"]["P-002"]["pr"] = {"ci": "red"}
        self.beam_path.write_text(json.dumps(data) + "\n")
        got = self.lines()
        self.assertEqual(self.gate("G1")["status"], "pending")
        self.assertIsNone(self.gate("G1")["evidence"])
        self.assertEqual(self.starts(got), [])
        self.assertEqual(beam.ready(self.load()), [])

    def test_paused_and_stopped_do_not_recompute(self):
        for state in ("paused", "stopped"):
            self.shawn(run_state=state)
            got = self.lines()
            self.assertTrue(got[0].startswith("alarm-repair: skipped"), got)
            self.assertEqual(self.gate("G1")["status"], "pending")
            lines, _data = beam.refresh_pending_gates(self.beam_path, now=NOW, run_dispatch=True)
            self.assertEqual(lines, [])
            self.assertEqual(self.gate("G1")["status"], "pending")
