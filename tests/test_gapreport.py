"""1.7.20: GAP-REPORT.md is merged per row id, guarded before every lane push, and cleaned without losing real gaps.

The bug: the lane listed GAP-REPORT.md as a union file. A PR that edits a row in place (closes a gap, fills in
evidence) left the old row next to the new one. One lane merge of #1451 on Forge-QA added 17 identical duplicates and
8 stale open copies of closed gaps. Then 1.7.19's "one row per id" cleanup deleted 66 twins (different gaps that reuse
one number) along with the 23 real copies.
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

HEAD = "# Gaps\n\nThe open product gaps.\n\n| id | title | status | evidence |\n|---|---|---|---|\n"


def row(gid, title, status="open", evidence="", pad=" "):
    return "|%s%s%s|%s%s%s|%s%s%s|%s%s%s|" % (pad, gid, pad, pad, title, pad, pad, status, pad, pad, evidence, pad)


def table(*rows):
    return HEAD + "\n".join(rows) + "\n"


def ids(text):
    return [gapreport.row_id(l) for l in text.splitlines() if gapreport.row_id(l)]


def by_id(text):
    out = {}
    for line in text.splitlines():
        if gapreport.row_id(line):
            out.setdefault(gapreport.row_id(line), []).append(line)
    return out


class ResolveTest(unittest.TestCase):
    def test_main_closed_and_the_pr_still_has_the_unchanged_open_row(self):
        base = table(row("G-A", "Session timeout"), row("G-B", "Export filter"))
        main = table(row("G-A", "Session timeout", "closed", "E27-S03"), row("G-B", "Export filter"))
        pr = table(row("G-A", "Session timeout"), row("G-B", "Export filter", "open", "E27-S06 in review"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertEqual(len(by_id(merged)["G-A"]), 1)
        self.assertIn("closed", by_id(merged)["G-A"][0])
        self.assertIn("E27-S06 in review", by_id(merged)["G-B"][0])
        self.assertEqual(gapreport.gap_check(main, pr, merged), [])

    def test_both_sides_add_different_new_ids(self):
        base = table(row("G-A", "a"))
        main = table(row("G-A", "a"), row("G-M1", "from main"), row("G-M2", "from main too"))
        pr = table(row("G-A", "a"), row("G-P1", "from the PR"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertEqual(ids(merged), ["G-A", "G-M1", "G-M2", "G-P1"])  # main's order first, then the PR's new rows
        self.assertEqual(gapreport.gap_check(main, pr, merged), [])

    def test_the_pr_closes_a_gap_main_left_open(self):
        base = table(row("G-A", "a"), row("G-B", "b"))
        main = table(row("G-A", "a", "open", "main filled this"), row("G-B", "b"))
        pr = table(row("G-A", "a", "closed", ""), row("G-B", "b"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertEqual(len(by_id(merged)["G-A"]), 1)
        self.assertIn("closed", by_id(merged)["G-A"][0])  # the terminal row wins when both changed

    def test_both_changed_and_neither_terminal_is_mains_row_plus_cells_the_pr_filled(self):
        base = table(row("G-A", "a", "open", ""))
        main = table(row("G-A", "a (web)", "open", ""))
        pr = table(row("G-A", "a", "open", "E27-S09"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertEqual(by_id(merged)["G-A"], ["| G-A | a (web) | open | E27-S09 |"])

    def test_twins_on_main_closed_by_the_pr_stay_two(self):
        base = table(row("G-1034", "RUN-002 retry", "open", "E16-S03"), row("G-1034", "RUN-002 export", "open", "E48-S05"))
        main = base
        pr = table(row("G-1034", "RUN-002 retry", "closed", "E16-S03"), row("G-1034", "RUN-002 export", "closed", "E48-S05"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertEqual(len(by_id(merged)["G-1034"]), 2)
        self.assertTrue(all("closed" in r for r in by_id(merged)["G-1034"]))
        self.assertEqual(gapreport.gap_check(main, pr, merged), [])

    def test_rows_are_written_exactly_as_the_chosen_side_had_them(self):
        base = table(row("G-A", "a"))
        main = table(row("G-A", "a", pad="  "))  # spacing only
        pr = table(row("G-A", "a", "closed", "E1"))
        merged = gapreport.gap_resolve(base, main, pr)
        self.assertIn(row("G-A", "a", "closed", "E1"), merged)

    def test_a_conflict_in_the_text_outside_the_table_aborts(self):
        base = table(row("G-A", "a"))
        main = base.replace("The open product gaps.", "The open product gaps, by area.")
        pr = base.replace("The open product gaps.", "Open gaps (updated nightly).")
        with self.assertRaises(gapreport.Conflict):
            gapreport.gap_resolve(base, main, pr)
        # One side changing the prose merges.
        self.assertIn("by area", gapreport.gap_resolve(base, main, base))


class Pr1451Test(unittest.TestCase):
    """The #1451 shape: the PR reformatted the whole table (spacing only) while main closed 8 rows in it. The old
    union kept both sides of the one big hunk: 17 identical duplicates and 8 stale open copies."""

    def fixture(self):
        names = ["G-%03d" % n for n in range(1, 26)]
        base = table(*[row(g, "gap %s" % g) for g in names])
        main = table(*[row(g, "gap %s" % g, "closed" if i < 8 else "open", "E27-S%02d" % i if i < 8 else "")
                       for i, g in enumerate(names)])
        pr = table(*[row(g, "gap %s" % g, pad="  ") for g in names])
        return names, base, main, pr

    def union_output(self, base, main, pr):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, text in (("pr", pr), ("base", base), ("main", main)):
            (tmp / name).write_text(text)
        done = subprocess.run(["git", "merge-file", "-p", str(tmp / "pr"), str(tmp / "base"), str(tmp / "main")],
                              capture_output=True, text=True)
        return lane.union_resolve(done.stdout)

    def test_the_old_union_made_17_duplicates_and_8_stale_rows_and_the_check_flags_every_one(self):
        names, base, main, pr = self.fixture()
        old = self.union_output(base, main, pr)
        rows = by_id(old)
        twice = [g for g in names if len(rows[g]) == 2]
        self.assertEqual(len(twice), 25)
        stale = [g for g in twice if any("closed" in r for r in rows[g])]
        self.assertEqual((len(twice) - len(stale), len(stale)), (17, 8))
        found = gapreport.gap_check(main, pr, old)
        flagged = {f.split(":")[0] for f in found if "rows after the merge" in f}
        self.assertEqual(flagged, set(names))

    def test_gap_resolve_makes_none_of_them(self):
        names, base, main, pr = self.fixture()
        merged = gapreport.gap_resolve(base, main, pr)
        rows = by_id(merged)
        self.assertEqual([len(rows[g]) for g in names], [1] * 25)
        self.assertEqual(sum("closed" in rows[g][0] for g in names), 8)
        self.assertEqual(gapreport.gap_check(main, pr, merged), [])


class UnionTest(unittest.TestCase):
    def test_union_resolve_is_unchanged_for_changelog_fragments(self):
        text = "<<<<<<< ours\n- a\n=======\n- b\n>>>>>>> theirs\n"
        self.assertEqual(lane.union_resolve(text), "- a\n- b\n")
        self.assertTrue(lane.is_union("changelog.d/123.md"))
        self.assertFalse(lane.is_union("docs/spec/GAP-REPORT.md"))
        self.assertFalse(lane.is_union("GAP-REPORT.md"))
        self.assertTrue(lane.resolvable("docs/spec/GAP-REPORT.md"))
        self.assertTrue(lane.resolvable("docs/dev/testing/test-verify.md"))

    def test_test_verify_merges_section_by_section(self):
        base = "# Verify\n\n## T-1\nresult: pending\n\n## T-2\nresult: pending\n"
        main = "# Verify\n\n## T-1\nresult: pass (2026-11-08)\n\n## T-2\nresult: pending\n"
        pr = "# Verify\n\n## T-1\nresult: pending\n\n## T-2\nresult: fail, see #12\n\n## T-3\nresult: pending\n"
        merged = gapreport.section_resolve(base, main, pr)
        self.assertEqual(merged.count("## T-1"), 1)
        self.assertIn("result: pass (2026-11-08)", merged)
        self.assertNotIn("## T-1\nresult: pending", merged)
        self.assertIn("result: fail, see #12", merged)
        self.assertIn("## T-3", merged)
        clash = pr.replace("result: pending\n\n## T-2", "result: skip\n\n## T-2")
        with self.assertRaises(gapreport.Conflict):
            gapreport.section_resolve(base, main, clash)

    def test_the_rebase_resolver_uses_the_row_merge(self):
        base = table(row("G-A", "a"))
        main = table(row("G-A", "a", "closed", "E1"))
        res = orchestrator.resolve_file("docs/spec/GAP-REPORT.md", base, main, base, ["docs/spec/GAP-REPORT.md"])
        self.assertEqual(res["action"], "resolved")
        self.assertEqual(len(by_id(res["text"])["G-A"]), 1)


class CleanupTest(unittest.TestCase):
    TEXT = table(
        row("G-1034", "RUN-002 retry", "open", "E16-S03"),
        row("G-1034", "RUN-002 export", "open", "E48-S05"),     # a twin: different gap, same number
        row("G-1620", "ADM-22 roles", "open", "E30-S01"),
        row("G-1620", "ADM-22 roles", "open", "E30-S01", pad="  "),  # a true copy (spacing only)
        row("G-1700", "Banner overflow", "open", "APP-06"),
        row("G-1700", "Banner overflow", "closed", "APP-06"),   # a stale open copy above its closed row
        row("G-1800", "Login flicker", "open", "APP-09"),
        row("G-1800", "Login flicker", "closed", "APP-09 merged"),  # stale: same title
    )

    def test_classify_and_report(self):
        problems, twins = gapreport.report(self.TEXT)
        self.assertEqual(len(problems), 3)
        self.assertTrue(any(p.startswith("G-1620: a repeated row") for p in problems), problems)
        self.assertTrue(any(p.startswith("G-1700: a stale open copy") for p in problems), problems)
        self.assertTrue(any(p.startswith("G-1800: a stale open copy") for p in problems), problems)
        self.assertEqual(len(twins), 1)
        self.assertIn("G-1034: reused id, different gaps", twins[0])

    def test_a_twin_survives_and_copies_and_stale_rows_go(self):
        clean, removed, renamed = gapreport.dedupe(self.TEXT)
        rows = by_id(clean)
        self.assertEqual(len(rows["G-1034"]), 2)
        self.assertEqual([len(rows[g]) for g in ("G-1620", "G-1700", "G-1800")], [1, 1, 1])
        self.assertIn("closed", rows["G-1700"][0])
        self.assertIn("closed", rows["G-1800"][0])
        self.assertEqual((len(removed), renamed), (3, []))
        # Never a row with unique content.
        unique = {gapreport.norm(l) for l in self.TEXT.splitlines() if gapreport.row_id(l)}
        kept = {gapreport.norm(l) for l in clean.splitlines() if gapreport.row_id(l)}
        lost = unique - kept
        self.assertTrue(all(gapreport.norm_no_status(l) in {gapreport.norm_no_status(k) for k in clean.splitlines()} or
                            "Login flicker" in l for l in lost), lost)

    def test_rename_twins(self):
        clean, removed, renamed = gapreport.dedupe(self.TEXT, rename_twins=True)
        self.assertEqual([(o, n) for o, n, _l in renamed], [("G-1034", "G-1034-B")])
        self.assertIn("| G-1034-B | RUN-002 export |", clean)
        self.assertEqual(gapreport.report(clean), ([], []))

    def test_restore_puts_back_the_twins_1_7_19_deleted_and_nothing_else(self):
        # What 1.7.19's "one row per id" left: one row for each id.
        after_bad = table(row("G-1034", "RUN-002 retry", "open", "E16-S03"), row("G-1620", "ADM-22 roles", "open", "E30-S01"),
                          row("G-1700", "Banner overflow", "closed", "APP-06"), row("G-1800", "Login flicker", "closed", "APP-09 merged"))
        text, restored = gapreport.restore(after_bad, self.TEXT)
        self.assertEqual([gapreport.row_id(l) for l in restored], ["G-1034"])
        self.assertEqual(len(by_id(text)["G-1034"]), 2)
        self.assertEqual(gapreport.report(text)[0], [])

    def test_the_guard_ignores_twins_already_on_main(self):
        main = self.TEXT
        pr = self.TEXT.replace(row("G-1034", "RUN-002 retry", "open", "E16-S03"), row("G-1034", "RUN-002 retry", "closed", "E16-S03"))
        self.assertEqual(gapreport.gap_check(main, pr, pr), [])


def git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class RepoCase(unittest.TestCase):
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
        self.pushes = []

    def commit(self, text, message, branch=None):
        if branch:
            git(self.repo, "checkout", "-q", branch)
        self.report.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)
        git(self.repo, "push", "-q", "origin", "HEAD")

    def run_git(self, cmd, cwd=None):
        if cmd[:2] == ["git", "push"]:
            self.pushes.append(cmd)
        return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *cmd[1:]],
                              cwd=cwd, capture_output=True, text=True)


class LaneTest(RepoCase):
    def test_a_conflicted_report_is_merged_by_id_and_pushed_once(self):
        f = Pr1451Test()
        f.addCleanup = self.addCleanup
        names, base, main, pr = f.fixture()
        self.commit(base, "base")
        git(self.repo, "checkout", "-q", "-b", "pr-1451")
        self.commit(pr, "PR reformats the table")
        self.commit(main, "main closes 8", branch="main")
        outcome, detail = lane.merge_into(self.repo, "pr-1451", "origin/main", run=self.run_git)
        self.assertEqual(outcome, "merged", detail)
        self.assertEqual(len(self.pushes), 1)
        git(self.repo, "fetch", "-q", "origin")
        text = git(self.repo, "show", "origin/pr-1451:docs/spec/GAP-REPORT.md")
        self.assertEqual(len(ids(text)), 25)
        self.assertEqual(gapreport.report(text)[0], [])

    def test_a_merge_that_adds_rows_is_a_conflict_and_is_not_pushed(self):
        base = table(row("G-A", "a"), row("G-B", "b"), row("G-C", "c"), row("G-D", "d"))
        self.commit(base, "base")
        git(self.repo, "checkout", "-q", "-b", "pr-9")
        # Both sides add a G-E, far apart: git merges that cleanly into two rows for one id.
        self.commit(base.replace(row("G-A", "a"), row("G-E", "flicker") + "\n" + row("G-A", "a")), "PR adds G-E on top")
        self.commit(base + row("G-E", "flicker on Safari") + "\n", "main adds G-E at the end", branch="main")
        outcome, detail = lane.merge_into(self.repo, "pr-9", "origin/main", run=self.run_git)
        self.assertEqual(outcome, "conflict", detail)
        self.assertIn("G-E: 2 rows after the merge", detail)
        self.assertEqual(self.pushes, [])

    def test_the_owners_head_skips_earlier_lane_commits(self):
        base = table(row("G-A", "a"))
        self.commit(base, "base")
        git(self.repo, "checkout", "-q", "-b", "pr-3")
        self.commit(table(row("G-A", "a", "closed", "E1")), "owner closes G-A")
        owner = git(self.repo, "rev-parse", "HEAD")
        self.report.write_text(table(row("G-A", "a", "closed", "E1"), row("G-A", "a")))
        git(self.repo, "-c", "user.name=Warp", "commit", "-qam", "lane merge")
        lane_head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(lane.owner_head(self.run_git, self.repo, lane_head), owner)

    def test_check_merge_ref_compares_with_main_and_the_pr_head(self):
        base = table(row("G-A", "a"))
        self.commit(base, "base")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/pull/7/head")
        self.commit(table(row("G-A", "a", "closed", "E1")), "main closes G-A")
        git(self.repo, "checkout", "-q", "-b", "fake")
        self.report.write_text(table(row("G-A", "a", "closed", "E1"), row("G-A", "a")))  # GitHub's union-ish result
        git(self.repo, "commit", "-qam", "merge ref")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/pull/7/merge")
        found = lane.check_merge_ref(self.repo, "7", run=self.run_git)
        self.assertTrue(any("G-A: 2 rows after the merge (main has 1, the PR has 1)" in f for f in found), found)
        git(self.repo, "push", "-q", "origin", "origin/main:refs/pull/8/merge", "origin/main:refs/pull/8/head")
        self.assertEqual(lane.check_merge_ref(self.repo, "8", run=self.run_git), [])


class CleanupCliTest(RepoCase):
    def test_gap_dedupe_refuses_on_main_and_writes_on_a_branch(self):
        self.commit(CleanupTest.TEXT, "report")
        script = HERE.parent / "scripts" / "lane.py"
        out = subprocess.run([sys.executable, str(script), "gap-dedupe", "--report", str(self.report), "--write"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refused: you are on main", out.stdout)
        git(self.repo, "checkout", "-q", "-b", "gap-cleanup")
        out = subprocess.run([sys.executable, str(script), "gap-dedupe", "--report", str(self.report), "--write"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("removed 3 repeated or stale rows", out.stdout)
        self.assertIn("kept: G-1034: reused id, different gaps", out.stdout)
        self.assertEqual(len(by_id(self.report.read_text())["G-1034"]), 2)
        out = subprocess.run([sys.executable, str(script), "gap-dedupe", "--report", str(self.report), "--write",
                              "--rename-twins"], capture_output=True, text=True)
        self.assertIn("rename a twin G-1034 -> G-1034-B", out.stdout)
        self.assertIn("update repo: docs/spec/GAP-REPORT.md", out.stdout)
        self.assertIn('jira: search labels = "warp:G-1034"', out.stdout)
        check = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "gapreport.py"), "check", str(self.report)],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout)


if __name__ == "__main__":
    unittest.main()
