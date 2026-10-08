"""Agent lifecycle: the gate, the worker contract, the reap check, the halt, and the clear.

One parent is long-lived. Every other agent is one step. Pause and stop tear
everything down. Nothing here depends on a Cursor hook.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agents  # noqa: E402
import beam  # noqa: E402
import orchestrator  # noqa: E402
import inbound  # noqa: E402
import pipeline  # noqa: E402
import scan  # noqa: E402
import ticket_state  # noqa: E402

NOW = "2026-10-06T12:00:00Z"
LATER = "2026-10-06T12:05:00Z"
MUCH_LATER = "2026-10-06T13:00:00Z"
ORIGIN = "https://github.com/smlsr/Warp.git"
OTHER = "https://github.com/acme/Other.git"
API = "https://api.cursor.com/v1/agents"


def ticket(tid="T-1", **extra):
    row = {
        "id": tid,
        "status": "coding",
        "summary": "ship %s" % tid,
        "size": "M",
        "module": "app",
        "deps": [],
        "locks": ["src/%s" % tid],
        "agent": "",
        "branch": "warp/%s" % tid,
        "attempts": 0,
        "pr": {},
        "shuttle": {},
    }
    row.update(extra)
    return row


def beam_of(*rows, **config):
    cfg = {"staleMinutes": 15, "maxAgents": 18, "baseBranch": "main", "maxRecoveries": 5}
    cfg.update(config)
    return {
        "version": 1,
        "runState": "running",
        "paused": False,
        "tickets": {row["id"]: row for row in rows},
        "gates": [],
        "config": cfg,
        "program": {"criticalPath": []},
    }


def cloud_item(ident, name="agent", status="IDLE", repo=ORIGIN, prompt="", run="run-1"):
    item = {"id": ident, "name": name, "status": status, "updatedAt": NOW, "latestRunId": run}
    if repo:
        item["repos"] = [{"url": repo}]
    if prompt:
        item["prompt"] = {"text": prompt}
    return item


def transport_for(items, cancel_status=200, archive_status=200):
    calls = []

    def transport(method, url, key, body=None):
        calls.append((method, url))
        if method == "POST" and url.endswith("/cancel"):
            return cancel_status, {}
        if method == "POST":
            return archive_status, {}
        if "/v1/agents?" in url:
            return 200, {"items": items}
        ident = url.rstrip("/").rsplit("/", 1)[-1]
        for item in items:
            if item.get("id") == ident:
                return 200, item
        return 404, {}

    return calls, transport


def posts(calls):
    return [url[len(API) :] for method, url in calls if method == "POST"]


def run(script, *args, env=None, cwd=None, stdin=None):
    clean = dict(os.environ)
    clean.pop("CURSOR_API_KEY", None)
    clean.pop("CURSOR_AGENT_ID", None)
    clean.update(env or {})
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=clean,
        cwd=cwd,
        input=stdin,
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.path = self.warp / "beam.json"
        os.environ.pop("CURSOR_API_KEY", None)

    def save(self, data):
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def load(self):
        return json.loads(self.path.read_text())

    def registry(self):
        return json.loads((self.warp / "agents.json").read_text())

    def rows(self, role="shuttle"):
        return [row for row in self.registry()["agents"] if row.get("role") == role]

    def launch(self, data, tid="T-1", step="implement", now=NOW):
        lines = agents.launch_lines(data, data["tickets"][tid], step, beam_path=self.path, now=now)
        self.save(data)
        return lines

    def halt(self, data, state):
        data["runState"] = state
        data["paused"] = state == "paused"
        lines = agents.halt_local(data, beam_path=self.path, reason=state, now=LATER)
        self.save(data)
        return lines


class HaltTests(Base):
    """Pause and stop are the same teardown."""

    def test_pause_and_stop_end_every_agent_and_free_every_slot(self):
        for state in ("paused", "stopped"):
            with self.subTest(state=state):
                data = beam_of(
                    ticket("T-1", shuttle={"pending": True, "step": "implement"}),
                    ticket("T-2", status="review", shuttle={"pending": False}),
                    ticket("T-3", status="merged", shuttle={"pending": True}),
                )
                (self.warp / "agents.json").unlink(missing_ok=True)
                self.save(data)
                self.launch(data, "T-1")
                self.launch(data, "T-2", step="fix")
                agents.note_listener_poll(data, now=NOW, beam_path=self.path)
                data["listener"] = {"state": "running", "pollStartedAt": NOW}
                lines = self.halt(data, state)
                self.assertEqual(lines[-1], "halt: %s agents=3" % state)
                self.assertEqual(sorted(lines[:-1]), ["stop: listener", "stop: subagent:T-1", "stop: subagent:T-2"])
                for row in self.registry()["agents"]:
                    self.assertEqual(row["state"], "stopped")
                    self.assertEqual(row["endReason"], state)
                    self.assertTrue(row["halted"])
                first = data["tickets"]["T-1"]
                self.assertFalse(first["shuttle"]["pending"])
                self.assertTrue(first["needsReplacement"])
                self.assertTrue(first["haltResume"])
                self.assertNotIn("needsReplacement", data["tickets"]["T-2"])
                self.assertNotIn("haltResume", data["tickets"]["T-3"], "a merged ticket is left alone")
                self.assertNotIn("pollStartedAt", data["listener"])
                self.assertEqual(data["listener"]["state"], "stopped")
                self.assertEqual(agents.halt_state(data), state)

    def test_a_second_halt_is_a_no_op(self):
        data = beam_of(ticket())
        self.save(data)
        self.launch(data)
        self.halt(data, "paused")
        again = agents.halt_local(data, beam_path=self.path, reason="paused", now=MUCH_LATER)
        self.assertEqual(again, ["halt: paused agents=0"])
        self.assertEqual(self.rows()[0]["ended"], LATER)

    def test_nothing_from_before_the_halt_is_resumed(self):
        data = beam_of(ticket(status="review"))
        self.save(data)
        self.launch(data)
        row = data["tickets"]["T-1"]
        agents.note_returned(data, row, now=NOW, beam_path=self.path)
        self.assertEqual(agents.can_resume(data, row, beam_path=self.path), "subagent:T-1")
        self.halt(data, "paused")
        data["runState"] = "running"
        data["paused"] = False
        self.assertEqual(agents.can_resume(data, row, beam_path=self.path), "")
        lines = self.launch(data, step="fix", now=MUCH_LATER)
        self.assertTrue(lines[0].startswith("start T-1"), lines)
        self.assertIn("agent=shuttle-T-1-r1", lines[0])
        self.assertNotIn("resume", "\n".join(lines))
        live = [item for item in self.rows() if item["state"] in {"starting", "running"}]
        self.assertEqual([item["id"] for item in live], ["shuttle-T-1-r1"])
        # The id from before the halt is told to exit, even with the run running again.
        old = agents.reap(data, "subagent:T-1", "T-1", beam_path=self.path, now=MUCH_LATER)
        self.assertEqual(old[0], "reap: exit halted")
        new = agents.reap(data, "shuttle-T-1-r1", "T-1", beam_path=self.path, now=MUCH_LATER)
        self.assertEqual(new, ["reap: continue"])

    def test_resume_after_a_halt_is_not_a_recovery(self):
        data = beam_of(ticket(shuttle={"pending": True, "step": "implement"}, recoveries=5))
        self.save(data)
        self.launch(data)
        self.halt(data, "paused")
        data["runState"] = "running"
        data["paused"] = False
        data["_agentBeam"] = str(self.path)
        row = data["tickets"]["T-1"]
        lines = []
        pipeline._launch_replacement(data, row, lines, "implement")
        text = "\n".join(lines)
        self.assertIn("start T-1", text)
        self.assertIn("resume after halt", text)
        self.assertNotIn("died", text)
        self.assertNotIn("herald:", text)
        self.assertEqual(row["recoveries"], 5, "a halt is not counted as a recovery")
        self.assertNotEqual(row["status"], "alarm", "the recovery cap does not park a halted ticket")
        self.assertNotIn("haltResume", row)
        self.assertFalse(row["needsReplacement"])

    def test_a_long_pause_is_not_a_dead_worker(self):
        stale = "2026-10-06T08:00:00Z"
        data = beam_of(
            ticket("T-1", status="coding", agent="subagent:T-1", lastSeenAt=stale, claimedAt=stale, shuttle={"pending": True, "step": "implement"}),
            # Shuttle work with no slot marked: claimed, and the launch had not been recorded yet.
            ticket("T-2", status="claimed", agent="subagent:T-2", lastSeenAt=stale, claimedAt=stale, shuttle={}),
        )
        self.save(data)
        self.halt(data, "paused")
        for tid in ("T-1", "T-2"):
            self.assertTrue(data["tickets"][tid]["needsReplacement"], tid)
            self.assertTrue(data["tickets"][tid]["haltResume"], tid)
        data["runState"] = "running"
        data["paused"] = False
        self.save(data)
        # Hours later the heartbeat is long stale. The watchdog must not call that a death.
        lines = beam.watchdog(self.path, now="2026-10-07T09:00:00Z")
        text = "\n".join(lines)
        self.assertNotIn("worker died", text)
        self.assertNotIn("shuttle: replace", text)
        after = self.load()
        for tid in ("T-1", "T-2"):
            self.assertFalse(after["tickets"][tid].get("recoveries"), tid)
        after["_agentBeam"] = str(self.path)
        out = []
        pipeline.dispatch_replacements(after, out)
        text = "\n".join(out)
        self.assertIn("fix: T-1 resume after halt", text)
        self.assertIn("fix: T-2 resume after halt", text)
        self.assertNotIn("herald:", text)
        for tid in ("T-1", "T-2"):
            self.assertFalse(after["tickets"][tid].get("recoveries"), tid)

    def test_scan_pause_and_stop_run_the_teardown(self):
        for verb, state in (("pause", "paused"), ("stop", "stopped")):
            with self.subTest(verb=verb):
                data = beam_of(ticket(agent="bc-shuttle-1", shuttle={"pending": True, "step": "implement"}))
                (self.warp / "agents.json").unlink(missing_ok=True)
                self.save(data)
                self.launch(data)
                done = run("scan.py", verb, "--beam", str(self.path), "--reason", "hold")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("stop: bc-shuttle-1", done.stdout)
                self.assertIn("halt: %s agents=1" % state, done.stdout)
                # No key: nothing is called, and the link is printed instead.
                self.assertIn("cleanup: no CURSOR_API_KEY", done.stdout)
                self.assertIn("cleanup: link https://cursor.com/agents/bc-shuttle-1", done.stdout)
                self.assertEqual(self.rows()[0]["state"], "stopped")
                saved = self.load()
                self.assertTrue(saved["tickets"]["T-1"]["haltResume"])
                reap = run("agents.py", "reap", "--beam", str(self.path), "--id", "bc-shuttle-1", "--ticket", "T-1")
                self.assertEqual(reap.returncode, 3, reap.stdout)
                self.assertIn("reap: exit %s" % state, reap.stdout)

    def test_beam_pause_runs_the_same_teardown(self):
        data = beam_of(ticket(shuttle={"pending": True, "step": "implement"}))
        self.save(data)
        self.launch(data)
        done = run("beam.py", "pause", "--beam", str(self.path), "--reason", "hold")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("stop: subagent:T-1", done.stdout)
        self.assertIn("halt: paused agents=1", done.stdout)
        self.assertEqual(self.rows()[0]["endReason"], "paused")

    def test_start_records_one_parent_and_prints_its_session(self):
        data = beam_of(ticket())
        self.save(data)
        first = run("scan.py", "start", "--beam", str(self.path), "--force")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        session = next(line for line in first.stdout.splitlines() if line.startswith("parent: session ")).split()[-1]
        self.assertEqual(self.load()["parentSession"], session)
        second = run("scan.py", "resume", "--beam", str(self.path))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        other = next(line for line in second.stdout.splitlines() if line.startswith("parent: session ")).split()[-1]
        self.assertNotEqual(session, other)
        parents = self.rows("parent")
        self.assertEqual([row["state"] for row in parents], ["ended", "running"])
        self.assertEqual(parents[0]["endReason"], "replaced")
        old = run("orchestrator.py", "parent-exit", "--beam", str(self.path), "--session", session)
        self.assertIn("parent: exit superseded", old.stdout)
        new = run("orchestrator.py", "parent-exit", "--beam", str(self.path), "--session", other)
        self.assertEqual(new.stdout.strip(), "parent: stay")
        check = run("agents.py", "reap", "--beam", str(self.path), "--role", "parent", "--id", session)
        self.assertEqual(check.returncode, 3)
        self.assertIn("reap: exit replaced", check.stdout)


class HaltCloudTests(Base):
    """With a key, pause and stop cancel and archive this run's cloud agents."""

    def halted(self, rows):
        data = beam_of(ticket())
        data["instance"] = {"id": "a1b2c3", "host": "laptop", "machine": "local-laptop-1"}
        data["runState"] = "paused"
        data["paused"] = True
        self.save(data)
        (self.warp / "agents.json").write_text(json.dumps({"agents": rows, "spawns": []}) + "\n")
        return data

    def test_registered_and_launched_agents_are_cancelled_and_archived(self):
        rows = [
            {"id": "subagent:T-1", "cloudId": "bc-bound", "ticket": "T-1", "role": "shuttle", "state": "stopped", "ended": NOW},
            {"id": "bc-row", "ticket": "T-2", "role": "shuttle", "state": "stopped", "ended": NOW},
            {"id": "bc-parent", "ticket": "", "role": "parent", "state": "stopped", "ended": NOW},
        ]
        items = [
            cloud_item("bc-bound", status="RUNNING"),
            cloud_item("bc-row"),
            cloud_item("bc-marked", "[warp:a1b2c3] T-9 implement", status="ACTIVE"),
            cloud_item("bc-implement", "[warp:a1b2c3] T-8 fix"),
            cloud_item("bc-listen", "[warp:a1b2c3] listener"),
            cloud_item("bc-named", name="Shuttle T-7", prompt="please fix CI on the shuttle"),
            cloud_item("bc-other-warp", "[warp:ffffff] T-1 implement", status="ACTIVE"),
            cloud_item("bc-parent", status="RUNNING", prompt="/warp-start"),
            cloud_item("bc-self", "[warp:a1b2c3] T-6 implement", status="RUNNING"),
            cloud_item("bc-elsewhere", "[warp:a1b2c3] T-1 implement", repo=OTHER),
        ]
        data = self.halted(rows)
        calls, transport = transport_for(items)
        lines = agents.halt_cloud(data, beam_path=self.path, now=LATER, key="k", transport=transport, origin=ORIGIN, self_id="bc-self")
        self.assertEqual(
            posts(calls),
            [
                "/bc-bound/runs/run-1/cancel",
                "/bc-bound/archive",
                "/bc-row/archive",
                "/bc-marked/runs/run-1/cancel",
                "/bc-marked/archive",
                "/bc-implement/archive",
                "/bc-listen/archive",
            ],
        )
        text = "\n".join(lines)
        self.assertIn("cleanup: cancelled bc-bound", text)
        self.assertIn("cleanup: skip bc-parent reason=parent", text)
        self.assertIn("cleanup: skip bc-self reason=self", text)
        for ident in ("bc-bound", "bc-row", "bc-marked", "bc-implement", "bc-listen"):
            self.assertIn("cleanup: archived %s" % ident, text)
        # The registry's agents go first, by id. The list sweep finds the other three.
        self.assertLess(text.index("cleanup: archived bc-row"), text.index("cleanup: count 3"))
        # A name that sounds like Warp is not the tag. Another Warp run's agents are not this run's.
        self.assertNotIn("bc-named", text)
        self.assertNotIn("bc-other-warp", text)
        self.assertNotIn("bc-elsewhere", text)
        saved = {row["id"]: row for row in self.registry()["agents"]}
        self.assertTrue(saved["subagent:T-1"]["archived"])
        self.assertTrue(saved["bc-row"]["archived"])
        self.assertNotIn("archived", saved["bc-parent"])

    def test_without_a_key_it_prints_one_link_per_cloud_agent_and_calls_nothing(self):
        rows = [
            {"id": "subagent:T-1", "cloudId": "bc-bound", "ticket": "T-1", "role": "shuttle", "state": "stopped", "ended": NOW},
            {"id": "bc-parent", "ticket": "", "role": "parent", "state": "stopped", "ended": NOW},
        ]
        data = self.halted(rows)
        calls, transport = transport_for([])
        lines = agents.halt_cloud(data, beam_path=self.path, key="", transport=transport, origin=ORIGIN, self_id="")
        self.assertEqual(calls, [])
        self.assertIn("cleanup: link https://cursor.com/agents/bc-bound", lines)
        self.assertNotIn("cleanup: link https://cursor.com/agents/bc-parent", lines)

    def test_the_halt_finds_tagged_agents_by_name_without_reading_every_agent(self):
        data = self.halted([])
        tagged = {"id": "bc-tagged", "name": "[warp:a1b2c3] T-4 implement", "status": "ACTIVE", "latestRunId": "run-4"}
        others = [{"id": "bc-other-%d" % n, "name": "Fix build %d" % n, "status": "ACTIVE", "latestRunId": "r"} for n in range(29)]
        others.append({"id": "bc-other-warp", "name": "[warp:ffffff] T-4 implement", "status": "ACTIVE", "latestRunId": "r"})
        detail = cloud_item("bc-tagged", "[warp:a1b2c3] T-4 implement", status="ACTIVE", run="run-4")
        calls = []

        def transport(method, url, key, body=None):
            calls.append((method, url))
            if "/v1/agents?" in url:
                return 200, {"items": others + [tagged]}
            if method == "GET":
                return 200, detail
            return 200, {}

        lines = agents.halt_cloud(data, beam_path=self.path, now=LATER, key="k", transport=transport, origin=ORIGIN, self_id="")
        self.assertEqual(posts(calls), ["/bc-tagged/runs/run-4/cancel", "/bc-tagged/archive"])
        gets = [url for method, url in calls if method == "GET" and "/v1/agents?" not in url]
        self.assertEqual(gets, [API + "/bc-tagged"], "only the tagged agent is read. The other 30 cost no call.")
        self.assertIn("cleanup: archived bc-tagged", lines)

    def test_registered_agents_are_cancelled_even_when_the_list_fails(self):
        rows = [{"id": "subagent:T-1", "cloudId": "bc-bound", "ticket": "T-1", "role": "shuttle", "state": "stopped", "ended": NOW}]
        data = self.halted(rows)
        calls = []

        def transport(method, url, key, body=None):
            calls.append((method, url))
            if "/v1/agents?" in url:
                return 0, {"error": "no network"}
            if method == "GET":
                return 200, cloud_item("bc-bound", status="RUNNING", run="run-9")
            return 200, {}

        lines = agents.halt_cloud(data, beam_path=self.path, now=LATER, key="k", transport=transport, origin=ORIGIN, self_id="")
        self.assertEqual(posts(calls), ["/bc-bound/runs/run-9/cancel", "/bc-bound/archive"])
        self.assertIn("cleanup: cancelled bc-bound", lines)
        self.assertIn("cleanup: archived bc-bound", lines)
        self.assertTrue(self.registry()["agents"][0]["archived"])

    def test_an_unreachable_api_never_raises(self):
        rows = [{"id": "subagent:T-1", "cloudId": "bc-bound", "ticket": "T-1", "role": "shuttle", "state": "stopped", "ended": NOW}]
        data = self.halted(rows)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            status, body = agents.default_transport("GET", "https://api.cursor.com/v1/agents/bc-x", "k")
        self.assertEqual(status, 0)
        self.assertIn("refused", body["error"])
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            self.assertEqual(agents.default_transport("POST", "https://api.cursor.com/v1/agents/bc-x/archive", "k")[0], 0)

        def down(method, url, key, body=None):
            return 0, {"error": "refused"}

        lines = agents.halt_cloud(data, beam_path=self.path, now=LATER, key="k", transport=down, origin=ORIGIN, self_id="")
        self.assertIn("cleanup: unreachable bc-bound https://cursor.com/agents/bc-bound", lines)
        self.assertNotIn("archived", self.registry()["agents"][0])

    def test_the_teardown_is_cut_short_at_the_deadline(self):
        rows = [{"id": "bc-%d" % n, "ticket": "T-%d" % n, "role": "shuttle", "state": "stopped", "ended": NOW} for n in range(8)]
        items = [cloud_item("bc-%d" % n) for n in range(8)]
        data = self.halted(rows)
        calls, transport = transport_for(items)
        ticks = iter(range(0, 1000, 10))
        lines = agents.halt_cloud(
            data, beam_path=self.path, key="k", transport=transport, origin=ORIGIN, self_id="", deadline=45.0, clock=lambda: next(ticks)
        )
        self.assertIn("cleanup: cut short; run /warp-cleanup for the rest", lines)
        self.assertGreater(len(posts(calls)), 0)
        self.assertLess(len(posts(calls)), 8)

    def test_finish_tears_the_run_down_once(self):
        data = beam_of(ticket(status="merged"))
        self.save(data)
        (self.warp / "agents.json").write_text(
            json.dumps({"agents": [{"id": "bc-done", "ticket": "T-1", "role": "shuttle", "state": "ended", "ended": NOW}], "spawns": []}) + "\n"
        )
        calls, transport = transport_for([cloud_item("bc-done")])
        first = agents.finish(data, beam_path=self.path, now=LATER, key="", transport=transport)
        self.assertEqual(first[0], "halt: done agents=0")
        self.assertIn("cleanup: link https://cursor.com/agents/bc-done", first)
        self.assertEqual(self.registry()["finishedAt"], LATER)
        self.assertEqual(agents.finish(data, beam_path=self.path, now=MUCH_LATER, key="", transport=transport), [])
        running = beam_of(ticket())
        self.assertEqual(agents.finish(running, beam_path=self.path, now=LATER, key=""), [])


class ReapTests(Base):
    """Every Warp agent asks one question first: continue, or exit."""

    def started(self, **extra):
        data = beam_of(ticket(**extra), ticket("T-2"))
        self.save(data)
        self.launch(data)
        return data

    def reason(self, data, ident="subagent:T-1", tid="T-1", role="shuttle", now=LATER, strict=False):
        return agents.reap_reason(data, ident, tid, role, now=now, beam_path=self.path, strict_local=strict)

    def test_a_live_agent_continues(self):
        data = self.started()
        self.assertEqual(agents.reap(data, "subagent:T-1", "T-1", beam_path=self.path, now=LATER), ["reap: continue"])
        self.assertNotIn("reaped", self.rows()[0])

    def test_paused_stopped_and_done_exit(self):
        for state in ("paused", "stopped"):
            data = self.started()
            data["runState"] = state
            data["paused"] = state == "paused"
            self.assertEqual(self.reason(data), state)
            self.assertEqual(self.reason(data, "listener", "", "listener"), state)
            self.assertEqual(self.reason(data, "session-a", "", "parent"), state)
        done = beam_of(ticket(status="merged"), ticket("T-2", status="parked"))
        self.save(done)
        self.assertEqual(self.reason(done), "done")

    def test_a_settled_ticket_exits_and_other_tickets_continue(self):
        data = self.started()
        data["tickets"]["T-1"]["status"] = "merged"
        self.assertEqual(self.reason(data), "settled")
        self.launch(data, "T-2")
        self.assertEqual(self.reason(data, "subagent:T-2", "T-2"), "")

    def test_a_replaced_agent_exits_and_its_replacement_continues(self):
        data = self.started()
        row = data["tickets"]["T-1"]
        lines = agents.launch_lines(data, row, "restart", replacing=True, beam_path=self.path, now=LATER)
        self.assertIn("agent=shuttle-T-1-r1", lines[0])
        self.assertEqual(self.reason(data, "subagent:T-1"), "replaced")
        self.assertEqual(self.reason(data, "shuttle-T-1-r1"), "")

    def test_an_archived_agent_exits(self):
        data = self.started()
        doc = agents.registry(data, self.path)
        doc["agents"][0]["archived"] = True
        self.assertEqual(self.reason(data), "archived")

    def test_a_returned_agent_that_was_not_resumed_exits_after_the_stale_window(self):
        data = self.started()
        agents.note_returned(data, data["tickets"]["T-1"], now=NOW, beam_path=self.path)
        self.assertEqual(self.reason(data, now=LATER), "", "inside the window the parent may still resume it")
        self.assertEqual(self.reason(data, now=MUCH_LATER), "not-resumed")
        agents.launch_lines(data, data["tickets"]["T-1"], "fix", beam_path=self.path, now=MUCH_LATER)
        self.assertEqual(self.reason(data, now=MUCH_LATER), "", "the resumed Shuttle carries on")

    def test_an_id_the_parent_never_launched_exits(self):
        data = self.started()
        # A worker one of the Shuttles started on its own: no row, and the ticket has an owner.
        self.assertEqual(self.reason(data, "rogue-1", strict=True), "replaced")
        agents.note_returned(data, data["tickets"]["T-1"], now=NOW, beam_path=self.path)
        self.assertEqual(self.reason(data, "rogue-1", strict=True), "not-launched")
        # Origin can lag the local registry, so the remote check does not use this rule.
        self.assertEqual(self.reason(data, "rogue-1", strict=False), "")
        lines = agents.reap(data, "rogue-1", "T-1", beam_path=self.path, now=LATER)
        self.assertEqual(lines[0], "reap: exit not-launched")

    def test_exit_is_recorded_once_and_journaled(self):
        data = self.started()
        data["runState"] = "stopped"
        self.save(data)
        first = agents.reap(data, "subagent:T-1", "T-1", beam_path=self.path, now=LATER)
        self.assertEqual(first[0], "reap: exit stopped")
        self.assertIn("result: T-1 stopped stopped", first[1])
        self.assertIn("Do not start an agent", first[1])
        row = self.rows()[0]
        self.assertEqual(row["reaped"], LATER)
        self.assertEqual(row["state"], "ended")
        self.assertEqual(row["endReason"], "stopped")
        agents.reap(data, "subagent:T-1", "T-1", beam_path=self.path, now=MUCH_LATER)
        self.assertEqual(self.rows()[0]["reaped"], LATER)
        journal = (self.warp / "journal.jsonl").read_text()
        self.assertEqual(journal.count('"type":"reap"'), 1)

    def test_each_role_is_told_how_to_exit(self):
        self.assertEqual(agents.reap_lines(""), ["reap: continue"])
        shuttle = agents.reap_lines("paused", "T-1")
        self.assertEqual(shuttle[0], "reap: exit paused")
        self.assertIn("result: T-1 stopped paused", shuttle[1])
        listener = agents.reap_lines("paused", role="listener")
        self.assertIn("listen: stopped", listener[1])
        self.assertIn("Do not read the channel", listener[1])
        parent = agents.reap_lines("replaced", role="parent")
        self.assertIn("End this turn", parent[1])
        for lines in (shuttle, listener, parent):
            self.assertIn("timer", lines[1])

    def test_the_reap_command(self):
        data = self.started()
        ok = run("agents.py", "reap", "--beam", str(self.path), "--id", "subagent:T-1", "--ticket", "T-1")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(ok.stdout.strip(), "reap: continue")
        data["runState"] = "paused"
        data["paused"] = True
        self.save(data)
        gone = run("agents.py", "reap", "--beam", str(self.path), "--id", "subagent:T-1", "--ticket", "T-1")
        self.assertEqual(gone.returncode, 3)
        self.assertTrue(gone.stdout.startswith("reap: exit paused"))
        missing = run("agents.py", "reap", "--beam", str(self.tmp / "nope" / ".warp" / "beam.json"), "--id", "x", "--ticket", "T-1")
        self.assertEqual(missing.returncode, 0)
        self.assertIn("reap: continue (no beam", missing.stdout)

    def test_writing_state_prints_the_reap_line(self):
        data = self.started()
        ok = run("ticket_state.py", "append", "--id", "T-1", "--state", "coding", "--agent", "subagent:T-1", "--root", str(self.tmp))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("state: coding", ok.stdout)
        self.assertEqual(ok.stdout.strip().splitlines()[-1], "reap: continue")
        data["runState"] = "paused"
        data["paused"] = True
        self.save(data)
        gone = run("ticket_state.py", "append", "--id", "T-1", "--state", "heartbeat", "--agent", "subagent:T-1", "--root", str(self.tmp))
        self.assertEqual(gone.returncode, 0, "a state write never fails because of the reap check")
        self.assertIn("reap: exit paused", gone.stdout)
        self.assertTrue((self.warp / "tickets" / "T-1" / "state.json").is_file())

    def test_a_state_write_with_no_beam_prints_no_reap_line(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        done = run("ticket_state.py", "append", "--id", "T-1", "--state", "coding", "--root", str(bare))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("reap:", done.stdout)

    def test_a_heartbeat_prints_the_reap_line(self):
        self.started(agent="subagent:T-1")
        beat = run("beam.py", "heartbeat", "--beam", str(self.path), "--id", "T-1", "--agent", "subagent:T-1")
        self.assertEqual(beat.returncode, 0, beat.stdout + beat.stderr)
        self.assertIn("reap: continue", beat.stdout)
        stranger = run("beam.py", "heartbeat", "--beam", str(self.path), "--id", "T-1", "--agent", "someone-else")
        self.assertNotEqual(stranger.returncode, 0)
        self.assertIn("reap: exit replaced", stranger.stdout)

    def test_the_remote_check_reads_origin_and_writes_nothing(self):
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        parent = self.tmp / "parent"
        parent.mkdir()

        def git(*args, cwd=parent):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "warp@example.com")
        git("config", "user.name", "Warp")
        git("remote", "add", "origin", str(origin))
        warp = parent / ".warp"
        warp.mkdir()
        data = beam_of(ticket(agent="subagent:T-1"))
        rows = {"agents": [{"id": "subagent:T-1", "ticket": "T-1", "role": "shuttle", "state": "running", "started": NOW, "ended": None}], "spawns": []}

        def publish(message):
            (warp / "beam.json").write_text(json.dumps(data) + "\n")
            (warp / "agents.json").write_text(json.dumps(rows) + "\n")
            git("add", "-f", ".warp/beam.json", ".warp/agents.json")
            git("commit", "-q", "-m", message)
            pushed = git("push", "-q", "origin", "main")
            self.assertEqual(pushed.returncode, 0, pushed.stderr)

        publish("running")
        vm = self.tmp / "vm"
        cloned = subprocess.run(["git", "clone", "-q", str(origin), str(vm)], capture_output=True, text=True)
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        live = run("agents.py", "reap", "--remote", "--root", str(vm), "--id", "subagent:T-1", "--ticket", "T-1")
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertEqual(live.stdout.strip(), "reap: continue")
        data["runState"] = "stopped"
        rows["agents"][0].update({"state": "stopped", "ended": LATER, "endReason": "stopped", "halted": True})
        publish("stopped")
        before = (vm / ".warp" / "agents.json").read_text()
        gone = run("agents.py", "reap", "--remote", "--root", str(vm), "--id", "subagent:T-1", "--ticket", "T-1")
        self.assertEqual(gone.returncode, 3, gone.stdout + gone.stderr)
        self.assertIn("reap: exit stopped", gone.stdout)
        self.assertEqual((vm / ".warp" / "agents.json").read_text(), before, "the VM's clone is not written")
        self.assertEqual(git("status", "--porcelain", cwd=vm).stdout.strip(), "")

    def test_the_parent_stamps_reaped_when_a_remote_shuttle_returns_stopped(self):
        self.started()
        self.assertNotIn("reaped", self.rows()[0])
        out = run(
            "checkout.py",
            "result",
            "--beam",
            str(self.path),
            "--id",
            "T-1",
            "--root",
            str(self.tmp),
            "--line",
            "result: T-1 stopped settled",
            cwd=self.tmp,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("result: ok T-1", out.stdout)
        row = self.rows()[0]
        self.assertEqual(row["state"], "ended")
        self.assertEqual(row["endReason"], "settled")
        self.assertTrue(row["reaped"])
        journal = (self.warp / "journal.jsonl").read_text()
        self.assertEqual(journal.count('"type":"reap"'), 1)
        self.assertIn('"reason":"settled"', journal)
        again = run(
            "checkout.py",
            "result",
            "--beam",
            str(self.path),
            "--id",
            "T-1",
            "--root",
            str(self.tmp),
            "--line",
            "result: T-1 stopped settled",
            cwd=self.tmp,
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.rows()[0]["reaped"], row["reaped"])
        self.assertEqual((self.warp / "journal.jsonl").read_text().count('"type":"reap"'), 1)

    def test_a_halted_row_keeps_its_end_reason_when_the_parent_stamps_reaped(self):
        data = self.started()
        self.halt(data, "paused")
        row = self.rows()[0]
        self.assertEqual(row["state"], "stopped")
        self.assertEqual(row["endReason"], "paused")
        self.assertNotIn("reaped", row)
        out = run(
            "checkout.py",
            "result",
            "--beam",
            str(self.path),
            "--id",
            "T-1",
            "--root",
            str(self.tmp),
            "--line",
            "result: T-1 stopped paused",
            cwd=self.tmp,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        stamped = self.rows()[0]
        self.assertEqual(stamped["state"], "stopped")
        self.assertEqual(stamped["endReason"], "paused")
        self.assertTrue(stamped["reaped"])

    def test_an_ordinary_result_does_not_stamp_reaped(self):
        self.started()
        out = run(
            "checkout.py",
            "result",
            "--beam",
            str(self.path),
            "--id",
            "T-1",
            "--root",
            str(self.tmp),
            "--line",
            "result: T-1 ok",
            cwd=self.tmp,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = self.rows()[0]
        self.assertNotIn("reaped", row)
        self.assertEqual(row["state"], "starting")


class ClearTests(Base):
    """/warp-cleanup is the clear you run yourself."""

    def clear(self, items, state="paused", rows=None, **kwargs):
        data = beam_of(ticket())
        data["runState"] = state
        data["paused"] = state == "paused"
        self.save(data)
        (self.warp / "agents.json").write_text(json.dumps({"agents": rows or [], "spawns": []}) + "\n")
        calls, transport = transport_for(items, **{k: kwargs.pop(k) for k in ("cancel_status", "archive_status") if k in kwargs})
        options = {"cloud": True, "apply": True, "key": "k", "now": LATER, "origin": ORIGIN, "self_id": ""}
        options.update(kwargs)
        lines = agents.cleanup(data, beam_path=self.path, transport=transport, **options)
        return lines, posts(calls)

    def test_running_agents_stay_unless_asked(self):
        items = [cloud_item("bc-run", "[warp] T-1 implement", status="RUNNING"), cloud_item("bc-idle", "[warp] T-2 fix")]
        lines, posted = self.clear(items)
        self.assertEqual(posted, ["/bc-idle/archive"])
        self.assertIn("cleanup: skip bc-run reason=running", lines)

    def test_running_cancels_the_run_then_archives(self):
        items = [cloud_item("bc-run", "[warp] T-1 implement", status="RUNNING", run="run-7"), cloud_item("bc-idle", "[warp] T-2 fix")]
        lines, posted = self.clear(items, running=True)
        self.assertEqual(posted, ["/bc-run/runs/run-7/cancel", "/bc-run/archive", "/bc-idle/archive"])
        self.assertIn("cleanup: cancelled bc-run", lines)
        self.assertIn("cleanup: archived 2", lines)

    def test_running_is_refused_while_the_run_is_running(self):
        items = [cloud_item("bc-run", "[warp] T-1 implement", status="RUNNING"), cloud_item("bc-idle", "[warp] T-2 fix")]
        lines, posted = self.clear(items, state="running", running=True)
        self.assertEqual(posted, ["/bc-idle/archive"])
        self.assertIn("cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.", lines)
        forced, forced_posted = self.clear(items, state="running", running=True, force=True)
        self.assertIn("/bc-run/runs/run-1/cancel", forced_posted)

    def test_a_dry_run_says_what_it_would_cancel(self):
        items = [cloud_item("bc-run", "[warp] T-1 implement", status="RUNNING")]
        lines, posted = self.clear(items, running=True, apply=False)
        self.assertEqual(posted, [])
        self.assertIn("cleanup: would cancel bc-run", lines)
        self.assertIn("cleanup: count 1", lines)
        self.assertIn("cleanup: dry-run", lines)

    def test_a_failed_cancel_leaves_the_agent_and_a_finished_run_is_fine(self):
        items = [cloud_item("bc-run", "[warp] T-1 implement", status="RUNNING")]
        lines, posted = self.clear(items, running=True, cancel_status=500)
        self.assertEqual(posted, ["/bc-run/runs/run-1/cancel"])
        self.assertTrue(any(line.startswith("cleanup: cancel failed bc-run") for line in lines))
        self.assertIn("cleanup: archived 0", lines)
        # 409: the run ended between the list and the cancel.
        lines, posted = self.clear(items, running=True, cancel_status=409)
        self.assertEqual(posted, ["/bc-run/runs/run-1/cancel", "/bc-run/archive"])

    def test_the_parent_of_this_run_is_never_touched(self):
        rows = [
            {"id": "bc-old-parent", "ticket": "", "role": "parent", "state": "ended", "ended": NOW, "endReason": "replaced", "superseded": True},
            {"id": "bc-parent", "ticket": "", "role": "parent", "state": "stopped", "ended": NOW, "endReason": "paused"},
        ]
        items = [cloud_item("bc-parent", status="RUNNING"), cloud_item("bc-old-parent")]
        lines, posted = self.clear(items, rows=rows, running=True, all_idle=True)
        self.assertEqual(posted, ["/bc-old-parent/archive"])
        self.assertIn("cleanup: skip bc-parent reason=parent", lines)

    def test_the_cleanup_command_takes_the_new_flags(self):
        data = beam_of(ticket())
        self.save(data)
        done = run("agents.py", "cleanup", "--beam", str(self.path), "--cloud", "--apply", "--running", "--force")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("cleanup: no CURSOR_API_KEY", done.stdout)
        helped = run("agents.py", "?")
        for word in ("--running", "reap", "not-launched", "runs/{runId}/cancel"):
            self.assertIn(word, helped.stdout)
        sub = run("agents.py", "cleanup", "--help")
        for flag in ("--cloud", "--apply", "--running", "--force", "--all-idle", "--any-repo"):
            self.assertIn(flag, sub.stdout)


class RegistryFileTests(Base):
    """`.warp/agents.json` is the registry. The copy on the beam never overwrites it."""

    def test_a_write_from_another_process_is_not_lost(self):
        data = beam_of(ticket(), ticket("T-2"))
        self.save(data)
        self.launch(data)
        # Another process (a Shuttle's reap check, a second window) writes the file.
        other = json.loads((self.warp / "agents.json").read_text())
        other["agents"][0]["cloudId"] = "bc-bound"
        other["agents"].append({"id": "elsewhere", "ticket": "T-9", "role": "shuttle", "state": "running", "started": NOW, "ended": None})
        (self.warp / "agents.json").write_text(json.dumps(other, indent=2) + "\n\n")
        # This process still holds the copy it read before, and writes again.
        self.launch(data, "T-2")
        ids = [row["id"] for row in self.registry()["agents"]]
        self.assertEqual(ids, ["subagent:T-1", "elsewhere", "subagent:T-2"])
        self.assertEqual(self.registry()["agents"][0]["cloudId"], "bc-bound")

    def test_a_beam_loaded_from_disk_does_not_bring_back_old_rows(self):
        data = beam_of(ticket())
        self.save(data)
        self.launch(data)
        stale = self.load()
        self.assertIn("agentRegistry", stale, "the beam still carries a copy")
        agents.note_returned(data, data["tickets"]["T-1"], now=LATER, beam_path=self.path)
        doc = agents.registry(stale, self.path)
        self.assertEqual(doc["agents"][0]["endReason"], "returned")


class CloudIdTests(Base):
    """A Shuttle on its own VM reports its cloud agent id through its ticket folder."""

    def test_the_folder_binds_the_id_and_a_new_vm_retires_the_old_one(self):
        data = beam_of(ticket(agent="subagent:T-1", status="coding"))
        self.save(data)
        self.launch(data)
        row = data["tickets"]["T-1"]

        def observe(cloud):
            state = {"id": "T-1", "state": "coding", "agent": "subagent:T-1", "cloudAgent": cloud, "startedAt": NOW, "updatedAt": NOW}
            ticket_state.apply_directory(self.path, data, row, state, [], NOW, set(), [])

        observe("bc-vm-1")
        self.assertEqual(self.rows()[0]["cloudId"], "bc-vm-1")
        self.assertEqual(agents.retire(data, beam_path=self.path, key="k"), [])
        observe("bc-vm-2")
        saved = self.rows()[0]
        self.assertEqual(saved["cloudId"], "bc-vm-2")
        self.assertEqual(saved["retired"], ["bc-vm-1"])
        calls, transport = transport_for([cloud_item("bc-vm-1", status="RUNNING", run="run-3")])
        lines = agents.retire(data, beam_path=self.path, now=LATER, key="k", transport=transport)
        self.assertEqual(posts(calls), ["/bc-vm-1/runs/run-3/cancel", "/bc-vm-1/archive"])
        self.assertEqual(lines, ["cleanup: cancelled bc-vm-1", "cleanup: archived bc-vm-1"])
        saved = self.rows()[0]
        self.assertEqual(saved["retired"], [])
        self.assertEqual(saved["archivedIds"], ["bc-vm-1"])
        self.assertEqual(saved["state"], "starting", "the live Shuttle is untouched")
        self.assertNotIn("archived", saved)
        self.assertEqual(agents.retire(data, beam_path=self.path, key="k", transport=transport), [])

    def test_without_a_key_the_left_behind_vm_is_linked_once(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        self.save(data)
        self.launch(data)
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-1")
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-2")
        self.assertEqual(agents.retire(data, beam_path=self.path, key=""), ["cleanup: link https://cursor.com/agents/bc-vm-1"])
        self.assertEqual(agents.retire(data, beam_path=self.path, key=""), [])

    def test_a_worktree_shuttle_never_takes_the_parents_id(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        data["parentSession"] = "session-a"
        self.save(data)
        agents.note_parent(data, now=NOW, beam_path=self.path, cloud_id="bc-parent")
        self.launch(data)
        self.assertFalse(agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-parent"))
        self.assertNotIn("cloudId", self.rows()[0])
        self.assertFalse(agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="not-a-cloud-id"))

    def test_merge_archives_every_vm_the_ticket_used(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        self.save(data)
        self.launch(data)
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-1")
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-2")
        lines = agents.release_ticket(data, data["tickets"]["T-1"], "merged", now=LATER, beam_path=self.path)
        self.assertEqual(
            sorted(lines),
            ["cleanup: link https://cursor.com/agents/bc-vm-1", "cleanup: link https://cursor.com/agents/bc-vm-2"],
        )


class OriginTests(unittest.TestCase):
    """A Shuttle on its own VM only sees what is on origin's base branch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.parent = self.tmp / "parent"
        self.parent.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "warp@example.com")
        self.git("config", "user.name", "Warp")
        self.git("remote", "add", "origin", str(self.origin))
        self.warp = self.parent / ".warp"
        self.warp.mkdir()
        self.path = self.warp / "beam.json"
        data = beam_of(
            ticket(status="coding", agent="subagent:T-1", lastSeenAt=NOW, shuttle={"pending": True, "step": "implement"}),
            runner="local",
            jiraTransition=False,
        )
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        agents.launch_lines(data, data["tickets"]["T-1"], "implement", replacing=True, beam_path=self.path, now=NOW)
        self.agent = data["tickets"]["T-1"]["agent"]
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        self.git("add", "-f", ".warp/beam.json", ".warp/agents.json")
        self.git("commit", "-q", "-m", "state")
        pushed = self.git("push", "-q", "-u", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.vm = self.tmp / "vm"
        cloned = subprocess.run(["git", "clone", "-q", str(self.origin), str(self.vm)], capture_output=True, text=True)
        self.assertEqual(cloned.returncode, 0, cloned.stderr)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.parent), *args], capture_output=True, text=True)

    def on_origin(self, name):
        self.git("fetch", "-q", "origin", "main", cwd=self.vm)
        shown = self.git("show", "origin/main:.warp/%s" % name, cwd=self.vm)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return json.loads(shown.stdout)

    def reap(self, agent):
        return run("agents.py", "reap", "--remote", "--root", str(self.vm), "--id", agent, "--ticket", "T-1")

    def test_pause_and_resume_reach_a_vm_through_origin(self):
        live = self.reap(self.agent)
        self.assertEqual(live.stdout.strip(), "reap: continue", live.stdout + live.stderr)

        paused = run("scan.py", "pause", "--beam", str(self.path), "--reason", "hold", cwd=self.parent)
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        self.assertIn("halt: paused agents=1", paused.stdout)
        beam_on_main = self.on_origin("beam.json")
        self.assertEqual(beam_on_main["runState"], "paused")
        row = beam_on_main["tickets"]["T-1"]
        # The sync loads main's beam. The halt is applied to that one too.
        self.assertFalse(row["shuttle"]["pending"])
        self.assertTrue(row["needsReplacement"])
        self.assertTrue(row["haltResume"])
        self.assertEqual(self.on_origin("agents.json")["agents"][-1]["state"], "stopped")
        gone = self.reap(self.agent)
        self.assertEqual(gone.returncode, 3, gone.stdout + gone.stderr)
        self.assertIn("reap: exit paused", gone.stdout)

        resumed = run("scan.py", "resume", "--beam", str(self.path), cwd=self.parent)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("fix: T-1 resume after halt", resumed.stdout)
        self.assertNotIn("worker died", resumed.stdout)
        # Resume pushes before any start line, so a new VM does not read the old pause.
        self.assertEqual(self.on_origin("beam.json")["runState"], "running")
        new_agent = json.loads(self.path.read_text())["tickets"]["T-1"]["agent"]
        self.assertNotEqual(new_agent, self.agent)
        fresh = self.reap(new_agent)
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertEqual(fresh.stdout.strip(), "reap: continue")
        old = self.reap(self.agent)
        self.assertEqual(old.returncode, 3, old.stdout + old.stderr)
        self.assertIn("reap: exit", old.stdout)

    def test_the_remote_check_uses_origins_registry_not_the_clones_file(self):
        # The pushed beam carries the parent's own registry pointer. The VM must not follow it.
        data = json.loads(self.path.read_text())
        self.assertIn("_agentBeam", data)
        doc = json.loads((self.warp / "agents.json").read_text())
        doc["agents"][-1].update({"state": "ended", "ended": LATER, "endReason": "dead"})
        (self.warp / "agents.json").write_text(json.dumps(doc, indent=2) + "\n")
        self.git("add", "-f", ".warp/agents.json")
        self.git("commit", "-q", "-m", "replaced")
        self.assertEqual(self.git("push", "-q", "origin", "main").returncode, 0)
        # The VM's clone still has the registry from when it was cloned: the row is live there.
        stale = json.loads((self.vm / ".warp" / "agents.json").read_text())
        self.assertEqual(stale["agents"][-1]["state"], "starting")
        gone = run("agents.py", "reap", "--remote", "--root", str(self.vm), "--id", self.agent, "--ticket", "T-1", cwd=self.vm)
        self.assertEqual(gone.returncode, 3, gone.stdout + gone.stderr)
        self.assertIn("reap: exit replaced", gone.stdout)


class RunControlTests(Base):
    """Start and resume are one command. Pause and stop are one teardown, and stop also reports."""

    def started(self, verb, *extra):
        out = run("scan.py", verb, "--beam", str(self.path), "--force", *extra)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def test_start_and_resume_do_the_same_thing_from_any_state(self):
        for verb in ("start", "resume"):
            with self.subTest(verb=verb):
                (self.warp / "agents.json").unlink(missing_ok=True)
                data = beam_of(ticket(shuttle={"pending": True, "step": "implement"}))
                data["runState"] = "stopped"
                data["paused"] = True
                self.save(data)
                first = self.started(verb)
                self.assertIn("run: was stopped. Starting.", first)
                self.assertIn("prompt-gate: force", first, "both run the same gate")
                self.assertIn("unfinished: 1 open.", first)
                self.assertIn("parent: session ", first)
                self.assertEqual(self.load()["runState"], "running")
                run("scan.py", "pause", "--beam", str(self.path), "--reason", "lunch")
                again = self.started(verb)
                self.assertIn("run: was paused (lunch). Continuing.", again)
                self.assertIn("fix: T-1 resume after halt", again)
                third = self.started(verb)
                self.assertIn("run: was already running. This session takes it over.", third)
                self.assertEqual(len([row for row in self.rows("parent") if row["state"] == "running"]), 1)

    def test_pause_and_stop_differ_only_in_the_report_and_the_state(self):
        outputs = {}
        for verb, state in (("pause", "paused"), ("stop", "stopped")):
            (self.warp / "agents.json").unlink(missing_ok=True)
            (self.warp / "warp-complete.html").unlink(missing_ok=True)
            data = beam_of(ticket(shuttle={"pending": True, "step": "implement"}))
            self.save(data)
            self.launch(data)
            out = run("scan.py", verb, "--beam", str(self.path), "--reason", "hold")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            saved = self.load()
            outputs[verb] = {
                "state": saved["runState"],
                "halt": "halt: %s agents=1" % state in out.stdout,
                "rows": [row["state"] for row in self.rows()],
                "slot": saved["tickets"]["T-1"]["shuttle"]["pending"],
                "resume": saved["tickets"]["T-1"].get("haltResume"),
                "report": (self.warp / "warp-complete.html").is_file(),
                "stoppedAt": bool(saved.get("stoppedAt")),
            }
        same = ("halt", "rows", "slot", "resume")
        self.assertEqual({k: outputs["pause"][k] for k in same}, {k: outputs["stop"][k] for k in same})
        self.assertEqual(outputs["pause"]["rows"], ["stopped"])
        self.assertEqual((outputs["pause"]["state"], outputs["stop"]["state"]), ("paused", "stopped"))
        # The one thing stop does that pause does not: the completion report.
        self.assertFalse(outputs["pause"]["report"])
        self.assertTrue(outputs["stop"]["report"])
        self.assertFalse(outputs["pause"]["stoppedAt"])
        self.assertTrue(outputs["stop"]["stoppedAt"])

    def test_retired_config_keys_are_named_and_ignored(self):
        (self.warp / "config.yaml").write_text("maxAgents: 6\nmaxLocalSubagents: 2\nmaxFixWorkers: 1\nlistenerRestartNote: 3\n")
        notes = orchestrator.retired_notes(self.tmp)
        self.assertEqual(len(notes), 3)
        self.assertTrue(notes[0].startswith("config: listenerRestartNote: 3 is retired and ignored."))
        self.assertTrue(any("maxLocalSubagents: 2 is retired and ignored. MaxAgents is the one cap" in line for line in notes))
        data = beam_of(ticket(), maxAgents=6)
        self.save(data)
        out = self.started("start")
        self.assertIn("config: maxFixWorkers: 1 is retired and ignored.", out)
        self.assertIn("maxAgents=6 maxInProgress=20", out)
        (self.warp / "config.yaml").write_text("maxAgents: 6\n")
        self.assertEqual(orchestrator.retired_notes(self.tmp), [])
        self.assertEqual(beam.fix_worker_room(data), beam.shuttle_room(data))


class ListTests(Base):
    """/warp-list: what is still out, for this run or for another tag. It changes nothing."""

    ITEMS = [
        cloud_item("bc-run", "[warp:a1b2c3] T-1 implement", status="ACTIVE"),
        cloud_item("bc-idle", "[warp:a1b2c3] T-2 fix"),
        cloud_item("bc-parent", "start warp", status="ACTIVE"),
        cloud_item("bc-other", "[warp:ffffff] T-1 implement", status="ACTIVE"),
        cloud_item("bc-old", "Shuttle T-9"),
        cloud_item("bc-old-run", "Shuttle T-8", status="ACTIVE"),
        cloud_item("bc-theirs", "Fix flaky CI", status="ACTIVE"),
        cloud_item("bc-notes", "garden notes"),
    ]

    def setUp(self):
        super().setUp()
        data = beam_of(ticket())
        data["instance"] = {"id": "a1b2c3", "host": "laptop", "machine": "local-laptop-1"}
        data["runState"] = "paused"
        data["paused"] = True
        self.save(data)
        rows = [
            {"id": "bc-parent", "ticket": "", "role": "parent", "state": "stopped", "ended": NOW},
            {"id": "subagent:T-1", "cloudId": "bc-run", "ticket": "T-1", "role": "shuttle", "state": "stopped", "ended": NOW},
        ]
        (self.warp / "agents.json").write_text(json.dumps({"agents": rows, "spawns": []}) + "\n")
        self.data = data

    def listed(self, **kwargs):
        calls, transport = transport_for(self.ITEMS)
        lines = agents.out_lines(self.data, beam_path=self.path, key="k", transport=transport, origin=ORIGIN, self_id="", **kwargs)
        self.assertEqual(posts(calls), [], "a listing never cancels or archives")
        ids = [line.split()[1][3:] for line in lines if line.startswith("cloud: id=")]
        return lines, ids

    def test_this_runs_agents_with_their_status(self):
        lines, ids = self.listed()
        self.assertEqual(lines[0], "instance: warp:a1b2c3 host=laptop machine=local-laptop-1")
        self.assertIn("registry: live=0 ended=2", lines)
        self.assertEqual(ids, ["bc-run", "bc-idle", "bc-parent"])
        self.assertTrue(any("id=bc-run" in line and "status=ACTIVE" in line for line in lines))
        self.assertIn("list: kept bc-parent reason=parent", lines)
        self.assertIn("out: tag [warp:a1b2c3] running=1 idle=1 kept=1", lines)
        self.assertIn("out: /warp-cleanup cancels the running ones and archives all 2.", lines)

    def test_another_tag_any_tag_and_untagged(self):
        lines, ids = self.listed(tag_override="warp:ffffff")
        self.assertEqual(ids, ["bc-other"])
        self.assertIn("list: tag [warp:ffffff] is not this run. This run's registry is left out.", lines)
        self.assertIn("out: tag [warp:ffffff] running=1 idle=0 kept=0", lines)
        self.assertFalse(any(line.startswith("agent: ") for line in lines))
        for spelled in ("all", "*"):
            lines, ids = self.listed(tag_override=spelled)
            self.assertEqual(ids, ["bc-run", "bc-idle", "bc-parent", "bc-other"], spelled)
            self.assertIn("out: tag any Warp run running=2 idle=1 kept=1", lines)
        lines, ids = self.listed(untagged=True)
        self.assertEqual(ids, ["bc-run", "bc-idle", "bc-parent", "bc-old", "bc-old-run", "bc-theirs"])
        # An agent that only matched on words and is still working may be someone else's.
        self.assertIn("list: kept bc-old-run reason=running-untagged", lines)
        self.assertIn("list: kept bc-theirs reason=running-untagged", lines)
        self.assertIn("out: tag [warp:a1b2c3] and untagged Warp roles running=1 idle=2 kept=3", lines)
        self.assertEqual(agents.parse_tag("[warp:A1B2C3]"), ("a1b2c3", False))
        self.assertEqual(agents.parse_tag("a1b2c3"), ("a1b2c3", False))
        self.assertEqual(agents.parse_tag("*"), ("", True))
        self.assertEqual(agents.parse_tag(""), ("", False))

    def test_without_a_key_it_lists_the_registry_and_says_so(self):
        lines = agents.out_lines(self.data, beam_path=self.path, key="")
        self.assertIn("agent: subagent:T-1 role=shuttle ticket=T-1 state=stopped started= ended=%s" % NOW, lines)
        self.assertIn("cloud: not read. Set CURSOR_API_KEY in the environment to list cloud agents.", lines)
        self.assertIn("cloud: link https://cursor.com/agents/bc-run", lines)
        self.assertNotIn("cloud: link https://cursor.com/agents/bc-parent", lines)

    def test_cleanup_takes_the_same_selection(self):
        def cleaned(**kwargs):
            calls, transport = transport_for(self.ITEMS)
            options = {"cloud": True, "apply": True, "running": True, "key": "k", "now": LATER, "origin": ORIGIN, "self_id": ""}
            options.update(kwargs)
            lines = agents.cleanup(self.data, beam_path=self.path, transport=transport, **options)
            return lines, posts(calls)

        lines, posted = cleaned()
        self.assertEqual(posted, ["/bc-run/runs/run-1/cancel", "/bc-run/archive", "/bc-idle/archive"])
        lines, posted = cleaned(tag_override="ffffff")
        self.assertEqual(posted, ["/bc-other/runs/run-1/cancel", "/bc-other/archive"])
        self.assertIn("cleanup: tag [warp:ffffff] is not this run. Nothing checks whether that run is still going.", lines)
        lines, posted = cleaned(untagged=True)
        self.assertIn("/bc-old/archive", posted)
        self.assertNotIn("/bc-theirs/archive", posted)
        self.assertNotIn("/bc-old-run/archive", posted)
        self.assertIn("cleanup: skip bc-theirs reason=running-untagged", lines)
        lines, posted = cleaned(untagged=True, force=True)
        self.assertIn("/bc-theirs/runs/run-1/cancel", posted, "--force cancels a running agent that only matched loosely")

    def test_the_list_command(self):
        out = run("agents.py", "list", "--beam", str(self.path), "--tag", "all", "--untagged")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("instance: warp:a1b2c3", out.stdout)
        self.assertIn("cloud: not read.", out.stdout)
        helped = run("agents.py", "list", "--help")
        for flag in ("--tag", "--untagged", "--all-idle", "--any-repo"):
            self.assertIn(flag, helped.stdout)
        cleaned = run("agents.py", "cleanup", "--help")
        for flag in ("--tag", "--untagged", "--running", "--force", "--apply", "--cloud"):
            self.assertIn(flag, cleaned.stdout)
        self.assertNotIn("--all-instances", cleaned.stdout)

    def test_question_mark_prints_the_full_help_and_does_nothing(self):
        rows = ("--tag <instance>", "--tag all", "--untagged", "--all-idle", "--any-repo", "reason=parent", "CURSOR_API_KEY")
        for forms in (["?"], ["help"], ["--beam", str(self.path), "?"]):
            listed = run("agents.py", "list", *forms)
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            self.assertIn("/warp-list: what this Warp run still has out", listed.stdout)
            self.assertIn("out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>", listed.stdout)
            for row in rows:
                self.assertIn(row, listed.stdout, row)
        before = (self.warp / "agents.json").read_text()
        # The acting command line with ? on the end is help, and nothing else.
        for forms in (["?"], ["--beam", str(self.path), "--cloud", "--apply", "--running", "?"], ["--cloud", "--apply", "help"]):
            cleaned = run("agents.py", "cleanup", *forms, env={"CURSOR_API_KEY": "k"})
            self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
            self.assertIn("/warp-cleanup: cancel and archive what this Warp run still has out.", cleaned.stdout)
            self.assertTrue(cleaned.stdout.startswith("usage: agents.py cleanup"), cleaned.stdout[:80])
            # A real run prints the tag it matched and reads the registry. Help does neither.
            self.assertNotIn("cleanup: tag ", cleaned.stdout)
            self.assertNotIn("agent: ", cleaned.stdout)
            for row in rows + ("--apply", "--running", "--force", "reason=running-untagged", "/warp-cleanup --untagged"):
                self.assertIn(row, cleaned.stdout, row)
        self.assertEqual((self.warp / "agents.json").read_text(), before)
        for name in ("warp-list.md", "warp-cleanup.md"):
            text = (ROOT / "commands" / name).read_text()
            stem = name[:-3]
            self.assertIn("`/%s ?` prints the help" % stem, text)
            self.assertIn("do nothing else", text)
            for row in ("`--tag <instance>`", "`--tag all`", "`--untagged`", "`--all-idle`", "`--any-repo`"):
                self.assertIn(row, text, "%s: %s" % (name, row))


class InstanceTests(Base):
    """`warp:<instance>` names one Warp run. Its agents carry it, and every message shows it."""

    def test_a_cloud_parent_gives_the_last_six_of_its_agent_id(self):
        with mock.patch.dict(os.environ, {"CURSOR_AGENT_ID": "bc-123e4567-e89b-12d3-a456-4266141A2B3C"}):
            minted = agents.mint_instance(self.path, now=NOW)
        self.assertEqual(minted["id"], "1a2b3c")
        self.assertEqual(minted["machine"], "bc-123e4567-e89b-12d3-a456-4266141A2B3C")
        self.assertEqual(minted["createdAt"], NOW)

    def test_one_laptop_gives_each_checkout_its_own_instance(self):
        cloud = agents.current_agent_id()
        first = agents.mint_instance(self.tmp / "a" / ".warp" / "beam.json")
        second = agents.mint_instance(self.tmp / "b" / ".warp" / "beam.json")
        again = agents.mint_instance(self.tmp / "a" / ".warp" / "beam.json")
        # On a cloud VM the machine id is this process's agent id, for every checkout.
        if cloud:
            for minted in (first, second, again):
                self.assertEqual(minted["machine"], cloud)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["id"], again["id"])
            return
        for minted in (first, second):
            self.assertRegex(minted["id"], r"^[0-9a-f]{6}$")
            self.assertTrue(minted["machine"].startswith("local-"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["id"], again["id"])

    def test_the_instance_is_set_once_and_survives_a_new_parent(self):
        data = beam_of(ticket())
        self.save(data)
        with mock.patch.dict(os.environ, {"CURSOR_AGENT_ID": "bc-aaaaaaaa-0000-0000-0000-000000aaa111"}):
            self.assertEqual(agents.ensure_instance(data, self.path), "aaa111")
        self.save(data)
        # /warp-resume from another cloud agent keeps the run's instance, so it still finds the agents.
        with mock.patch.dict(os.environ, {"CURSOR_AGENT_ID": "bc-bbbbbbbb-0000-0000-0000-000000bbb222"}):
            self.assertEqual(agents.ensure_instance(self.load(), self.path), "aaa111")
        self.assertEqual(agents.instance_id({"instance": "AAA111"}), "aaa111")
        self.assertEqual(agents.instance_id({}), "")
        self.assertEqual(agents.instance_line({}), "instance: none yet. /warp-start sets it.")

    def test_start_status_version_list_and_messages_show_it(self):
        import herald_fmt
        import status_post
        import version

        data = beam_of(ticket())
        self.save(data)
        self.assertNotIn("warp:", herald_fmt.header(self.tmp))
        started = run("scan.py", "start", "--beam", str(self.path), "--force")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        saved = self.load()["instance"]
        tag = "warp:%s" % saved["id"]
        line = "instance: %s host=%s machine=%s" % (tag, saved["host"], saved["machine"])
        self.assertIn(line, started.stdout)
        resumed = run("scan.py", "resume", "--beam", str(self.path))
        self.assertIn(line, resumed.stdout, "resume keeps the same instance")
        status = run("scan.py", "status", "--beam", str(self.path))
        self.assertIn(line, status.stdout)
        self.assertIn("Instance %s host=" % tag, (self.warp / "STATUS.md").read_text())
        self.assertEqual(json.loads((self.warp / "status.json").read_text())["instance"]["tag"], tag)
        listed = run("agents.py", "list", "--beam", str(self.path))
        self.assertEqual(listed.stdout.splitlines()[0], line)
        self.assertIn(line, version.describe(self.tmp))
        self.assertIn("this machine: ", version.describe(self.tmp))
        # Every Slack and Teams message opens with the header.
        self.assertTrue(herald_fmt.header(self.tmp).endswith(" | %s" % tag), herald_fmt.header(self.tmp))
        message = herald_fmt.message(self.tmp, "Claim T-1")
        self.assertIn(tag, message["text"].splitlines()[0])
        self.assertIn(tag, message["slack"]["blocks"][0]["text"]["text"])
        self.assertIn(tag, message["teams"]["markdown"])
        with mock.patch("builtins.print"):
            posted = status_post.payload(self.path)
        self.assertIn("Instance: %s on %s" % (tag, saved["host"]), posted["text"])

    def test_the_listener_prompt_carries_the_instance(self):
        data = beam_of(ticket())
        data["instance"] = {"id": "a1b2c3", "host": "laptop", "machine": "local-laptop-1"}
        self.save(data)
        lines = orchestrator.listener_prompt(self.path)
        self.assertIn("named `[warp:a1b2c3] listener`", lines[0])
        self.assertEqual(lines[1], "[warp:a1b2c3] listener")


class RaceTests(Base):
    """A pause from another process wins over work that was already under way."""

    def test_a_pause_during_a_pass_is_not_written_over(self):
        data = beam_of(ticket(status="claimed", agent="subagent:T-1"))
        self.save(data)
        path = self.path

        def pass_then_pause(live, provider=None, beam_path=None, now=None):
            # The pass issues a start, and meanwhile /warp-pause lands from another process.
            lines = agents.launch_lines(live, live["tickets"]["T-1"], "implement", beam_path=beam_path, now=NOW)
            scan.set_run(path, "paused", "hold")
            return lines

        with mock.patch.object(pipeline, "advance", pass_then_pause), mock.patch("builtins.print"):
            lines = orchestrator.supervise(self.path, provider={}, now=NOW)
        self.assertTrue(lines[0].startswith("pass: aborted. The run was paused"), lines)
        self.assertFalse(any(line.startswith("start ") for line in lines), lines)
        saved = self.load()
        self.assertEqual(saved["runState"], "paused")
        self.assertTrue(saved["paused"])
        self.assertEqual([row["state"] for row in self.rows()], ["stopped"], "the row this pass recorded is ended too")
        self.assertEqual(orchestrator.parent_exit(self.path)[0], "parent: exit")

    def test_a_pause_during_a_launch_prints_no_prompt(self):
        data = beam_of(ticket(status="claimed", agent="subagent:T-1", jiraKey="WAR-1"), runner="cloud", subagentVm=True)
        self.save(data)
        import checkout

        live = self.load()
        agents.note_checkout(live, live["tickets"]["T-1"], beam_path=self.path)
        with mock.patch("builtins.print"):
            scan.set_run(self.path, "paused", "hold")
        shown = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: shown.append(" ".join(str(x) for x in a))):
            saved = checkout._save_launch(self.path, live, "T-1")
        self.assertFalse(saved)
        self.assertIn("spawn: closed T-1", shown)
        self.assertEqual(self.load()["runState"], "paused")
        self.assertEqual([row["state"] for row in self.rows()], ["stopped"])

    def test_resume_from_the_channel_while_running_keeps_the_parent(self):
        data = beam_of(ticket())
        data["parentSession"] = "session-a"
        self.save(data)
        agents.note_parent(data, now=NOW, beam_path=self.path, cloud_id="")
        self.save(data)
        for text in ("warp:resume", "warp:start"):
            result = inbound.apply_command(self.path, text)
            self.assertIn("already running", result["detail"], text)
        self.assertEqual(self.load()["parentSession"], "session-a")
        self.assertEqual(len(self.rows("parent")), 1)
        self.assertEqual(orchestrator.parent_exit(self.path, session="session-a"), ["parent: stay"])

    def test_a_failed_archive_call_does_not_cost_the_pass(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        self.save(data)
        self.launch(data)
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-1")
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-2")
        self.save(data)
        os.environ["CURSOR_API_KEY"] = "k"
        try:
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
                lines = orchestrator.supervise(self.path, provider={}, now=LATER)
        finally:
            os.environ.pop("CURSOR_API_KEY", None)
        self.assertIn("cleanup: unreachable bc-vm-1 https://cursor.com/agents/bc-vm-1", lines)
        self.assertEqual(self.load()["pass"]["count"], 1, "the pass was saved")
        self.assertIn("bc-vm-1", self.rows()[0]["retired"], "it is tried again on the next pass")


class VmIdentityTests(Base):
    """Two VMs can hold the same agent id. Only the one the row points at is live."""

    def bound(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        self.save(data)
        self.launch(data)
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-old")
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-new")
        return data

    def as_vm(self, ident):
        return mock.patch.dict(os.environ, {"CURSOR_AGENT_ID": ident})

    def test_the_vm_the_ticket_left_is_told_it_was_replaced(self):
        data = self.bound()
        with self.as_vm("bc-old"):
            self.assertEqual(agents.reap_reason(data, "subagent:T-1", "T-1", beam_path=self.path), "replaced")
        with self.as_vm("bc-new"):
            self.assertEqual(agents.reap_reason(data, "subagent:T-1", "T-1", beam_path=self.path), "")

    def test_a_retired_vm_never_becomes_the_live_one_again(self):
        data = self.bound()
        self.assertFalse(agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-old"))
        row = self.rows()[0]
        self.assertEqual(row["cloudId"], "bc-new")
        self.assertEqual(row["retired"], ["bc-old"])
        calls, transport = transport_for([cloud_item("bc-old")])
        agents.retire(data, beam_path=self.path, now=LATER, key="k", transport=transport)
        self.assertEqual(posts(calls), ["/bc-old/archive"], "an idle agent is archived without a cancel")
        self.assertFalse(agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-old"), "nor after it was archived")
        self.assertEqual(self.rows()[0]["cloudId"], "bc-new")

    def test_replacing_a_dead_shuttle_retires_its_vm(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        self.save(data)
        self.launch(data)
        agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-vm-1")
        agents.launch_lines(data, data["tickets"]["T-1"], "restart", replacing=True, beam_path=self.path, now=LATER)
        old = next(row for row in self.rows() if row["id"] == "subagent:T-1")
        self.assertEqual(old["endReason"], "dead")
        self.assertEqual(old["retired"], ["bc-vm-1"])
        calls, transport = transport_for([cloud_item("bc-vm-1", status="RUNNING", run="run-2")])
        lines = agents.retire(data, beam_path=self.path, now=LATER, key="k", transport=transport)
        self.assertEqual(posts(calls), ["/bc-vm-1/runs/run-2/cancel", "/bc-vm-1/archive"])
        self.assertEqual(lines, ["cleanup: cancelled bc-vm-1", "cleanup: archived bc-vm-1"])
        with self.as_vm("bc-vm-1"):
            self.assertEqual(agents.reap_reason(data, "subagent:T-1", "T-1", beam_path=self.path), "replaced")

    def test_the_parents_own_vm_is_never_bound_or_archived(self):
        data = beam_of(ticket(agent="subagent:T-1"))
        data["parentSession"] = "session-a"
        self.save(data)
        # The parent's cloud id was not known when the run started.
        agents.note_parent(data, now=NOW, beam_path=self.path, cloud_id="")
        self.launch(data)
        with self.as_vm("bc-parent"):
            self.assertFalse(agents.bind_cloud(data, "subagent:T-1", "T-1", self.path, cloud="bc-parent"))
            self.assertNotIn("cloudId", self.rows()[0])
            # Even a row that somehow carries it is not archived on merge.
            doc = agents.registry(data, self.path)
            doc["agents"][-1]["cloudId"] = "bc-parent"
            agents.flush(data, self.path)
            lines = agents.release_ticket(data, data["tickets"]["T-1"], "merged", now=LATER, beam_path=self.path)
        self.assertEqual(lines, [])

    def test_only_a_shuttle_that_pushes_reports_a_cloud_id(self):
        with self.as_vm("bc-parent"):
            code = ticket_state.append_status(self.tmp, "T-1", "coding", agent="subagent:T-1")
        self.assertEqual(code, 0)
        state = json.loads((self.warp / "tickets" / "T-1" / "state.json").read_text())
        self.assertNotIn("cloudAgent", state)


class GateTests(Base):
    """`checkout.py launch` is the one place a prompt comes from."""

    def vm_beam(self, **extra):
        data = beam_of(ticket(status="claimed", agent="subagent:T-1", jiraKey="WAR-1", acs=["it works"]), runner="cloud", subagentVm=True)
        data.update(extra)
        self.save(data)
        return data

    def test_no_prompt_while_paused_stopped_or_finished(self):
        for extra, word in (
            ({"runState": "paused", "paused": True}, "paused"),
            ({"runState": "stopped"}, "stopped"),
        ):
            self.vm_beam(**extra)
            out = run("checkout.py", "launch", "--root", str(self.tmp), "--id", "T-1", cwd=self.tmp)
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("spawn: closed T-1", out.stdout)
            self.assertIn("refuse: Warp is %s" % word, out.stdout)
            self.assertNotIn("SUBAGENT T-1", out.stdout)
            self.assertFalse((self.warp / "agents.json").is_file(), "a refused launch records no agent")

    def test_no_prompt_for_a_settled_ticket(self):
        data = self.vm_beam()
        data["tickets"]["T-1"]["status"] = "merged"
        data["tickets"]["T-2"] = ticket("T-2")
        self.save(data)
        out = run("checkout.py", "launch", "--root", str(self.tmp), "--id", "T-1", cwd=self.tmp)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refuse: ticket T-1 is merged", out.stdout)
        self.assertNotIn("SUBAGENT T-1", out.stdout)

    def test_the_parent_instruction_is_above_the_mark_and_the_contract_below_it(self):
        self.vm_beam()
        out = run("checkout.py", "launch", "--root", str(self.tmp), "--id", "T-1", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        above, mark, below = out.stdout.partition(orchestrator.PROMPT_MARK)
        self.assertTrue(mark, out.stdout)
        self.assertIn("for the parent: %s" % orchestrator.VM_INSTRUCTION, above)
        # The tag: the name the parent gives the agent, and the first line of the prompt.
        instance = self.load()["instance"]["id"]
        self.assertRegex(instance, r"^[a-z0-9]{6}$")
        self.assertIn("name: [warp:%s] T-1 implement" % instance, above)
        self.assertEqual(below.strip().splitlines()[0], "[warp:%s] T-1 implement" % instance)
        self.assertIn("SUBAGENT T-1", below)
        again = run("checkout.py", "launch", "--root", str(self.tmp), "--id", "T-1", cwd=self.tmp)
        self.assertIn("name: [warp:%s] T-1 implement" % instance, again.stdout, "the instance is set once and kept")
        self.assertIn("agent: subagent:T-1", below)
        self.assertIn(orchestrator.WORKER_CONTRACT, below)
        self.assertIn("agents.py reap --remote --id subagent:T-1 --ticket T-1", below)
        # The worker is never told to start anything.
        self.assertNotIn("Start one subagent", below)
        self.assertNotIn(orchestrator.VM_INSTRUCTION, below)
        self.assertEqual(self.rows()[0]["id"], "subagent:T-1")

    def test_every_worker_prompt_carries_the_contract(self):
        row = ticket(agent="shuttle-T-1-r2", jiraKey="WAR-1", acs=["it works"])
        prompts = {
            "worktree": orchestrator.subagent_prompt(row, {}, "warp/T-1", "/work/.warp/worktrees/T-1", "/work", instance="a1b2c3"),
            "vm": orchestrator.subagent_vm_prompt(row, {}, "warp/T-1", instance="a1b2c3"),
            "agent": orchestrator.implement_prompt(row, {}, instance="a1b2c3"),
        }
        for name, text in prompts.items():
            with self.subTest(prompt=name):
                self.assertIn("agent: shuttle-T-1-r2", text)
                self.assertIn(orchestrator.WORKER_CONTRACT, text)
                self.assertIn("reap: exit", text)
                self.assertIn("agents.py reap", text)
                self.assertIn("--id shuttle-T-1-r2 --ticket T-1", text)
                self.assertNotIn("Start one subagent", text)
                self.assertTrue(agents.strict_role(text), "a halt finds this prompt by its marker")
                self.assertEqual(text.splitlines()[0], "[warp:a1b2c3] T-1 implement")
                self.assertEqual(agents.tag_instance(text), "a1b2c3")
        self.assertIn("You never start another agent of any kind", orchestrator.WORKER_CONTRACT)
        self.assertIn("set a timer", orchestrator.WORKER_CONTRACT)
        self.assertIn("woken later", orchestrator.WORKER_CONTRACT)
        self.assertIn("The parent does all the waiting", orchestrator.WORKER_CONTRACT)
        local = prompts["worktree"]
        self.assertIn("agents.py reap --beam /work/.warp/beam.json --id shuttle-T-1-r2 --ticket T-1", local)
        self.assertIn("ticket_state.py append --id T-1 --state coding --agent shuttle-T-1-r2 --root /work", local)
        self.assertIn(str(SCRIPTS), local, "a worktree Shuttle gets the absolute path of the scripts")
        self.assertIn("agents.py reap --remote", prompts["vm"])

    def test_launch_markers(self):
        self.assertEqual(agents.launch_marker("SUBAGENT T-1\nticket: T-1"), ("shuttle", "T-1"))
        self.assertEqual(agents.launch_marker("intro\nIMPLEMENT WV-02\n"), ("shuttle", "WV-02"))
        self.assertEqual(agents.launch_marker("LISTEN once\nRead the channel."), ("listener", ""))
        self.assertEqual(agents.launch_marker("please implement T-1"), ("", ""))
        self.assertEqual(agents.launch_marker("explore the SUBAGENT T-1 code path"), ("", ""))
        self.assertFalse(agents.strict_role("Shuttle T-1: fix CI"))


class LocalHookTests(Base):
    """The hook is a local IDE duplicate of the gate. Cloud runners never run it."""

    def payload(self, task, call="call-1", sub="sub-1"):
        return {"task": task, "tool_call_id": call, "subagent_id": sub}

    def test_other_subagents_are_always_allowed(self):
        data = beam_of(ticket())
        data["runState"] = "stopped"
        self.save(data)
        verdict = agents.hook_start(self.payload("explore the repo"), root=self.tmp, now=NOW)
        self.assertEqual(verdict, {"permission": "allow"})

    def test_a_warp_launch_is_denied_while_halted_or_without_a_launch(self):
        data = beam_of(ticket())
        self.save(data)
        none = agents.hook_start(self.payload("SUBAGENT T-1\nticket: T-1"), root=self.tmp, now=NOW)
        self.assertEqual(none["permission"], "deny")
        self.assertIn("checkout.py launch", none["user_message"])
        self.launch(data)
        ok = agents.hook_start(self.payload("SUBAGENT T-1\nticket: T-1"), root=self.tmp, now=NOW)
        self.assertEqual(ok, {"permission": "allow"})
        same = agents.hook_start(self.payload("SUBAGENT T-1\nticket: T-1"), root=self.tmp, now=NOW)
        self.assertEqual(same, {"permission": "allow"}, "the same call asked twice is one launch")
        twin = agents.hook_start(self.payload("SUBAGENT T-1\nticket: T-1", call="call-2", sub="sub-2"), root=self.tmp, now=NOW)
        self.assertEqual(twin["permission"], "deny")
        self.assertIn("already has its agent", twin["user_message"])
        data["runState"] = "paused"
        data["paused"] = True
        self.save(data)
        paused = agents.hook_start(self.payload("SUBAGENT T-1\nticket: T-1", call="call-3", sub="sub-3"), root=self.tmp, now=NOW)
        self.assertEqual(paused["permission"], "deny")
        self.assertIn("Warp is paused", paused["user_message"])
        journal = (self.warp / "journal.jsonl").read_text()
        self.assertEqual(journal.count("spawn-denied"), 3)

    def test_the_listener_starts_only_for_an_issued_poll(self):
        data = beam_of(ticket())
        self.save(data)
        early = agents.hook_start(self.payload("LISTEN once"), root=self.tmp, now=NOW)
        self.assertEqual(early["permission"], "deny")
        (self.warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.assertEqual(orchestrator.supervise_listener(self.path, now=NOW), ["listener: poll"])
        due = agents.hook_start(self.payload("LISTEN once"), root=self.tmp, now=NOW)
        self.assertEqual(due, {"permission": "allow"})
        second = agents.hook_start(self.payload("LISTEN once", call="call-2", sub="sub-2"), root=self.tmp, now=NOW)
        self.assertEqual(second["permission"], "deny")

    def test_the_switch_and_a_missing_beam_allow_everything(self):
        verdict = agents.hook_start(self.payload("SUBAGENT T-1"), root=self.tmp / "nowhere", now=NOW)
        self.assertEqual(verdict, {"permission": "allow"})
        data = beam_of(ticket())
        data["runState"] = "stopped"
        self.save(data)
        os.environ["WARP_SPAWN_GATE"] = "off"
        try:
            self.assertEqual(agents.hook_start(self.payload("SUBAGENT T-1"), root=self.tmp, now=NOW), {"permission": "allow"})
        finally:
            os.environ.pop("WARP_SPAWN_GATE", None)

    def test_the_hook_commands_answer_in_json_and_never_fail(self):
        data = beam_of(ticket())
        data["runState"] = "stopped"
        self.save(data)
        env = {"CURSOR_PROJECT_DIR": str(self.tmp)}
        payload = json.dumps({"task": "SUBAGENT T-1", "workspace_roots": [str(self.tmp)], "tool_call_id": "c", "subagent_id": "s"})
        start = run("agents.py", "hook-start", env=env, cwd=self.tmp, stdin=payload)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(json.loads(start.stdout)["permission"], "deny")
        junk = run("agents.py", "hook-start", env=env, cwd=self.tmp, stdin="not json")
        self.assertEqual(json.loads(junk.stdout), {"permission": "allow"})
        stop = run("agents.py", "hook-stop", env=env, cwd=self.tmp, stdin=json.dumps({"task": "SUBAGENT T-1", "status": "completed", "workspace_roots": [str(self.tmp)]}))
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(json.loads(stop.stdout), {})
        self.assertIn("subagent-end", (self.warp / "journal.jsonl").read_text())

    def test_hook_scripts_are_executable_and_say_cloud_does_not_run_them(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertIn("subagentStart", hooks)
        for name in ("session-start.sh", "on-stop.sh", "subagent-start.sh", "subagent-stop.sh"):
            path = ROOT / "hooks" / name
            self.assertTrue(os.access(path, os.X_OK), "%s is not executable" % name)
            self.assertIn("Cloud runners do not execute this hook", path.read_text())


if __name__ == "__main__":
    unittest.main()
