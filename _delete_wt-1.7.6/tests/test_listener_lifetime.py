"""1.7.4: the listener outlives a parent woken by a timer.

Before, it died three ways: the end-of-turn `session-stop` released it (118 stops), the 5-minute orphan check fired
between 10-minute timer wakes, and a rotation left no listener until the next parent pass.
"""

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

import inbound  # noqa: E402
import orchestrator  # noqa: E402
import session_note  # noqa: E402

NOW = "2026-10-16T21:35:58Z"


def plus(stamp, minutes):
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        (self.warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\nlistenerOrphanMinutes: 5\n")
        self.beam_path = self.warp / "beam.json"
        self.beam_path.write_text(json.dumps({
            "version": 1, "runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "review"}}, "gates": [],
            "config": {"listenerFastSeconds": 4, "listenerSlowSeconds": 120, "listenerMaxMinutes": 60, "listenerMaxReads": 4}}) + "\n")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def write(self, data):
        self.beam_path.write_text(json.dumps(data) + "\n")


class SessionStopTest(Base):
    def test_the_end_of_a_turn_keeps_the_listener_while_the_run_runs(self):
        session_note.note(self.warp, "session-stop")
        raw = self.beam()["listener"]
        self.assertEqual((raw["holder"], raw["state"]), ("listener", "running"))
        self.assertTrue(raw.get("parentSeenAt"))  # and it counts as a heartbeat

    def test_a_paused_run_still_ends_it(self):
        data = self.beam()
        data.update(runState="paused", paused=True)
        self.write(data)
        session_note.note(self.warp, "session-stop")
        self.assertEqual(self.beam()["listener"]["holder"], "")


class TimerParentTest(Base):
    def test_a_parent_woken_every_ten_minutes_is_not_an_orphan(self):
        self.assertIn("parent: alive at 21:35:58Z (woken every 600s); the listener waits up to 13m for the next one",
                      orchestrator.parent_alive(self.beam_path, every=600, now=NOW))
        # 6 minutes with no pass: past listenerOrphanMinutes 5, inside the timer's 13.
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=plus(NOW, 6))
        self.assertEqual(lines[0], "reap: continue")
        self.assertEqual(self.beam()["listener"]["holder"], "listener")
        # A wake that never comes still ends it.
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=plus(NOW, 14))
        self.assertEqual(lines[0], "reap: exit orphaned")

    def test_parent_commands_are_heartbeats(self):
        inbound.parent_alive_quiet(self.beam_path)
        self.assertTrue(self.beam()["listener"]["parentSeenAt"])

    def test_no_listener_up_says_to_run_a_pass(self):
        data = self.beam()
        inbound.stamp_return(data, self.beam_path, "rotated", NOW, holder="listener")
        self.write(data)
        got = orchestrator.parent_alive(self.beam_path, every=600, now=NOW)
        self.assertIn("listener: none is up; run a supervise pass now to start one", got)


class RotationTest(Base):
    def test_rotation_waits_for_a_waiting_parent_and_age_expiry_too(self):
        for minute in range(1, 6):
            lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=plus(NOW, minute))
            self.assertEqual(lines[0], "reap: continue", lines)
            inbound.touch_parent(self.beam_path, plus(NOW, minute))
        self.assertEqual(self.beam()["listener"]["holder"], "listener")  # past 4 reads, still up: no parent waits
        data = self.beam()
        data["config"]["listenerMaxMinutes"] = 1
        self.write(data)
        # A timer parent's pass does not expire it by age either.
        self.assertIn("hold reason=live", orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 6))[0])
        # Once the parent waits, the rotation goes ahead and the wait wakes on the stamp.
        inbound.touch_parent(self.beam_path, plus(NOW, 7), waiting=True)
        self.assertEqual(inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=plus(NOW, 7)), ["listen: rotate"])

    def test_an_expired_lease_nobody_hands_back_is_freed(self):
        data = self.beam()
        inbound.expire_lease(data, NOW)
        self.write(data)
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 1)),
                         ["listener: hold reason=await-return generation=2"])
        later = orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 9))
        self.assertIn("listener: generation 2 never handed its expired lease back; its slot is free.", later)
        self.assertEqual(later[-1], "listener: poll")


if __name__ == "__main__":
    unittest.main()
