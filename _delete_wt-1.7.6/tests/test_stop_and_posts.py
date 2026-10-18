"""1.6.18: a bare warp:stop ignores finished runs, a stopped pass says so, and every pass's news is posted."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_bugbot_heal import SHA, URL, Repo, row  # noqa: E402
from test_runs import RepoCase, ticket  # noqa: E402

import inbound  # noqa: E402
import notify  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import ticket_state  # noqa: E402


class BareStopTest(RepoCase):
    """The edit run's channel got `warp:stop`; the view run was finished (20 of 20 merged)."""

    def setUp(self):
        super().setUp()
        self.run_published(self.repo, "view", {"WV-01": ticket("WV-01", status="merged"), "WV-02": ticket("WV-02", status="merged")})
        self.mine = self.second_checkout()
        self.write_config(self.mine, run="edit")
        self.write_beam(self.mine, {"WE-01": ticket("WE-01", status="review")}, run="edit")
        self.path = self.mine / ".warp" / "beam.json"

    def test_a_finished_run_does_not_make_a_bare_stop_ambiguous(self):
        out = inbound._decide(self.path, "warp:stop")
        self.assertFalse(out["ignored"])
        self.assertEqual(out["decision"]["action"], "stop", out["decision"].get("ack"))

    def test_an_open_run_still_does(self):
        self.run_published(self.repo, "view", {"WV-01": ticket("WV-01", status="merged"), "WV-02": ticket("WV-02", status="coding")})
        mine = self.second_checkout("three")
        self.write_config(mine, run="edit")
        self.write_beam(mine, {"WE-01": ticket("WE-01", status="review")}, run="edit")
        out = inbound._decide(mine / ".warp" / "beam.json", "warp:stop")
        self.assertEqual(out["decision"]["action"], "none")
        self.assertIn("more than one run (edit, view)", out["decision"]["ack"])


class StoppedPassTest(Repo):
    def test_a_stopped_run_says_start_nothing(self):
        data = self.write([row("WE-01")])
        data.update(runState="stopped", paused=True, stoppedAt="2026-10-14T03:27:05Z", run="edit")
        self.beam_path.write_text(json.dumps(data))
        lines = orchestrator.supervise(self.beam_path, provider={}, now="2026-10-14T03:28:00Z")
        self.assertIn("run: stopped at 2026-10-14T03:27:05Z. Start nothing: no Shuttle, no listener, no resume. "
                      "Run parent-exit and end the turn; only /warp-resume or warp:resume edit starts it again.", lines)
        self.assertFalse(any(line.startswith("start ") for line in lines), lines)


class PassPostTest(Repo):
    """WE-01's claim, code done, pull request and Bugbot lines were printed and never posted."""

    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp-demo\nbugbotRequired: true\nmaxAgents: 4\n")
        self.payload = self.warp / "notify-pass.json"

    def test_a_pass_writes_its_news_as_one_payload_to_post(self):
        lines = ["run: pass 3 at 02:50:44Z", "start WE-01 checkout=worktree step=implement",
                 "herald: WE-01 claimed: Editor scaffold (size S, auto-merge). A Shuttle is coding it.",
                 "herald: WE-01 pull request opened: %s" % URL]
        out = notify.pass_post(self.tmp, lines, 3)
        self.assertTrue(out[0].startswith("herald: post .warp/notify-pass.json to slack warp-demo"), out)
        self.assertTrue(out[1].startswith("herald: then run: python3 ") and out[1].endswith(" posted --root ."), out)
        body = json.loads(self.payload.read_text())
        self.assertEqual((body["kind"], body["action"], body["pass"]), ("pass", "post", 3))
        self.assertEqual(body["lines"], ["WE-01 claimed: Editor scaffold (size S, auto-merge). A Shuttle is coding it.",
                                         "WE-01 pull request opened: %s" % URL])
        self.assertIn("Run update (pass 3)", body["text"])
        # Posting instructions are not news; a pass with nothing to say leaves the payload alone.
        self.assertEqual(notify.pass_post(self.tmp, out + ["review: wait WE-01"], 4), [])

    def test_lines_nobody_marked_posted_are_carried_once_marked_they_are_not(self):
        notify.pass_post(self.tmp, ["herald: WE-01: code complete; the Shuttle finished and returned."], 5)
        notify.pass_post(self.tmp, ["herald: WE-01 Cursor Bugbot is reviewing PR #24 (d801c83)."], 6)
        self.assertEqual(json.loads(self.payload.read_text())["lines"],
                         ["WE-01: code complete; the Shuttle finished and returned.", "WE-01 Cursor Bugbot is reviewing PR #24 (d801c83)."])
        self.assertEqual(notify.mark_posted(self.tmp), "herald: marked posted (pass)")
        notify.pass_post(self.tmp, ["herald: WE-01 Cursor Bugbot finished PR #24: no issues (took 6m)."], 7)
        self.assertEqual(json.loads(self.payload.read_text())["lines"], ["WE-01 Cursor Bugbot finished PR #24: no issues (took 6m)."])

    def test_quiet_posts_only_what_needs_someone(self):
        self.config.write_text("messenger: slack\nslackChannel: warp-demo\nnotify: quiet\n")
        self.assertEqual(notify.pass_post(self.tmp, ["herald: WE-01: the Shuttle is coding."], 2), [])
        out = notify.pass_post(self.tmp, ["herald: WE-01: the Shuttle is coding.",
                                          "herald: WE-02 is ready and waits for approval. Reply warp:proceed WE-02 to merge it."], 3)
        self.assertTrue(out[0].startswith("herald: post "), out)
        self.assertEqual(json.loads(self.payload.read_text())["lines"], ["WE-02 is ready and waits for approval. Reply warp:proceed WE-02 to merge it."])

    def test_supervise_ends_with_the_post_for_a_claim(self):
        queued = row("WE-01", status="queued", phase=None, summary="Editor scaffold", size="S", branch="warp/edit/WE-01-WAR-63")
        queued["pr"] = {}
        self.write([queued])
        lines = orchestrator.supervise(self.beam_path, provider={}, now="2026-10-14T02:50:44Z")
        claim = [line for line in lines if line.startswith("herald: WE-01 claimed: Editor scaffold (size S, auto-merge; locks ")]
        self.assertEqual(len(claim), 1, lines)
        self.assertTrue(any(line.startswith("herald: post .warp/notify-pass.json to slack warp-demo") for line in lines), lines)
        self.assertIn(claim[0][len("herald: "):], json.loads(self.payload.read_text())["lines"])


class NewsLinesTest(Repo):
    def test_a_pull_request_is_announced_once_and_an_old_one_not_at_all(self):
        ticket_row = row("WE-01")
        ticket_row["pr"]["openedAt"] = "2026-10-14T02:56:25Z"
        lines = []
        pipeline.note_pr_announced(ticket_row, lines, now="2026-10-14T02:57:00Z")
        self.assertEqual(lines, ["herald: WE-01 pull request opened: %s" % URL])
        pipeline.note_pr_announced(ticket_row, lines, now="2026-10-14T02:58:00Z")
        self.assertEqual(len(lines), 1)
        old = row("WV-01")
        old["pr"]["openedAt"] = "2026-10-13T19:00:00Z"
        quiet = []
        pipeline.note_pr_announced(old, quiet, now="2026-10-14T02:57:00Z")
        self.assertEqual(quiet, [])
        self.assertEqual(old["pr"]["heraldUrl"], URL)

    def test_an_approval_wait_is_posted(self):
        ready = row("WE-02", autoMerge=False)
        lines = []
        pipeline._hold_or_queue(ready, lines)
        self.assertIn("herald: WE-02 is ready and waits for approval: %s. Reply warp:proceed WE-02 to merge it." % URL, lines)

    def test_a_ticket_folder_state_reads_as_a_sentence(self):
        lines = []
        ticket_state._herald(lines, set(), "WE-01", "done")
        self.assertEqual(lines, ["herald: WE-01: code complete; the Shuttle finished and returned."])


if __name__ == "__main__":
    unittest.main()
