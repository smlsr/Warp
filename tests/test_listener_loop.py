"""The listener is one background agent. A new one starts only from the return stamp."""

import json
import os
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
import agents  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-07T12:00:00Z"


def plus(stamp, minutes):
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class Clock:
    """A clock the test moves. sleep() advances it and counts the naps."""

    def __init__(self, start):
        self.now = float(start)
        self.naps = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.naps += 1
        self.now += float(seconds)


class ListenerLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: Warp-Run\n")
        self.beam_path = warp / "beam.json"
        self.beam_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "runState": "running",
                    "tickets": {"T-1": {"id": "T-1", "status": "review", "pr": {"url": "https://example.test/1", "rollup": "pending"}}},
                    "gates": [],
                    "config": {"pollSeconds": 300, "listenerStaleMinutes": 15},
                }
            )
            + "\n"
        )

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def write(self, data):
        self.beam_path.write_text(json.dumps(data) + "\n")

    def file(self):
        path = self.tmp / ".warp" / "listener.json"
        self.assertTrue(path.is_file(), "missing .warp/listener.json")
        return json.loads(path.read_text())

    def pin(self, stamp):
        """Set the time the last poll finished, in the file and on the beam."""
        path = self.tmp / ".warp" / "listener.json"
        body = json.loads(path.read_text()) if path.is_file() else {}
        body["lastPollAt"] = stamp
        path.write_text(json.dumps(body) + "\n")
        data = self.beam()
        data.setdefault("listener", {})["lastSeenAt"] = stamp
        self.write(data)

    def listener_rows(self):
        doc = json.loads((self.tmp / ".warp" / "agents.json").read_text())
        return [row for row in doc["agents"] if row.get("role") == "listener"]

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
        self.assertEqual(self.beam()["listener"]["lastSeenAt"], body["lastPollAt"])
        self.assertEqual(inbound.poll_seconds(self.beam_path), 300)

    def test_supervise_issues_one_listener_and_a_second_call_holds(self):
        first = orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(first, ["listener: poll"])
        raw = self.beam()["listener"]
        self.assertEqual(raw["state"], "running")
        self.assertEqual(raw["agentId"], "listener")
        self.assertEqual(raw["holder"], "listener")
        self.assertEqual(raw["generation"], 1)
        self.assertEqual(raw["pollStartedAt"], NOW)
        self.assertEqual(raw["parentSeenAt"], NOW)
        for step in range(3):
            again = orchestrator.supervise_listener(self.beam_path, now=plus(NOW, step))
            self.assertEqual(again, ["listener: hold reason=live holder=listener generation=1"])
        rows = self.listener_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "listener")
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["polls"], 1)

    def test_polled_writes_the_return_stamp_and_the_next_generation_starts(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(inbound.polled(self.beam_path, count=2, cursor="1759850000.000100"), "listener: polled 2")
        raw = self.beam()["listener"]
        self.assertEqual(raw["state"], "stopped")
        self.assertEqual(raw["returnReason"], "polled")
        self.assertNotIn("pollStartedAt", raw)
        self.assertEqual(raw["holder"], "")
        self.assertEqual(raw["lastCount"], 2)
        self.assertEqual(raw["cursor"], "1759850000.000100")
        self.assertEqual(self.file()["reason"], "polled")
        self.assertEqual(self.listener_rows()[0]["state"], "ended")
        self.assertEqual(self.listener_rows()[0]["endReason"], "polled")
        # The stamp is in and nobody holds the lease, so the next pass starts.
        # A quiet heartbeat does not. Four minutes is not a second start by itself.
        self.pin(plus(NOW, -4))
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])
        rows = self.listener_rows()
        self.assertEqual(len(rows), 1, "one listener row for the run, not one per generation")
        self.assertEqual(rows[0]["polls"], 2)
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: hold reason=live holder=listener generation=2"])

    def test_returned_closes_a_generation_the_listener_did_not_record(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        lines = orchestrator.supervise_listener(self.beam_path, returned="listen: 0", now=plus(NOW, 1))
        self.assertEqual(lines, ["listener: poll"])
        raw = self.beam()["listener"]
        self.assertEqual(raw["generation"], 2)
        self.assertEqual(raw["holder"], "listener")
        self.assertEqual(self.file()["returnReason"], "")

    def test_listen_stopped_idles_one_pass_and_a_later_pass_starts(self):
        """Carrying `listen: stopped` must not idle every later pass."""
        data = self.beam()
        data["config"]["pollSeconds"] = 0
        self.write(data)
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])
        lines = orchestrator.supervise_listener(self.beam_path, returned="listen: stopped", now=plus(NOW, 1))
        self.assertEqual(lines, ["listener: idle"])
        raw = self.beam()["listener"]
        self.assertNotIn("pollStartedAt", raw)
        self.assertEqual(raw["returnReason"], "stopped")
        self.assertEqual(raw["holder"], "")
        self.assertEqual(self.file()["reason"], "stopped")
        # The same argument on the next pass, with tickets still open, starts again.
        again = orchestrator.supervise_listener(self.beam_path, returned="listen: stopped", now=plus(NOW, 2))
        self.assertEqual(again, ["listener: poll"])
        self.assertEqual(self.beam()["listener"]["generation"], 2)
        self.assertEqual(self.beam()["listener"]["holder"], "listener")

    def test_a_stale_heartbeat_does_not_start_another_listener(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertEqual(
            orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 16)),
            ["listener: hold reason=live holder=listener generation=1"],
        )
        self.assertEqual(self.beam()["listener"]["generation"], 1)
        journal = self.tmp / ".warp" / "journal.jsonl"
        if journal.is_file():
            self.assertNotIn("listener-poll-lost", journal.read_text())
        rows = self.listener_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["polls"], 1)

    def test_listener_intervals_default_when_unset(self):
        data = self.beam()
        self.assertEqual(inbound.fast_seconds(data, self.beam_path), 15)
        self.assertEqual(inbound.slow_seconds(data, self.beam_path), 60)
        self.assertEqual(inbound.orphan_minutes(data, self.beam_path), 5)
        self.assertEqual(inbound.max_minutes(data, self.beam_path), 60)
        self.assertEqual(inbound.max_reads(data, self.beam_path), 60)
        self.assertEqual(inbound.listener_model(data, self.beam_path), "")

    def test_the_pass_prints_the_prompt_for_one_background_listener(self):
        lines = orchestrator.supervise(self.beam_path, now=NOW, provider={})
        at = lines.index("listener: poll")
        self.assertIn("start one background warp-listen subagent named `[warp] listener`", lines[at + 1])
        self.assertIn("do not wait for it", lines[at + 1])
        self.assertIn("model: inherit", lines[at + 1])
        self.assertNotIn("subagentVm", lines[at + 1])
        prompt = lines[at + 2 : at + 2 + 10]
        self.assertEqual(prompt[0], "[warp] listener")
        self.assertEqual(prompt[1], "LISTEN")
        self.assertNotIn("LISTEN once", prompt)
        self.assertEqual(prompt[2], "beam: %s" % self.beam_path.resolve())
        self.assertEqual(prompt[3], "agent: listener")
        self.assertEqual(prompt[4], "lease: 1")
        self.assertEqual(prompt[5], "holder: listener")
        self.assertEqual(prompt[6], "background: true")
        self.assertIn("subagentVm does not apply", prompt[7])
        self.assertIn("You never start another agent of any kind", prompt[7])
        self.assertIn("inbound.py poll --beam %s --lease 1 --holder listener" % self.beam_path.resolve(), prompt[8])
        self.assertIn("inbound.py wait --beam %s --lease 1 --holder listener" % self.beam_path.resolve(), prompt[9])
        self.assertEqual(agents.launch_marker("\n".join(prompt)), ("listener", ""))
        self.assertTrue(agents.has_tag("\n".join(prompt)))
        again = orchestrator.supervise(self.beam_path, now=NOW, provider={})
        self.assertIn("listener: hold reason=live holder=listener generation=1", again)
        self.assertNotIn("\nLISTEN\n", "\n".join(again))
        self.assertEqual(self.beam()["pass"]["count"], 2)

    def test_listener_model_is_carried_when_set(self):
        data = self.beam()
        data["config"]["listenerModel"] = "composer-2.5"
        self.write(data)
        lines = orchestrator.listener_prompt(self.beam_path)
        # No generation is out yet, so the prompt still names the default holder.
        self.assertIn("model: composer-2.5", lines[0])
        self.assertIn("model: composer-2.5", lines)

    def test_no_channel_means_no_listener(self):
        (self.tmp / ".warp" / "config.yaml").write_text("messenger: both\n")
        self.assertEqual(inbound.channels(self.beam_path), [])
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: none"])
        self.assertNotIn("listener", self.beam())
        self.assertFalse((self.tmp / ".warp" / "agents.json").is_file())

    def test_pause_and_stop_do_not_poll(self):
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, returned="pause", now=NOW), ["listener: idle"])
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, returned="warp:stop", now=NOW), ["listener: idle"])
        data = self.beam()
        data["runState"] = "paused"
        data["paused"] = True
        self.write(data)
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: idle"])
        self.assertTrue(orchestrator.parent_may_exit(data))
        running = {"runState": "running", "tickets": {}}
        self.assertTrue(orchestrator.parent_may_exit(running))
        open_ticket = {"runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "review"}}}
        self.assertFalse(orchestrator.parent_may_exit(open_ticket))

    def test_poll_prints_the_reap_line_and_where_to_read_from(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        lines = inbound.poll_lines(self.beam_path)
        self.assertEqual(lines[0], "reap: continue")
        self.assertEqual(lines[1], "listen: slack channel=warp-run since=")
        inbound.polled(self.beam_path, count=0, cursor="1759850000.000100")
        self.pin(plus(NOW, -6))
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        self.assertIn("listen: slack channel=warp-run since=1759850000.000100", inbound.poll_lines(self.beam_path))

    def test_a_poll_started_after_a_pause_exits_without_reading(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        data = self.beam()
        data["runState"] = "paused"
        data["paused"] = True
        self.write(data)
        lines = inbound.poll_lines(self.beam_path)
        self.assertEqual(lines[0], "reap: exit paused")
        self.assertIn("Do not read the channel", lines[1])
        self.assertFalse(any(line.startswith("listen:") for line in lines))
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "inbound.py"), "poll", "--beam", str(self.beam_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("reap: exit paused", proc.stdout)

    def test_poll_and_polled_commands(self):
        orchestrator.supervise_listener(self.beam_path, now=NOW)
        poll = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "inbound.py"), "poll", "--beam", str(self.beam_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(poll.returncode, 0, poll.stderr)
        self.assertIn("reap: continue", poll.stdout)
        self.assertIn("listen: slack channel=warp-run", poll.stdout)
        done = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "inbound.py"), "polled", "--beam", str(self.beam_path), "--count", "1", "--cursor", "9.1"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "listener: polled 1")
        self.assertEqual(self.beam()["listener"]["cursor"], "9.1")


class ParentWaitTests(unittest.TestCase):
    """`parent-exit --wait` is the one wait in a run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.beam_path = warp / "beam.json"
        self.start = epoch(NOW)
        self.clock = Clock(self.start)
        self.write(
            {
                "version": 1,
                "runState": "running",
                "tickets": {"T-1": {"id": "T-1", "status": "review", "pr": {"url": "https://example.test/1", "rollup": "pending"}}},
                "gates": [],
                "config": {"pollSeconds": 300, "listenerStaleMinutes": 15},
                "pass": {"startedAt": NOW, "count": 1},
                # The last poll finished as this pass started, so none is due yet.
                "listener": {"state": "stopped", "lastSeenAt": NOW},
            }
        )

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def write(self, data):
        self.beam_path.write_text(json.dumps(data) + "\n")

    def wait(self, limit=None):
        return orchestrator.parent_wait(self.beam_path, limit=limit, sleep=self.clock.sleep, clock=self.clock.time)

    def touch_ticket(self, tid, when):
        folder = self.tmp / ".warp" / "tickets" / tid
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "state.json"
        target.write_text("{}\n")
        os.utime(target, (when, when))

    def test_times_out_after_the_limit_and_sleeps_in_between(self):
        self.assertEqual(self.wait(limit=10), "timeout")
        self.assertGreaterEqual(self.clock.now - self.start, 10)
        self.assertGreaterEqual(self.clock.naps, 2)

    def test_default_limit_is_poll_seconds_and_a_quiet_listener_does_not_end_it(self):
        # A listener that is already up is not a reason to wake. The cap is pollSeconds.
        self.assertEqual(self.wait(), "timeout")
        self.assertLessEqual(self.clock.now - self.start, 302)
        self.assertGreaterEqual(self.clock.now - self.start, 298)
        self.assertTrue(self.beam()["listener"].get("parentSeenAt"))

    def test_a_pending_command_wakes_the_wait(self):
        path = self.tmp / ".warp" / "pending-commands.jsonl"
        path.write_text('{"id":"1","text":"warp:status","applied":false}\n')
        os.utime(path, (self.start + 5, self.start + 5))
        self.assertEqual(self.wait(limit=60), "command")
        self.assertEqual(self.clock.naps, 0)

    def test_a_return_stamp_wakes_the_wait(self):
        data = self.beam()
        data["listener"]["returnReason"] = "rotated"
        data["listener"]["returnedAt"] = plus(NOW, 1)
        data["listener"]["holder"] = ""
        self.write(data)
        self.assertEqual(self.wait(limit=60), "return")
        self.assertEqual(self.clock.naps, 0)

    def test_a_ticket_folder_written_since_the_pass_started_returns_at_once(self):
        self.touch_ticket("T-1", self.start - 30)
        self.assertEqual(self.wait(limit=6), "timeout", "a folder older than the pass is not a change")
        self.touch_ticket("T-1", self.start + 5)
        naps = self.clock.naps
        self.assertEqual(self.wait(limit=60), "ticket T-1")
        self.assertEqual(self.clock.naps, naps)

    def test_a_pause_meanwhile_ends_the_wait(self):
        beam_path = self.beam_path

        def sleep_then_pause(seconds):
            self.clock.sleep(seconds)
            data = json.loads(beam_path.read_text())
            data["runState"] = "paused"
            data["paused"] = True
            beam_path.write_text(json.dumps(data) + "\n")

        got = orchestrator.parent_wait(self.beam_path, limit=120, sleep=sleep_then_pause, clock=self.clock.time)
        self.assertEqual(got, "halt")
        self.assertEqual(self.clock.naps, 1)

    def test_parent_exit_waits_then_stays(self):
        lines = orchestrator.parent_exit(self.beam_path, wait=5, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(lines, ["pass: timeout", "parent: stay"])

    def test_parent_exit_without_wait_does_not_sleep(self):
        lines = orchestrator.parent_exit(self.beam_path, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(lines, ["parent: stay"])
        self.assertEqual(self.clock.naps, 0)

    def test_parent_exits_when_paused_and_does_not_wait(self):
        data = self.beam()
        data["runState"] = "paused"
        data["paused"] = True
        self.write(data)
        lines = orchestrator.parent_exit(self.beam_path, wait=60, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(lines[0], "parent: exit")
        self.assertEqual(self.clock.naps, 0)

    def test_a_later_start_takes_the_run_over(self):
        data = self.beam()
        data["parentSession"] = "session-a"
        agents.note_parent(data, now=NOW, beam_path=self.beam_path, cloud_id="")
        self.write(data)
        stay = orchestrator.parent_exit(self.beam_path, session="session-a")
        self.assertEqual(stay, ["parent: stay"])
        data = self.beam()
        data["parentSession"] = "session-b"
        agents.note_parent(data, now=plus(NOW, 1), beam_path=self.beam_path, cloud_id="")
        self.write(data)
        old = orchestrator.parent_exit(self.beam_path, session="session-a", wait=60, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(old[0], "parent: exit superseded")
        self.assertEqual(self.clock.naps, 0)
        new = orchestrator.parent_exit(self.beam_path, session="session-b")
        self.assertEqual(new, ["parent: stay"])

    def test_parent_exit_and_interval_commands(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "orchestrator.py"), "parent-exit", "--beam", str(self.beam_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "parent: stay")
        waited = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "orchestrator.py"), "parent-exit", "--beam", str(self.beam_path), "--wait", "0", "--session", "none"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("pass: ", waited.stdout)
        self.assertIn("parent: stay", waited.stdout)
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
        self.assertIn("--wait", help_text.stdout)
        self.assertIn("reason=await-return", help_text.stdout)
        self.assertIn("listener.parentSeenAt", help_text.stdout)
        self.assertNotIn("listenerStaleMinutes", help_text.stdout)


class ListenerLeaseTests(unittest.TestCase):
    """The generation lease, the orphan exit, rotation, and the reap on pause."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.beam_path = warp / "beam.json"
        self.beam_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "runState": "running",
                    "tickets": {"T-1": {"id": "T-1", "status": "review"}},
                    "gates": [],
                    "config": {
                        "listenerFastSeconds": 4,
                        "listenerSlowSeconds": 6,
                        "listenerOrphanMinutes": 5,
                        "listenerMaxMinutes": 60,
                        "listenerMaxReads": 60,
                    },
                }
            )
            + "\n"
        )

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def write(self, data):
        self.beam_path.write_text(json.dumps(data) + "\n")

    def start(self):
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])

    def test_a_mismatched_lease_exits_before_a_read(self):
        self.start()
        self.assertEqual(inbound.take_lease(self.beam_path, "chat", now=NOW), "listener: lease generation=2 holder=chat")
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(lines[0], "reap: exit superseded")
        self.assertFalse(any(line.startswith("listen: slack") for line in lines))
        raw = self.beam()["listener"]
        self.assertEqual(raw["holder"], "chat")
        self.assertEqual(raw["generation"], 2)
        self.assertEqual(raw.get("returnReason") or "", "")

    def test_a_halted_lease_exits_before_a_read(self):
        self.start()
        data = self.beam()
        data["runState"] = "stopped"
        self.write(data)
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(lines[0], "reap: exit stopped")
        self.assertFalse(any(line.startswith("listen: slack") for line in lines))
        paused = self.beam()
        paused["runState"] = "paused"
        paused["paused"] = True
        paused["listener"]["holder"] = "listener"
        paused["listener"]["generation"] = 1
        paused["listener"]["state"] = "running"
        paused["listener"]["pollStartedAt"] = NOW
        self.write(paused)
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(lines[0], "reap: exit paused")
        self.assertFalse(any(line.startswith("listen: slack") for line in lines))

    def test_an_orphaned_parent_exits_and_stamps(self):
        self.start()
        data = self.beam()
        data["listener"]["parentSeenAt"] = plus(NOW, -6)
        self.write(data)
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(lines[0], "reap: exit orphaned")
        self.assertIn("listen: stopped", lines)
        self.assertFalse(any(line.startswith("listen: slack") for line in lines))
        self.assertEqual(self.beam()["listener"]["returnReason"], "orphaned")
        self.assertEqual(self.beam()["listener"]["holder"], "")

    def test_no_respawn_before_the_return_stamp(self):
        data = self.beam()
        data["config"]["listenerMaxMinutes"] = 1
        self.write(data)
        self.start()
        held = orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 2))
        self.assertEqual(held, ["listener: hold reason=await-return generation=2"])
        self.assertEqual(self.beam()["listener"]["holder"], "")
        self.assertTrue(self.beam()["listener"]["awaitReturn"])
        still = orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 3))
        self.assertEqual(still, ["listener: hold reason=await-return generation=2"])
        self.assertNotEqual(still, ["listener: poll"])
        stamped = inbound.mark_returned(self.beam_path, "rotated", lease=1, holder="listener", now=plus(NOW, 3))
        self.assertEqual(stamped, "listener: returned rotated")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=plus(NOW, 4)), ["listener: poll"])
        self.assertEqual(self.beam()["listener"]["generation"], 3)
        self.assertEqual(self.beam()["listener"]["holder"], "listener")

    def test_rotation_at_max_reads(self):
        data = self.beam()
        data["config"]["listenerMaxReads"] = 2
        self.write(data)
        self.start()
        self.assertEqual(inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)[0], "reap: continue")
        self.assertEqual(inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)[0], "reap: continue")
        self.assertEqual(self.beam()["listener"]["reads"], 2)
        rotated = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(rotated, ["listen: rotate"])
        self.assertEqual(self.beam()["listener"]["returnReason"], "rotated")
        self.assertEqual(self.beam()["listener"]["holder"], "")

    def test_wait_uses_the_fast_interval_then_the_slow_one(self):
        self.start()
        clock = Clock(epoch(NOW))

        def nap(seconds):
            clock.sleep(seconds)

        fast = inbound.wait_for(
            self.beam_path, lease=1, holder="listener", fast=True, sleep=nap, clock=clock.time, step=2, now=None
        )
        self.assertEqual(fast, ["listen: wait"])
        self.assertEqual(self.beam()["listener"]["interval"], "fast")
        self.assertGreaterEqual(clock.now - epoch(NOW), 4)
        started = clock.now
        slow = inbound.wait_for(
            self.beam_path, lease=1, holder="listener", fast=False, sleep=nap, clock=clock.time, step=2
        )
        self.assertEqual(slow, ["listen: wait"])
        self.assertEqual(self.beam()["listener"]["interval"], "slow")
        self.assertGreaterEqual(clock.now - started, 6)

    def test_wait_reaps_on_pause_and_on_stop_within_one_step(self):
        self.start()
        for state in ("paused", "stopped"):
            data = self.beam()
            data["runState"] = "running"
            data["paused"] = False
            data["listener"]["holder"] = "listener"
            data["listener"]["generation"] = 1
            data["listener"]["state"] = "running"
            data["listener"]["pollStartedAt"] = NOW
            data["listener"]["generationStartedAt"] = NOW
            data["listener"]["parentSeenAt"] = NOW
            data["listener"]["returnReason"] = ""
            data["listener"]["reads"] = 0
            self.write(data)
            clock = Clock(epoch(NOW))
            beam_path = self.beam_path

            def nap(seconds, state=state):
                clock.sleep(seconds)
                live = json.loads(beam_path.read_text())
                live["runState"] = state
                live["paused"] = state != "running"
                beam_path.write_text(json.dumps(live) + "\n")

            lines = inbound.wait_for(
                self.beam_path, lease=1, holder="listener", fast=False, sleep=nap, clock=clock.time, step=2
            )
            self.assertEqual(lines[0], "reap: exit %s" % state, lines)
            self.assertIn("listen: stopped", lines)
            self.assertEqual(clock.naps, 1)
            self.assertLessEqual(clock.now - epoch(NOW), 2.0)

    def test_a_duplicate_slack_ts_writes_no_second_ack(self):
        first = inbound.accept(self.beam_path, "warp:status", by="shawn", source="slack")
        self.assertIn("Posting the digest", first["ack"])
        inbound.enqueue(self.beam_path, "warp:status", "shawn", "200.1", "slack")
        acks = (self.tmp / ".warp" / "journal.jsonl").read_text().count('"type": "ack"')
        dup = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "inbound.py"),
                "accept",
                "--beam",
                str(self.beam_path),
                "--text",
                "warp:status",
                "--message-id",
                "200.1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(dup.returncode, 0, dup.stderr)
        self.assertIn("inbound: duplicate 200.1", dup.stdout)
        self.assertNotIn("ack:", dup.stdout)
        self.assertEqual((self.tmp / ".warp" / "journal.jsonl").read_text().count('"type": "ack"'), acks)


if __name__ == "__main__":
    unittest.main()
