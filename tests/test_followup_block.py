"""1.6.16: a Bugbot follow-up sits in its parent's block and does not reopen it."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import blocks  # noqa: E402


def beam(**follow):
    tickets = {
        "WV-01": {"id": "WV-01", "status": "merged", "gate": "G0"},
        "WV-11": {"id": "WV-11", "status": "merged", "gate": "G2"},
        "WV-16": {"id": "WV-16", "status": "merged", "gate": "G2"},
        "WV-20": {"id": "WV-20", "status": "queued", "gate": "G3"},
        "WV-11-BB1": dict({"id": "WV-11-BB1", "status": "coding", "gate": None, "followUpOf": "WV-11"}, **follow),
    }
    gates = [{"key": k, "members": [], "status": "green"} for k in ("G0", "G1", "G2", "G3")]
    return {"tickets": tickets, "gates": gates, "config": {}}


class FollowUpBlockTest(unittest.TestCase):
    def test_a_follow_up_is_in_its_parents_block(self):
        self.assertEqual(blocks.assign(beam())["WV-11-BB1"], "G2")

    def test_it_does_not_reopen_the_block_or_hold_the_next_one(self):
        data = beam()
        rows = {row["key"]: row for row in blocks.states(data)}
        self.assertTrue(rows["G0"]["cleared"])
        self.assertTrue(rows["G2"]["cleared"])
        self.assertEqual(rows["G2"]["followUps"], ["WV-11-BB1"])
        self.assertIsNone(blocks.hold_reason(blocks.plan(data), "WV-20"))

    def test_a_range_is_not_complete_while_a_follow_up_is_open(self):
        data = beam()
        data["tickets"]["WV-20"]["status"] = "merged"
        data["range"] = {"from": "G0", "to": "G3", "spec": "G0-G3"}
        self.assertFalse(blocks.range_done(data))
        data["tickets"]["WV-11-BB1"]["status"] = "merged"
        self.assertTrue(blocks.range_done(data))


if __name__ == "__main__":
    unittest.main()
