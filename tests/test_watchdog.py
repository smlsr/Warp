"""Dead Shuttle and listener recovery. Heartbeats live on the beam, not in a process table."""

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
import beam  # noqa: E402
import inbound  # noqa: E402
import scan  # noqa: E402

NOW = "2026-10-06T12:00:00Z"
STALE = "2026-10-06T11:00:00Z"
FRESH = "2026-10-06T11:50:00Z"


def ticket(tid="WV-01", **extra):
    row = {
        "id": tid,
        "status": "coding",
        "summary": "ship %s" % tid,
        "size": "M",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": True,
        "deps": [],
        "locks": ["src/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "complexity": "MEDIUM",
        "agent": "shuttle-%s" % tid,
        "branch": "warp/%s-WAR-1" % tid,
        "claimedAt": STALE,
        "workerStartedAt": STALE,
        "lastSeenAt": STALE,
        "attempts": 0,
        "alarm": None,
        "pr": {"url": "https://github.com/acme/app/pull/7", "bugbot": None, "ci": None},
        "jira": {"startedAt": "2026-10-06T08:00:00Z", "previousStatus": "To Do"},
        "jiraKey": "WAR-1",
    }
    row.update(extra)
    return row


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        warp = self.repo / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"

    def write(self, tickets=None, listener=None, **extra):
        body = {
            "version": 1,
            "tickets": tickets if tickets is not None else {"WV-01": ticket()},
            "gates": [],
            "runState": "running",
            "paused": False,
            "config": {"maxAgents": 18, "staleMinutes": 15, "maxRecoveries": 3},
            "program": {"criticalPath": []},
        }
        if listener is not None:
            body["listener"] = listener
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def load(self):
        return json.loads(self.beam_path.read_text())

    def lines(self, now=NOW):
        return beam.watchdog(self.beam_path, now=now)

    def test_stale_listener_is_replaced_once(self):
        self.write(
            tickets={},
            listener={
                "state": "running",
                "agentId": "listener-1",
                "pid": "99",
                "startedAt": STALE,
                "lastSeenAt": STALE,
            },
        )
        first = self.lines()
        self.assertIn("listener: died listener-1", first)
        self.assertIn("listener: replace listener-r1", first)
        self.assertIn("herald: Listener died. A new one started.", first)
        self.assertEqual(first.count("listener: replace listener-r1"), 1)
        data = self.load()
        listener = data["listener"]
        self.assertEqual(listener["state"], "running")
        self.assertEqual(listener["agentId"], "listener-r1")
        self.assertNotIn("lastSeenAt", listener)
        self.assertEqual(listener["startedAt"], NOW)
        self.assertEqual(listener["replacedAgentId"], "listener-1")
        self.assertEqual(listener["recoveries"], 1)
        self.assertEqual(listener["recoveredAt"], NOW)
        ack = json.loads((self.repo / ".warp" / "recovery.json").read_text())
        self.assertEqual(ack["lines"], ["Listener died. A new one started."])
        journal = (self.repo / ".warp" / "journal.jsonl").read_text()
        self.assertIn("listener-died", journal)
        self.assertIn("listener-replace", journal)

        second = self.lines()
        self.assertIn("listener: fresh listener-r1", second)
        self.assertNotIn("listener: replace", "\n".join(second))
        self.assertNotIn("herald:", "\n".join(second))
        self.assertEqual(self.load()["listener"]["agentId"], "listener-r1")
        self.assertEqual(self.load()["listener"]["recoveries"], 1)

    def test_fresh_heartbeat_is_left_alone(self):
        self.write(
            tickets={},
            listener={
                "state": "running",
                "agentId": "listener-1",
                "startedAt": STALE,
                "lastSeenAt": FRESH,
            },
        )
        got = self.lines()
        self.assertEqual(got, ["listener: alive listener-1"])
        self.assertEqual(self.load()["listener"]["agentId"], "listener-1")
        self.assertFalse((self.repo / ".warp" / "recovery.json").exists())

    def test_never_heartbeated_old_start_is_dead_and_fresh_start_is_not(self):
        self.write(
            tickets={},
            listener={"state": "running", "agentId": "listener-1", "startedAt": STALE},
        )
        self.assertIn("listener: replace listener-r1", self.lines())
        self.write(
            tickets={},
            listener={"state": "running", "agentId": "listener-9", "startedAt": FRESH},
        )
        self.assertEqual(self.lines(), ["listener: fresh listener-9"])
        self.assertEqual(self.load()["listener"]["agentId"], "listener-9")

    def test_paused_and_stopped_do_not_replace(self):
        listener = {
            "state": "running",
            "agentId": "listener-1",
            "startedAt": STALE,
            "lastSeenAt": STALE,
        }
        self.write(listener=listener, runState="paused", paused=True)
        self.assertEqual(self.lines(), ["watchdog: skipped (paused)"])
        self.assertEqual(self.load()["listener"]["agentId"], "listener-1")
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "coding")
        self.assertEqual(self.load()["tickets"]["WV-01"]["branch"], "warp/WV-01-WAR-1")

        self.write(listener=listener, runState="stopped", paused=False)
        self.assertEqual(self.lines(), ["watchdog: skipped (stopped)"])
        self.assertEqual(self.load()["listener"]["agentId"], "listener-1")
        self.assertEqual(self.load()["tickets"]["WV-01"]["agent"], "shuttle-WV-01")

    def test_stale_shuttle_is_redispatched_once_and_keeps_its_branch(self):
        self.write(listener={"state": "stopped", "agentId": None})
        first = self.lines()
        self.assertIn("shuttle: replace WV-01 agent=shuttle-WV-01-r1 branch=warp/WV-01-WAR-1", first)
        self.assertIn("herald: WV-01 worker died. A new Shuttle started.", first)
        self.assertEqual(sum(1 for line in first if line.startswith("shuttle: replace")), 1)
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["status"], "recovering")
        self.assertEqual(row["agent"], "shuttle-WV-01-r1")
        self.assertEqual(row["branch"], "warp/WV-01-WAR-1")
        self.assertEqual(row["pr"]["url"], "https://github.com/acme/app/pull/7")
        self.assertEqual(row["jira"]["startedAt"], "2026-10-06T08:00:00Z")
        self.assertEqual(row["locks"], ["src/WV-01"])
        self.assertEqual(row["recoveries"], 1)
        self.assertEqual(row["recoveredAt"], NOW)
        self.assertEqual(row["recoveryPriorStatus"], "coding")
        self.assertNotIn("lastSeenAt", row)
        self.assertEqual(row["workerStartedAt"], NOW)

        other = ticket("WV-02", status="queued", locks=["src/WV-01"], lastSeenAt=None, claimedAt=None, workerStartedAt=None)
        other["agent"] = None
        other["branch"] = None
        data = self.load()
        data["tickets"]["WV-02"] = other
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        ready = beam.ready(self.load())
        self.assertEqual([t["id"] for t in ready], [])

        second = self.lines()
        self.assertIn("shuttle: fresh WV-01", second)
        self.assertNotIn("shuttle: replace", "\n".join(second))
        self.assertNotIn("herald:", "\n".join(second))
        again = self.load()["tickets"]["WV-01"]
        self.assertEqual(again["recoveries"], 1)
        self.assertEqual(again["agent"], "shuttle-WV-01-r1")
        self.assertEqual(again["branch"], "warp/WV-01-WAR-1")
        self.assertEqual(again["status"], "recovering")

    def test_second_tick_does_not_double_start_listener_or_shuttle(self):
        self.write(
            listener={
                "state": "running",
                "agentId": "listener-1",
                "startedAt": STALE,
                "lastSeenAt": STALE,
            }
        )
        self.lines()
        self.lines()
        data = self.load()
        self.assertEqual(data["listener"]["agentId"], "listener-r1")
        self.assertEqual(data["listener"]["recoveries"], 1)
        self.assertEqual(data["tickets"]["WV-01"]["agent"], "shuttle-WV-01-r1")
        self.assertEqual(data["tickets"]["WV-01"]["recoveries"], 1)
        text = (self.repo / ".warp" / "journal.jsonl").read_text()
        self.assertEqual(text.count("listener-replace"), 1)
        self.assertEqual(text.count("shuttle-recover"), 1)

    def test_cap_trips_alarm_and_does_not_start_another(self):
        self.write(
            tickets={
                "WV-01": ticket(recoveries=3, lastSeenAt=STALE),
            },
            listener={"state": "stopped"},
        )
        got = self.lines()
        self.assertIn("shuttle: alarm WV-01 worker-died", got)
        self.assertIn("herald: WV-01 worker died. Recovery cap reached.", got)
        self.assertNotIn("shuttle: replace", "\n".join(got))
        row = self.load()["tickets"]["WV-01"]
        self.assertEqual(row["status"], "alarm")
        self.assertEqual(row["alarm"], "worker-died")
        self.assertEqual(row["recoveries"], 3)
        self.assertEqual(row["branch"], "warp/WV-01-WAR-1")
        self.assertEqual(row["pr"]["url"], "https://github.com/acme/app/pull/7")
        self.assertEqual(row["jira"]["startedAt"], "2026-10-06T08:00:00Z")
        self.assertEqual(row["locks"], ["src/WV-01"])
        again = self.lines()
        self.assertNotIn("shuttle: replace", "\n".join(again))
        self.assertNotIn("shuttle: alarm", "\n".join(again))
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "alarm")

    def test_yaml_stale_minutes_overrides_the_beam(self):
        (self.repo / ".warp" / "config.yaml").write_text("staleMinutes: 90\nmaxRecoveries: 3\n")
        self.write(
            tickets={},
            listener={
                "state": "running",
                "agentId": "listener-1",
                "startedAt": STALE,
                "lastSeenAt": STALE,
            },
        )
        self.assertEqual(self.lines(), ["listener: alive listener-1"])

    def test_heartbeat_refreshes_last_seen_and_claim_sets_it(self):
        self.write(tickets={"WV-01": ticket(lastSeenAt=STALE)}, listener={"state": "stopped"})
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "beam.py"), "heartbeat", "--beam", str(self.beam_path), "--id", "WV-01", "--agent", "shuttle-WV-01"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WV-01 heartbeat shuttle-WV-01", proc.stdout)
        self.assertNotEqual(self.load()["tickets"]["WV-01"]["lastSeenAt"], STALE)
        stranger = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "beam.py"), "heartbeat", "--beam", str(self.beam_path), "--id", "WV-01", "--agent", "other"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(stranger.returncode, 0)

        self.write(tickets={}, listener={"state": "stopped"})
        self.assertTrue(inbound.claim(self.beam_path, "listener-7").startswith("listener: started"))
        claimed = self.load()["listener"]
        self.assertEqual(claimed["lastSeenAt"], claimed["startedAt"])
        beat = inbound.heartbeat(self.beam_path, "listener-7")
        self.assertEqual(beat, "listener: heartbeat listener-7")
        self.assertEqual(inbound.heartbeat(self.beam_path, "someone-else"), "listener: heartbeat refused listener-7")

    def test_start_runs_the_watchdog(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write(
            tickets={},
            listener={
                "state": "running",
                "agentId": "listener-1",
                "startedAt": old,
                "lastSeenAt": old,
            },
            runState="paused",
            paused=True,
        )
        scan.set_run(self.beam_path, "running", "go")
        data = self.load()
        self.assertEqual(data["runState"], "running")
        self.assertEqual(data["listener"]["agentId"], "listener-r1")
        self.assertNotIn("lastSeenAt", data["listener"])


if __name__ == "__main__":
    unittest.main()
