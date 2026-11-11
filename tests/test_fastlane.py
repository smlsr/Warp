"""1.7.25: clean pull requests merge as soon as CI is green and Bugbot has no open threads; every other pull request
rebases, passes local CI, and pushes once, all at the same time."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import bugbot  # noqa: E402
import fastlane  # noqa: E402
import pipeline  # noqa: E402
from test_merges_per_pass import NOW, beam, merged_ids, ready  # noqa: E402


def reviewing(tid, rank, **pr):
    t = ready(tid, rank)
    t["phase"] = "reviewing"
    t["pr"].update(pr)
    return t


class BehindTest(unittest.TestCase):
    def test_behind_is_not_a_conflict_and_a_behind_ready_pr_merges(self):
        t = ready("A", 1)
        t["pr"].update(behind=True, mergeStateStatus="BEHIND")
        self.assertFalse(pipeline._conflicted(t, {}))
        self.assertTrue(pipeline._conflicted(t, {"fastMerge": False}), "1.7.24 behaviour when off")
        data = beam([t, ready("B", 2, pr=dict(ready("B", 2)["pr"], behind=True))])
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertEqual(merged_ids(lines), ["A", "B"], lines)
        self.assertFalse(any("rebase" in l for l in lines), lines)

    def test_a_real_conflict_rebases_at_once_in_one_push_with_every_fix(self):
        t = reviewing("C", 3, bugbot="pending", bugbotSha="h3", conflict=["src/c.py"], mergeStateStatus="DIRTY",
                      ci="red", ciSha="h3", rollup="red", rollupSha="h3", checksPending=2)
        t["pr"]["failedChecks"] = ["lint"]
        data = beam([t])
        lines = pipeline.advance(data, {}, now=NOW)
        text = "\n".join(lines)
        self.assertIn("fixer: C rebase", text)
        self.assertIn("push once: one push for the merge, every conflict and every fix together", text)
        self.assertIn("run the local CI command", text)
        self.assertNotIn("bugbot: wait C", text)
        self.assertNotIn("waits for the whole batch", text)
        self.assertIn("ci", t["fixCounts"])
        self.assertIn("conflict", t["fixCounts"])


class BugbotThreadsTest(unittest.TestCase):
    def tearDown(self):
        bugbot.THREADS = None

    def test_reviewed_before_and_every_thread_addressed_is_clean_now(self):
        held = reviewing("D0", 3, bugbot="pending", bugbotSha="h3")
        data = beam([held], bugbotRequired=True)
        bugbot.THREADS = lambda root, url: []
        self.assertEqual(merged_ids(pipeline.advance(data, {}, now=NOW)), [], "never reviewed by Bugbot: it waits")
        t = reviewing("D", 4, bugbot="pending", bugbotSha="h4", bugbotEverSha="h0")
        data = beam([t], bugbotRequired=True)
        bugbot.THREADS = lambda root, url: [{"id": 1, "line": "x", "resolved": True}, {"id": 2, "line": "y", "replied": True}]
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertIn("bugbot: clear D on h4: all 2 Bugbot threads addressed; it merges when CI is green (fastMerge)", lines)
        self.assertEqual(merged_ids(lines), ["D"], lines)

    def test_an_open_thread_or_no_earlier_review_waits(self):
        bugbot.THREADS = lambda root, url: [{"id": 1, "line": "x", "resolved": False}]
        t = reviewing("E", 5, bugbot="pending", bugbotSha="h5", bugbotEverSha="h0")
        self.assertFalse(fastlane.note_bugbot_clear({"tickets": {}}, t, {}))
        self.assertEqual(t["pr"]["bugbotOpenThreads"], 1)
        bugbot.THREADS = lambda root, url: []
        never = reviewing("F", 6, bugbot="pending", bugbotSha="h6")
        self.assertFalse(fastlane.note_bugbot_clear({"tickets": {}}, never, {}), "Bugbot never reviewed: the ladder runs")

    def test_a_pass_or_fail_is_remembered(self):
        pr = {"headSha": "h9"}
        fastlane.remember_review(pr, "pending", "h9")
        self.assertNotIn("bugbotEverSha", pr)
        fastlane.remember_review(pr, "fail", "h8")
        self.assertEqual(pr["bugbotEverSha"], "h8")


class MergeTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=tmp, check=True)
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "config.yaml").write_text("gitProvider: github\n")
        self.path = tmp / ".warp" / "beam.json"
        self.updates = []
        import ghrest

        real = ghrest.update_branch
        self.addCleanup(setattr, ghrest, "update_branch", real)
        self.answer = (True, "")
        ghrest.update_branch = lambda root, url, head="", runner=None: (self.updates.append((url, head)) or self.answer)

    def run_one(self, refusal):
        data = beam([ready("G", 7)])
        data["_live"] = True
        lines = pipeline.advance(data, {}, beam_path=self.path, now=NOW)
        out = pipeline.run_merges(data, self.path, lines, merger=lambda root, n, cfg: ("rejected", refusal))
        return data["tickets"]["G"], out

    def test_ci_green_skips_the_local_merge_check(self):
        data = beam([ready("H", 8)])
        data["_live"] = True
        lines = pipeline.advance(data, {}, beam_path=self.path, now=NOW)
        out = pipeline.run_merges(data, self.path, lines, merger=lambda root, n, cfg: ("merged", "abc1234"))
        self.assertIn("merge: check skipped H: CI is green on h8 (fastMerge)", out)
        self.assertEqual(merged_ids(out), ["H"])

    def test_must_be_up_to_date_asks_github_to_update_the_branch(self):
        t, out = self.run_one("Required status check expected; Head branch is not up to date with the base branch")
        self.assertEqual(self.updates, [("https://github.com/o/r/pull/7", "h7")])
        self.assertEqual((t["status"], t["phase"]), ("review", "ready"))
        self.assertTrue(any("GitHub is updating the branch (no agent)" in l for l in out), out)
        self.assertFalse(any(l.startswith("merge: MUST DO G") for l in out), out)

    def test_an_update_github_cannot_make_is_a_rebase_round(self):
        self.answer = (False, "merge conflict between base and head")
        t, out = self.run_one("Head branch is not up to date with the base branch")
        self.assertTrue(t["pr"]["conflict"])
        self.assertIn("fixer: G rebase", out)


if __name__ == "__main__":
    unittest.main()
