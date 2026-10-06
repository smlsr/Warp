"""One lock-escape repair at a time. The repair widens locks to the escaped paths."""

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

    def test_one_at_a_time(self):
        self.write(
            {
                "WV-02": ticket("WV-02"),
                "WV-01": ticket("WV-01"),
            }
        )
        first = self.lines()
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in first))
        self.assertNotIn("alarm-repair: start WV-01", "\n".join(first))
        data = self.load()
        self.assertEqual(data["alarmRepair"]["active"], "WV-02")
        self.assertEqual(data["tickets"]["WV-02"]["status"], "claimed")
        self.assertEqual(data["tickets"]["WV-02"]["alarm"], "lock-escape")
        self.assertEqual(data["tickets"]["WV-01"]["status"], "alarm")
        self.assertEqual(data["tickets"]["WV-01"]["alarmRepairs"] if "alarmRepairs" in data["tickets"]["WV-01"] else 0, 0)
        again = self.lines()
        self.assertIn("alarm-repair: working WV-02", again)
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in again))
        self.assertEqual(self.load()["tickets"]["WV-02"]["alarmRepairs"], 1)
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "alarm")

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
                "WV-02": ticket("WV-02", status="coding", alarm=None, escaped=None, locks=["src/held"]),
            }
        )
        got = self.lines()
        self.assertIn("alarm-repair: locked WV-01 by WV-02", got)
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in got))
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["status"], "alarm")
        self.assertEqual(row["alarm"], "lock-escape")
        self.assertEqual(row["locks"], ["src/a"])
        self.assertNotIn("addedLocks", row)
        self.assertNotIn("alarmRepairs", row)
        self.assertIsNone(self.load()["alarmRepair"].get("active"))
        # Still held on the next tick: do not start the other alarm either.
        self.write(
            {
                "WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/held"]),
                "WV-09": ticket("WV-09", locks=["src/free"], escaped=["src/other"]),
                "WV-02": ticket("WV-02", status="coding", alarm=None, escaped=None, locks=["src/held"]),
            }
        )
        held = self.lines()
        self.assertIn("alarm-repair: locked WV-01 by WV-02", held)
        self.assertEqual(self.load()["tickets"]["WV-09"]["status"], "alarm")
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in held))

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

    def test_fail_moves_to_the_next_and_keeps_the_alarm(self):
        self.write({"WV-01": ticket("WV-01", locks=["src/a"], escaped=["src/b"]), "WV-02": ticket("WV-02")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "alarm"
        data["tickets"]["WV-01"]["alarm"] = "lock-escape"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: failed WV-01", got)
        self.assertIn("WV-01 lock-escape repair failed. Taking WV-02.", self.heralds(got))
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in got))
        saved = self.load()
        first = saved["tickets"]["WV-01"]
        self.assertEqual(first["status"], "alarm")
        self.assertEqual(first["alarm"], "lock-escape")
        self.assertEqual(first["alarmRepairs"], 1)
        self.assertEqual(first["locks"], ["src/a", "src/b"])
        self.assertEqual(saved["tickets"]["WV-02"]["status"], "claimed")
        self.assertEqual(saved["alarmRepair"]["active"], "WV-02")
        # Same pass does not start WV-01 again.
        saved["tickets"]["WV-02"]["status"] = "alarm"
        saved["tickets"]["WV-02"]["alarm"] = "lock-escape"
        self.beam_path.write_text(json.dumps(saved, indent=2) + "\n")
        parked = self.lines(now=plus(NOW, 2))
        self.assertNotIn("alarm-repair: start WV-01", "\n".join(parked))
        self.assertEqual(self.load()["tickets"]["WV-01"]["alarmRepairs"], 1)

    def test_success_waits_for_completion_before_the_next(self):
        self.write({"WV-01": ticket("WV-01"), "WV-02": ticket("WV-02")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "coding"
        data["tickets"]["WV-01"]["alarm"] = None
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        waiting = self.lines(now=plus(NOW, 1))
        self.assertIn("alarm-repair: waiting WV-01", waiting)
        self.assertEqual(self.load()["tickets"]["WV-02"]["status"], "alarm")
        self.assertFalse(any(line.startswith("alarm-repair: start ") for line in waiting))
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "merged"
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        nxt = self.lines(now=plus(NOW, 2))
        self.assertIn("alarm-repair: passed WV-01", nxt)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in nxt))

    def test_returned_on_the_normal_path_starts_the_next(self):
        self.write({"WV-01": ticket("WV-01"), "WV-02": ticket("WV-02")})
        self.lines()
        data = self.load()
        data["tickets"]["WV-01"]["status"] = "planning"
        data["tickets"]["WV-01"]["alarm"] = None
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        got = self.lines(now=plus(NOW, 1), returned="WV-01")
        self.assertIn("alarm-repair: passed WV-01", got)
        self.assertTrue(any(line.startswith("alarm-repair: start WV-02 ") for line in got))

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
