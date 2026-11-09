"""1.7.19: GAP-REPORT.md merges by gap id; duplicate or stale rows are never pushed; a one-time cleanup.

1.7.15 merged the gap report line by line ("union"). A pull request that closed a row in place left the old open row
next to the new closed one. One merge of #1451 on Forge-QA added 23 bad rows: 5 duplicated gaps and 11 closed gaps
with a stale open copy.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import gapreport  # noqa: E402
import lane  # noqa: E402
import orchestrator  # noqa: E402

HEAD = "# Gaps\n\n| id | gap | status | lands in |\n|---|---|---|---|\n"
BASE = HEAD + ("| G-001 | Session timeout missing | open | E27-S03 |\n"
               "| G-002 | Export drops tenant filter | open | E27-S06 |\n"
               "| G-003 | Audit log misses role changes | open | E49-VT-J05 |\n")
# Main closed G-001 in place, and added G-004.
MAIN = HEAD + ("| G-001 | Session timeout missing | closed (E27-S03) | E27-S03 |\n"
               "| G-002 | Export drops tenant filter | open | E27-S06 |\n"
               "| G-003 | Audit log misses role changes | open | E49-VT-J05 |\n"
               "| G-004 | Retry never backs off | open | E50-X01 |\n")
# The branch (#1451) closed G-002 and reworded G-003 slightly, and added G-005.
BRANCH = HEAD + ("| G-001 | Session timeout missing | open | E27-S03 |\n"
                 "| G-002 | Export drops tenant filter | closed (E27-S06) | E27-S06 |\n"
                 "| G-003 | Audit log misses role changes for members | open | E49-VT-J05 |\n"
                 "| G-005 | Banner overflows on narrow screens | open | APP-06 |\n")


def rows(text):
    return [l for l in text.splitlines() if gapreport.row_id(l)]


class MergeTest(unittest.TestCase):
    def test_rows_merge_by_id(self):
        merged = gapreport.merge3(BASE, BRANCH, MAIN)
        ids = [gapreport.row_id(l) for l in rows(merged)]
        self.assertEqual(sorted(ids), ["G-001", "G-002", "G-003", "G-004", "G-005"])
        self.assertEqual(len(ids), 5)  # one row per gap
        by = {gapreport.row_id(l): l for l in rows(merged)}
        self.assertIn("closed (E27-S03)", by["G-001"])  # main's close survives the branch's unchanged copy
        self.assertIn("closed (E27-S06)", by["G-002"])
        self.assertIn("for members", by["G-003"])
        self.assertEqual(gapreport.problems(merged), [])

    def test_the_old_line_union_is_what_made_the_bad_rows(self):
        import subprocess as sp

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, text in (("b", BASE), ("o", BRANCH), ("t", MAIN)):
            (tmp / name).write_text(text)
        with open(tmp / "u", "w") as handle:
            sp.run(["git", "merge-file", "-p", "--union", str(tmp / "o"), str(tmp / "b"), str(tmp / "t")], stdout=handle)
        found = gapreport.problems((tmp / "u").read_text())
        self.assertTrue(any("stale open copy" in p for p in found), found)

    def test_both_sides_changing_one_cell_differently_is_a_conflict_unless_one_closes(self):
        ours = BASE.replace("| open | E27-S03 |", "| open | E27-S09 |")
        theirs = BASE.replace("| open | E27-S03 |", "| open | E27-S10 |")
        with self.assertRaises(gapreport.Conflict):
            gapreport.merge3(BASE, ours, theirs)
        theirs = BASE.replace("| open | E27-S03 |", "| done | E27-S03 |")
        merged = gapreport.merge3(BASE, BASE.replace("Session timeout missing", "Session timeout missing (web)"), theirs)
        self.assertIn("| G-001 | Session timeout missing (web) | done | E27-S03 |", merged)

    def test_a_row_deleted_on_one_side_and_untouched_on_the_other_is_deleted(self):
        ours = BASE.replace("| G-003 | Audit log misses role changes | open | E49-VT-J05 |\n", "")
        self.assertNotIn("G-003", gapreport.merge3(BASE, ours, BASE))


class CheckAndCleanupTest(unittest.TestCase):
    BAD = HEAD + ("| G-001 | Session timeout missing | open | E27-S03 |\n"
                  "| G-001 | Session timeout missing | closed (E27-S03) | E27-S03 |\n"
                  "| G-002 | Export drops tenant filter | open | E27-S06 |\n"
                  "| G-002 | Export drops tenant filter on page two | open | E27-S06 |\n"
                  "| G-G-G-003 | Audit log misses role changes | open | E49-VT-J05 |\n"
                  "| G-003 | Audit log misses role changes | closed | E49-VT-J05 |\n")

    def test_problems(self):
        found = gapreport.problems(self.BAD)
        self.assertTrue(any(p.startswith("G-001: a stale open copy of a closed gap") for p in found), found)
        self.assertTrue(any(p.startswith("G-002: a duplicated gap") for p in found), found)
        self.assertTrue(any("G-003 / G-G-G-003" in p for p in found), found)
        self.assertIn("conflict markers are left in the file", gapreport.problems("<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n"))

    def test_dedupe_keeps_the_closed_copy_then_the_newest(self):
        clean, removed = gapreport.dedupe(self.BAD)
        by = {gapreport.row_id(l): l for l in rows(clean)}
        self.assertIn("closed (E27-S03)", by["G-001"])
        self.assertIn("on page two", by["G-002"])
        self.assertEqual(len(removed), 2)
        self.assertEqual([gapreport.row_id(l) for l in rows(clean)][:2], ["G-001", "G-002"])  # kept in place

    def test_the_cli(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "GAP-REPORT.md"
        path.write_text(self.BAD)
        script = HERE.parent / "scripts" / "gapreport.py"
        self.assertEqual(subprocess.run([sys.executable, str(script), "check", str(path)], capture_output=True).returncode, 1)
        out = subprocess.run([sys.executable, str(script), "dedupe", str(path), "--write"], capture_output=True, text=True)
        self.assertIn("removed 2 duplicate or stale rows", out.stdout)
        self.assertNotIn("G-001 | Session timeout missing | open", path.read_text())

    def test_the_rebase_resolver_merges_by_id_too(self):
        res = orchestrator.resolve_file("docs/spec/GAP-REPORT.md", BASE, BRANCH, MAIN, ["docs/spec/GAP-REPORT.md"])
        self.assertEqual(res["action"], "resolved")
        self.assertEqual(gapreport.problems(res["text"]), [])


def git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class LaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "docs" / "spec").mkdir(parents=True)
        self.report = self.repo / "docs" / "spec" / "GAP-REPORT.md"
        self.report.write_text(BASE)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "-b", "pr-1451")
        self.report.write_text(BRANCH)
        git(self.repo, "commit", "-qam", "branch")
        git(self.repo, "push", "-q", "origin", "pr-1451")
        git(self.repo, "checkout", "-q", "main")
        self.report.write_text(MAIN)
        git(self.repo, "commit", "-qam", "main")
        git(self.repo, "push", "-q", "origin", "main")
        self.pushes = []

    def run_git(self, cmd, cwd=None):
        if cmd[:2] == ["git", "push"]:
            self.pushes.append(cmd)
        return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *cmd[1:]],
                              cwd=cwd, capture_output=True, text=True)

    def test_the_lane_merges_the_report_by_id_and_pushes_one_clean_table(self):
        outcome, detail = lane.merge_into(self.repo, "pr-1451", "origin/main", run=self.run_git)
        self.assertEqual(outcome, "merged", detail)
        self.assertEqual(len(self.pushes), 1)
        git(self.repo, "fetch", "-q", "origin")
        text = git(self.repo, "show", "origin/pr-1451:docs/spec/GAP-REPORT.md")
        self.assertEqual(gapreport.problems(text), [])
        self.assertEqual(len(rows(text)), 5)

    def test_a_merge_that_would_duplicate_rows_is_refused_and_not_pushed(self):
        # Both sides add a G-006 with different text, far apart: git merges it cleanly into two rows.
        git(self.repo, "checkout", "-q", "pr-1451")
        self.report.write_text(BRANCH.replace(HEAD, HEAD + "| G-006 | Login page flickers | open | APP-09 |\n"))
        git(self.repo, "commit", "-qam", "branch adds G-006 at the top")
        git(self.repo, "push", "-q", "origin", "pr-1451")
        git(self.repo, "checkout", "-q", "main")
        self.report.write_text(MAIN + "| G-006 | Login page flickers on Safari | open | APP-09 |\n")
        git(self.repo, "commit", "-qam", "main adds G-006 at the bottom")
        git(self.repo, "push", "-q", "origin", "main")
        outcome, detail = lane.merge_into(self.repo, "pr-1451", "origin/main", run=self.run_git)
        self.assertIn(outcome, {"refused", "conflict"}, detail)
        self.assertEqual(self.pushes, [])

    def test_check_merge_ref_reads_githubs_merge(self):
        # Simulate refs/pull/7/merge on the origin: a merge that left both copies of G-001.
        git(self.repo, "checkout", "-q", "-b", "fake-merge", "origin/main")
        self.report.write_text(MAIN + "| G-001 | Session timeout missing | open | E27-S03 |\n")
        git(self.repo, "commit", "-qam", "bad merge")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/pull/7/merge")
        found = lane.check_merge_ref(self.repo, "7", run=self.run_git)
        self.assertTrue(any("G-001: a stale open copy" in p for p in found), found)
        git(self.repo, "push", "-q", "origin", "origin/main:refs/pull/8/merge")
        self.assertEqual(lane.check_merge_ref(self.repo, "8", run=self.run_git), [])


if __name__ == "__main__":
    unittest.main()
