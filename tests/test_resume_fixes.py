"""What a /warp-resume on WarpDemo showed: a dead session's listener lease, a review Shuttle's result
waiting for a return, and supervise not reading the ticket folders."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import inbound  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import scan  # noqa: E402
import ticket_state  # noqa: E402
from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402

SNAP = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                  "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}}


class LiveProvider:
    """supervise with no --provider, without gh: the pull request snapshot and checks are fixed."""

    def __enter__(self):
        self.saved = (pipeline.live_provider, ticket_state._default_checks, ticket_state._default_fetch)
        pipeline.live_provider = lambda data, path: SNAP
        ticket_state._default_checks = lambda root, ticket: None
        ticket_state._default_fetch = lambda root, branch: True
        return self

    def __exit__(self, *exc):
        pipeline.live_provider, ticket_state._default_checks, ticket_state._default_fetch = self.saved


class ListenerAfterResumeTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp\nbugbotTrigger: shuttle\n")

    def old_lease(self, **extra):
        data = self.load()
        data["listener"] = dict({"generation": 7, "holder": "listener", "state": "running", "agentId": "listener",
                                 "generationStartedAt": "2026-10-13T19:52:31Z", "pollStartedAt": "2026-10-13T19:52:31Z",
                                 "reads": 0, "returnReason": "", "awaitReturn": False}, **extra)
        self.beam_path.write_text(json.dumps(data))

    def resume(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            scan.set_run(self.beam_path, "running", "resume")
        return buf.getvalue()

    def test_resume_closes_the_previous_sessions_lease_and_the_next_pass_starts_one(self):
        self.write([row()], messenger="slack", slackChannel="warp")
        self.old_lease()
        out = self.resume()
        self.assertIn("listener: generation 7 belonged to the previous session and ended with it.", out)
        raw = self.load()["listener"]
        self.assertEqual((raw["holder"], raw["returnReason"]), ("", "orphaned"))
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-13T21:10:00Z"), ["listener: poll"])
        self.assertEqual(self.load()["listener"]["generation"], 8)
        self.assertEqual(self.load()["listener"]["session"], self.load()["parentSession"])

    def test_an_expired_lease_waiting_for_a_return_is_closed_too(self):
        self.write([row()], messenger="slack", slackChannel="warp")
        self.old_lease(holder="", awaitReturn=True, generation=8)
        self.resume()
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-13T21:10:00Z"), ["listener: poll"])

    def test_a_lease_from_another_session_is_closed_by_supervise(self):
        self.write([row()], messenger="slack", slackChannel="warp")
        data = self.load()
        data["parentSession"] = "new-session"
        self.beam_path.write_text(json.dumps(data))
        self.old_lease(session="old-session", generationStartedAt="2026-10-13T21:09:00Z")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-13T21:10:00Z"), ["listener: poll"])

    def test_a_listener_a_person_holds_is_left_alone(self):
        self.write([row()], messenger="slack", slackChannel="warp")
        self.old_lease(holder="chat")
        self.resume()
        self.assertEqual(self.load()["listener"]["holder"], "chat")


class ReviewShuttleResultTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("bugbotTrigger: shuttle\n")

    def test_supervise_reads_the_folder_and_a_recorded_review_ends_the_review_shuttle(self):
        ticket = row(checkout="worktree", worktree=".warp/worktrees/WV-01",
                     shuttle={"pending": True, "step": "review", "hadPr": True},
                     workerStartedAt="2026-10-13T21:10:00Z", lastSeenAt="2026-10-13T21:11:00Z")
        ticket["pr"].update({"check": "green", "checkSha": SHA, "bugbotReviewSha": SHA, "bugbotReviews": 1})
        self.write([ticket])
        with redirect_stdout(io.StringIO()):
            ticket_state.append_status(self.tmp, "WV-01", "review-pass", sha=SHA[:7], source="shuttle")
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T21:15:00Z")
        self.assertIn("herald: WV-01 Bugbot review by the Shuttle: pass.", lines)
        self.assertNotIn("shuttle: hold WV-01", lines)
        saved = self.load()["tickets"]["WV-01"]
        self.assertEqual(saved["pr"]["bugbot"], "pass")
        self.assertFalse(saved["shuttle"]["pending"])
        self.assertFalse(any(line.startswith("start WV-01 ") for line in lines), lines)

    def test_returned_for_a_review_step_is_the_recorded_review(self):
        ticket = row(shuttle={"pending": True, "step": "review"})
        self.assertFalse(pipeline._returned(ticket, {}))
        ticket["pr"]["bugbotRecordedSha"] = SHA
        self.assertTrue(pipeline._returned(ticket, {}))
        ticket["shuttle"]["step"] = "fix"
        self.assertFalse(pipeline._returned(ticket, {}))


if __name__ == "__main__":
    import unittest

    unittest.main()
