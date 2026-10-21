"""1.7.3: bugs from run warp:bce0ae (HumanifyOS).

1. An older listener stopped a newer listener's lease.
2. Parked tickets lost their locks, and a retry relaunched them with none (lock-escape at once).
3. `supervise --returned` accepted any text and stamped the live listener.
4. Cursor Bugbot's `neutral` check was always red, even with no finding on the head.
5. A Shuttle or repair claimed and never launched held a slot forever (phantom claims).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import inbound  # noqa: E402
import orchestrator  # noqa: E402
import provider  # noqa: E402
import reconcile  # noqa: E402

NOW = "2026-10-16T19:11:14Z"


class ListenerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.beam_path = warp / "beam.json"
        self.beam_path.write_text(json.dumps({
            "version": 1, "runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "review"}}, "gates": [],
            "config": {"listenerFastSeconds": 4, "listenerSlowSeconds": 6, "listenerOrphanMinutes": 5,
                       "listenerMaxMinutes": 60, "listenerMaxReads": 60}}) + "\n")

    def beam(self):
        return json.loads(self.beam_path.read_text())

    def write(self, data):
        self.beam_path.write_text(json.dumps(data) + "\n")

    def next_generation(self):
        """Generation 1 out, then generation 2 issued while 1 is still out (the repro's step 2)."""
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])
        data = self.beam()
        inbound.begin_generation(data, NOW)
        self.write(data)
        self.assertEqual(self.beam()["listener"]["generation"], 2)


class LeaseTest(ListenerBase):
    def test_a_late_poll_from_an_older_generation_does_not_stop_the_newer_one(self):
        self.next_generation()
        lines = inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        self.assertEqual(lines[0], "reap: exit superseded")
        self.assertEqual(lines[-1], "listen: stopped superseded lease=1")
        raw = self.beam()["listener"]
        self.assertEqual((raw["generation"], raw["holder"], raw["state"]), (2, "listener", "running"))
        self.assertEqual(raw.get("returnReason") or "", "")
        # The same from the wait.
        self.assertEqual(inbound._wait_check(self.beam_path, 1, "listener", NOW)[0], "reap: exit superseded")
        self.assertEqual(self.beam()["listener"]["state"], "running")
        # The newer generation still polls.
        self.assertTrue(any(l.startswith("listen: cycle") for l in inbound.poll_lines(self.beam_path, lease=2, holder="listener", now=NOW)))

    def test_an_expired_lease_is_still_handed_back_by_its_own_copy(self):
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])
        data = self.beam()
        inbound.expire_lease(data, NOW)  # generation 2, no holder, waiting for 1's return stamp
        self.write(data)
        inbound.poll_lines(self.beam_path, lease=1, holder="listener", now=NOW)
        raw = self.beam()["listener"]
        self.assertEqual(raw["returnReason"], "stopped")
        self.assertTrue(inbound.return_ready(self.beam()))


class ReturnedTest(ListenerBase):
    def test_a_ticket_id_or_a_superseded_line_does_not_stamp_the_live_listener(self):
        self.next_generation()
        for text in ("C-20", "result: C-20 ok", "listen: stopped superseded lease=1", "reap: exit superseded"):
            lines = orchestrator.supervise_listener(self.beam_path, returned=text, now=NOW)
            self.assertTrue(lines[0].startswith("listener: "), lines)
            self.assertIn("hold reason=live", "\n".join(lines))
            raw = self.beam()["listener"]
            self.assertEqual((raw["holder"], raw["state"]), ("listener", "running"), text)
        self.assertIn("is not a line a listener returns", orchestrator.supervise_listener(self.beam_path, returned="C-20", now=NOW)[0])
        stale = orchestrator.supervise_listener(self.beam_path, returned="listen: stopped lease=1", now=NOW)
        self.assertIn("came from a listener whose lease was already taken over", stale[0])

    def test_the_live_listeners_own_line_still_counts(self):
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now=NOW), ["listener: poll"])
        lines = orchestrator.supervise_listener(self.beam_path, returned="listen: stopped", now=NOW)
        self.assertEqual(lines, ["listener: idle"])
        self.assertEqual(self.beam()["listener"]["returnReason"], "stopped")


def ticket(tid, status, **kw):
    row = {"id": tid, "status": status, "alarm": None, "locks": ["src/%s" % tid], "deps": [], "after": [], "size": "S",
           "summary": tid, "pr": {}, "branch": "warp/%s" % tid, "module": "core", "tokens": 0, "minutes": 0}
    row.update(kw)
    return row


class BeamBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.parent.mkdir(parents=True)

    def write(self, tickets, **cfg):
        config = {"maxAgents": 4, "runner": "local", "maxRecoveries": 5, "jiraTransition": False}
        config.update(cfg)
        data = {"runState": "running", "paused": False, "config": config, "gates": [],
                "tickets": {t["id"]: t for t in tickets}}
        self.path.write_text(json.dumps(data))
        return data

    def load(self):
        return json.loads(self.path.read_text())


class LocksTest(BeamBase):
    def test_parked_locks_are_kept_and_a_retry_gives_them_back(self):
        data = self.write([ticket("C-20", "parked", alarm="parked", locks=["app/a.ts", "app/b.ts"],
                                  pr={"url": "https://github.com/acme/app/pull/20"})])
        lines = reconcile.run(data, self.path, now=NOW)["lines"]
        self.assertIn("reconcile: C-20 is parked but still held 2 locks; released", lines)
        t = data["tickets"]["C-20"]
        self.assertEqual((t["locks"], t["releasedLocks"]), ([], ["app/a.ts", "app/b.ts"]))
        self.path.write_text(json.dumps(data))
        said = inbound._requeue(self.path, "C-20", by="autoscan")
        t = self.load()["tickets"]["C-20"]
        self.assertEqual(t["status"], "fix")
        self.assertEqual(t["locks"], ["app/a.ts", "app/b.ts"])
        self.assertNotIn("releasedLocks", t)
        self.assertIn("locks: C-20 restored 2 locks (kept when it was released)", said)

    def test_a_ticket_emptied_before_the_fix_gets_the_plans_locks_and_its_repairs(self):
        (self.path.parent / "_ingested_schedule.json").write_text(json.dumps({"tickets": [
            {"id": "C-45", "locks": ["svc/x.go"]}, {"id": "Q-1", "locks": []}]}))
        data = self.write([ticket("C-45", "fix", locks=[], alarmRepair={"added": ["svc/x_test.go"]}),
                           ticket("Q-1", "queued", locks=[])])
        lines = reconcile.run(data, self.path, now=NOW)["lines"]
        self.assertEqual(data["tickets"]["C-45"]["locks"], ["svc/x.go", "svc/x_test.go"])
        self.assertTrue(any("C-45 is fix with no locks" in l and "from the plan and its lock-escape repairs" in l for l in lines), lines)
        self.assertEqual(data["tickets"]["Q-1"]["locks"], [])  # the plan gave it none: nothing to restore


def checks(conclusion="NEUTRAL"):
    return [{"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "Cursor Bugbot", "conclusion": conclusion, "status": "COMPLETED"}]


class BugbotNeutralTest(unittest.TestCase):
    URL = "https://github.com/acme/app/pull/77"
    HEAD = "f" * 40

    def fake(self, comments, comments_ok=True, conclusion="NEUTRAL", resolved=(), threads_ok=True, pr=None):
        view = {"url": self.URL, "headRefOid": self.HEAD, "statusCheckRollup": checks(conclusion), "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN", "commits": [], "state": "OPEN"}
        threads = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
            {"isResolved": True, "comments": {"nodes": [{"databaseId": cid}]}} for cid in resolved]}}}}}

        def run(cmd, cwd=None, timeout=20):
            if cmd[:3] == ["gh", "pr", "view"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(view), stderr="")
            if cmd[:3] == ["gh", "api", "graphql"]:
                return SimpleNamespace(returncode=0 if threads_ok else 1, stdout=json.dumps(threads), stderr="")
            if cmd[:2] == ["gh", "api"]:
                return SimpleNamespace(returncode=0 if comments_ok else 1, stdout=json.dumps(comments), stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        real_run, real_which = provider._run, provider.shutil.which
        provider._run = run
        provider.shutil.which = lambda name: "/usr/bin/gh"
        self.addCleanup(setattr, provider, "_run", real_run)
        self.addCleanup(setattr, provider.shutil, "which", real_which)
        return provider.fetch_pr_state(Path("."), pr or self.URL)

    def comment(self, **kw):
        row = {"user": {"login": "cursor[bot]"}, "body": "### Null check\n**High Severity**", "path": "app/x.ts",
               "line": 12, "position": 3, "commit_id": self.HEAD}
        row.update(kw)
        return row

    def test_only_outdated_threads_is_a_pass(self):
        old = self.comment(line=None, position=None, commit_id="a" * 40, original_commit_id="a" * 40)
        snap = self.fake([old, self.comment(in_reply_to_id=5)])
        self.assertEqual((snap["bugbot"], snap["rollup"], snap["findings"]), ("pass", "green", []))
        self.assertTrue(snap["bugbotNeutralClean"])
        # The wake compares what GitHub's check says, so a neutral read as a pass does not wake the parent every minute.
        listed = provider.signal_of(provider._signal_fields({"headRefOid": self.HEAD, "statusCheckRollup": checks(), "state": "OPEN"}))
        self.assertEqual(provider.signal_of(snap), listed)

    def test_a_finding_on_the_head_is_still_a_fail(self):
        snap = self.fake([self.comment()])
        self.assertEqual(snap["bugbot"], "fail")
        self.assertEqual(snap["findings"], ["app/x.ts:12 Null check (High)"])

    def test_a_resolved_thread_carried_to_the_head_is_a_pass(self):
        # A merge of main leaves the thread's lines alone, so GitHub moves the fixed finding to the new head.
        snap = self.fake([self.comment(id=4330706640, original_commit_id="d" * 40)], resolved=[4330706640])
        self.assertEqual((snap["bugbot"], snap["rollup"], snap["findings"]), ("pass", "green", []))
        self.assertTrue(snap["bugbotNeutralClean"])

    def test_a_pull_request_recorded_by_number_reads_its_comments(self):
        old = self.comment(line=None, position=None, commit_id="a" * 40, original_commit_id="a" * 40)
        self.assertEqual(self.fake([old], pr="77")["bugbot"], "pass")
        self.assertEqual(self.fake([self.comment()], pr="77")["findings"], ["app/x.ts:12 Null check (High)"])

    def test_unreadable_threads_keep_the_finding(self):
        snap = self.fake([self.comment(id=7)], resolved=[7], threads_ok=False)
        self.assertEqual(snap["bugbot"], "fail")
        self.assertEqual(snap["findings"], ["app/x.ts:12 Null check (High)"])

    def test_unreadable_comments_are_not_a_pass(self):
        snap = self.fake([], comments_ok=False)
        self.assertEqual(snap["bugbot"], "fail")
        self.assertTrue(snap["findings"][0].startswith("Cursor Bugbot found issues on"))


class PhantomTest(BeamBase):
    def test_a_claim_nobody_launched_stops_holding_a_slot(self):
        old = "2026-10-16T18:00:00Z"
        data = self.write([
            ticket("C-20", "coding", shuttle={"pending": True, "launching": True, "step": "implement", "markedAt": old}),
            ticket("C-45", "claimed", shuttle={"step": "repair", "pending": False},
                   alarmRepair={"state": "working", "startedAt": old}),
            ticket("N-1", "coding", shuttle={"pending": True, "launching": True, "step": "implement", "markedAt": NOW}),
        ])
        self.assertEqual(orchestrator.live_slot_holders(data, now=NOW), ["C-20", "C-45", "N-1"])
        lines = reconcile.run(data, self.path, now=NOW)["lines"]
        self.assertTrue(any(l.startswith("reconcile: C-20 was claimed for a Shuttle (implement) that was never launched") for l in lines), lines)
        self.assertTrue(any(l.startswith("reconcile: C-45 was claimed for a lock-escape repair") for l in lines), lines)
        self.assertEqual(orchestrator.live_slot_holders(data, now=NOW), ["N-1"])  # a fresh mark still holds its slot
        self.assertEqual(reconcile.run(data, self.path, now=NOW)["lines"], ["reconcile: ok (1 Shuttle out, matching the beam)"])  # said once


if __name__ == "__main__":
    unittest.main()
