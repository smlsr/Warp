"""1.7.9: up to maxMergesPerPass (default 3) merges in one pass, and every merge asks for its Jira move to Done in full
until it is recorded."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import jira_sync  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402

NOW = "2026-10-20T15:00:00Z"


def ready(tid, rank, **extra):
    row = {"id": tid, "status": "review", "phase": "ready", "locks": ["src/%s" % tid.lower()], "size": "S",
           "module": "app", "autoMerge": True, "jiraKey": "WAR-%d" % rank, "branch": "warp/%s" % tid, "rank": rank,
           "summary": tid, "plan": "p", "result": "r",
           "pr": {"url": "https://github.com/o/r/pull/%d" % rank, "headSha": "h%d" % rank, "rollup": "green",
                  "rollupSha": "h%d" % rank, "ci": "green", "ciSha": "h%d" % rank, "bugbot": "pass",
                  "bugbotSha": "h%d" % rank, "acResults": {"AC-1": "pass"}, "acSha": "h%d" % rank}}
    row.update(extra)
    return row


def beam(tickets, **config):
    cfg = {"maxAgents": 4, "bugbotRequired": False, "autoMergeSizes": ["S", "M"], "runner": "cloud",
           "launch": "worktree", "subagentVm": False}
    cfg.update(config)
    return {"version": 1, "runState": "running", "baseGreen": True, "gates": [], "config": cfg,
            "tickets": {t["id"]: t for t in tickets}}


def merged_ids(lines):
    return [line.split()[1] for line in lines if line.startswith("merge: ") and line.endswith(" merged")]


class PerPassTest(unittest.TestCase):
    def github_checkout(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=tmp, check=True)
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "config.yaml").write_text("gitProvider: github\n")
        return tmp / ".warp" / "beam.json"

    def test_three_ready_merges_in_one_pass_by_default_and_the_fourth_waits(self):
        data = beam([ready("A", 1), ready("B", 2), ready("C", 3), ready("D", 4)])
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertEqual(merged_ids(lines), ["A", "B", "C"], lines)
        self.assertIn("merge: 3 pull requests taken this pass (maxMergesPerPass 3)", lines)
        self.assertNotEqual(data["tickets"]["D"]["status"], "merged")
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertEqual(merged_ids(lines), ["D"])

    def test_the_cap_is_a_config_number(self):
        data = beam([ready("A", 1), ready("B", 2), ready("C", 3)], maxMergesPerPass=1)
        self.assertEqual(merged_ids(pipeline.advance(data, {}, now=NOW)), ["A"])
        data = beam([ready("A", 1), ready("B", 2), ready("C", 3)], maxMergesPerPass=5)
        self.assertEqual(merged_ids(pipeline.advance(data, {}, now=NOW)), ["A", "B", "C"])

    def test_a_live_pass_asks_for_three_and_run_merges_merges_each(self):
        path = self.github_checkout()
        data = beam([ready("A", 1), ready("B", 2), ready("C", 3), ready("D", 4)])
        data["_live"] = True
        lines = pipeline.advance(data, {}, beam_path=path, now=NOW)
        asked = [line.split()[2] for line in lines if line.startswith("merge: request ")]
        self.assertEqual(asked, ["A", "B", "C"])
        out = pipeline.run_merges(data, path, lines, merger=lambda root, n, cfg: ("merged", "abc1234"),
                                  checker=lambda root, t, cfg: ("skipped", "no check"))
        self.assertEqual(merged_ids(out), ["A", "B", "C"])

    def test_a_later_merge_refused_after_an_earlier_one_is_asked_again_not_a_must_do(self):
        path = self.github_checkout()
        data = beam([ready("A", 1), ready("B", 2)])
        data["_live"] = True
        lines = pipeline.advance(data, {}, beam_path=path, now=NOW)
        results = iter([("merged", "abc1234"), ("rejected", "Pull request is not mergeable: the base branch was modified")])
        out = pipeline.run_merges(data, path, lines, merger=lambda root, n, cfg: next(results),
                                  checker=lambda root, t, cfg: ("skipped", "no check"))
        self.assertEqual(merged_ids(out), ["A"])
        self.assertIn("merge: B not merged after 1 merge in this pass (Pull request is not mergeable: the base branch was "
                      "modified); it is asked again next pass", out)
        self.assertFalse(any(line.startswith("merge: MUST DO B") for line in out), out)
        b = data["tickets"]["B"]
        self.assertEqual((b["status"], b["phase"]), ("review", "ready"))
        self.assertNotIn("mergeRequestedSha", b["pr"])

    def test_while_main_is_red_the_base_fix_merges_first_and_green_prs_still_merge(self):
        fix = ready("FIX", 2, baseFix=True)
        data = beam([ready("A", 1), fix])
        data["baseGreen"] = False
        self.assertEqual([t["id"] for t in orchestrator.merge_queue(data)], ["FIX", "A"])
        self.assertEqual(merged_ids(pipeline.advance(data, {}, now=NOW)), ["FIX", "A"])


class JiraDoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("jiraProject: WAR\njiraMcp: atlassian\njiraKeyPrefixes: WAR\n")
        self.path = warp / "beam.json"

    def merge(self, data, tid):
        lines = []
        pipeline.finish_merge(data, data["tickets"][tid], lines, sha="abc", how="merged on the provider")
        self.path.write_text(json.dumps(data) + "\n")
        return lines

    def test_a_merge_asks_for_done_in_full_and_again_until_it_is_recorded(self):
        data = beam([ready("A", 1)])
        lines = self.merge(data, "A")
        self.assertIn("jira: MUST DO A (WAR-1) transition to Done.", lines)
        out = pipeline.jira_done_lines(data, self.path, lines)
        self.assertNotIn("jira: MUST DO A (WAR-1) transition to Done.", lines)  # replaced by the full one
        text = "\n".join(out)
        self.assertIn('jira: MUST DO A (WAR-1). Server "atlassian"', text)
        self.assertIn("transition", text)
        self.assertIn("jira_sync.py record", text)
        self.assertEqual(data["tickets"]["A"]["jira"]["doneAsks"], 1)
        todo = json.loads((self.path.parent / "jira-todo.json").read_text())
        self.assertEqual(todo["tickets"][0]["id"], "A")
        # Not recorded: asked again on the next passes, then one alarm, then a reminder.
        again = pipeline.jira_done_lines(data, self.path, [])
        self.assertTrue(again[0].startswith("jira: A merged and Jira is not Done yet (ask 2 of 3)"), again)
        pipeline.jira_done_lines(data, self.path, [])
        after = pipeline.jira_done_lines(data, self.path, [])
        self.assertTrue(after[0].startswith("herald: ALARM A (WAR-1) merged, and its Jira issue was not moved to Done after 3 asks"), after)
        self.assertTrue(after[1].startswith("jira: A merged, Jira still not Done. python3 "), after)
        self.assertFalse(any(line.startswith("herald:") for line in pipeline.jira_done_lines(data, self.path, [])))

    def test_a_recorded_done_ends_it(self):
        data = beam([ready("A", 1)])
        lines = self.merge(data, "A")
        pipeline.jira_done_lines(data, self.path, lines)
        self.path.write_text(json.dumps(data) + "\n")
        jira_sync.record(self.path, "A", "done", "moved", "In Review", "Done", None)
        data = json.loads(self.path.read_text())
        self.assertEqual(pipeline.jira_done_lines(data, self.path, []), [])

    def test_a_manual_merge_left_at_qa_ready_is_not_asked(self):
        (self.path.parent / "config.yaml").write_text("jiraProject: WAR\njiraKeyPrefixes: WAR\njiraDoneOnManualMerge: false\n")
        data = beam([ready("M", 1, autoMerge=False, size="L")], jiraDoneOnManualMerge=False)
        lines = self.merge(data, "M")
        self.assertEqual(pipeline.jira_done_lines(data, self.path, lines), [])


if __name__ == "__main__":
    unittest.main()
