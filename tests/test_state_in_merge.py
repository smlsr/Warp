"""1.6.21: the merge commit carries the run state; no separate `Warp state` commit follows a merge."""

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

from test_isolation import git  # noqa: E402
from test_merge_options import Origin  # noqa: E402

import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402

URL = "https://github.com/smlsr/WarpDemo/pull/24"
BRANCH = "warp/edit/WE-01-WAR-63"


class StateInMerge(Origin):
    def setUp(self):
        super().setUp()
        self.branch(BRANCH)
        self.head = git(self.repo, "rev-parse", BRANCH).stdout.strip()
        ticket = dict(self.ticket("WE-01", status="review", branch=BRANCH), summary="Editor scaffold", hours=4, phase="ready",
                      autoMerge=True, gate="G0", acs=[{"id": "AC-1", "text": "builds"}])
        ticket["pr"] = {"url": URL, "headSha": self.head, "bugbot": "pass", "bugbotSha": self.head, "check": "green",
                        "checkSha": self.head, "ciNone": True, "acResults": {"AC-1": "pass"}, "acSha": self.head}
        queued = dict(self.ticket("WE-02"), summary="Edit store", hours=2)
        self.path = self.beam([ticket, queued], gitProvider="github", baseBranch="main", bugbotRequired=True)
        self.env = mock.patch.dict(os.environ, {"WARP_PUBLISH": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.resolve = mock.patch.object(provider, "resolve", lambda root, cfg=None, gh=None: {"provider": "github"})
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.queue = mock.patch.object(provider, "detect_merge_queue", lambda root, cfg=None: None)
        self.queue.start()
        self.addCleanup(self.queue.stop)

    def merge(self, **cfg):
        data = json.loads(self.path.read_text())
        data["config"].update(cfg)
        return provider.merge_with_state(self.repo, self.path, data, data["tickets"]["WE-01"], "24", data["config"],
                                         confirm=lambda: "MERGED", wait=lambda s: None)

    def test_one_merge_commit_with_the_branch_and_the_run_state(self):
        outcome, sha = self.merge()
        self.assertEqual(outcome, "merged", sha)
        self.assertEqual(git(self.bare, "rev-parse", "main").stdout.strip(), sha)
        parents = git(self.bare, "rev-list", "--parents", "-n", "1", "main").stdout.split()
        self.assertEqual(parents[2], self.head)  # the reviewed head is a parent: GitHub marks the pull request merged
        self.assertEqual(git(self.bare, "show", "main:work.txt").stdout, "work\n")
        published = json.loads(git(self.bare, "show", "main:.warp/beam.json").stdout)
        self.assertEqual(published["tickets"]["WE-01"]["status"], "merged")
        self.assertEqual(published["tickets"]["WE-02"]["status"], "queued")
        self.assertIn("WE-01", git(self.bare, "show", "main:.warp/BOARD.md").stdout)
        message = git(self.bare, "log", "-1", "--format=%B", "main").stdout
        self.assertTrue(message.startswith("Merge pull request #24 from smlsr/%s" % BRANCH), message)
        self.assertIn("[WE-01] Editor scaffold", message)
        # The live beam is not marked merged until the provider confirms it.
        self.assertEqual(json.loads(self.path.read_text())["tickets"]["WE-01"]["status"], "review")

    def test_it_falls_back_to_the_provider_merge(self):
        self.assertEqual(self.merge(mergeMethod="squash")[0], "fallback")
        self.assertEqual(self.merge(stateInMerge=False)[0], "fallback")
        self.assertEqual(self.merge(publishOnChange=False)[0], "fallback")
        # The branch moved past the reviewed head.
        (self.repo / "late.txt").write_text("late\n")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "add", "late.txt")
        git(self.repo, "commit", "-q", "-m", "late")
        git(self.repo, "push", "-q", "origin", BRANCH)
        git(self.repo, "checkout", "-q", "main")
        outcome, why = self.merge()
        self.assertEqual(outcome, "fallback")
        self.assertIn("not the reviewed head", why)
        self.assertNotEqual(git(self.bare, "show", "main:work.txt").returncode, 0)  # nothing was pushed

    def test_a_refused_push_falls_back(self):
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'protected branch hook declined' >&2\nexit 1\n")
        hook.chmod(0o755)
        outcome, why = self.merge()
        self.assertEqual(outcome, "fallback")
        self.assertIn("refused", why)

    def test_a_conflict_outside_the_run_state_falls_back(self):
        (self.repo / "work.txt").write_text("main changed it\n")
        git(self.repo, "add", "work.txt")
        git(self.repo, "commit", "-q", "-m", "main edit")
        git(self.repo, "push", "-q", "origin", "main")
        outcome, why = self.merge()
        self.assertEqual(outcome, "fallback")
        self.assertIn("conflicts outside the run state: work.txt", why)

    def test_supervise_merges_with_the_state_and_makes_no_second_commit(self):
        snap = {"WE-01": {"headSha": self.head, "rollup": "absent", "checkCount": 0, "mergeable": "MERGEABLE",
                          "mergeStateStatus": "CLEAN", "prState": "OPEN"}}
        import ticket_state

        with mock.patch.object(pipeline, "live_provider", lambda data, p: snap), \
                mock.patch.object(provider, "merge_connected", lambda *a, **k: self.fail("merged through gh")), \
                mock.patch.object(provider, "_pr_state", lambda root, number: "MERGED"), \
                mock.patch.object(ticket_state, "_default_checks", lambda root, ticket: None):
            lines = orchestrator.supervise(self.path, now="2026-10-14T04:11:53Z")
        self.assertTrue(any(line.startswith("merge: WE-01 merged on the provider (a merge commit with the run state") for line in lines), lines)
        self.assertIn("merge: WE-01 merged", lines)
        self.assertIn("state: in the merge commit (WE-01)", lines)
        self.assertEqual(git(self.bare, "log", "-1", "--format=%s", "main").stdout.strip(), "Merge pull request #24 from smlsr/%s" % BRANCH)
        saved = json.loads(self.path.read_text())["tickets"]["WE-01"]
        self.assertEqual((saved["status"], saved["pr"]["mergeMethod"]), ("merged", "merge"))
        self.assertEqual(saved["pr"]["stateInMerge"], git(self.bare, "rev-parse", "main").stdout.strip())


if __name__ == "__main__":
    unittest.main()
