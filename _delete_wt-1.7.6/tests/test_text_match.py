"""1.6.24: jiraMatchOnText (and jira_match.py --text-only) makes a summary that contains the plan summary's first
60 characters a hit on its own, whatever the issue's External ID says. Off by default."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_external_title import SCHEDULE, with_external  # noqa: E402
from test_jira_match import issue, ticket  # noqa: E402

import jira_lookup  # noqa: E402
import jira_match  # noqa: E402
import jira_sync  # noqa: E402

# The Jira title holds the schedule text, but its External ID is not the plan id.
OTHER_ID = with_external(issue("WAR-63", "WE-01 " + SCHEDULE), "EDIT-1")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        self.beam.write_text(json.dumps({"tickets": {"WE-01": ticket("WE-01", SCHEDULE)}, "config": {}}))
        self.config('')

    def config(self, extra):
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraExternalIdField: customfield_10050\n' + extra)

    def load(self):
        return json.loads(self.beam.read_text())


class MatchTest(Base):
    def match(self, issues, **opts):
        data = {"issues": issues, "searches": [{"jql": "summary ~ x", "issues": issues, "isLast": True, "total": len(issues)}]}
        args = dict(only_id=None, include_all=False, chars=60, min_score=0.9, include_done=False, do_apply=False, yes=False)
        args.update(opts)
        return jira_match.match_results(self.beam, data, **args)

    def test_off_by_default(self):
        self.assertNotIn("confirmed: WAR-63", self.match([OTHER_ID]))

    def test_the_switch_makes_the_text_a_hit(self):
        self.config("jiraMatchOnText: true\n")
        self.assertIn("confirmed: WAR-63 (text, 1.00, high)", self.match([OTHER_ID]))
        self.match([OTHER_ID], do_apply=True)
        self.assertEqual(self.load()["tickets"]["WE-01"]["jiraKey"], "WAR-63")

    def test_the_flag_does_the_same_for_one_run(self):
        self.assertIn("confirmed: WAR-63 (text, 1.00, high)", self.match([OTHER_ID], text_only=True))

    def test_two_issues_with_the_text_are_ambiguous(self):
        twin = with_external(issue("WAR-90", "WE-01 " + SCHEDULE + " (copy)"), "EDIT-9")
        self.assertIn("ambiguous", self.match([OTHER_ID, twin], text_only=True))


class LookupTest(Base):
    class Client:
        def fields(self):
            return []

        def search(self, jql):
            return [OTHER_ID] if "summary" in jql else []

    def test_the_scans_lookup_stores_it_as_a_hit(self):
        row = jira_lookup.resolve_ticket("WE-01", SCHEDULE, "WAR", "customfield_10050", self.Client(), ["WAR"])
        self.assertEqual(row["outcome"], "miss")  # off: a summary that is not exact, and another External ID
        row = jira_lookup.resolve_ticket("WE-01", SCHEDULE, "WAR", "customfield_10050", self.Client(), ["WAR"], text_only=True)
        self.assertEqual((row["outcome"], row["key"], row["confidence"]), ("hit", "WAR-63", "high"))
        self.config("jiraMatchOnText: true\n")
        text = jira_sync.apply_jira_lookup(self.beam, self.Client())
        self.assertIn("jira: WE-01 -> WAR-63 (summary, high)", text)
        self.assertEqual(self.load()["tickets"]["WE-01"]["jiraKey"], "WAR-63")

    def test_map_match_proposes_it(self):
        results = self.tmp / "results.json"
        results.write_text(json.dumps({"WE-01": [OTHER_ID]}))
        self.assertIn("jira: WE-01 no summary match", jira_sync.match_search(self.beam, results, yes=False))
        self.config("jiraMatchOnText: true\n")
        self.assertIn("jira: propose WE-01=WAR-63", jira_sync.match_search(self.beam, results, yes=False))


if __name__ == "__main__":
    unittest.main()
