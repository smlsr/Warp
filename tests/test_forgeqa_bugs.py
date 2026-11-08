"""1.7.17: the Warp 1.7.16 bugs filed from the Forge-QA run (instance b8f371, 8 Nov 2026)."""

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

import audit  # noqa: E402
import autoscan  # noqa: E402
import beam  # noqa: E402
import fleet  # noqa: E402
import gaps  # noqa: E402
import selfdrive  # noqa: E402
import unpark  # noqa: E402

NOW = "2026-11-08T21:50:00Z"
REPORT = """# Gaps

| id | gap | status | lands in |
|---|---|---|---|
| G-001 | APP-03 shell has no session timeout handling | open | E27-S03, E27-S05 |
| G-002 | APP-04 export drops the tenant filter on page two | closed (E27-S06) | E27-S06 |
| G-003 | APP-05 audit log misses role changes for members | open | E49-VT-J05 |
| G-004 | APP-06 report header overflows on narrow screens | Resolved | |
| G-009 | Queue retry never backs off after a provider 429 | open | E50-X01 |
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.path = self.tmp / ".warp" / "beam.json"
        self.tickets = {tid: {"id": tid, "status": "merged"} for tid in ("E27-S03", "E27-S05", "E27-S06", "E49-VT-J05")}
        self.tickets["E50-X01"] = {"id": "E50-X01", "status": "coding"}
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "config": {}, "tickets": self.tickets}))


class GapReportTest(Base):
    def test_ids_closed_rows_lands_in_and_living_copy_only(self):
        (self.tmp / "docs" / "spec").mkdir(parents=True)
        (self.tmp / "docs" / "spec" / "GAP-REPORT.md").write_text(REPORT)
        (self.tmp / "PROOFLINE" / "spec").mkdir(parents=True)
        (self.tmp / "PROOFLINE" / "spec" / "GAP-REPORT.md").write_text(REPORT.replace("G-009", "G-777"))
        found = audit.discover(self.tmp, {"tickets": self.tickets}, {})
        self.assertEqual([g["gid"] for g in found], ["G-009"])  # 001/003 landed (all merged), 002/004 closed
        self.assertEqual(gaps.gap_id("G", "G-001"), "G-001")
        self.assertEqual(gaps.gap_id("AUTH", "rbac"), "G-AUTH-RBAC")

    def test_an_imported_gap_is_parked_for_triage_until_it_has_locks_and_acs(self):
        gid, lines = gaps.create(self.path, lambda: beam.load_json(self.path), "G", "G-009", "Queue retry never backs off",
                                 source="GAP-REPORT", row="G-009", gid="G-009")
        row = beam.load_json(self.path)["tickets"]["G-009"]
        self.assertEqual((gid, row["status"], row["triage"]), ("G-009", "parked", True))
        self.assertIn("needs triage", lines[-1])
        self.assertFalse(unpark.runnable({"tickets": {}}, row))
        self.assertEqual(unpark.reconsider({"tickets": {}}, row, {}, [], NOW), "")
        row["locks"], row["acs"] = ["src/queue"], [{"id": "AC-1", "text": "backs off"}]
        self.assertEqual(unpark.reconsider({"tickets": {}}, row, {}, [], NOW), "queued")
        # The same row again: linked, never a second ticket.
        again, lines = gaps.create(self.path, lambda: beam.load_json(self.path), "G", "G-009", "Queue retry never backs off",
                                   row="G-009", gid="G-009")
        self.assertIn("already covers it", lines[0])


class FleetTextTest(unittest.TestCase):
    def test_the_pr_comes_from_the_branch_and_never_prints_hash_question(self):
        items = [{"id": "bc-1", "status": "RUNNING", "updatedAt": "2026-11-08T18:00:00Z", "target": {"branchName": "e49/vt-j20"}},
                 {"id": "bc-2", "status": "RUNNING", "updatedAt": "2026-11-08T18:00:00Z", "name": "coord worker for #1442"},
                 {"id": "bc-3", "status": "RUNNING", "updatedAt": "2026-11-08T18:00:00Z", "target": {"branchName": "e49/scratch"}}]
        data = {"tickets": {"T-1": {"id": "T-1", "agent": "bc-4"}}}
        rows = fleet.inventory(items, [], data, heads={"e49/vt-j20": "1418"})
        self.assertEqual([r["pr"] for r in rows], ["1418", "1442", ""])
        self.assertEqual([fleet.where(r) for r in rows], ["#1418", "#1442", "your branch e49/scratch"])
        self.assertEqual(fleet.where({"ticket": "T-1"}), "ticket T-1")
        told = []
        cfg = selfdrive.config({}, None)
        stalled = [dict(r, **{"class": "stalled", "why": "idle"}) for r in rows]
        fleet.act(Path(tempfile.mkdtemp()) / "beam.json", {}, stalled, [], cfg, stop=lambda r: (True, ""),
                  send=lambda o, t, a="": told.append(t) or [], restaff=lambda r: [], assign=lambda n: [], now=NOW)
        self.assertTrue(all("#?" not in t for t in told), told)
        self.assertIn("on your branch e49/scratch", told[2])


class ParkTest(Base):
    def test_beam_py_park(self):
        self.tickets["G-G-G-001"] = {"id": "G-G-G-001", "status": "coding", "phase": "implementing", "alarm": "stalled"}
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "config": {}, "tickets": self.tickets}))
        lines = beam.park(self.path, "G-G-G-001", "imported from a resolved GAP-REPORT row")
        row = beam.load_json(self.path)["tickets"]["G-G-G-001"]
        self.assertEqual((row["status"], row["phase"], row["alarm"], row["parkedBy"]), ("parked", "parked", None, "hand"))
        self.assertTrue(lines[0].startswith("park: G-G-G-001 parked (was coding)"))
        self.assertEqual(autoscan.dead_end({"config": {}}, row), "")  # the auto-scan does not reset it
        self.assertEqual(unpark.reconsider({"tickets": self.tickets}, row, {}, [], NOW), "")
        self.assertFalse(unpark.runnable({"tickets": self.tickets}, row))
        self.assertEqual(unpark.reconsider({"tickets": self.tickets}, row, {"prState": "MERGED"}, [], NOW), "merged")


if __name__ == "__main__":
    unittest.main()
