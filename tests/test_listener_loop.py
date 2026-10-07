"""The channel listener loops, heartbeats a file, and the parent restarts one."""

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
import inbound  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-07T12:00:00Z"


def plus(stamp, minutes):
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ListenerLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        self.beam_path = warp / "beam.json"
        self.beam_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "runState": "running",
                    "tickets": {"T-1": {"id": "T-1", "status": "review", "pr": {"url": "https://example.test/1", "rollup": "pending"}}},
                    "gates": [],
                    "config": {"pollSeconds": 300, "listenerStaleMinutes": 15, "listenerRestartNote": 3},
                }
            )
            + "\n"
        )

    def file(self):
        path = self.tmp / ".warp" / "listener.json"
        self.assertTrue(path.is_file(), "missing .warp/listener.json")
        return json.loads(path.read_text())

    def pin(self, stamp, clear_pending=False):
        path = self.tmp / ".warp" / "listener.json"
        body = json.loads(path.read_text())
        body["lastPollAt"] = stamp
        path.write_text(json.dumps(body) + "\n")
        data = json.loads(self.beam_path.read_text())
        data["listener"]["lastSeenAt"] = stamp
        if clear_pending:
            data["listener"].pop("pendingStart", None)
        self.beam_path.write_text(json.dumps(data) + "\n")

    def test_claim_and_heartbeat_write_the_file(self):
        line = inbound.claim(self.beam_path, "listener-1", pid="42", turn="turn-a")
        self.assertEqual(line, "listener: started listener-1")
        body = self.file()
        self.assertEqual(body["agentId"], "listener-1")
        self.assertEqual(body["pid"], "42")
        self.assertEqual(body["reason"], "start")
        self.assertTrue(body["lastPollAt"])
        beat = inbound.heartbeat(self.beam_path, "listener-1", pid="42", reason="poll")
        self.assertEqual(beat, "listener: heartbeat listener-1")
        body = self.file()
        self.assertEqual(body["reason"], "poll")
        self.assertEqual(body["pid"], "42")
        self.assertEqual(json.loads(self.beam_path.read_text())["listener"]["lastSeenAt"], body["lastPollAt"])
        self.assertEqual(inbound.poll_seconds(self.beam_path), 300)

    def test_fresh_heartbeat_holds_and_a_return_starts_one(self):
        inbound.claim(self.beam_path, "listener-1", turn="turn-a")
        self.pin(NOW)
        held = orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(held, ["listener: hold listener-1"])
        started = orchestrator.supervise_listener(self.beam_path, returned="recycle", now=NOW)
        self.assertEqual(started[0], "listener: start reason=recycle")
        self.assertEqual(started[1], "listener: restart 1 reason=recycle")
        self.assertEqual(sum(1 for line in started if line.startswith("listener: start")), 1)
        again = orchestrator.supervise_listener(self.beam_path, returned="recycle", now=NOW)
        self.assertEqual(again, ["listener: hold listener-1"])
        journal = (self.tmp / ".warp" / "journal.jsonl").read_text()
        self.assertEqual(journal.count("listener-restart"), 1)

    def test_stale_file_starts_one_and_a_sleeping_poll_does_not(self):
        inbound.claim(self.beam_path, "listener-1", turn="turn-a")
        inbound.heartbeat(self.beam_path, "listener-1", reason="poll")
        data = json.loads(self.beam_path.read_text())
        data["config"]["listenerStaleMinutes"] = 2
        self.beam_path.write_text(json.dumps(data) + "\n")
        self.pin(plus(NOW, -4), clear_pending=True)
        # pollSeconds 300 makes the window 6 minutes, so 4 minutes is still alive.
        self.assertEqual(orchestrator.listener_stale_minutes(data, self.beam_path), 6)
        held = orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(held, ["listener: hold listener-1"])
        self.pin(plus(NOW, -20), clear_pending=True)
        started = orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(started[0], "listener: start reason=stale")
        self.assertEqual(sum(1 for line in started if line.startswith("listener: start")), 1)

    def test_pause_and_stop_do_not_start_and_parent_stays_for_a_running_loop(self):
        inbound.claim(self.beam_path, "listener-1", turn="turn-a")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, returned="pause", now=NOW), ["listener: idle"])
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, returned="warp:stop", now=NOW), ["listener: idle"])
        data = json.loads(self.beam_path.read_text())
        data["runState"] = "paused"
        data["paused"] = True
        self.beam_path.write_text(json.dumps(data) + "\n")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: idle"])
        self.assertTrue(orchestrator.parent_may_exit(data))
        running = {"runState": "running", "tickets": {}}
        self.assertTrue(orchestrator.parent_may_exit(running))
        open_ticket = {"runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "review"}}}
        self.assertFalse(orchestrator.parent_may_exit(open_ticket))

    def test_repeated_restarts_log_each_one_and_post_one_note(self):
        inbound.claim(self.beam_path, "listener-1", turn="turn-a")
        self.pin(plus(NOW, -60), clear_pending=True)
        notes = []
        for step in range(4):
            lines = orchestrator.supervise_listener(self.beam_path, now=plus(NOW, step * 20))
            self.assertTrue(any(line.startswith("listener: start") for line in lines))
            self.assertEqual(sum(1 for line in lines if line.startswith("listener: start")), 1)
            notes.extend(line for line in lines if line.startswith("herald:"))
        self.assertEqual(notes, ["herald: Listener kept restarting. One listener is running."])
        posted = json.loads((self.tmp / ".warp" / "listener-note.json").read_text())
        self.assertEqual(posted["lines"], ["Listener kept restarting. One listener is running."])
        journal = (self.tmp / ".warp" / "journal.jsonl").read_text()
        self.assertEqual(journal.count('"type":"listener-restart"'), 4)
        data = json.loads(self.beam_path.read_text())
        self.assertEqual(data["listener"]["restarts"], 4)
        self.assertTrue(data["listener"]["restartNoted"])

    def test_parent_exit_and_interval_commands(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "orchestrator.py"), "parent-exit", "--beam", str(self.beam_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("parent: stay", proc.stdout)
        interval = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "inbound.py"), "interval", "--beam", str(self.beam_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(interval.returncode, 0, interval.stderr)
        self.assertEqual(interval.stdout.strip(), "300")
        help_text = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "orchestrator.py"), "?"],
            capture_output=True,
            text=True,
        )
        self.assertIn("--returned", help_text.stdout)
        self.assertIn("listenerStaleMinutes", help_text.stdout)


if __name__ == "__main__":
    unittest.main()
