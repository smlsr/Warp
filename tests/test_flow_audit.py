"""1.7.13: the stall paths a flow audit found, each one a run that sat still with no line telling the parent what to do."""

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

import basecheck  # noqa: E402
import beam as beam_mod  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import runs  # noqa: E402
from test_flow_starts import LiveRepo, done_ticket, queued  # noqa: E402

NOW = "2026-10-26T13:30:00Z"


def at(minutes, base=NOW):
    moment = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def data_of(*tickets, **cfg):
    config = {"maxAgents": 4, "runner": "local", "launch": "worktree", "subagentVm": False, "bugbotRequired": False,
              "autoMergeSizes": ["S", "M"]}
    config.update(cfg)
    return {"version": 1, "runState": "running", "baseGreen": True, "gates": [], "config": config,
            "tickets": {t["id"]: t for t in tickets}}


class FixerTest(unittest.TestCase):
    def test_a_working_shuttle_past_its_phase_timer_is_not_parked(self):
        start = at(-100)
        row = queued("UI-1", status="coding", phase="implementing", branch="warp/ui/UI-1", workerStartedAt=start,
                     claimedAt=start, shuttle={"pending": True, "step": "implement", "startedAt": start})
        data = data_of(row, staleMinutes=30)
        for minute in range(0, 21, 3):
            row["lastSeenAt"] = at(minute)  # its heartbeat is fresh every pass
            pipeline.advance(data, {}, now=at(minute))
        self.assertNotEqual(row["status"], "parked")
        self.assertFalse((row.get("watch") or {}).get("fixerAttempts"))

    def test_slow_ci_is_rerun_once_a_window_not_every_pass(self):
        row = done_ticket("UI-2", status="review", phase="reviewing")
        row["pr"].update(rollup="pending", ci="pending")
        data = data_of(row)
        reruns = 0
        for minute in range(0, 60, 3):
            lines = pipeline.advance(data, {}, now=at(minute, at(50)))
            reruns += sum(1 for line in lines if line == "ci: rerun UI-2")
        self.assertLessEqual(reruns, 2)
        self.assertNotEqual(row["status"], "parked")


class BaseFixTest(unittest.TestCase):
    def test_a_red_base_gets_a_fix_ticket_that_starts_and_green_prs_still_merge(self):
        data = data_of(done_ticket("UI-1"), queued("UI-2"), maxAgents=1)
        basecheck.apply(data, {"sha": "abcdef1234", "state": "red", "failed": ["build"]}, NOW)
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertIn("merge: UI-1 merged", lines)
        self.assertTrue(any(line.startswith("start BASE-abcdef1 ") for line in lines), lines)
        self.assertFalse(any(line.startswith("dispatch-base-fix") for line in lines), lines)
        self.assertIn("any path the fix of the base branch needs", orchestrator.locks_text(data["tickets"]["BASE-abcdef1"]))
        basecheck.apply(data, {"sha": "fffffff000", "state": "green", "failed": []}, at(10))
        self.assertIs(data["baseGreen"], True)

    def test_a_base_fix_never_started_is_skipped_when_main_goes_green(self):
        data = data_of(queued("UI-1"), maxAgents=0)
        basecheck.apply(data, {"sha": "abcdef1234", "state": "red", "failed": ["build"]}, NOW)
        lines = basecheck.apply(data, {"sha": "fffffff000", "state": "green", "failed": []}, at(5))
        self.assertIn("base: BASE-abcdef1 skipped; main is green without it", lines)


class QueuedLeftoverTest(unittest.TestCase):
    def test_a_queued_ticket_with_a_leftover_ready_phase_is_started_not_reviewed(self):
        data = data_of(queued("UI-3", phase="ready"), maxInProgress=1)
        self.assertFalse(beam_mod.ticket_in_progress(data["tickets"]["UI-3"]))
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertIn("dispatch: UI-3 is queued with a leftover phase ready; cleared, so the refill starts it", lines)
        self.assertTrue(any(line.startswith("start UI-3 ") for line in lines), lines)

    def test_a_queued_ticket_with_a_leftover_implementing_phase_waits_for_its_dependency(self):
        data = data_of(queued("UI-4"), queued("UI-5", deps=["UI-4"], phase="implementing"), maxAgents=4)
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertFalse(any(line.startswith("start UI-5 ") for line in lines), lines)
        self.assertTrue(any(line.startswith("start UI-4 ") for line in lines), lines)


class ReadyRecheckTest(unittest.TestCase):
    def test_a_ready_ticket_whose_ci_went_red_goes_to_fix(self):
        row = done_ticket("UI-6")
        row["pr"].update(ci="red", rollup="red")
        data = data_of(row)
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertIn("review: UI-6 left ready: ci red; rollup red; back to review", lines)
        self.assertEqual(row["phase"], "fixing")
        self.assertTrue(any(line.startswith("start UI-6 ") and "step=fix" in line for line in lines), lines)


class RepairTest(LiveRepo):
    def test_a_lock_escape_repair_starts_on_one_free_slot_and_its_launch_repeats(self):
        row = queued("UI-7", status="alarm", alarm="lock-escape", phase="implementing", escaped=["src/other.py"],
                     branch="warp/ui/UI-7", agent="subagent:UI-7", claimedAt=NOW)
        self.write(row, maxAgents=1)
        lines = orchestrator.supervise(self.path, now=NOW)
        self.assertTrue(any(line.startswith("alarm-repair: start UI-7") for line in lines), lines)
        for minute in (3, 6, 15, 30):
            lines = orchestrator.supervise(self.path, now=at(minute))
            self.assertIn("alarm-repair: not launched UI-7. Launch it now:", lines)
            self.assertFalse(any(line.startswith("start UI-7 ") for line in lines), lines)
        saved = self.beam()["tickets"]["UI-7"]
        self.assertEqual((saved["status"], saved["alarm"]), ("claimed", "lock-escape"))


class DeadDepTest(unittest.TestCase):
    def test_a_dependency_that_will_never_merge_is_named_and_alarmed_once(self):
        parked = done_ticket("UI-8", status="parked", phase="parked")
        data = data_of(parked, queued("UI-9", deps=["UI-8"]), queued("UI-10", deps=["UI-99"]))
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertIn("deps: UI-9 waits on UI-8 (parked); it will not merge on its own: warp:retry UI-8 to work it again, "
                      "or drop it from UI-9's dependencies", lines)
        self.assertTrue(any(line.startswith("deps: UI-10 waits on UI-99 (no ticket has that id)") for line in lines))
        self.assertEqual(sum(1 for line in lines if line.startswith("herald: ALARM")), 2)
        again = pipeline.advance(data, {}, now=at(3))
        self.assertFalse(any(line.startswith("herald: ALARM") for line in again))

    def test_a_parked_ticket_merged_by_hand_is_recorded(self):
        parked = done_ticket("UI-11", status="parked", phase="parked")
        data = data_of(parked, queued("UI-12", deps=["UI-11"]))
        lines = pipeline.advance(data, {"UI-11": {"prState": "MERGED", "mergeSha": "d" * 40}}, now=NOW)
        self.assertEqual(parked["status"], "merged")
        self.assertTrue(any(line.startswith("start UI-12 ") for line in lines), lines)


class ExternalRunTest(unittest.TestCase):
    def test_a_stopped_or_silent_run_holds_no_locks(self):
        now = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)
        self.assertEqual(runs._run_gone({"runState": "stopped"}, now), "the run is stopped")
        self.assertEqual(runs._run_gone({"runState": "running", "checkpointAt": "2026-10-26T01:00:00Z"}, now), "no checkpoint for 12h")
        self.assertEqual(runs._run_gone({"runState": "running", "checkpointAt": "2026-10-26T12:00:00Z"}, now), "")
        self.assertEqual(runs._run_gone({"runState": "paused", "checkpointAt": "2026-10-26T12:00:00Z"}, now), "")


if __name__ == "__main__":
    unittest.main()
