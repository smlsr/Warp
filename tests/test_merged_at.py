"""1.6.22: every merge records pr.mergedAt. A scanned ticket's pr starts with "mergedAt": null, and
finish_merge used setdefault, which kept the null, so the viewer's Merged column was empty for every ticket."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import heal  # noqa: E402
import jira_sync  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402
from test_real_merge import OPEN, Live, ready_row  # noqa: E402

NULLS = {"openedAt": "2026-10-14T02:56:25Z", "reviewedAt": None, "approvedAt": None, "mergedAt": None}


class RecordTest(Repo):
    def test_finish_merge_stamps_a_null_merged_at(self):
        ticket = row("WE-01", status="merging", phase="merging")
        ticket["pr"].update(NULLS)
        data = self.data([ticket])
        pipeline.finish_merge(data, data["tickets"]["WE-01"], [], sha="5796d14", how="merged on the provider")
        self.assertTrue(data["tickets"]["WE-01"]["pr"]["mergedAt"])

    def test_the_providers_time_wins(self):
        ticket = row("WE-01", status="merging", phase="merging")
        ticket["pr"].update(NULLS)
        pipeline.note_times(ticket, {"mergedAt": "2026-10-14T04:11:53Z"})
        data = self.data([ticket])
        pipeline.finish_merge(data, data["tickets"]["WE-01"], [], how="merged on the provider (confirmed)")
        self.assertEqual(data["tickets"]["WE-01"]["pr"]["mergedAt"], "2026-10-14T04:11:53Z")

    def test_a_reported_merge_and_a_jira_merged_record_stamp_it(self):
        ticket = row("WE-02", status="merging")
        ticket["pr"].update(NULLS)
        self.assertTrue(orchestrator.apply_reported_merge(ticket, "merged", sha="67d17b4"))
        self.assertTrue(ticket["pr"]["mergedAt"])
        other = row("WE-03", status="merging")
        other["pr"].update(NULLS)
        jira_sync._write_intent(other, "merged", "Done")
        self.assertTrue(other["pr"]["mergedAt"])

    def test_a_live_pass_records_it(self):
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/smlsr/WarpDemo.git"], cwd=self.tmp, check=True)
        self.config.write_text("bugbotRequired: true\ngitProvider: github\n")
        ticket = ready_row()
        ticket["pr"].update(NULLS)
        self.write([ticket])
        data = self.load()
        data["gates"] = [{"key": "G0", "members": ["WV-01"], "status": "pending", "blocking": True}]
        self.beam_path.write_text(json.dumps(data))
        with Live(OPEN):
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T22:00:00Z")
        self.assertIn("merge: WV-01 merged", lines)
        self.assertTrue(self.load()["tickets"]["WV-01"]["pr"]["mergedAt"])


class LateCheckTest(Repo):
    def test_the_late_check_takes_the_providers_time_and_still_looks(self):
        ticket = row("WE-01", status="merged", phase="merged")
        ticket["pr"].update(NULLS, mergeConfirmed=True)
        data = self.data([ticket], bugbotRequired=True)
        snap = {"prState": "MERGED", "mergedAt": "2026-10-14T04:11:53Z"}
        pipeline.late_findings(data, self.beam_path, [], fetch=lambda url: snap, now="2026-10-14T04:20:00Z")
        pr = data["tickets"]["WE-01"]["pr"]
        self.assertEqual(pr["mergedAt"], "2026-10-14T04:11:53Z")
        # Before 1.6.22 a merge with no time was settled as `none` (as if merged before 1.6.14) without a look.
        self.assertNotIn("lateBugbot", pr)


class BackfillTest(Repo):
    def commit(self):
        subprocess.run(["git", "config", "user.email", "warp@example.com"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "Warp"], cwd=self.tmp, check=True)
        (self.tmp / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "a.txt"], cwd=self.tmp, check=True)
        env = {"GIT_COMMITTER_DATE": "2026-10-14T04:11:53Z", "GIT_AUTHOR_DATE": "2026-10-14T04:11:53Z", "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "commit", "-q", "-m", "[WE-01] merge"], cwd=self.tmp, check=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp, capture_output=True, text=True).stdout.strip()

    def test_a_merged_ticket_read_while_open_is_read_again(self):
        ticket = row("WE-01", status="merged", phase="merged")
        ticket["pr"].update(NULLS, timesRead=True)
        self.write([ticket])
        lines = heal.backfill_times(self.beam_path, fetch=lambda url: {"mergedAt": "2026-10-14T04:11:53Z", "openedAt": "2026-10-14T02:56:25Z"})
        self.assertEqual(self.load()["tickets"]["WE-01"]["pr"]["mergedAt"], "2026-10-14T04:11:53Z")
        self.assertEqual(lines, ["times: filled in WE-01 from the pull requests"])

    def test_the_merge_commit_gives_the_time_without_the_provider(self):
        sha = self.commit()
        ticket = row("WE-01", status="merged", phase="merged")
        ticket["pr"].update(NULLS, sha=sha[:7])
        self.write([ticket])
        self.assertEqual(heal.fill_merge_times(self.beam_path), ["times: merged time filled in for WE-01 from the merge commits"])
        self.assertEqual(self.load()["tickets"]["WE-01"]["pr"]["mergedAt"], "2026-10-14T04:11:53Z")
        self.assertEqual(heal.fill_merge_times(self.beam_path), [])


if __name__ == "__main__":
    unittest.main()
