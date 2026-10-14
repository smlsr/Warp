"""1.6.14: Cursor Bugbot gates the merge, late findings become follow-ups, one merge announcement, listener hand-off."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bugbot  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402

# Check runs and a review comment as GitHub returned them for WarpDemo PR #12 and #14.
NEUTRAL = {"name": "Cursor Bugbot", "status": "completed", "conclusion": "neutral"}
SUCCESS = {"name": "Cursor Bugbot", "status": "completed", "conclusion": "success"}
RUNNING = {"name": "Cursor Bugbot", "status": "in_progress", "conclusion": None}
COMMENT = {
    "user": {"login": "cursor[bot]"},
    "commit_id": SHA,
    "path": "WARPDEMO/WarpView/app/src/view/table/table.mjs",
    "line": 134,
    "body": "### Table render mutates shared runs\n\n**Medium Severity**\n\n<!-- DESCRIPTION START -->\n`renderTable` calls `plan(run)`...",
}


class ReadingBugbotTest(unittest.TestCase):
    def test_neutral_is_a_finding_and_success_is_a_pass(self):
        self.assertEqual(provider._bugbot_from_checks([NEUTRAL]), "fail")
        self.assertEqual(provider._bugbot_from_checks([SUCCESS]), "pass")
        self.assertEqual(provider._bugbot_from_checks([RUNNING]), "pending")
        self.assertEqual(provider.interpret_rollup([NEUTRAL]), "red")
        # Another check's neutral is still a success.
        self.assertEqual(provider.interpret_rollup([SUCCESS, {"name": "lint", "status": "completed", "conclusion": "neutral"}]), "green")
        self.assertEqual(orchestrator.check_kind("Cursor Bugbot", "neutral"), "red")
        self.assertEqual(orchestrator.check_kind("lint", "neutral"), "green")

    def test_findings_come_from_bugbots_review_comments_on_the_head(self):
        other = dict(COMMENT, commit_id="f" * 40)
        person = dict(COMMENT, user={"login": "smlsr"}, body="looks fine")

        def runner(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([COMMENT, other, person]), stderr="")

        found = provider.bugbot_findings(Path("."), URL, SHA, runner=runner)
        self.assertEqual(found, ["WARPDEMO/WarpView/app/src/view/table/table.mjs:134 Table render mutates shared runs (Medium)"])


class GateTest(Repo):
    """bugbotTrigger: shuttle. The Shuttle's review comes first; Cursor Bugbot still has to pass."""

    def ready(self, **extra):
        ticket = row(status="review", phase="reviewing", acs=["builds"], **extra)
        ticket["pr"].update({"ciNone": True, "bugbotRequested": True, "bugbotRequestedSha": SHA, "bugbotRequestedAt": "2026-10-14T00:10:00Z",
                             "acResults": {"AC-1": "pass"}, "acSha": SHA})
        bugbot.record_result(ticket, "pass", source="shuttle", sha=SHA)
        return ticket

    def run_one(self, data, snap, now="2026-10-14T00:12:00Z"):
        lines = []
        pipeline._advance_one(data, data["tickets"]["WV-01"], snap, lines, [], now=now)
        return lines

    def test_a_shuttle_pass_waits_for_bugbot_where_bugbot_runs(self):
        data = self.data([self.ready()], bugbotTrigger="shuttle", bugbotRequired=True)
        data["bugbotSeen"] = True
        lines = self.run_one(data, {"headSha": SHA, "rollup": "absent", "ci": "absent"})
        ticket = data["tickets"]["WV-01"]
        self.assertNotEqual(ticket["phase"], "ready", lines)
        self.assertEqual(ticket["pr"]["shuttleReview"], "pass")
        self.assertTrue(any(line.startswith("bugbot: wait WV-01 (the Shuttle's review passed") for line in lines), lines)
        self.assertFalse(any("step=review" in line for line in lines), lines)

    def test_bugbot_that_never_starts_is_asked_with_a_comment(self):
        data = self.data([self.ready()], bugbotTrigger="shuttle", bugbotRequired=True, bugbotStartMinutes=5)
        data["bugbotSeen"] = True
        lines = self.run_one(data, {"headSha": SHA, "rollup": "absent", "ci": "absent"}, now="2026-10-14T00:30:00Z")
        self.assertIn("bugbot: comment WV-01 %s" % URL, lines)

    def test_a_bugbot_finding_goes_to_a_fix_and_a_pass_merges(self):
        data = self.data([self.ready()], bugbotTrigger="shuttle", bugbotRequired=True)
        data["bugbotSeen"] = True
        self.beam_path.write_text(json.dumps(data))
        snap = {"headSha": SHA, "rollup": "red", "ci": "absent", "bugbot": "fail",
                "findings": ["app/table.mjs:134 Table render mutates shared runs (Medium)"]}
        lines = pipeline.advance(data, provider={"WV-01": snap}, beam_path=self.beam_path, now="2026-10-14T00:12:00Z")
        ticket = data["tickets"]["WV-01"]
        self.assertEqual(ticket["phase"], "fixing", lines)
        self.assertTrue(any(line.startswith("start WV-01 ") and "step=fix" in line for line in lines), lines)
        self.assertTrue(any("Table render mutates shared runs" in line for line in lines), lines)

        clean = self.data([self.ready()], bugbotTrigger="shuttle", bugbotRequired=True)
        clean["bugbotSeen"] = True
        self.run_one(clean, {"headSha": SHA, "rollup": "green", "ci": "absent", "bugbot": "pass", "findings": []})
        self.assertEqual(clean["tickets"]["WV-01"]["phase"], "ready")

    def test_a_repo_without_bugbot_merges_on_the_shuttles_review(self):
        data = self.data([self.ready()], bugbotTrigger="shuttle", bugbotRequired=True)
        self.run_one(data, {"headSha": SHA, "rollup": "absent", "ci": "absent"})
        self.assertEqual(data["tickets"]["WV-01"]["phase"], "ready")


class LateFindingTest(Repo):
    def merged(self, tid="WV-11", **pr):
        ticket = row(tid, status="merged", phase="merged", locks=["app/src/view/table/"])
        ticket["pr"].update({"url": URL, "mergeConfirmed": True, "mergedAt": "2026-10-14T00:14:14Z"}, **pr)
        return ticket

    def test_a_finding_after_the_merge_queues_a_follow_up_once(self):
        data = self.data([self.merged()], bugbotRequired=True)
        snap = {"bugbot": "fail", "findings": ["app/src/view/table/table.mjs:134 Table render mutates shared runs (Medium)",
                                               "app/src/store/runs.mjs:9 Shared state written (Low)"]}
        lines = pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: snap, now="2026-10-14T00:30:00Z")
        self.assertIn("bugbot: late WV-11 %s 2 finding(s) after the merge. Follow-up ticket WV-11-BB1 queued." % URL, lines)
        follow = data["tickets"]["WV-11-BB1"]
        self.assertEqual((follow["status"], follow["followUpOf"]), ("queued", "WV-11"))
        self.assertIn("app/src/store/runs.mjs", follow["locks"])
        self.assertNotIn("app/src/view/table/table.mjs", follow["locks"])  # inside the table/ lock already
        self.assertEqual(follow["acs"][0]["text"], "Bugbot finding fixed: app/src/view/table/table.mjs:134 Table render mutates shared runs (Medium)")
        self.assertEqual(len(follow["acs"]), 3)
        # Checked once.
        again = pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: self.fail("fetched again"), now="2026-10-14T00:40:00Z")
        self.assertEqual(again, [])

    def test_a_pass_or_no_bugbot_settles_it(self):
        data = self.data([self.merged("WV-14"), self.merged("WV-05", mergedAt=None)], bugbotRequired=True)
        pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: {"bugbot": "pass"}, now="2026-10-14T00:30:00Z")
        self.assertEqual(data["tickets"]["WV-14"]["pr"]["lateBugbot"], "pass")
        data = self.data([self.merged("WV-05", mergedAt=None)], bugbotRequired=True)
        pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: {}, now="2026-10-14T00:30:00Z")
        self.assertEqual(data["tickets"]["WV-05"]["pr"]["lateBugbot"], "none")

    def test_a_follow_up_merge_asks_for_no_jira_move(self):
        data = self.data([self.merged()], bugbotRequired=True)
        pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: {"bugbot": "fail", "findings": ["a/b.mjs:1 x"]}, now="2026-10-14T00:30:00Z")
        follow = data["tickets"]["WV-11-BB1"]
        follow["status"] = "merging"
        lines = []
        pipeline.finish_merge(data, follow, lines, how="merged on the provider (confirmed)")
        self.assertFalse(any(line.startswith("jira: MUST DO") for line in lines), lines)


class OneAnnouncementTest(Repo):
    def test_a_merge_is_announced_once_and_a_recorded_done_is_not_asked_again(self):
        ticket = row("WV-10", status="merging", phase="merging", jiraKey="WAR-56")
        data = self.data([ticket])
        lines = []
        pipeline.finish_merge(data, ticket, lines, how="merged on the provider (confirmed)")
        self.assertIn("jira: MUST DO WV-10 (WAR-56) transition to Done.", lines)
        ticket["jira"] = {"doneAt": "2026-10-14T00:20:00Z"}
        again = []
        pipeline.finish_merge(data, ticket, again, how="merged on the provider (confirmed)")
        self.assertEqual(again, [])
        # A ticket put back by heal and merged again says it once more, without the Done move.
        ticket["pr"].pop("mergeAnnounced")
        ticket["status"] = "merging"
        third = []
        pipeline.finish_merge(data, ticket, third, how="merged on the provider (confirmed)")
        self.assertIn("jira: WV-10 already Done (recorded 2026-10-14T00:20:00Z). Nothing to move.", third)


class ListenerHandOffTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp\n")
        self.write([row()])

    def test_each_cycle_is_numbered_and_a_generation_rotates_after_four(self):
        orchestrator.supervise_listener(self.beam_path, now="2026-10-14T00:00:00Z")
        lease = self.load()["listener"]["generation"]
        seen = []
        for minute in range(1, 6):
            lines = inbound.poll_lines(self.beam_path, lease=lease, holder="listener", now="2026-10-14T00:%02d:00Z" % minute)
            seen.append(lines)
        self.assertIn("listen: cycle 1 of 4 at 00:01:00Z (generation %s)" % lease, seen[0])
        self.assertIn("listen: cycle 4 of 4 at 00:04:00Z (generation %s)" % lease, seen[3])
        self.assertEqual(seen[4], ["reap: continue", "listen: rotate"] if seen[4][0] == "reap: continue" else ["listen: rotate"])
        # The parent starts the next generation at once.
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-14T00:05:10Z"), ["listener: poll"])

    def test_the_parent_wakes_when_the_listener_goes_silent(self):
        orchestrator.supervise_listener(self.beam_path, now="2026-10-14T00:00:00Z")
        data = self.load()
        data["listener"]["lastSeenAt"] = data["listener"]["generationStartedAt"] = "2026-01-01T00:00:00Z"
        self.beam_path.write_text(json.dumps(data))
        self.assertEqual(orchestrator.parent_wait(self.beam_path, limit=60, sleep=lambda s: None), "return")


class ListenerCutOffTest(Repo):
    """A listener that has to stop hands its slot back (leases 17, 23, 24 were left live)."""

    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp\n")
        self.write([row()])
        orchestrator.supervise_listener(self.beam_path, now="2026-10-14T00:00:00Z")
        self.lease = self.load()["listener"]["generation"]

    def test_the_holder_can_return_cut_off_and_the_next_one_starts(self):
        inbound.poll_lines(self.beam_path, lease=self.lease, holder="listener", now="2026-10-14T00:01:00Z")
        # An early exit after a quiet cycle is still refused.
        got = inbound.mark_returned(self.beam_path, "polled", lease=self.lease, holder="listener", now="2026-10-14T00:02:00Z")
        self.assertTrue(got.startswith("listener: not returned."), got)
        got = inbound.mark_returned(self.beam_path, "cut-off", lease=self.lease, holder="listener", now="2026-10-14T00:02:00Z")
        self.assertEqual(got, "listener: returned cut-off. The slot is free; the parent starts the next listener on its next pass.")
        self.assertEqual(json.loads((self.warp / "listener.json").read_text()).get("holder"), "")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-14T00:02:30Z"), ["listener: poll"])
        self.assertEqual(self.load()["listener"]["generation"], self.lease + 1)

    def test_a_copy_that_is_not_the_holder_cannot_use_cut_off(self):
        got = inbound.mark_returned(self.beam_path, "cut-off", lease=self.lease + 5, holder="someone", now="2026-10-14T00:02:00Z")
        self.assertEqual(got, "listener: return refused")
        self.assertEqual(self.load()["listener"]["holder"], "listener")

    def test_the_parents_exit_closes_the_lease(self):
        data = self.load()
        data["runState"] = "stopped"
        self.beam_path.write_text(json.dumps(data))
        lines = orchestrator.parent_exit(self.beam_path)
        self.assertEqual(lines[0], "parent: exit")
        self.assertIn("listener: generation %s closed with the parent's exit." % self.lease, lines)
        self.assertEqual(self.load()["listener"]["holder"], "")
        self.assertEqual(json.loads((self.warp / "listener.json").read_text()).get("holder"), "")


if __name__ == "__main__":
    unittest.main()
