"""1.6.23: an External ID hit matches when the Jira summary contains the schedule's summary (its first 60
characters) anywhere. Jira titles that put the id in front (`WE-01 Editor scaffold …`) missed before."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_jira_match import issue, ticket  # noqa: E402

import jira_lookup  # noqa: E402
import jira_match  # noqa: E402
import jira_sync  # noqa: E402

SCHEDULE = "Editor scaffold on WarpView's modules, and the read-only audit"
LONG = "Phase menu: allowed targets, same-phase and unknown refused, with the drawer kept in sync on every change"


def with_external(row: dict, value: str, field: str = "customfield_10050") -> dict:
    row = dict(row)
    row["fields"] = {field: value, "summary": row["summary"]}
    return row


class ContainedTest(unittest.TestCase):
    def test_the_schedule_text_anywhere_in_the_jira_summary(self):
        self.assertTrue(jira_match.summary_contained(SCHEDULE, "WE-01 " + SCHEDULE))
        self.assertTrue(jira_match.summary_contained(SCHEDULE, "[WE-01] " + SCHEDULE + " (G0)"))
        # Only the first 60 characters have to be there.
        self.assertTrue(jira_match.summary_contained(LONG, "WE-03 " + LONG[:70] + " — trimmed"))
        self.assertFalse(jira_match.summary_contained(LONG, "WE-03 " + LONG[:40]))
        self.assertFalse(jira_match.summary_contained(SCHEDULE, "WE-01 Something else entirely"))


class RankTest(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraMcp: atlassian\njiraExternalIdField: customfield_10050\n')
        self.beam.write_text(json.dumps({"tickets": {"WE-01": ticket("WE-01", SCHEDULE)}, "config": {}}))

    def load(self):
        return json.loads(self.beam.read_text())

    def match(self, issues, **opts):
        data = {"issues": issues, "searches": [{"jql": "summary ~ x", "issues": issues, "isLast": True, "total": len(issues)}]}
        args = dict(only_id=None, include_all=False, chars=60, min_score=0.9, include_done=False, do_apply=False, yes=False)
        args.update(opts)
        return jira_match.match_results(self.beam, data, **args)

    def test_external_id_and_contained_summary_is_confirmed(self):
        text = self.match([with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "WE-01")])
        self.assertIn("confirmed: WAR-63 (external, 1.00, high)", text)
        text = self.match([with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "WE-01")], do_apply=True)
        saved = self.load()["tickets"]["WE-01"]
        self.assertEqual((saved.get("jiraKey"), saved.get("jiraKeySource")), ("WAR-63", "external"))

    def test_it_needs_both_the_id_and_the_text(self):
        # The id alone, with a summary that does not contain the schedule's: no hit here.
        text = self.match([with_external(issue("WAR-63", "WE-01 Something else"), "WE-01")])
        self.assertNotIn("confirmed: WAR-63", text)
        # The text alone, behind a prefix, with another ticket's External ID: not an external match either.
        text = self.match([with_external(issue("WAR-64", "WE-01 " + SCHEDULE), "WE-02")])
        self.assertNotIn("(external,", text)

    def test_an_external_match_beside_another_exact_one_is_ambiguous(self):
        both = [with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "WE-01"), issue("WAR-90", SCHEDULE)]
        text = self.match(both)
        self.assertIn("ambiguous", text)


class SearchAndLookupTest(unittest.TestCase):
    def test_match_search_and_resolve_accept_it(self):
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        warp = tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text('jiraProject: "WAR"\njiraExternalIdField: customfield_10050\n')
        beam = warp / "beam.json"
        beam.write_text(json.dumps({"tickets": {"WE-01": ticket("WE-01", SCHEDULE)}, "config": {}}))
        results = tmp / "results.json"
        results.write_text(json.dumps({"WE-01": [with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "WE-01")]}))
        self.assertIn("jira: propose WE-01=WAR-63", jira_sync.match_search(beam, results, yes=False))

        class Client:
            def fields(self):
                return []

            def search(self, jql):
                return [with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "WE-01")] if "summary" in jql else []

        row = jira_lookup.resolve_ticket("WE-01", SCHEDULE, "WAR", "customfield_10050", Client(), ["WAR"])
        self.assertEqual((row["outcome"], row["key"]), ("proposed", "WAR-63"))


if __name__ == "__main__":
    unittest.main()
