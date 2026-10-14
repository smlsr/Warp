"""1.6.20: `warp:sync` pushes the run state to the repo on request."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_merge_options import Origin  # noqa: E402
from test_isolation import git  # noqa: E402

import inbound  # noqa: E402
import orchestrator  # noqa: E402


class SyncTest(Origin):
    def setUp(self):
        super().setUp()
        tickets = [dict(self.ticket("WE-01", status="merged", pr={"url": "https://github.com/smlsr/WarpDemo/pull/24"}), summary="Editor scaffold", hours=1),
                   dict(self.ticket("WE-02", status="review"), summary="Edit store", hours=1), dict(self.ticket("WE-03", status="queued"), summary="Phase menu", hours=1)]
        self.path = self.beam(tickets, baseBranch="main", publishOnChange=False)
        (self.repo / ".warp" / "config.yaml").write_text("messenger: slack\nslackChannel: warp-demo\n")

    def test_it_parses_and_every_run_answers_it(self):
        parsed = inbound.parse("warp:sync")
        self.assertEqual((parsed["ok"], parsed["verb"]), (True, "sync"))
        self.assertEqual(inbound.parse("warp:sync edit")["addr"], "edit")
        decision = inbound.plan_command({}, parsed)
        self.assertEqual(decision["action"], "sync")
        self.assertTrue(decision["ack"].startswith("Received warp:sync. Pushing the run state"))
        self.assertIn("warp:sync", inbound.ACCEPTED_FORMS)

    def test_sync_pushes_the_state_even_with_publish_on_change_off_and_says_where(self):
        with mock.patch.dict(os.environ, {"WARP_PUBLISH": "1"}):
            out = inbound.apply_command(self.path, "warp:sync", by="Shawn Lewis", source="slack")
        text = out.get("detail") or ""
        self.assertIn("action: sync", text)
        self.assertIn("state: board and status refreshed", text)
        self.assertIn("state: published (warp:sync from Shawn Lewis)", text)
        sha = git(self.bare, "rev-parse", "--short", "main").stdout.strip()
        self.assertIn("herald: Run state pushed to main at %s (.warp/): 1 merged, 1 queued, 1 review." % sha, text)
        self.assertIn("herald: post .warp/notify-pass.json to slack warp-demo", text)
        published = json.loads(git(self.bare, "show", "main:.warp/beam.json").stdout)
        self.assertEqual(published["tickets"]["WE-01"]["status"], "merged")
        self.assertEqual(git(self.bare, "show", "main:.warp/BOARD.md").returncode, 0)

    def test_a_failed_push_is_said_in_the_channel(self):
        with mock.patch.dict(os.environ, {"WARP_PUBLISH": "0"}):
            lines = orchestrator.sync_state(self.path, by="Shawn Lewis")
        self.assertIn("state: not published (WARP_PUBLISH=0)", lines)
        self.assertEqual(lines[-1], "herald: warp:sync could not push the run state: not published (WARP_PUBLISH=0).")


if __name__ == "__main__":
    unittest.main()
