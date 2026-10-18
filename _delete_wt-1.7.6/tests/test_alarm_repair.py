"""Lock-escape repairs start at once, in parallel, decided one at a time so two never take the same file."""

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


def plus(stamp, minutes):
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(tid="WV-01", **extra):
    row = {
        "id": tid,
        "status": "alarm",
        "summary": "ship %s" % tid,
        "size": "M",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": True,
        "deps": [],
        "locks": ["src/%s" % tid],
        "escaped": ["extra/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "agent": None,
        "branch": "warp/%s-WAR-1" % tid,
        "attempts": 0,
        "alarm": "lock-escape",
        "pr": {"url": None, "bugbot": None, "ci": None},
        "jira": {},
    }
    row.update(extra)
    return row


class AlarmRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        warp = self.repo / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"

    def write(self, tickets, **extra):
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {"maxAgents": 18, "alarmRepairMinutes": 15, "maxAlarmRepairs": 3},
            "program": {"criticalPath": []},
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def load(self):
        return json.loads(self.beam_path.read_text())

    def lines(self, now=NOW, returned=None, error=None):
        return alarm_repair.next_repair(self.beam_path, now=now, returned=returned, error=error)

    def heralds(self, lines):
        return [line[len("herald: ") :] for line in lines if line.startswith("herald: ")]

    def test_every_free_escape_starts_at_once(self):
        self.write({"WV-02": ticket("WV-02"), "WV-01": ticket("WV-01")})
        first = self.lines()
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in first))
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in first))
        data = self.load()
        self.assertEqual(data["alarmRepair"]["active"], ["WV-02", "WV-01"])  # plan order
        for tid in ("WV-01", "WV-02"):
            self.assertEqual((data["tickets"][tid]["status"], data["tickets"][tid]["alarm"], data["tickets"][tid]["alarmRepairs"]),
                             ("claimed", "lock-escape", 1))
        again = self.lines()
        self.assertIn("alarm-repair: working WV-02", again)
        self.assertIn("alarm-repair: working WV-01", again)
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in again))

    def test_an_escape_starts_without_waiting_for_the_interval(self):
        self.write({"WV-01": ticket("WV-01", status="coding", alarm=None, escaped=None)})
        self.assertIn("alarm-repair: idle", self.lines())
        data = self.load()
        data["tickets"]["WV-01"].update(status="alarm", alarm="lock-escape", escaped=["extra/WV-01"])
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1))  # one minute later, well inside alarmRepairMinutes
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in got), got)
        self.assertNotIn("alarm-repair: wait", got)

    def test_two_escapes_into_one_file_start_one_at_a_time(self):
        # Decided serially, run in parallel: the first takes the file; the second sees it held and waits.
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/shared.ts"]),
                    "WV-02": ticket("WV-02", locks=["src/b"], escaped=["src/shared.ts"]),
                    "WV-03": ticket("WV-03", locks=["src/c"], escaped=["src/own.ts"])})
        got = self.lines()
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in got))
        self.assertIn("alarm-repair: locked WV-02 by WV-01", got)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-03 ") for line in got))  # not held back by WV-02
        data = self.load()
        self.assertEqual(data["tickets"]["WV-01"]["locks"], ["src/a", "src/shared.ts"])
        self.assertEqual((data["tickets"]["WV-02"]["status"], data["tickets"]["WV-02"]["locks"]), ("alarm", ["src/b"]))
        self.assertNotIn("alarmRepairs", data["tickets"]["WV-02"])
        # Normal dispatch in the same pass sees the repair holding the file.
        data["tickets"]["WV-09"] = ticket("WV-09", status="queued", alarm=None, escaped=None, locks=["src/shared.ts"])
        self.assertNotIn("WV-09", [t["id"] for t in beam.ready(data)])
        data["tickets"].pop("WV-09")
        # WV-01 merges: WV-02 takes the file on the next tick.
        data["tickets"]["WV-01"]["status"] = "merged"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        nxt = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: passed WV-01", nxt)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in nxt))
        self.assertEqual(self.load()["tickets"]["WV-02"]["locks"], ["src/b", "src/shared.ts"])

    def test_one_supervise_pass_repairs_before_it_dispatches(self):
        import pipeline

        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/shared.ts"]),
                    "WV-02": ticket("WV-02", locks=["src/b"], escaped=["src/shared.ts"]),
                    "WV-09": ticket("WV-09", status="queued", alarm=None, escaped=None, locks=["src/shared.ts"], branch=None)})
        data = self.load()
        lines = pipeline.advance(data, beam_path=self.beam_path, now=NOW)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in lines), lines)
        self.assertIn("alarm-repair: locked WV-02 by WV-01", lines)
        self.assertFalse(any(line.startswith("start WV-09") for line in lines), lines)  # the repair holds the file first
        self.assertEqual(data["tickets"]["WV-09"]["status"], "queued")

    def test_a_started_repair_holds_its_slot_until_launch_across_calls(self):
        self.write({"WV-01": ticket("WV-01"), "WV-02": ticket("WV-02"), "WV-03": ticket("WV-03"),
                    "WV-09": ticket("WV-09", status="queued", alarm=None, escaped=None, locks=["src/q"], branch=None)})
        data = self.load()
        data["config"]["maxAgents"] = 2
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        self.lines()
        data = self.load()
        self.assertEqual(beam.shuttle_room(data), 0)  # two repairs, not launched yet, already hold both slots
        self.assertEqual(beam.ready(data), [])  # so the dispatch in the same pass starts nothing new
        later = self.lines(now=plus(NOW, 1))
        self.assertFalse(any(line.startswith("alarm-repair: start WV-03 ") for line in later), later)
        self.assertIn("alarm-repair: queued WV-03 (no free Shuttle slot; maxAgents)", later)

    def test_a_lockfile_counts_as_its_manifest(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["app/package-lock.json"]),
                    "WV-02": ticket("WV-02", status="coding", alarm=None, escaped=None, locks=["app/package.json"])})
        self.assertIn("alarm-repair: locked WV-01 by WV-02", self.lines())
        self.assertTrue(beam.lock_overlap(["app/package.json"], ["app/package-lock.json"]))
        self.assertFalse(beam.lock_overlap(["app/package.json"], ["web/package-lock.json"]))

    def test_an_escape_seen_while_its_shuttle_works_waits_and_still_holds_its_files(self):
        live = {"pending": True, "step": "implement"}
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/b"], shuttle=live, workerStartedAt=NOW, lastSeenAt=NOW),
                    "WV-02": ticket("WV-02", locks=["src/c"], escaped=["src/a/x.ts"])})
        got = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: shuttle-out WV-01 (repaired when its Shuttle returns)", got)
        self.assertIn("alarm-repair: locked WV-02 by WV-01", got)  # its Shuttle is still in src/a
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "alarm")

    def test_a_restarted_alarm_waits_for_locks_a_repair_took(self):
        import pipeline

        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/shared.ts"]),
                    "WV-05": ticket("WV-05", alarm="worker-died", escaped=None, locks=["src/shared.ts"])})
        self.lines()
        data = self.load()
        data["_repoRoot"] = str(self.repo)
        lines = []
        pipeline._restart(data, data["tickets"]["WV-05"], lines, "implementing")
        self.assertIn("shuttle: hold WV-05 (its locks are held by WV-01)", lines)
        self.assertEqual(data["tickets"]["WV-05"]["status"], "alarm")

    def test_start_and_resume_repair_escapes_before_anything_else(self):
        import pipeline

        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/shared.ts"]),
                    "WV-09": ticket("WV-09", status="queued", alarm=None, escaped=None, locks=["src/shared.ts"], branch=None),
                    "WV-10": ticket("WV-10", status="queued", alarm=None, escaped=None, locks=["src/other"], branch=None)})
        data = self.load()
        self.assertTrue(pipeline.unfinished_line(data).startswith("unfinished: Lock-escape alarms first (locks widened, relaunched): WV-01. "))
        lines = pipeline.cold_start(self.beam_path)
        first = next(i for i, line in enumerate(lines) if line.startswith("alarm-repair: start WV-01 "))
        self.assertEqual(first, 0, lines)
        self.assertFalse(any(line.startswith("start WV-09") for line in lines), lines)  # the repair holds src/shared.ts
        self.assertTrue(any(line.startswith("start WV-10") for line in lines), lines)  # new work after, in free slots
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "claimed")

    def test_repairs_take_shuttle_slots(self):
        self.write({"WV-01": ticket("WV-01"), "WV-02": ticket("WV-02"), "WV-03": ticket("WV-03")})
        data = self.load()
        data["config"]["maxAgents"] = 2
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines()
        self.assertEqual(len([line for line in got if line.startswith("alarm-repair: start ")]), 2, got)
        self.assertIn("alarm-repair: queued WV-03 (no free Shuttle slot; maxAgents)", got)
        self.assertEqual(self.load()["tickets"]["WV-03"]["status"], "alarm")

    def test_repair_adds_escaped_paths(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/b", "src/c", "src/b"])})
        got = self.lines()
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["locks"], ["src/a", "src/b", "src/c"])
        self.assertEqual(row["addedLocks"], ["src/b", "src/c"])
        self.assertEqual(row["alarmRepair"]["added"], ["src/b", "src/c"])
        self.assertEqual(row["alarm"], "lock-escape")
        self.assertIn("Added src/b, src/c.", "\n".join(self.heralds(got)))
        self.assertTrue(any("added=src/b,src/c" in line for line in got))

    def test_does_not_start_when_escaped_path_is_held_in_flight(self):
        self.write(
            {
                "WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/held"]),
                "WV-09": ticket("WV-09", locks=["src/free"], escaped=["src/other"]),
                "WV-02": ticket("WV-02", status="coding", alarm=None, escaped=None, locks=["src/held"]),
            }
        )
        got = self.lines()
        self.assertIn("alarm-repair: locked WV-01 by WV-02", got)
        self.assertFalse(any(line.startswith("alarm-repair: start WV-01 ") for line in got))
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual((row["status"], row["alarm"], row["locks"]), ("alarm", "lock-escape", ["src/a"]))
        self.assertNotIn("addedLocks", row)
        self.assertNotIn("alarmRepairs", row)
        # A held ticket no longer holds back the ones after it.
        self.assertTrue(any(line.startswith("alarm-repair: start WV-09 ") for line in got))
        self.assertEqual(self.load()["alarmRepair"]["active"], ["WV-09"])

    def test_queued_overlap_is_widened_and_started(self):
        self.write(
            {
                "WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/queued"]),
                "WV-02": ticket("WV-02", status="queued", alarm=None, escaped=None, locks=["src/queued"]),
            }
        )
        self.lines()
        data = self.load()
        self.assertEqual(data["tickets"]["WV-01"]["status"], "claimed")
        self.assertEqual(data["tickets"]["WV-01"]["locks"], ["src/a", "src/queued"])
        self.assertEqual(data["tickets"]["WV-01"]["addedLocks"], ["src/queued"])
        self.assertEqual(data["tickets"]["WV-02"]["status"], "queued")
        self.assertEqual(data["tickets"]["WV-02"]["locks"], ["src/queued"])

    def test_starts_after_the_holder_finishes(self):
        self.write(
            {
                "WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/held"]),
                "WV-02": ticket("WV-02", status="coding", alarm=None, escaped=None, locks=["src/held"]),
            }
        )
        self.assertIn("alarm-repair: locked WV-01 by WV-02", self.lines())
        self.assertEqual(self.load()["tickets"]["WV-01"]["locks"], ["src/a"])
        data = self.load()
        data["tickets"]["WV-02"]["status"] = "merged"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1))
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in got))
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["locks"], ["src/a", "src/held"])
        self.assertEqual(row["addedLocks"], ["src/held"])
        self.assertEqual(row["status"], "claimed")

    def test_a_failure_keeps_the_alarm_and_retries_after_the_interval(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/b"])})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "alarm"
        data["tickets"]["WV-01"]["alarm"] = "lock-escape"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: failed WV-01", got)
        self.assertIn("WV-01 lock-escape repair failed (attempt 1 of 3). It tries again in alarmRepairMinutes.", self.heralds(got))
        self.assertIn("alarm-repair: retry WV-01 after 15 minutes (failed %s)" % plus(NOW, 1), got)
        saved = self.load()["tickets"]["WV-01"]
        self.assertEqual((saved["status"], saved["alarm"], saved["alarmRepairs"], saved["locks"]), ("alarm", "lock-escape", 1, ["src/a", "src/b"]))
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in self.lines(now=plus(NOW, 10))))
        again = self.lines(now=plus(NOW, 16))
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in again), again)
        self.assertEqual(self.load()["tickets"]["WV-01"]["alarmRepairs"], 2)

    def test_success_is_settled_when_the_ticket_moves_on(self):
        self.write({"WV-01": ticket("WV-01"), "WV-02": ticket("WV-02")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "coding"
        data["tickets"]["WV-01"]["alarm"] = None
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        waiting = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: waiting WV-01", waiting)
        self.assertIn("alarm-repair: working WV-02", waiting)
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "merged"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        nxt = self.lines(now=plus(NOW, 2))
        self.assertIn("alarm-repair: passed WV-01", nxt)
        self.assertEqual(self.load()["alarmRepair"]["active"], ["WV-02"])

    def test_returned_on_the_normal_path_passes(self):
        self.write({"WV-01": ticket("WV-01")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "planning"
        data["tickets"]["WV-01"]["alarm"] = None
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1), returned="WV-01")
        self.assertIn("alarm-repair: passed WV-01", got)
        self.assertEqual(self.load()["alarmRepair"]["active"], [])

    def test_an_old_single_active_repair_is_read_as_a_list(self):
        self.write({"WV-01": ticket("WV-01", status="claimed", agent="repair-WV-01-1", alarmRepairs=1,
                                    alarmRepair={"state": "working", "attempts": 1}), "WV-02": ticket("WV-02")},
                   alarmRepair={"active": "WV-01", "naming": None, "tried": ["WV-01"], "passOpen": True})
        got = self.lines()
        self.assertIn("alarm-repair: working WV-01", got)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in got))
        state = self.load()["alarmRepair"]
        self.assertEqual((state["active"], state["naming"]), (["WV-01", "WV-02"], []))
        self.assertNotIn("tried", state)

    def test_start_prints_the_launch_and_marks_the_repair_step(self):
        self.write({"WV-01": ticket("WV-01", shuttle={"pending": False, "step": "implement", "returned": True})})
        got = self.lines()
        launch = [line for line in got if line.startswith("alarm-repair: launch WV-01.")]
        self.assertEqual(len(launch), 1, got)
        self.assertIn("checkout.py launch --root %s --id WV-01" % self.repo.resolve(), launch[0])
        self.assertIn("Pass --returned WV-01 only after that Shuttle returns", launch[0])
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["shuttle"], {"step": "repair", "pending": False, "hadPr": False})

    def test_a_return_with_no_shuttle_launched_is_not_counted(self):
        self.write({"WV-01": ticket("WV-01")})
        self.lines()
        for error in (None, "no worker"):
            got = self.lines(now=plus(NOW, 1), returned="WV-01", error=error)
            self.assertIn("alarm-repair: not launched WV-01. No repair Shuttle ran for attempt 1, so it is not counted.", got)
            self.assertTrue(any(line.startswith("alarm-repair: launch WV-01.") for line in got))
            self.assertNotIn("alarm-repair: failed WV-01", got)
            row = self.load()["tickets"]["WV-01"]
            self.assertEqual(row["alarmRepairs"], 1)
            self.assertEqual(row["alarmRepair"]["state"], "working")
            self.assertEqual(self.load()["alarmRepair"]["active"], ["WV-01"])
        # The launch is recorded; the same return now settles the attempt.
        data = self.load()
        data["tickets"]["WV-01"]["alarmRepair"]["launchedAt"] = plus(NOW, 2)
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 3), returned="WV-01")
        self.assertIn("alarm-repair: failed WV-01", got)

    def test_a_heartbeat_counts_as_a_launched_shuttle(self):
        self.write({"WV-01": ticket("WV-01")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["lastSeenAt"] = plus(NOW, 1)
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        self.assertIn("alarm-repair: failed WV-01", self.lines(now=plus(NOW, 2), returned="WV-01"))

    def test_cap_skips_later_passes(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/b"])})
        now = NOW
        done = []
        for attempt in (1, 2, 3):
            got = self.lines(now=now)
            self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in got), got)
            data = self.load()
            data["tickets"]["WV-01"]["status"] = "alarm"
            data["tickets"]["WV-01"]["alarm"] = "lock-escape"
            self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
            failed_at = plus(now, 1)
            done = self.lines(now=failed_at)
            self.assertIn("alarm-repair: failed WV-01", done)
            self.assertEqual(self.load()["tickets"]["WV-01"]["alarmRepairs"], attempt)
            now = plus(failed_at, 15)
        self.assertTrue(self.load()["tickets"]["WV-01"]["alarmRepair"].get("gaveUp"))
        self.assertIn("WV-01 lock-escape repair given up.", self.heralds(done))
        later = self.lines(now=plus(now, 15))
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in later))
        self.assertNotIn("WV-01 lock-escape repair given up.", self.heralds(later))
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "alarm")
        self.assertEqual(self.load()["tickets"]["WV-01"]["alarm"], "lock-escape")
        self.assertEqual(self.load()["tickets"]["WV-01"]["alarmRepairs"], 3)

    def test_paused_and_stopped_do_not_repair(self):
        row = ticket("WV-01", locks=["src/a"], escaped=["src/b"])
        for state in ("paused", "stopped"):
            self.write({"WV-01": row}, runState=state, paused=(state == "paused"))
            got = self.lines()
            self.assertEqual(got, ["alarm-repair: skipped (%s)" % state])
            saved = self.load()["tickets"]["WV-01"]
            self.assertEqual(saved["status"], "alarm")
            self.assertEqual(saved["locks"], ["src/a"])
            self.assertNotIn("alarmRepairs", saved)

    def test_non_lock_escape_is_not_picked(self):
        self.write(
            {
                "WV-01": ticket("WV-01", alarm="bugbot-failed", escaped=["src/nope"]),
                "WV-02": ticket("WV-02", alarm="worker-died", escaped=["src/nope"]),
                "WV-03": ticket("WV-03", alarm="stuck", escaped=["src/nope"]),
                "WV-04": ticket("WV-04", locks=["src/keep"], escaped=["src/yes"]),
            }
        )
        got = self.lines()
        self.assertTrue(any(line.startswith("alarm-repair: start WV-04 ") for line in got))
        data = self.load()["tickets"]
        for tid in ("WV-01", "WV-02", "WV-03"):
            self.assertEqual(data[tid]["status"], "alarm")
            self.assertNotIn("src/nope", data[tid]["locks"])
        self.assertEqual(data["WV-04"]["locks"], ["src/keep", "src/yes"])

    def test_paths_command_stores_without_widening_until_next(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=[])})
        named = self.lines()
        self.assertIn("alarm-repair: name WV-01", named)
        self.assertEqual(self.load()["tickets"]["WV-01"]["locks"], ["src/a"])
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "alarm")
        again = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: naming WV-01", again)
        stored = alarm_repair.store_paths(self.beam_path, "WV-01", ["src/new", "src/new"])
        self.assertIn("alarm-repair: paths WV-01 src/new", stored)
        self.assertEqual(self.load()["tickets"]["WV-01"]["locks"], ["src/a"])
        self.assertEqual(self.load()["tickets"]["WV-01"]["escaped"], ["src/new"])
        started = self.lines(now=plus(NOW, 2), returned="WV-01")
        self.assertTrue(any(line.startswith("alarm-repair: start WV-01 ") for line in started))
        self.assertEqual(self.load()["tickets"]["WV-01"]["locks"], ["src/a", "src/new"])
        self.assertEqual(self.load()["tickets"]["WV-01"]["addedLocks"], ["src/new"])

    def test_beam_set_stores_escaped_paths_on_the_alarm(self):
        self.write({"WV-01": ticket("WV-01", status="coding", alarm=None, escaped=None, locks=["src/a"])})
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "beam.py"),
                "set",
                "--beam",
                str(self.beam_path),
                "--id",
                "WV-01",
                "--status",
                "alarm",
                "--alarm",
                "lock-escape",
                "--escaped",
                "src/out",
                "--escaped",
                "src/out",
                "--escaped",
                " lib/other ",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["alarm"], "lock-escape")
        self.assertEqual(row["escaped"], ["src/out", "lib/other"])
        self.assertEqual(row["locks"], ["src/a"])


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, str(ROOT / "tests"))
from test_isolation import GitRepo, run  # noqa: E402


class RepairLaunchTest(GitRepo):
    def test_checkout_launch_records_the_repair_launch(self):
        row = ticket("WV-01", locks=["src/a"], escaped=["src/b"], plan={}, branch=None)
        self.beam([row], runner="local", baseBranch="main", launch="worktree", subagentVm=False)
        path = self.repo / ".warp" / "beam.json"
        data = json.loads(path.read_text())
        data["runState"] = "running"
        path.write_text(json.dumps(data, indent=2) + "\n")
        alarm_repair.next_repair(path, now=NOW)
        out = run("checkout.py", "launch", "--root", str(self.repo), "--id", "WV-01", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("WV-01 repair", out.stdout)
        saved = json.loads(path.read_text())["tickets"]["WV-01"]
        self.assertTrue(saved["alarmRepair"].get("launchedAt"))
        self.assertTrue(saved["shuttle"]["pending"])
        self.assertTrue(alarm_repair.worker_seen(saved))
