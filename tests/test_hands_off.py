"""1.7.18: no repair path sends a coordinator-owned pull request back to fix.

On 1.7.17 the repair sweep (sweep.py) still sent 43 coordinator-owned pull requests, among them #517, #1418 and
#1442, back to `fix`, and the next wake would have started a second agent on each.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import orchestrator  # noqa: E402
import ownership  # noqa: E402
import pipeline  # noqa: E402
import sweep  # noqa: E402
import watch  # noqa: E402
from test_flow_starts import LiveRepo  # noqa: E402

NOW = "2026-11-08T22:10:00Z"
OWNED = [517, 1418, 1442] + list(range(2000, 2040))  # 43
URL = "https://github.com/o/r/pull/%d"


def red(tid, number, **extra):
    row = {"id": tid, "status": "review", "phase": "reviewing", "locks": ["src/%s" % tid.lower()], "size": "S",
           "module": "m", "summary": tid, "deps": [], "autoMerge": True, "jiraKey": "HOS-%d" % number,
           "pr": {"url": URL % number, "headSha": "h%d" % number, "ci": "red", "ciSha": "h%d" % number,
                  "rollup": "red", "rollupSha": "h%d" % number, "bugbot": "fail", "bugbotFindings": ["x"],
                  "conflict": number % 2 == 0, "failedChecks": ["test"]},
           "stalled": True, "watch": {"stalledAt": "2026-11-08T20:00:00Z"}}
    row.update(extra)
    return row


class HandsOffTest(LiveRepo):
    def tickets(self):
        rows = [red("C-%d" % n, n) for n in OWNED]
        rows.append(red("W-1", 9, workerStartedAt="2026-11-08T19:00:00Z"))
        return rows

    def owners(self):
        return {str(n): {"owner": "coord-e49", "coordinator": "coord-e49", "agent": "bc-%d" % n} for n in OWNED}

    def test_the_repair_sweep_leaves_owned_prs_alone(self):
        data = {"runState": "running", "config": {"maxAgents": 60}, "tickets": {t["id"]: t for t in self.tickets()},
                "prOwners": self.owners(), "_repoRoot": str(self.tmp)}
        lines = sweep.run(data, provider={}, beam_path=self.path, now=NOW)
        statuses = {tid: t["status"] for tid, t in data["tickets"].items()}
        self.assertTrue(all(statuses["C-%d" % n] == "review" for n in OWNED), [k for k, v in statuses.items() if v != "review"])
        self.assertFalse(any(l.startswith("sweep: action C-") or l.startswith("start C-") for l in lines), lines)
        self.assertTrue(any("#1418 is owned by coord-e49" in l for l in lines), lines)

    def test_the_watch_fixer_and_entry_points_hold_off(self):
        data = {"tickets": {}, "prOwners": self.owners(), "_repoRoot": str(self.tmp)}
        ticket = red("C-517", 517)
        self.assertEqual(watch.choose_fix(data, ticket, {}), "")
        lines = []
        pipeline.begin_fix(data, ticket, ["ci red"], lines)
        pipeline.start_rebase(data, ticket, lines)
        self.assertEqual(ticket["status"], "review")
        self.assertNotIn("attempts", ticket)
        self.assertFalse(any(l.startswith("start ") for l in lines), lines)
        self.assertEqual(ownership.hands_off(data, {"id": "E", "status": "external", "externalOwner": "coord-x"}), "coord-x")

    def test_a_whole_supervise_pass_starts_no_second_agent(self):
        self.write(*self.tickets(), maxAgents=60, repairSweepMinutes=0)
        doc = self.beam()
        doc["prOwners"] = self.owners()
        self.path.write_text(json.dumps(doc))
        lines = orchestrator.supervise(self.path, now=NOW)
        starts = [l.split()[1] for l in lines if l.startswith(("start ", "resume "))]
        self.assertFalse([s for s in starts if s.startswith("C-")], starts)
        after = self.beam()["tickets"]
        self.assertEqual({after["C-%d" % n]["status"] for n in OWNED}, {"review"})
        self.assertNotEqual(after["W-1"]["status"], "review")  # Warp's own pull request still gets its fix


if __name__ == "__main__":
    unittest.main()
