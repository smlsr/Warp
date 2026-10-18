"""1.6.20: mergeMethod, deleteBranchOnMerge (off by default), and the run state published on a merge and a start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_isolation import GitRepo, git, run  # noqa: E402

import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402

SHA = "579fb1873f14055f8397c9ffac21b6c8109b2f4b"
URL = "https://github.com/smlsr/WarpDemo/pull/4"


class MethodTest(unittest.TestCase):
    def test_the_method_is_read_from_the_config(self):
        self.assertEqual(provider.merge_method({}), "merge")
        self.assertEqual(provider.merge_method({"mergeMethod": "Squash"}), "squash")
        self.assertEqual(provider.merge_method({"mergeMethod": "rebase"}), "rebase")
        self.assertEqual(provider.merge_method({"mergeMethod": "fast-forward"}), "merge")
        self.assertFalse(provider.delete_on_merge({}))
        self.assertTrue(provider.delete_on_merge({"deleteBranchOnMerge": "true"}))

    def test_gh_gets_the_configured_method(self):
        calls = []

        def fake(cmd, root):
            calls.append(cmd)
            out = json.dumps({"state": "MERGED", "mergeCommit": {"oid": "c" * 40}}) if cmd[:3] == ["gh", "pr", "view"] else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with mock.patch.object(provider, "_run", fake), mock.patch.object(provider, "resolve", lambda root, cfg: {"provider": "github"}), \
                mock.patch.object(provider, "detect_merge_queue", lambda root, cfg: None), mock.patch.object(provider.shutil, "which", lambda name: "/usr/bin/gh"):
            outcome = provider.merge_connected(Path("."), "24", {})
            squashed = provider.merge_connected(Path("."), "25", {"mergeMethod": "squash"})
        self.assertEqual(outcome, ("merged", "c" * 40))
        self.assertIn(["gh", "pr", "merge", "24", "--merge"], calls)  # the default
        self.assertEqual(squashed[0], "merged")
        self.assertIn(["gh", "pr", "merge", "25", "--squash"], calls)


class Origin(GitRepo):
    def setUp(self):
        super().setUp()
        self.bare = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.bare))
        git(self.repo, "remote", "add", "origin", str(self.bare))
        self.assertEqual(git(self.repo, "push", "-u", "origin", "main").returncode, 0)

    def branch(self, name, text="work\n"):
        git(self.repo, "checkout", "-q", "-b", name)
        (self.repo / "work.txt").write_text(text)
        git(self.repo, "add", "work.txt")
        git(self.repo, "commit", "-q", "-m", "work on %s" % name)
        git(self.repo, "push", "-q", "origin", name)
        git(self.repo, "checkout", "-q", "main")

    def on_origin(self, name):
        return git(self.bare, "rev-parse", "--verify", "-q", "refs/heads/%s" % name).returncode == 0


class DeleteTest(Origin):
    def test_a_merged_branch_is_deleted_on_origin_and_here_but_never_the_base(self):
        self.branch("warp/edit/WE-01-WAR-63")
        self.assertEqual(provider.delete_merged_branch(self.repo, "warp/edit/WE-01-WAR-63", "main"), (True, ""))
        self.assertFalse(self.on_origin("warp/edit/WE-01-WAR-63"))
        self.assertNotEqual(git(self.repo, "rev-parse", "--verify", "-q", "refs/heads/warp/edit/WE-01-WAR-63").returncode, 0)
        # Already gone counts as deleted.
        self.assertTrue(provider.delete_merged_branch(self.repo, "warp/edit/WE-01-WAR-63", "main")[0])
        self.assertEqual(provider.delete_merged_branch(self.repo, "main", "main"), (False, "no ticket branch to delete"))
        self.assertTrue(self.on_origin("main"))

    def test_after_merges_is_off_by_default_and_deletes_once_when_on(self):
        self.branch("warp/edit/WE-03-WAR-65")
        ticket = {"id": "WE-03", "status": "merged", "branch": "warp/edit/WE-03-WAR-65", "pr": {"url": URL}}
        path = self.beam([ticket])
        data = json.loads(path.read_text())
        lines = ["merge: WE-03 merged"]
        self.assertEqual(pipeline.after_merges(data, path, lines), [])
        self.assertTrue(self.on_origin("warp/edit/WE-03-WAR-65"))
        data["config"]["deleteBranchOnMerge"] = True
        self.assertEqual(pipeline.after_merges(data, path, lines), ["merge: branch deleted WE-03 warp/edit/WE-03-WAR-65"])
        self.assertFalse(self.on_origin("warp/edit/WE-03-WAR-65"))
        self.assertTrue(data["tickets"]["WE-03"]["pr"]["branchDeleted"])
        self.assertEqual(pipeline.after_merges(data, path, lines), [])


class LocalMergeTest(Origin):
    def test_merge_makes_a_merge_commit_and_squash_does_not(self):
        self.branch("warp/T-1")
        self.beam([{"id": "T-1", "status": "review", "pr": {}}])
        ok, _ = provider.merge_local(self.repo, "warp/T-1", "main", "[T-1] work")  # the default
        self.assertTrue(ok)
        parents = git(self.repo, "rev-list", "--parents", "-n", "1", "main").stdout.split()
        self.assertEqual(len(parents), 3, parents)  # the commit and its two parents
        self.branch("warp/T-2", "more\n")
        ok, _ = provider.merge_local(self.repo, "warp/T-2", "main", "[T-2] work", method="squash")
        self.assertTrue(ok)
        self.assertEqual(len(git(self.repo, "rev-list", "--parents", "-n", "1", "main").stdout.split()), 2)


class PublishTest(Origin):
    def ready(self):
        ticket = {"id": "WE-01", "status": "review", "phase": "ready", "autoMerge": True, "size": "S", "gate": "G0",
                  "module": "WarpEdit", "summary": "Editor scaffold", "hours": 4, "tokens": 0, "minutes": 0,
                  "branch": "warp/edit/WE-01-WAR-63", "locks": ["app/"], "acs": [{"id": "AC-1", "text": "builds"}],
                  "pr": {"url": URL, "headSha": SHA, "bugbot": "pass", "bugbotSha": SHA, "check": "green", "checkSha": SHA,
                         "ciNone": True, "acResults": {"AC-1": "pass"}, "acSha": SHA}}
        path = self.beam([ticket], gitProvider="github", bugbotRequired=True, baseBranch="main")
        data = json.loads(path.read_text())
        data["gates"] = [{"key": "G0", "members": ["WE-01"], "status": "pending", "blocking": True}]
        path.write_text(json.dumps(data))
        return path

    def supervise(self, path):
        snap = {"WE-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "mergeable": "MERGEABLE",
                          "mergeStateStatus": "CLEAN", "prState": "OPEN"}}
        with mock.patch.object(pipeline, "live_provider", lambda data, p: snap), \
                mock.patch.object(provider, "merge_connected", lambda root, number, cfg=None: ("merged", "d" * 40)), \
                mock.patch.dict(os.environ, {"WARP_PUBLISH": "1"}):
            import ticket_state

            with mock.patch.object(ticket_state, "_default_checks", lambda root, ticket: None):
                return orchestrator.supervise(path, now="2026-10-14T04:11:53Z")

    def test_a_merge_publishes_the_run_state_to_origin(self):
        path = self.ready()
        lines = self.supervise(path)
        self.assertIn("merge: WE-01 merged", lines)
        self.assertIn("state: published (merged WE-01)", lines)
        published = git(self.bare, "show", "main:.warp/beam.json")
        self.assertEqual(published.returncode, 0, lines)
        self.assertEqual(json.loads(published.stdout)["tickets"]["WE-01"]["status"], "merged")
        # The board and status went out as of the merge, not as of an earlier pass.
        self.assertIn("WE-01", git(self.bare, "show", "main:.warp/BOARD.md").stdout)
        status = json.loads(git(self.bare, "show", "main:.warp/status.json").stdout)
        self.assertIn("WE-01", json.dumps(status))

    def test_publish_on_change_false_leaves_it_to_the_parent(self):
        path = self.ready()
        data = json.loads(path.read_text())
        data["config"]["publishOnChange"] = False
        path.write_text(json.dumps(data))
        lines = self.supervise(path)
        self.assertIn("merge: WE-01 merged", lines)
        self.assertFalse(any(line.startswith("state: published") for line in lines), lines)
        self.assertNotEqual(git(self.bare, "show", "main:.warp/beam.json").returncode, 0)

    def launch(self, **config):
        self.beam([self.ticket(status="claimed", agent="shuttle-T-1")], runner="cloud", baseBranch="main", launch="worktree",
                  subagentVm=False, **config)
        return run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo, env=dict(os.environ, WARP_PUBLISH="1"))

    def test_a_new_ticket_branch_does_not_push_by_default(self):
        launched = self.launch()
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn("created: branch warp/T-1", launched.stdout)
        self.assertNotIn("state: published", launched.stdout)
        self.assertNotEqual(git(self.bare, "show", "main:.warp/beam.json").returncode, 0)
        # The live board still shows it: the state is current here, and the next merge or warp:sync carries it.
        self.assertTrue((self.repo / ".warp" / "beam.json").is_file())

    def test_push_state_new_publishes_it_in_progress(self):
        launched = self.launch(pushStateNew=True)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn("state: published (started T-1 on warp/T-1)", launched.stdout)
        published = json.loads(git(self.bare, "show", "main:.warp/beam.json").stdout)
        self.assertEqual(published["tickets"]["T-1"]["branch"], "warp/T-1")
        self.assertEqual(git(self.bare, "show", "main:.warp/BOARD.md").returncode, 0)  # refreshed and published with it
        # The prompt still comes last.
        self.assertLess(launched.stdout.find("state: published"), launched.stdout.find(orchestrator.PROMPT_MARK))
        again = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo, env=dict(os.environ, WARP_PUBLISH="1"))
        self.assertNotIn("state: published", again.stdout)  # a reused branch is not a new start

if __name__ == "__main__":
    unittest.main()
