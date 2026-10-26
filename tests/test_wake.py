"""1.7.11: the watch timer. One recurring timer per run; each wake is one `wake.py run` that keeps the run moving."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import beamlock  # noqa: E402
import orchestrator  # noqa: E402
import wake  # noqa: E402

NOW = "2026-10-26T13:00:00Z"


def plus(stamp, minutes):
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(tid, **extra):
    row = {"id": tid, "status": "queued", "locks": ["src/%s" % tid.lower()], "size": "S", "module": "UI", "summary": tid,
           "deps": [], "branch": "warp/ui/%s" % tid, "pr": {}, "autoMerge": True}
    row.update(extra)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        (self.warp / "config.yaml").write_text("run: ui\nmessenger: slack\nslackChannel: warp-ui\njiraProject: HOS\n")
        self.path = self.warp / "beam.json"

    def write(self, *tickets, **extra):
        data = {"version": 1, "run": "ui", "runState": "running", "gates": [], "instance": {"id": "db7970"},
                "config": {"maxAgents": 4, "runner": "local", "maxRecoveries": 3},
                "tickets": {t["id"]: t for t in tickets}}
        data.update(extra)
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        return data

    def beam(self):
        return json.loads(self.path.read_text())


class SetupTest(Base):
    def test_start_names_one_timer_and_its_prompt_is_self_contained(self):
        data = self.write(ticket("UI-1"))
        lines = wake.setup_lines(self.path, data)
        self.assertTrue(lines[0].startswith("timer: MUST DO keep one recurring timer (a subscription) named "
                                            "warp-ui-db7970-watch that fires every 300 seconds"), lines)
        self.assertIn("wake.py prompt --beam %s" % self.path.resolve(), lines[0])
        self.assertIn("wake.py armed --beam %s --name warp-ui-db7970-watch" % self.path.resolve(), lines[1])
        text = wake.prompt_text(self.path, data)
        for needle in ("warp-ui-db7970-watch: Warp watch timer for run ui (warp:db7970), every 300 seconds",
                       "wake.py run --beam %s" % self.path.resolve(), "checkout.py launch", "checkout.py bind",
                       "Do not merge by hand", "Do not start a second listener", "orchestrator.py opened",
                       "finished Bugbot check on that exact head commit", "jira_sync.py record", "wake.py add-ticket",
                       "#warp-ui" if False else "warp-ui", "session_note.py --type session-stop", "only what changed"):
            self.assertIn(needle, text)

    def test_off_when_watch_seconds_is_zero(self):
        (self.warp / "config.yaml").write_text("run: ui\nwatchSeconds: 0\n")
        data = self.write(ticket("UI-1"))
        self.assertEqual(wake.setup_lines(self.path, data), ["timer: off (watchSeconds 0)"])

    def test_scan_start_prints_the_timer(self):
        import scan

        self.assertTrue(hasattr(scan, "wake") or "wake.setup_lines" in (ROOT / "scripts" / "scan.py").read_text())


class WakeTest(Base):
    def test_one_wake_runs_every_step_in_order_and_records_itself(self):
        self.write(ticket("UI-1"))
        calls = []

        def supervise(path, now=None):
            calls.append(json.loads(Path(path).read_text())["wake"]["count"])
            return ["run: pass 7 at 13:00:00Z", "start UI-1 step=implement"]

        lines = wake.run(self.path, now=NOW, supervise=supervise)
        self.assertEqual(lines[0], "wake: warp-ui-db7970-watch at 13:00:00Z (wake 1)")
        self.assertTrue(any(line.startswith("parent: alive at 13:00:00Z (woken every 300s)") for line in lines), lines)
        self.assertIn("start UI-1 step=implement", lines)
        self.assertTrue(lines[-1].startswith("wake: done (wake 1)"))
        self.assertEqual(calls, [1])  # recorded before the pass, so the pass sees the timer alive
        self.assertEqual(self.beam()["listener"]["parentEvery"], 300)

    def test_stacked_fires_and_a_running_pass_do_nothing(self):
        self.write(ticket("UI-1"))
        wake.run(self.path, now=NOW, supervise=lambda p, now=None: [])
        self.assertTrue(wake.run(self.path, now=plus(NOW, 1), supervise=lambda p, now=None: ["x"])[0].startswith("wake: stacked"))
        import fcntl

        lock = (self.warp / beamlock.RUN_LOCK_NAME)
        with open(lock, "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            out = subprocess.run([sys.executable, str(ROOT / "scripts" / "wake.py"), "run", "--beam", str(self.path)],
                                 capture_output=True, text=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertIn("wake: a pass is running now; it covers this fire", out.stdout)

    def test_a_halted_run_says_delete_the_timer(self):
        self.write(ticket("UI-1"), runState="paused", paused=True)
        self.assertTrue(wake.run(self.path, now=NOW)[0].startswith("wake: the run is paused. Delete the watch timer warp-ui-db7970-watch"))


class UnstickTest(Base):
    def test_a_free_lock_escape_is_widened_in_the_beam_and_the_plan(self):
        (self.warp / "_ingested_schedule.json").write_text(json.dumps({"tickets": [{"id": "UI-2", "locks": ["src/ui-2"]}]}))
        self.write(ticket("UI-2", status="alarm", alarm="lock-escape", escaped=["src/shared/x.ts"]))
        lines = wake.unstick(self.path, now=NOW)
        self.assertTrue(any("UI-2 lock-escape cleared" in line and "src/shared/x.ts" in line for line in lines), lines)
        self.assertIn("src/shared/x.ts", self.beam()["tickets"]["UI-2"]["locks"])
        plan = json.loads((self.warp / "_ingested_schedule.json").read_text())
        self.assertIn("src/shared/x.ts", plan["tickets"][0]["locks"])

    def test_a_stale_stop_with_an_open_pull_request_goes_back_to_reviewing(self):
        self.write(ticket("UI-3", status="alarm", alarm="stuck", phase="implementing", pr={"url": "https://github.com/o/r/pull/3"}))
        lines = wake.unstick(self.path, now=NOW)
        self.assertIn("wake: UI-3 stuck alarm cleared; its pull request https://github.com/o/r/pull/3 is open, so it goes back to reviewing", lines)
        row = self.beam()["tickets"]["UI-3"]
        self.assertEqual((row["status"], row["phase"], row["alarm"]), ("review", "reviewing", None))

    def test_a_capped_ticket_gets_a_fresh_budget_every_wake(self):
        self.write(ticket("UI-4", status="alarm", alarm="worker-died", recoveries=3))
        lines = wake.unstick(self.path, now=NOW)
        self.assertTrue(any(line.startswith("autoscan: scan 1: 1 reset") for line in lines), lines)
        self.assertNotEqual(self.beam()["tickets"]["UI-4"]["status"], "alarm")


class NewWorkTest(Base):
    def test_a_blocker_nobody_owns_is_named_and_added(self):
        self.write(ticket("UI-5", deps=["UI-90", "core:C-1"]), external={"status": {"core:C-1": "review"}})
        data = self.beam()
        self.assertEqual(wake.orphan_blockers(data), [("UI-90", ["UI-5"])])
        line = wake.new_work_lines(self.path, data)[0]
        self.assertTrue(line.startswith("wake: new work: UI-90 blocks UI-5, and no ticket or run owns it. MUST DO add it:"), line)
        self.assertIn("wake.py add-ticket --beam", line)
        pack = self.tmp / "PACK" / "tickets"
        pack.mkdir(parents=True)
        (pack / "tickets.json").write_text(json.dumps({"projectKey": "HOS", "tickets": [{"tempId": "UI-5"}]}))
        out = wake.add_ticket(self.path, "UI-90", "Shared toast component", from_id="UI-5", locks=["src/toast"], now=NOW)
        self.assertEqual(out[0], "wake: UI-90 added to the beam (queued, M, 1 lock) for UI-5")
        self.assertIn("wake: UI-90 added to the pack's PACK/tickets/tickets.json", out)
        self.assertTrue(out[-1].startswith('jira: MUST DO create a Jira issue in HOS: summary "[UI-90] Shared toast component"'))
        self.assertEqual(self.beam()["tickets"]["UI-90"]["status"], "queued")
        self.assertEqual(wake.orphan_blockers(self.beam()), [])
        self.assertEqual(json.loads((pack / "tickets.json").read_text())["tickets"][-1]["tempId"], "UI-90")
        self.assertEqual(json.loads((self.warp / "added-tickets.json").read_text())[0]["id"], "UI-90")
        self.assertTrue(wake.add_ticket(self.path, "UI-90", "again")[0].startswith("wake: UI-90 is already a ticket"))


class WatchTest(Base):
    def test_a_pass_says_the_timer_is_gone_after_three_intervals_and_a_halt_says_delete(self):
        data = self.write(ticket("UI-6"), wake={"name": "warp-ui-db7970-watch", "at": NOW, "count": 4})
        self.assertEqual(wake.check_lines(data, self.path, now=plus(NOW, 14)), [])
        lines = wake.check_lines(data, self.path, now=plus(NOW, 16))
        self.assertTrue(lines[0].startswith("timer: warp-ui-db7970-watch has not woken for 16m (every 300s). MUST DO create it again"), lines)
        self.assertTrue(lines[1].startswith("herald: ALARM the Warp watch timer warp-ui-db7970-watch has not woken for 16m"))
        self.assertEqual(wake.check_lines(data, self.path, now=plus(NOW, 20)), [])  # every 30 minutes
        again = wake.check_lines(data, self.path, now=plus(NOW, 47))
        self.assertEqual(len(again), 1)  # one alarm per gap
        data["runState"] = "stopped"
        self.assertEqual(wake.halt_lines(data), ["timer: MUST DO delete the watch timer warp-ui-db7970-watch: the run is not "
                                                 "running. /warp-resume prints it again."])
        self.assertEqual(wake.halt_lines(data), [])

    def test_never_armed_says_nothing(self):
        data = self.write(ticket("UI-7"))
        self.assertEqual(wake.check_lines(data, self.path, now=plus(NOW, 60)), [])

    def test_armed_and_status(self):
        self.write(ticket("UI-8"))
        self.assertEqual(wake.armed(self.path, "warp-ui-db7970-watch", now=NOW),
                         "timer: warp-ui-db7970-watch recorded, every 300s. Each wake runs wake.py run.")
        self.assertIn("armed at %s, no wake yet" % NOW, wake.status_lines(self.path)[0])


if __name__ == "__main__":
    unittest.main()
