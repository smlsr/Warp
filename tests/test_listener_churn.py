"""1.7.9: why each listener ended is kept, churn is named with its cause, and the auto-scan runs on its own clock."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import autoscan  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-20T12:00:00Z"


def plus(stamp, seconds):
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        (self.warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\nautoScan: 600\n")
        self.path = self.warp / "beam.json"
        self.path.write_text(json.dumps({
            "version": 1, "runState": "running", "gates": [],
            "tickets": {"T-1": {"id": "T-1", "status": "review", "size": "M", "locks": [], "deps": [], "module": "app", "summary": "t", "branch": "warp/T-1"}},
            "config": {"listenerFastSeconds": 4, "listenerSlowSeconds": 120, "maxRecoveries": 5}}) + "\n")
        self.assertEqual(orchestrator.supervise_listener(self.path, now=NOW), ["listener: poll"])

    def beam(self):
        return json.loads(self.path.read_text())

    def write(self, data):
        self.path.write_text(json.dumps(data) + "\n")


class HistoryTest(Base):
    def test_each_end_is_kept_with_its_life_and_reads(self):
        inbound.poll_lines(self.path, lease=1, holder="listener", now=plus(NOW, 10))
        inbound.mark_returned(self.path, "cut-off", lease=1, holder="listener", now=plus(NOW, 70))
        row = self.beam()["listener"]["history"][-1]
        self.assertEqual((row["g"], row["reason"], row["lived"], row["reads"]), (1, "cut-off", 70, 1))
        journal = (self.warp / "journal.jsonl").read_text()
        self.assertIn('"type":"listener-end"', journal)
        lines = inbound.history_lines(self.beam())
        self.assertIn("listener: generation 1 ended cut-off at 12:01:10Z, lived 1m10s, 1 reads", lines)

    def test_a_copy_whose_lease_was_taken_says_so(self):
        inbound.take_lease(self.path, "listener", now=plus(NOW, 30))  # generation 2 holds it now
        lines = inbound.poll_lines(self.path, lease=1, holder="listener", now=plus(NOW, 40))
        self.assertIn("listen: stopped superseded lease=1", lines)
        row = self.beam()["listener"]["history"][-1]
        self.assertEqual((row["g"], row["reason"], row["by"]), (1, "superseded", 2))

    def test_three_short_lives_in_a_row_are_churn_with_the_cause(self):
        at = NOW
        for gen in (1, 2, 3):
            inbound.mark_returned(self.path, "cut-off", lease=gen, holder="listener", now=plus(at, 40))
            at = plus(at, 60)
            got = orchestrator.supervise_listener(self.path, now=at)
        self.assertEqual(got[-1], "listener: poll")
        churn = [line for line in got if line.startswith("listener: churn.")]
        self.assertEqual(len(churn), 1, got)
        self.assertIn("The last 3 listeners lived 40s, 40s, 40s (cut-off 3)", churn[0])
        self.assertIn("flagged the listener's poll-and-wait cycle as a loop", churn[0])
        self.assertTrue(any(line.startswith("herald: Listener churn:") for line in got))
        # Posted once an hour, named every pass.
        inbound.mark_returned(self.path, "cut-off", lease=4, holder="listener", now=plus(at, 40))
        again = orchestrator.supervise_listener(self.path, now=plus(at, 60))
        self.assertTrue(any(line.startswith("listener: churn.") for line in again))
        self.assertFalse(any(line.startswith("herald:") for line in again))

    def test_long_lives_are_not_churn(self):
        inbound.mark_returned(self.path, "cut-off", lease=1, holder="listener", now=plus(NOW, 600))
        self.assertEqual(inbound.churn_line(self.beam()), "")


class ClockTest(Base):
    def test_a_wake_a_few_seconds_early_is_on_time(self):
        data = self.beam()
        data["autoScan"] = {"at": NOW}
        self.assertTrue(autoscan.due(data, self.path, plus(NOW, 598)))
        self.assertTrue(autoscan.due(data, self.path, plus(NOW, 540)))
        self.assertFalse(autoscan.due(data, self.path, plus(NOW, 539)))

    def test_the_listener_runs_the_scan_when_no_pass_ran_and_the_next_pass_posts_it(self):
        data = self.beam()
        data["autoScan"] = {"at": NOW, "count": 2}
        data["pass"] = {"startedAt": NOW, "count": 9}
        data["tickets"]["T-1"].update(status="alarm", alarm="worker-died", recoveries=5)
        self.write(data)
        # Inside the interval: the listener leaves it alone.
        early = inbound.poll_lines(self.path, lease=1, holder="listener", now=plus(NOW, 300))
        self.assertFalse(any("auto-scan" in line for line in early), early)
        # Eleven minutes with no pass and the parent not waiting: the poll runs it.
        lines = inbound.poll_lines(self.path, lease=1, holder="listener", now=plus(NOW, 660))
        ran = [line for line in lines if "auto-scan ran here" in line]
        self.assertEqual(len(ran), 1, lines)
        self.assertIn("listen: no parent pass for 11m", ran[0])
        self.assertTrue(ran[0].endswith("Keep cycling."))
        after = self.beam()
        self.assertEqual(after["autoScan"]["count"], 3)
        self.assertEqual(after["autoScan"]["by"], "listener")
        self.assertNotEqual(after["tickets"]["T-1"]["status"], "alarm")
        self.assertEqual(after["listener"]["holder"], "listener")  # the listener is still up
        posted = autoscan.unposted_lines(self.path)
        self.assertTrue(posted[0].startswith("autoscan: scan 3 ran from the listener at 12:11:00Z"), posted)
        self.assertTrue(any(line.startswith("herald: Auto-scan: reset T-1") for line in posted), posted)
        self.assertEqual(autoscan.unposted_lines(self.path), [])

    def test_a_waiting_parent_runs_it_itself(self):
        data = self.beam()
        data["autoScan"] = {"at": NOW, "count": 2}
        data["pass"] = {"startedAt": NOW, "count": 9}
        self.write(data)
        inbound.touch_parent(self.path, plus(NOW, 650), waiting=True)
        lines = inbound.poll_lines(self.path, lease=1, holder="listener", now=plus(NOW, 660))
        self.assertFalse(any("auto-scan" in line for line in lines), lines)
        self.assertEqual(self.beam()["autoScan"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
