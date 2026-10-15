"""1.7.1: the parent wakes the moment there is something new to do, and every pass says why Shuttle slots are idle."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import beam as beam_mod  # noqa: E402
import nextwork  # noqa: E402
import orchestrator  # noqa: E402


def row(tid, status="queued", **kw):
    base = {"id": tid, "status": status, "alarm": None, "locks": ["src/%s" % tid], "deps": [], "after": [], "size": "S",
            "summary": tid, "pr": {}, "branch": None}
    base.update(kw)
    return base


def run(tickets, **cfg):
    config = {"maxAgents": 4, "runner": "local"}
    config.update(cfg)
    return {"runState": "running", "paused": False, "config": config, "gates": [], "tickets": {t["id"]: t for t in tickets}}


class KeysTest(unittest.TestCase):
    def test_every_kind_of_immediate_work(self):
        data = run([
            row("R-1", "coding", report={"returned": True}),
            row("L-1", "alarm", alarm="lock-escape", escaped=["x"]),
            row("D-1", "coding", needsReplacement=True),
            row("F-1", "fix"),
            row("M-1", "review", phase="ready"),
            row("Q-1"),
        ])
        got = nextwork.keys(data)
        for key in ("return:R-1", "escape:L-1", "replace:D-1", "fix:F-1", "merge:M-1", "ready:Q-1"):
            self.assertIn(key, got)

    def test_only_what_the_last_pass_did_not_have_wakes_the_wait(self):
        data = run([row("L-1", "alarm", alarm="lock-escape", escaped=["x"])])
        data["pass"] = {"work": nextwork.keys(data)}
        self.assertIsNone(nextwork.new_since_pass(data))  # the pass already knew: no spin
        data["tickets"]["D-1"] = row("D-1", "coding", report={"returned": True})  # a Shuttle exits without a PR
        self.assertEqual(nextwork.new_since_pass(data), "return:D-1")
        paused = dict(data, runState="paused", paused=True)
        self.assertEqual(nextwork.keys(paused), [])


class WaitTest(unittest.TestCase):
    def test_parent_wait_returns_work_without_waiting_out_poll_seconds(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / ".warp" / "beam.json"
        path.parent.mkdir(parents=True)
        data = run([row("Q-1", "coding", report={"returned": True})], pollSeconds=600)
        data["listener"] = {}
        data["pass"] = {"work": [], "startedAt": "2026-10-15T10:00:00Z"}
        path.write_text(json.dumps(data))
        clock = iter(range(1000000, 1001000, 2))
        got = orchestrator.parent_wait(path, sleep=lambda s: None, clock=lambda: next(clock))
        self.assertEqual(got, "work return:Q-1")


class CapacityTest(unittest.TestCase):
    def test_max_in_progress_holding_free_slots_is_named_and_posted_once(self):
        tickets = [row("W-%d" % i, "review", phase="reviewing", pr={"url": "u"}) for i in range(4)]
        tickets += [row("N-1"), row("N-2")]
        data = run(tickets, maxAgents=4, maxInProgress=4)
        info = beam_mod.capacity(data)
        self.assertEqual((info["limiter"], info["startable"], info["free"]), ("maxInProgress", 2, 4))
        self.assertIn("maxInProgress 4/4 holds them (4 of the open tickets wait on review, CI, or approval", info["line"])
        lines = nextwork.capacity_news(data)
        self.assertTrue(lines[1].startswith("herald: New tickets are held: maxInProgress 4/4 is full while 4 of 4 Shuttle slots are free"))
        self.assertEqual(len(nextwork.capacity_news(data)), 1)  # posted once
        data["config"]["maxInProgress"] = "auto"
        again = nextwork.capacity_news(data)
        self.assertIn("herald: New tickets are starting again: maxInProgress is no longer full.", again)
        self.assertIn("2 tickets can start now", again[0])

    def test_nothing_ready_says_what_holds_it(self):
        data = run([row("A-1", "coding", locks=["src/x"]), row("B-1", deps=["A-1"]), row("C-1", locks=["src/x"])])
        data["tickets"]["A-1"]["shuttle"] = {"pending": True}
        info = beam_mod.capacity(data)
        self.assertIn("nothing can start: 1 on dependencies, 1 on locks another ticket holds", info["line"])


def out_shuttle(tid, status="coding", step="implement", started="2026-10-15T10:00:00Z", **kw):
    t = row(tid, status, phase="implementing", agent="subagent:%s" % tid, branch="warp/%s" % tid,
            shuttle={"pending": True, "step": step, "startedAt": started}, lastSeenAt=started, **kw)
    return t


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.parent.mkdir(parents=True)

    def journal(self, *rows):
        with (self.path.parent / "journal.jsonl").open("a") as fh:
            for item in rows:
                fh.write(json.dumps(item) + "\n")

    def test_an_agent_that_ended_without_done_is_handled_in_the_same_pass(self):
        import pipeline
        import reconcile

        data = run([out_shuttle("D-01"), out_shuttle("A-01")])
        self.journal({"ts": "2026-10-15T09:00:00Z", "type": "subagent-end", "role": "shuttle", "ticket": "A-01", "status": "completed"},
                     {"ts": "2026-10-15T10:04:00Z", "type": "subagent-end", "role": "shuttle", "ticket": "D-01", "status": "completed"})
        got = reconcile.run(data, self.path, now="2026-10-15T10:05:00Z")
        self.assertEqual(got["returned"], ["D-01"])  # A-01's end is from before this Shuttle started
        self.assertTrue(any(l.startswith("reconcile: D-01 Shuttle ended without writing done (step=implement): its Agent ended (completed)")
                            for l in got["lines"]), got["lines"])
        self.assertTrue(data["tickets"]["D-01"]["shuttle"]["returned"])
        self.assertNotIn("returned", data["tickets"]["A-01"]["shuttle"])
        # The pass handles it: no pull request, so it goes back to work at once (not 15 minutes later).
        lines = pipeline.advance(data, now="2026-10-15T10:05:00Z")
        self.assertIn("fix: D-01 no pull request", lines)
        self.assertTrue(any(l.startswith(("start D-01", "resume D-01")) and "step=fix" in l for l in lines), lines)
        live = run([out_shuttle("D-01")])
        reconcile.run(live, self.path, now="2026-10-15T10:05:00Z")
        live["_live"] = True  # supervise's own pass opens the pull request for the pushed branch instead
        self.assertIn("pr: open D-01 warp/D-01", pipeline.advance(live, now="2026-10-15T10:05:00Z"))
        self.assertEqual(reconcile.run(data, self.path, now="2026-10-15T10:05:30Z")["returned"], [])

    def test_a_failed_end_restarts_the_step(self):
        import pipeline
        import reconcile

        data = run([out_shuttle("E-01")], maxRecoveries=3)
        self.journal({"ts": "2026-10-15T10:03:00Z", "type": "subagent-end", "role": "shuttle", "ticket": "E-01", "status": "error"})
        got = reconcile.run(data, self.path, now="2026-10-15T10:04:00Z")
        self.assertIn("as a failed step", got["lines"][0])
        lines = pipeline.advance(data, now="2026-10-15T10:04:00Z")
        t = data["tickets"]["E-01"]
        self.assertEqual(t["status"], "recovering", lines)
        self.assertNotIn("failed", t["shuttle"])

    def test_the_registry_shows_an_end(self):
        import reconcile

        data = run([out_shuttle("R-01")])
        (self.path.parent / "agents.json").write_text(json.dumps({"agents": [
            {"id": "subagent:R-01", "ticket": "R-01", "role": "shuttle", "started": "2026-10-15T10:00:00Z",
             "state": "ended", "ended": "2026-10-15T10:02:00Z", "endReason": "returned"}], "spawns": []}))
        got = reconcile.run(data, self.path, now="2026-10-15T10:03:00Z")
        self.assertEqual(got["returned"], ["R-01"], got["lines"])

    def test_settled_slots_locks_and_an_idle_repair_backoff(self):
        import reconcile

        merged = out_shuttle("M-01", status="merged")
        escape = row("L-01", "alarm", alarm="lock-escape", escaped=["x"],
                     alarmRepair={"state": "failed", "failedAt": "2026-10-15T10:00:00Z", "attempts": 1})
        data = run([merged, escape])
        got = reconcile.run(data, self.path, now="2026-10-15T10:01:00Z")
        text = "\n".join(got["lines"])
        self.assertIn("reconcile: M-01 is merged but still had a Shuttle marked out; freed its slot", text)
        self.assertIn("reconcile: M-01 is merged but still held 1 lock; released", text)
        self.assertIn("reconcile: L-01 lock-escape repair retries now", text)
        self.assertFalse(data["tickets"]["M-01"]["shuttle"]["pending"])
        self.assertEqual(data["tickets"]["M-01"]["locks"], [])
        import alarm_repair
        import beam as beam_mod

        self.assertIsNone(alarm_repair._backoff(data["tickets"]["L-01"], beam_mod.parse_ts("2026-10-15T10:01:00Z"), 15))
        self.assertEqual(reconcile.run(data, self.path, now="2026-10-15T10:02:00Z")["lines"], ["reconcile: ok (0 Shuttles out, matching the beam)"])

    def test_a_live_shuttle_keeps_the_backoff_and_a_returned_repair_is_settled(self):
        import reconcile

        busy = out_shuttle("B-01", started="2026-10-15T10:00:00Z")
        escape = row("L-01", "alarm", alarm="lock-escape", escaped=["x"],
                     alarmRepair={"state": "failed", "failedAt": "2026-10-15T10:00:00Z", "attempts": 1})
        repaired = out_shuttle("P-01", status="claimed", step="repair",
                               alarmRepair={"state": "working", "launchedAt": "2026-10-15T10:00:00Z"})
        repaired["shuttle"]["returned"] = True
        data = run([busy, escape, repaired])
        data["alarmRepair"] = {"active": ["P-01"]}
        got = reconcile.run(data, self.path, now="2026-10-15T10:01:00Z")
        self.assertEqual(got["repairs"], ["P-01"])
        self.assertFalse(any("L-01" in l for l in got["lines"]), got["lines"])

    def test_checkout_result_marks_the_return(self):
        import checkout

        data = run([out_shuttle("C-01")])
        self.assertEqual(checkout.record_result(data, "C-01", "result: C-01 ok no pr", False), "ok")
        self.assertTrue(data["tickets"]["C-01"]["shuttle"]["returned"])
        self.assertTrue(data["tickets"]["C-01"]["pr"]["shuttleReturned"])


class FlowTest(unittest.TestCase):
    def test_a_pass_that_merged_runs_the_next_one_at_once_up_to_the_limit(self):
        data = run([row("M-1", "review", phase="ready"), row("M-2", "review", phase="ready")])
        nextwork.note_pass(data, ["merge: M-0 merged"])
        self.assertEqual(nextwork.continue_now(data), "continue merge:M-1")
        for _ in range(nextwork.RUSH_LIMIT):
            nextwork.note_pass(data, ["merge: M-0 merged"])
        self.assertIsNone(nextwork.continue_now(data))  # past the limit: wait like any pass
        nextwork.note_pass(data, ["review: wait M-1"])
        self.assertIsNone(nextwork.continue_now(data))

    def test_a_stall_is_named_once_and_its_end_too(self):
        data = run([row("A-1", "review", phase="reviewing", pr={"url": "u", "ci": "pending"}),
                    row("B-1", deps=["A-1"])], bugbotRequired=False)
        self.assertEqual(nextwork.note_pass(data, ["review: wait A-1"]), [])  # one quiet pass is not a stall
        got = nextwork.note_pass(data, ["review: wait A-1"])
        self.assertTrue(got[0].startswith("stall: no Shuttle is out and nothing moved for 2 passes."), got)
        self.assertIn("1 on CI (A-1)", got[0])
        self.assertIn("nothing can start: 1 on dependencies", got[0])
        self.assertTrue(any(l.startswith("herald: Nothing is running.") for l in got), got)
        again = nextwork.note_pass(data, ["review: wait A-1"])
        self.assertFalse(any(l.startswith("herald:") for l in again), again)
        self.assertEqual(nextwork.note_pass(data, ["merge: A-1 merged"]), ["herald: Work is moving again."])

    def test_a_start_line_nobody_launched_is_a_stall(self):
        t = row("S-1", "coding", phase="implementing", shuttle={"pending": True, "launching": True, "step": "implement"})
        data = run([t])
        nextwork.note_pass(data, ["start S-1 step=implement"])
        got = nextwork.note_pass(data, ["shuttle: not launched S-1.", "start S-1 step=implement"])
        self.assertIn("1 on a Shuttle launch (S-1)", got[0])
        self.assertTrue(got[1].startswith("stall: MUST DO run the start"), got)


class ReviewSignalTest(unittest.TestCase):
    URL = "https://github.com/acme/app/pull/24"

    def data(self, signal):
        t = row("V-1", "review", phase="reviewing", pr={"url": self.URL, "signal": signal})
        return run([t, row("A-1", "awaiting_approval", phase="ready", pr={"url": "u2"})])

    def test_signal_matches_between_the_pass_and_the_list(self):
        import provider

        doc = {"url": self.URL, "headRefOid": "a" * 40, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "state": "OPEN",
               "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"},
                                     {"name": "Cursor Bugbot", "conclusion": "", "status": "IN_PROGRESS"}]}

        class Done:
            returncode = 0
            stdout = json.dumps([doc])

        listed = provider.review_signals(Path("."), runner=lambda cmd, root: Done())
        self.assertEqual(listed[self.URL], "a" * 40 + "|green|pending||OPEN")
        snap = {"headSha": "a" * 40, "ci": "green", "bugbot": "pending", "conflictState": "", "prState": "OPEN"}
        self.assertEqual(provider.signal_of(snap), listed[self.URL])

    def test_a_changed_or_merged_pull_request_wakes_the_wait(self):
        same = "a|pending|pending||OPEN"
        data = self.data(same)
        self.assertIsNone(nextwork.review_changed(data, "/x/.warp/beam.json", lister=lambda root: {self.URL: same}))
        self.assertEqual(nextwork.review_changed(data, "/x/.warp/beam.json", lister=lambda root: {self.URL: "a|green|pass||OPEN"}), "review V-1")
        self.assertEqual(nextwork.review_changed(data, "/x/.warp/beam.json", lister=lambda root: {}), "review V-1")  # merged elsewhere
        self.assertIsNone(nextwork.review_changed(data, "/x/.warp/beam.json", lister=lambda root: None))  # no gh: nothing to say

    def test_awaiting_approval_is_not_a_merge_to_rush(self):
        data = self.data("s")
        self.assertNotIn("merge:A-1", nextwork.keys(data))


class ReviewWaitTest(unittest.TestCase):
    def test_parent_wait_checks_review_every_review_poll_seconds(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / ".warp" / "beam.json"
        path.parent.mkdir(parents=True)
        data = run([row("V-1", "review", phase="reviewing", pr={"url": "u", "signal": "s"})], pollSeconds=600, reviewPollSeconds=30)
        data["listener"] = {}
        data["pass"] = {"work": nextwork.keys(data), "startedAt": "2026-10-15T10:00:00Z"}
        path.write_text(json.dumps(data))
        calls = []
        real = nextwork.review_changed
        nextwork.review_changed = lambda d, p, lister=None: calls.append(1) or ("review V-1" if len(calls) > 1 else None)
        self.addCleanup(setattr, nextwork, "review_changed", real)
        clock = iter(range(1000000, 1002000, 2))
        got = orchestrator.parent_wait(path, sleep=lambda s: None, clock=lambda: next(clock))
        self.assertEqual(got, "work review V-1")
        self.assertEqual(len(calls), 2)  # once at 30s, again at 60s: not every step


class CapOpensTest(unittest.TestCase):
    def capped(self, alarm, **kw):
        t = row("K-1", "alarm", alarm=alarm, branch="warp/K-1", recoveries=5, stuckRestarts=5,
                acceptance=["AC-1: it works", "AC-2: it is fast"], **kw)
        return run([t], maxRecoveries=5)

    def test_worker_died_cap_with_passing_criteria_opens_the_pull_request(self):
        import pipeline

        data = self.capped("worker-died", pr={"acResults": {"AC-1": "pass", "AC-2": "pass"}})
        lines = pipeline.advance(data)
        t = data["tickets"]["K-1"]
        self.assertIn("pr: open K-1 warp/K-1", lines)
        self.assertTrue(any(l.startswith("herald: K-1 reached its worker-died cap, but its acceptance criteria pass") for l in lines), lines)
        self.assertEqual((t["status"], t["alarm"], t["phase"]), ("coding", None, "pr-open"))

    def test_stuck_cap_with_a_pull_request_goes_back_to_review(self):
        import pipeline

        data = self.capped("stuck", pr={"url": "https://github.com/acme/app/pull/9", "acResults": {"AC-1": "pass", "AC-2": "pass"}})
        lines = pipeline.advance(data)
        t = data["tickets"]["K-1"]
        self.assertEqual(t["status"], "review", lines)
        self.assertIsNone(t["alarm"])
        self.assertTrue(any("back to review on https://github.com/acme/app/pull/9" in l for l in lines), lines)

    def test_failing_criteria_keep_the_alarm(self):
        import pipeline

        data = self.capped("worker-died", pr={"acResults": {"AC-1": "pass", "AC-2": "fail"}})
        lines = pipeline.advance(data)
        self.assertEqual(data["tickets"]["K-1"]["status"], "alarm")
        self.assertIn("cap: K-1 worker-died cap reached; acceptance 1/2 pass, so no pull request is opened for it", lines)

    def test_a_given_up_lock_escape_with_passing_criteria_opens_too(self):
        import pipeline

        data = self.capped("lock-escape", escaped=["app/x.ts"], alarmRepair={"state": "gave-up", "gaveUp": True},
                           pr={"acResults": {"AC-1": "pass", "AC-2": "pass"}})
        lines = pipeline.advance(data)
        self.assertIn("pr: open K-1 warp/K-1", lines)
        self.assertIn("cap: K-1 edited files outside its locks: app/x.ts; the review and the merge check cover them", lines)


if __name__ == "__main__":
    unittest.main()
