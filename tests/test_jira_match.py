"""Summary matching and External ID write-back. No live Jira calls."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jira_lookup  # noqa: E402
import jira_match  # noqa: E402
import jira_sync  # noqa: E402


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def issue(key, summary, status="To Do", category="new"):
    return {
        "key": key,
        "summary": summary,
        "status": {"name": status, "statusCategory": {"key": category, "name": status}},
    }


def ticket(tid, summary, **extra):
    row = {
        "id": tid,
        "summary": summary,
        "status": "queued",
        "size": "S",
        "jira": {"status": None, "lastCommentAt": None},
    }
    row.update(extra)
    return row


FIELDS = [{"id": "customfield_10050", "name": "External ID"}]
EDITMETA = {"fields": {"customfield_10050": {"name": "External ID"}}}


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraMcp: atlassian\n')
        self.write_beam([ticket("WV-01", "Build the widget!")])

    def write_beam(self, tickets):
        self.beam.write_text(json.dumps({"tickets": {row["id"]: row for row in tickets}, "config": {}}))

    def load(self):
        return json.loads(self.beam.read_text())

    def match(self, issues, **opts):
        data = {
            "issues": issues,
            "searches": [{"jql": "summary ~ x", "issues": issues, "isLast": True, "total": len(issues)}],
        }
        data.update(opts.pop("payload", {}))
        defaults = dict(
            only_id=None,
            include_all=False,
            chars=60,
            min_score=0.9,
            include_done=False,
            do_apply=False,
            yes=False,
        )
        defaults.update(opts)
        return jira_match.match_results(self.beam, data, **defaults)

    def test_exact_normalizes_case_punctuation_and_space(self):
        kind, score = jira_match.classify_pair("Build the widget!", "  build, the   widget ", 60, 0.9)
        self.assertEqual(kind, "exact")
        self.assertEqual(score, 1.0)
        text = self.match([issue("WAR-1", "build the widget")])
        self.assertIn("confirmed: WAR-1 (exact, 1.00, high)", text)
        self.assertIn("dry-run WV-01 -> WAR-1", text)
        self.assertFalse((self.warp / "jira-map.json").exists())
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))

    def test_prefix_of_sixty_characters_is_confirmed(self):
        stem = ("alpha " * 12).strip()
        self.assertGreaterEqual(len(jira_match.normalize_summary(stem)), 60)
        left = stem + " left tail"
        right = stem + " right tail"
        self.write_beam([ticket("WV-01", left)])
        kind, _score = jira_match.classify_pair(left, right, 60, 0.9)
        self.assertEqual(kind, "prefix")
        text = self.match([issue("WAR-1", right)])
        self.assertIn("confirmed: WAR-1 (prefix,", text)
        self.assertIn("medium", text)

    def test_fuzzy_is_a_proposal_until_apply_yes(self):
        left = "a b c d e f g h i j"
        right = "a b c d e f g h i j k"
        self.assertLess(len(left), 60)
        kind, score = jira_match.classify_pair(left, right, 60, 0.9)
        self.assertEqual(kind, "fuzzy")
        self.assertGreaterEqual(score, 0.9)
        self.write_beam([ticket("WV-01", left)])
        quiet = self.match([issue("WAR-1", right)])
        self.assertIn("proposal: WAR-1 (fuzzy,", quiet)
        self.assertIn("Not stored until --apply --yes", quiet)
        held = self.match([issue("WAR-1", right)], do_apply=True)
        self.assertIn("Pass --apply --yes", held)
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))
        stored = self.match([issue("WAR-1", right)], do_apply=True, yes=True)
        self.assertIn("summary, low", stored)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jiraKey"], "WAR-1")
        saved = json.loads((self.warp / "jira-map.json").read_text())["WV-01"]
        self.assertEqual(saved["key"], "WAR-1")
        self.assertEqual(saved["source"], "summary")
        self.assertEqual(saved["confidence"], "low")

    def test_ambiguous_exact_is_not_stored(self):
        text = self.match(
            [issue("WAR-1", "Build the widget"), issue("WAR-2", "build the widget!")],
            do_apply=True,
            yes=True,
        )
        self.assertIn("ambiguous: WAR-1, WAR-2", text)
        self.assertIn("Not stored", text)
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))
        self.assertFalse((self.warp / "jira-map.json").exists())

    def test_one_issue_claimed_by_two_tickets_is_not_stored(self):
        self.write_beam(
            [
                ticket("WV-01", "Build the widget"),
                ticket("WV-02", "Build the widget!"),
            ]
        )
        text = self.match([issue("WAR-1", "build the widget")], do_apply=True)
        self.assertIn("WAR-1 claimed by WV-01, WV-02", text)
        self.assertIn("ambiguous:", text)
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))
        self.assertIsNone(self.load()["tickets"]["WV-02"].get("jiraKey"))

    def test_already_mapped_issue_is_excluded(self):
        self.write_beam(
            [
                ticket("WV-01", "Build the widget"),
                ticket("WV-02", "Build the widget", jiraKey="WAR-1", jiraKeySource="export", jiraKeyConfidence="high"),
            ]
        )
        text = self.match([issue("WAR-1", "Build the widget")], do_apply=True)
        self.assertIn("WAR-1 already mapped to WV-02", text)
        self.assertIn("no match", text)
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))
        self.assertEqual(self.load()["tickets"]["WV-02"]["jiraKey"], "WAR-1")

    def test_done_is_skipped_unless_include_done(self):
        done = issue("WAR-1", "Build the widget", status="Done", category="done")
        skipped = self.match([done], do_apply=True)
        self.assertIn("WAR-1 is Done", skipped)
        self.assertIsNone(self.load()["tickets"]["WV-01"].get("jiraKey"))
        kept = self.match([done], include_done=True, do_apply=True)
        self.assertIn("confirmed: WAR-1 (exact, 1.00, high)", kept)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jiraKey"], "WAR-1")
        self.assertEqual(self.load()["tickets"]["WV-01"]["jiraKeyConfidence"], "high")

    def test_apply_stores_exact_and_prefix_and_leaves_manual(self):
        text = self.match([issue("WAR-1", "Build the widget!")], do_apply=True)
        self.assertIn("summary, high", text)
        saved = json.loads((self.warp / "jira-map.json").read_text())["WV-01"]
        self.assertEqual(saved["source"], "summary")
        self.assertEqual(saved["confidence"], "high")
        self.write_beam(
            [
                ticket(
                    "WV-01",
                    "Build the widget",
                    jiraKey="WAR-9",
                    jiraKeySource="manual",
                    jiraKeyForced=True,
                )
            ]
        )
        manual = self.match([issue("WAR-1", "Build the widget")], include_all=True, do_apply=True, yes=True)
        self.assertIn("manual key left as is", manual)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jiraKey"], "WAR-9")

    def test_truncated_search_is_named(self):
        text = self.match(
            [issue("WAR-1", "Build the widget")],
            payload={"searches": [{"jql": "summary ~ x", "issues": [], "isLast": False, "total": 400}], "truncated": True},
        )
        self.assertIn("search truncated", text)
        self.assertIn("summary ~", text)
        self.assertIn("2 pages of 50", text)

    def test_prepare_bounds_pages_and_probes_when_project_empty(self):
        text = jira_match.prepare_match(self.beam, only_id=None, include_all=False, chars=60, min_score=0.9, include_done=False)
        self.assertIn("searchJiraIssuesUsingJql", text)
        self.assertIn("maxResults 50", text)
        saved = json.loads((self.warp / "jira-match.json").read_text())
        self.assertEqual(saved["pageSize"], 50)
        self.assertEqual(saved["maxPages"], 2)
        self.assertIn("project = WAR AND summary ~", saved["queries"][0]["jql"])
        (self.warp / "config.yaml").write_text('jiraProject: ""\n')
        empty = jira_match.prepare_match(self.beam, only_id=None, include_all=False, chars=60, min_score=0.9, include_done=False)
        self.assertIn("getVisibleJiraProjects", empty)

    def test_check_mentions_summary_candidate_and_keeps_verify_link(self):
        self.match([issue("WAR-1", "Build the widget")])
        beam = self.load()
        cfg = jira_sync.settings(self.beam, beam)
        lines = jira_sync.link_lines(beam["tickets"]["WV-01"], cfg, self.beam)
        self.assertIn("verify --link", lines)
        self.assertIn("summary candidate: WAR-1 (exact 1.00)", lines)
        self.assertIn("jira_match.py --apply --id WV-01", lines)

    def test_from_jira_summary_reuses_normalized_exact_match(self):
        wanted = "Build the widget!"
        jql = jira_lookup.summary_jql(wanted, "WAR")
        client = jira_lookup.ReplayClient(
            {"searches": [{"jql": jql, "issues": [{"key": "WAR-6", "summary": "build, the widget"}]}]}
        )
        row = jira_lookup.resolve_ticket("WV-01", wanted, "WAR", "externalId", client, ["WAR"])
        self.assertEqual(row["outcome"], "proposed")
        self.assertEqual(row["source"], "summary")
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["key"], "WAR-6")


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraWriteExternalId: false\n')
        self.beam.write_text(
            json.dumps(
                {
                    "tickets": {
                        "WV-01": ticket(
                            "WV-01",
                            "Build the widget",
                            jiraKey="WAR-1",
                            jiraKeySource="summary",
                            jiraKeyConfidence="high",
                        )
                    },
                    "config": {},
                }
            )
        )

    def load(self):
        return json.loads(self.beam.read_text())

    def transcript(self, *, before="", result="updated", fields=None, editmeta=None):
        return {
            "fields": FIELDS if fields is None else fields,
            "editmeta": EDITMETA if editmeta is None else editmeta,
            "issues": [{"key": "WAR-1", "fields": {"customfield_10050": before}}],
            "edits": [{"id": "WV-01", "key": "WAR-1", "before": before, "result": result}],
        }

    def test_dry_run_shows_before_after_and_writes_nothing(self):
        text = jira_match.prepare_write(self.beam, [("WV-01", "WAR-1")], yes=False, force=False)
        self.assertIn("dry-run", text)
        self.assertIn("before (empty or not loaded) after WV-01", text)
        self.assertIn("editJiraIssue", text)
        self.assertIn("No comment is posted", text)
        self.assertIn("jiraWriteExternalId is false", text)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])

    def test_write_success_records_external_id(self):
        text = jira_match.record_write(self.beam, self.transcript(), yes=True, force=False)
        self.assertIn("before (empty) after WV-01. Recorded.", text)
        written = self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]
        self.assertEqual(written["value"], "WV-01")
        self.assertTrue(written["at"])
        self.assertNotIn("addComment", text)

    def test_missing_field_is_skipped(self):
        text = jira_match.record_write(
            self.beam,
            self.transcript(fields=[{"id": "summary", "name": "Summary"}], editmeta={"fields": {"summary": {"name": "Summary"}}}),
            yes=True,
            force=False,
        )
        self.assertIn("External ID field is missing. Skipped.", text)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])

    def test_read_only_field_is_skipped(self):
        text = jira_match.record_write(
            self.beam,
            self.transcript(editmeta={"fields": {"summary": {"name": "Summary"}}}),
            yes=True,
            force=False,
        )
        self.assertIn("read-only", text)
        self.assertIn("Nothing was written", text)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])

    def test_different_value_needs_force(self):
        blocked = jira_match.record_write(self.beam, self.transcript(before="ZZ-99"), yes=True, force=False)
        self.assertIn("before ZZ-99 after WV-01", blocked)
        self.assertIn("Not written", blocked)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])
        forced = jira_match.record_write(self.beam, self.transcript(before="ZZ-99"), yes=True, force=True)
        self.assertIn("Recorded.", forced)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["value"], "WV-01")

    def test_second_run_is_idempotent(self):
        first = jira_match.record_write(self.beam, self.transcript(), yes=True, force=False)
        self.assertIn("Recorded.", first)
        stamp = self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["at"]
        second = jira_match.record_write(self.beam, self.transcript(result="updated"), yes=True, force=False)
        self.assertIn("already written", second)
        self.assertIn("Not sent again", second)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["at"], stamp)

    def test_scan_hint_follows_config(self):
        quiet = jira_sync.record_resolved(self.beam, "WV-01", "WAR-2", source="external")
        self.assertNotIn("editJiraIssue", quiet)
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraWriteExternalId: true\n')
        self.beam.write_text(
            json.dumps({"tickets": {"WV-01": ticket("WV-01", "Build the widget")}, "config": {}})
        )
        loud = jira_sync.record_resolved(self.beam, "WV-01", "WAR-2", source="external")
        self.assertIn("jiraWriteExternalId is on", loud)
        self.assertIn("editJiraIssue", loud)
        self.assertIn("does not post a comment", loud)

    def test_cli_apply_help_and_write_skip(self):
        self.beam.write_text(json.dumps({"tickets": {"WV-01": ticket("WV-01", "Build the widget!")}, "config": {}}))
        results = self.tmp / "candidates.json"
        results.write_text(json.dumps({"issues": [issue("WAR-1", "Build the widget!")]}))
        dry = run(
            "jira_match.py",
            "--beam",
            str(self.beam),
            "--results",
            str(results),
            "--apply",
            "--dry-run",
            cwd=self.tmp,
        )
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("Not written", dry.stdout)
        self.assertFalse((self.warp / "jira-map.json").exists())
        stored = run(
            "jira_match.py",
            "--beam",
            str(self.beam),
            "--results",
            str(results),
            "--apply",
            cwd=self.tmp,
        )
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jiraKeySource"], "summary")
        help_text = run("jira_match.py", "?", cwd=self.tmp)
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        for needle in (
            "--chars",
            "--min-score",
            "--include-done",
            "--write-external-id",
            "--set-external-id",
            "--force-external-id",
            "editJiraIssue",
            "getJiraIssueEditmeta",
        ):
            self.assertIn(needle, help_text.stdout)
        bad = run("jira_match.py", "--set-external-id", "not-a-pair", cwd=self.tmp)
        self.assertEqual(bad.returncode, 2)
        edits = self.tmp / "edits.json"
        edits.write_text(
            json.dumps(
                self.transcript(fields=[{"id": "summary", "name": "Summary"}], editmeta={"fields": {}})
            )
        )
        skipped = run(
            "jira_match.py",
            "--beam",
            str(self.beam),
            "--write-external-id",
            "--yes",
            "--results",
            str(edits),
            cwd=self.tmp,
        )
        self.assertEqual(skipped.returncode, 0, skipped.stdout + skipped.stderr)
        self.assertIn("External ID field is missing", skipped.stdout)
        sync = run(
            "jira_sync.py",
            "external-id",
            "--beam",
            str(self.beam),
            "--set",
            "WV-01=WAR-1",
            cwd=self.tmp,
        )
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        self.assertIn("dry-run", sync.stdout)
        self.assertIn("before", sync.stdout)
        self.assertIn("after WV-01", sync.stdout)


if __name__ == "__main__":
    unittest.main()
