"""1.6.18: a ticket waiting on Cursor Bugbot says so: when its check starts, while it runs, and when it ends."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bugbot  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
import watch  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402
from test_listener_loop import NOW, Clock, epoch  # noqa: E402

NEW = "c6ec784" + "0" * 33
# WarpEdit PR #24: Bugbot started at 02:58 and finished neutral with two Medium findings at 03:05.
RUNNING = {"name": "Cursor Bugbot", "status": "IN_PROGRESS", "conclusion": "", "startedAt": "2026-10-14T02:58:10Z",
           "completedAt": "0001-01-01T00:00:00Z"}
NEUTRAL = {"name": "Cursor Bugbot", "status": "COMPLETED", "conclusion": "NEUTRAL", "startedAt": "2026-10-14T02:58:10Z",
           "completedAt": "2026-10-14T03:05:40Z"}
FINDINGS = ["WARPDEMO/WarpEdit/app/src/main.mjs:12 Editor boots the viewer twice (Medium)",
            "WARPDEMO/WarpEdit/app/src/store/status.mjs:40 Comment stripping hides status writes (Medium)"]


def pending(**extra):
    return dict({"headSha": SHA, "bugbot": "pending", "bugbotStartedAt": "2026-10-14T02:58:10Z", "rollup": "pending", "ci": "absent"}, **extra)


def failed(**extra):
    return dict({"headSha": SHA, "bugbot": "fail", "bugbotStartedAt": "2026-10-14T02:58:10Z", "bugbotAt": "2026-10-14T03:05:40Z",
                 "findings": FINDINGS, "rollup": "red", "ci": "absent"}, **extra)


def herald(lines):
    return [line for line in lines if line.startswith("herald: ")]


class ReadingTest(unittest.TestCase):
    def test_the_start_time_is_read_and_an_unset_one_is_not(self):
        self.assertEqual(provider.pr_times({}, [RUNNING])["bugbotStartedAt"], "2026-10-14T02:58:10Z")
        self.assertNotIn("bugbotAt", provider.pr_times({}, [RUNNING]))
        self.assertEqual(provider.pr_times({}, [NEUTRAL])["bugbotAt"], "2026-10-14T03:05:40Z")
        queued = {"name": "Cursor Bugbot", "status": "QUEUED", "startedAt": "0001-01-01T00:00:00Z"}
        self.assertNotIn("bugbotStartedAt", provider.pr_times({}, [queued]))


class ProgressTest(unittest.TestCase):
    def test_start_running_and_done_each_say_so_and_post_twice(self):
        ticket = row("WE-01")
        first = bugbot.progress(ticket, pending(), now="2026-10-14T02:59:00Z")
        self.assertEqual(first[0], "bugbot: running WE-01 %s on %s (started 02:58Z)" % (URL, SHA[:12]))
        self.assertEqual(herald(first), ["herald: WE-01 Cursor Bugbot is reviewing PR #4 (%s)." % SHA[:7]])
        second = bugbot.progress(ticket, pending(), now="2026-10-14T03:03:30Z")
        self.assertEqual(second, ["bugbot: running WE-01 on %s, 5m so far" % SHA[:12]])
        done = bugbot.progress(ticket, failed(), now="2026-10-14T03:06:00Z")
        self.assertEqual(done[0], "bugbot: done WE-01 2 finding(s) on %s (took 7m): %s" % (SHA[:12], "; ".join(FINDINGS)))
        self.assertEqual(herald(done), ["herald: WE-01 Cursor Bugbot finished PR #4: 2 finding(s) (took 7m). Back to fix."])
        self.assertEqual(bugbot.progress(ticket, failed(), now="2026-10-14T03:07:00Z"), [])  # said once

    def test_a_check_that_ran_between_passes_still_says_done(self):
        ticket = row("WE-02")
        snap = {"headSha": SHA, "bugbot": "pass", "bugbotStartedAt": "2026-10-14T02:58:10Z", "bugbotAt": "2026-10-14T03:02:00Z"}
        lines = bugbot.progress(ticket, snap, now="2026-10-14T03:04:00Z")
        self.assertEqual(lines, ["bugbot: done WE-02 pass on %s (took 3m)" % SHA[:12],
                                 "herald: WE-02 Cursor Bugbot finished PR #4: no issues (took 3m)."])

    def test_an_old_result_seen_first_on_a_resume_is_shown_not_posted(self):
        ticket = row("WE-03")
        snap = {"headSha": SHA, "bugbot": "pass", "bugbotAt": "2026-10-14T01:00:00Z"}
        lines = bugbot.progress(ticket, snap, now="2026-10-14T03:04:00Z")
        self.assertEqual(lines, ["bugbot: done WE-03 pass on %s" % SHA[:12]])

    def test_a_new_head_starts_a_new_run(self):
        ticket = row("WE-01")
        bugbot.progress(ticket, failed(), now="2026-10-14T03:06:00Z")
        ticket["pr"]["headSha"] = NEW
        lines = bugbot.progress(ticket, pending(headSha=NEW, bugbotStartedAt="2026-10-14T03:20:00Z"), now="2026-10-14T03:21:00Z")
        self.assertTrue(lines[0].startswith("bugbot: running WE-01 %s on %s (started 03:20Z)" % (URL, NEW[:12])), lines)
        self.assertEqual(len(herald(lines)), 1)

    def test_status_shows_running_and_how_long_it_took(self):
        ticket = row("WE-01")
        bugbot.progress(ticket, pending(), now="2026-10-14T02:59:00Z")
        self.assertEqual(bugbot.activity(ticket["pr"], "2026-10-14T03:02:20Z"), "running(4m)")
        self.assertEqual(watch.next_action(ticket, "2026-10-14T03:02:20Z"), "wait for Bugbot (running 4m)")
        self.assertEqual(watch._row(ticket, "2026-10-14T03:02:20Z")["bugbot"], "running(4m)")
        bugbot.progress(ticket, failed(), now="2026-10-14T03:06:00Z")
        self.assertEqual(bugbot.activity(ticket["pr"], "2026-10-14T03:10:00Z"), "fail(7m)")
        ticket["pr"]["headSha"] = NEW  # a pushed fix: the old run is not this head's
        self.assertEqual(bugbot.activity(ticket["pr"], "2026-10-14T03:10:00Z"), "")


class PassTest(Repo):
    """bugbotTrigger auto: what a supervise pass prints for a ticket waiting on Bugbot."""

    def run_one(self, data, snap, now):
        lines = []
        self.deferred = []
        pipeline._advance_one(data, data["tickets"]["WE-01"], snap, lines, self.deferred, now=now)
        return lines

    def test_not_started_then_running_then_findings_back_to_fix(self):
        ticket = row("WE-01", acs=["builds"])
        ticket["pr"].update({"bugbotRequestedAt": "2026-10-14T02:55:00Z", "acResults": {"AC-1": "pass"}, "acSha": SHA})
        data = self.data([ticket], bugbotRequired=True)
        data["bugbotSeen"] = True
        lines = self.run_one(data, {"headSha": SHA, "rollup": "absent", "ci": "absent"}, now="2026-10-14T02:57:00Z")
        self.assertIn("bugbot: not started WE-01 on %s (asked 2m ago)" % SHA[:12], lines)
        lines = self.run_one(data, pending(), now="2026-10-14T02:59:00Z")
        self.assertIn("bugbot: running WE-01 %s on %s (started 02:58Z)" % (URL, SHA[:12]), lines)
        self.assertFalse(any(line.startswith("bugbot: not started") for line in lines), lines)
        lines = self.run_one(data, pending(), now="2026-10-14T03:03:00Z")
        self.assertIn("bugbot: running WE-01 on %s, 4m so far" % SHA[:12], lines)
        self.assertEqual(herald(lines), [])
        lines = self.run_one(data, failed(), now="2026-10-14T03:06:00Z")
        self.assertTrue(any(line.startswith("bugbot: done WE-01 2 finding(s)") for line in lines), lines)
        self.assertIn("herald: WE-01 Cursor Bugbot finished PR #4: 2 finding(s) (took 7m). Back to fix.", lines)
        self.assertEqual(data.get("_fixDefer"), ["WE-01"])  # the fix starts later in the same pass

    def test_a_shuttle_pass_waiting_on_bugbot_says_it_has_not_started(self):
        ticket = row("WE-01", acs=["builds"])
        ticket["pr"].update({"bugbotRequestedAt": "2026-10-14T02:50:00Z", "acResults": {"AC-1": "pass"}, "acSha": SHA, "ciNone": True})
        bugbot.record_result(ticket, "pass", source="shuttle", sha=SHA)
        data = self.data([ticket], bugbotTrigger="shuttle", bugbotRequired=True)
        data["bugbotSeen"] = True
        lines = self.run_one(data, {"headSha": SHA, "rollup": "absent", "ci": "absent"}, now="2026-10-14T02:53:00Z")
        self.assertIn("bugbot: not started WE-01 on %s (asked 3m ago)" % SHA[:12], lines)

    def test_a_late_check_says_when_it_runs_and_its_finding_is_posted_once(self):
        ticket = row("WE-01", status="merged", phase="merged")
        ticket["pr"].update({"mergeConfirmed": True, "mergedAt": "2026-10-14T02:57:00Z"})
        data = self.data([ticket], bugbotRequired=True)
        lines = pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: pending(), now="2026-10-14T02:59:00Z")
        self.assertIn("bugbot: running WE-01 %s on %s (started 02:58Z) after the merge" % (URL, SHA[:12]), lines)
        self.assertEqual(herald(lines), ["herald: WE-01 Cursor Bugbot is reviewing PR #4 after the merge (%s)." % SHA[:7]])
        lines = pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: failed(), now="2026-10-14T03:06:00Z")
        self.assertTrue(any(line.startswith("bugbot: done WE-01 2 finding(s) on %s (took 7m) after the merge" % SHA[:12]) for line in lines), lines)
        self.assertEqual(herald(lines), ["herald: WE-01 Bugbot found 2 issue(s) after the merge; WE-01-BB1 fixes them."])


class WaitTest(Repo):
    """`parent-exit --wait` says what the run waits on, and looks again sooner while Bugbot runs."""

    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp-run\nbugbotRequired: true\n")
        running = row("WE-01")
        running["pr"]["bugbotRun"] = {"sha": SHA, "startedAt": "2026-10-07T11:54:00Z"}
        asked = row("WE-02")
        asked["pr"]["bugbotRequestedAt"] = "2026-10-07T11:58:00Z"
        quiet = row("WE-03", status="coding", phase="implementing")
        quiet["pr"] = {}
        data = self.write([running, asked, quiet], pollSeconds=300)
        data["pass"] = {"startedAt": NOW, "count": 4}
        data["listener"] = {"state": "stopped", "lastSeenAt": NOW}
        self.beam_path.write_text(__import__("json").dumps(data))
        self.clock = Clock(epoch(NOW))

    def test_the_summary_names_each_ticket(self):
        self.assertEqual(bugbot.waiting_summary(self.load(), NOW),
                         "bugbot: waiting on Cursor Bugbot: WE-01 running 6m, WE-02 not started 2m")

    def test_the_wait_ends_after_bugbot_poll_seconds_and_prints_the_summary(self):
        lines = orchestrator.parent_exit(self.beam_path, wait=-1, sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(lines[0], "pass: timeout")
        self.assertLessEqual(self.clock.now - epoch(NOW), 122)
        self.assertTrue(lines[1].startswith("bugbot: waiting on Cursor Bugbot: WE-01 running "), lines)
        self.assertEqual(lines[-2:], ["run: next is pass 5", "parent: stay"])


if __name__ == "__main__":
    unittest.main()
