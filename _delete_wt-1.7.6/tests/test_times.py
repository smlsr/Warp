"""1.6.17: the pull request timestamps, the claim time, and Spent are recorded (the viewer showed `—`)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import heal  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402

DOC = {
    "createdAt": "2026-10-14T00:10:00Z",
    "mergedAt": "2026-10-14T00:14:14Z",
    "latestReviews": [{"state": "COMMENTED", "submittedAt": "2026-10-14T00:12:00Z", "author": {"login": "cursor"}},
                      {"state": "APPROVED", "submittedAt": "2026-10-14T00:13:00Z", "author": {"login": "smlsr"}}],
}
CHECKS = [{"name": "Cursor Bugbot", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-10-14T00:12:30Z"}]


class ProviderTimesTest(unittest.TestCase):
    def test_the_pull_requests_own_times(self):
        self.assertEqual(provider.pr_times(DOC, CHECKS), {
            "openedAt": "2026-10-14T00:10:00Z", "mergedAt": "2026-10-14T00:14:14Z", "bugbotAt": "2026-10-14T00:12:30Z",
            "approvedAt": "2026-10-14T00:13:00Z", "approvedBy": "smlsr"})


class PipelineTimesTest(Repo):
    def test_each_step_stamps_its_time(self):
        ticket = row(status="review", phase="reviewing", autoMerge=True, acs=["builds"])
        ticket["pr"].update({"ciNone": True, "acResults": {"AC-1": "pass"}, "acSha": SHA})
        data = self.data([ticket], bugbotRequired=True)
        lines = []
        pipeline._advance_one(data, data["tickets"]["WV-01"], {"headSha": SHA, "rollup": "green", "ci": "absent", "bugbot": "pass",
                                                                "findings": [], "openedAt": "2026-10-14T00:10:00Z", "bugbotAt": "2026-10-14T00:12:30Z"},
                              lines, [], now="2026-10-14T00:13:00Z")
        pr = data["tickets"]["WV-01"]["pr"]
        self.assertEqual(data["tickets"]["WV-01"]["phase"], "ready", lines)
        self.assertEqual(pr["openedAt"], "2026-10-14T00:10:00Z")
        self.assertEqual(pr["reviewedAt"], "2026-10-14T00:12:30Z")
        self.assertTrue(pr["approvedAt"])
        self.assertEqual(pr["approvedBy"], "auto-merge")
        pipeline.confirm_merge(data, data["tickets"]["WV-01"], {"prState": "MERGED", "mergedAt": "2026-10-14T00:14:14Z"}, [])
        self.assertEqual(pr["mergedAt"], "2026-10-14T00:14:14Z")

    def test_a_github_approval_wins(self):
        ticket = row(status="review", phase="reviewing", autoMerge=False)
        pipeline.note_times(ticket, {"approvedAt": "2026-10-14T00:13:00Z", "approvedBy": "smlsr"})
        self.assertEqual((ticket["pr"]["approvedAt"], ticket["pr"]["approvedBy"]), ("2026-10-14T00:13:00Z", "smlsr"))

    def test_a_start_stamps_the_claim_and_spent_comes_from_the_registry(self):
        ticket = row("WV-02", status="queued", pr={})
        ticket.pop("phase", None)
        data = self.data([ticket])
        pipeline.mark_shuttle(ticket, "implement", data)
        pipeline._start_lines(data, ticket, "implement")
        self.assertTrue(ticket.get("claimedAt"))
        (self.warp / "agents.json").write_text(json.dumps({"agents": [
            {"id": "subagent:WV-02", "ticket": "WV-02", "role": "shuttle", "started": "2026-10-14T00:00:00Z", "ended": "2026-10-14T00:25:00Z", "state": "ended"},
            {"id": "shuttle-WV-02-r1", "ticket": "WV-02", "role": "shuttle", "started": "2026-10-14T00:30:00Z", "ended": "2026-10-14T00:41:00Z", "state": "ended"},
            {"id": "bugbot:WV-02:x", "ticket": "WV-02", "role": "bugbot", "started": "2026-10-14T00:41:00Z", "ended": "2026-10-14T00:50:00Z"}]}))
        data = self.load()
        data["_repoRoot"] = str(self.tmp)
        pipeline.note_spent(data, self.beam_path)
        self.assertEqual(data["tickets"]["WV-02"]["minutes"], 36)


class BackfillTest(Repo):
    def test_merged_tickets_get_their_times_once(self):
        merged = row("WV-11", status="merged", phase="merged", autoMerge=True, gate="G2")
        merged["pr"].update({"url": URL})
        follow = row("WV-11-BB1", status="merged", phase="merged", followUpOf="WV-11", gate=None)
        follow["pr"] = {"url": URL.replace("/4", "/22"), "openedAt": "x", "reviewedAt": "x", "approvedAt": "x", "mergedAt": "x"}
        data = self.write([merged, follow])
        data["gates"] = [{"key": "G0", "members": []}, {"key": "G2", "members": ["WV-11"]}]
        self.beam_path.write_text(json.dumps(data))
        calls = []

        def fetch(url):
            calls.append(url)
            return dict(provider.pr_times(dict(DOC, latestReviews=[]), CHECKS), prState="MERGED")

        lines = heal.backfill_times(self.beam_path, fetch=fetch)
        self.assertEqual(lines, ["times: filled in WV-11 from the pull requests"])
        pr = self.load()["tickets"]["WV-11"]["pr"]
        self.assertEqual((pr["openedAt"], pr["reviewedAt"], pr["mergedAt"]), ("2026-10-14T00:10:00Z", "2026-10-14T00:12:30Z", "2026-10-14T00:14:14Z"))
        self.assertEqual((pr["approvedAt"], pr["approvedBy"]), ("2026-10-14T00:12:30Z", "auto-merge"))
        self.assertEqual(self.load()["tickets"]["WV-11-BB1"]["gate"], "G2")
        self.assertEqual(heal.backfill_times(self.beam_path, fetch=fetch), [])
        self.assertEqual(calls, [URL])


if __name__ == "__main__":
    unittest.main()
