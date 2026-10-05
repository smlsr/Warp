"""jira_view classifies a key or an external id and renders a saved Jira transcript."""

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
import jira_view  # noqa: E402

LONG = ("alpha " * 80) + "UNIQUE_END"
ADF = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "What"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "viewer serves the static host"}]},
    ],
}
ISSUE = {
    "key": "WAR-1",
    "id": "10001",
    "names": {
        "summary": "Summary",
        "description": "Description",
        "status": "Status",
        "assignee": "Assignee",
        "labels": "Labels",
        "components": "Components",
        "created": "Created",
        "customfield_10050": "External ID",
        "customfield_10051": "Notes",
        "customfield_10099": "API Token",
    },
    "fields": {
        "summary": "Viewer repo skeleton and static host",
        "description": ADF,
        "status": {"name": "To Do", "id": "10000", "statusCategory": {"key": "new", "name": "To Do"}},
        "assignee": {
            "displayName": "Ada Lovelace",
            "accountId": "acct-1",
            "emailAddress": "ada@example.com",
        },
        "labels": ["warp-harness", "size:S", "auto-merge"],
        "components": [{"name": "Viewer"}, {"name": "Host"}],
        "created": "2026-10-05T12:00:00.000+0000",
        "customfield_10050": "WV-01",
        "customfield_10051": None,
        "customfield_10099": "super-secret-token",
        "comment": {
            "total": 1,
            "comments": [
                {
                    "author": {"displayName": "Ada Lovelace", "accountId": "acct-1"},
                    "created": "2026-10-05T12:04:00.000+0000",
                    "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "ship it"}]}]},
                }
            ],
        },
        "issuelinks": [
            {
                "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                "outwardIssue": {"key": "WAR-2"},
            }
        ],
    },
}


def args(**kwargs):
    defaults = dict(show_all=False, json=False, full=False, verbose=False, comments=False, links=False)
    defaults.update(kwargs)
    return argparse_namespace(defaults)


class argparse_namespace(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        self.beam.write_text(json.dumps({"tickets": {}, "config": {}}))
        (self.warp / "config.yaml").write_text('jiraProject: "WAR"\njiraMcp: "atlassian"\n')

    def render(self, query, data, **flags):
        return jira_view.render_view(self.beam, query, data, args(**flags))

    def test_classify_key_external_and_unknown_project(self):
        self.assertEqual(jira_view.classify("WAR-1", ["WAR"]), "key")
        self.assertEqual(jira_view.classify("war-1", ["WAR"]), "key")
        self.assertEqual(jira_view.classify("WV-01", ["WAR"]), "external")
        self.assertEqual(jira_view.classify("WV-01", []), "key")
        self.assertEqual(jira_view.classify("T-1", []), "external")
        self.assertEqual(jira_view.classify("not a key", ["WAR"]), "external")

    def test_prepare_key_and_external_id(self):
        key = jira_view.prepare_view(self.beam, "WAR-1", args())
        self.assertIn("as an issue key", key)
        self.assertIn("getJiraIssue", key)
        self.assertIn("getTransitionsForJiraIssue", key)
        saved = json.loads((self.warp / "jira-view.json").read_text())
        self.assertEqual(saved["classify"], "key")
        self.assertTrue(saved["tryDirect"])
        self.assertEqual(saved["fetchKey"], "WAR-1")
        external = jira_view.prepare_view(self.beam, "WV-01", args(comments=True, links=True))
        self.assertIn("as an external id", external)
        self.assertNotIn("as an issue key", external)
        self.assertIn("searchJiraIssuesUsingJql", external)
        self.assertNotIn("getVisibleJiraProjects", external)
        self.assertIn("--comments --links", external)
        saved = json.loads((self.warp / "jira-view.json").read_text())
        self.assertEqual(saved["classify"], "external")
        self.assertEqual(saved["query"], "WV-01")
        self.assertTrue(saved["queries"])
        self.assertTrue(all(row["kind"] != "summary" for row in saved["queries"]))
        beam = json.loads(self.beam.read_text())
        self.assertEqual(beam["tickets"], {})

    def test_empty_project_probes_visible_projects(self):
        (self.warp / "config.yaml").write_text('jiraProject: ""\n')
        text = jira_view.prepare_view(self.beam, "WV-01", args())
        self.assertIn("getVisibleJiraProjects", text)
        self.assertIn("as an issue key", text)

    def test_render_fields_adf_user_empty_and_token(self):
        data = {"issue": ISSUE, "transitions": [{"id": "11", "name": "Start", "to": {"name": "In Progress"}}, {"id": "21", "name": "Done"}]}
        text = self.render("WAR-1", data)
        self.assertIn("resolved: WAR-1 (issue key)", text)
        self.assertIn("status: To Do", text)
        self.assertIn("External ID (customfield_10050): WV-01", text)
        self.assertIn("What", text)
        self.assertIn("viewer serves the static host", text)
        self.assertNotIn('"type": "doc"', text)
        self.assertIn("Assignee: Ada Lovelace", text)
        self.assertNotIn("acct-1", text)
        self.assertNotIn("ada@example.com", text)
        self.assertIn("Labels: warp-harness, size:S, auto-merge", text)
        self.assertIn("Components: Viewer, Host", text)
        self.assertIn("Created: 2026-10-05T12:00:00Z", text)
        self.assertNotIn("Notes", text)
        self.assertNotIn("super-secret-token", text)
        self.assertIn("<redacted>", text)
        self.assertNotIn("ship it", text)
        self.assertNotIn("WAR-2", text)
        self.assertIn("11: Start -> In Progress", text)
        self.assertIn("discovered field: External ID (customfield_10050)", text)
        self.assertIn("beam: not stored", text)
        self.assertIn("map: not stored", text)

        shown = self.render("WAR-1", data, show_all=True)
        self.assertIn("Notes (customfield_10051): null", shown)
        verbose = self.render("WAR-1", data, verbose=True)
        self.assertIn("Ada Lovelace (acct-1)", verbose)
        linked = self.render("WAR-1", data, comments=True, links=True)
        self.assertIn("count: 1", linked)
        self.assertIn("ship it", linked)
        self.assertIn("blocks WAR-2", linked)

        long_issue = json.loads(json.dumps(ISSUE))
        long_issue["fields"]["description"] = LONG
        long_text = self.render("WAR-1", {"issue": long_issue, "transitions": []})
        self.assertNotIn("UNIQUE_END", long_text)
        self.assertIn("...", long_text)
        full = self.render("WAR-1", {"issue": long_issue, "transitions": []}, full=True)
        self.assertIn("UNIQUE_END", full)

        raw = json.loads(self.render("WAR-1", data, json=True))
        self.assertEqual(raw["issue"]["key"], "WAR-1")
        self.assertEqual(raw["issue"]["fields"]["customfield_10099"], "<redacted>")
        self.assertNotIn("super-secret-token", json.dumps(raw))

    def test_external_id_from_search_and_from_the_map(self):
        jql = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        data = {
            "projects": ["WAR"],
            "searches": [{"jql": jql, "kind": "external", "issues": [{"key": "WAR-1", "fields": {"customfield_10050": "WV-01"}}]}],
            "issue": ISSUE,
            "transitions": [{"id": "11", "name": "In Progress"}],
        }
        text = self.render("WV-01", data)
        self.assertIn("resolved: WAR-1 (external id)", text)
        self.assertIn("external id field: External ID (customfield_10050): WV-01", text)
        self.assertIn("Summary: Viewer repo skeleton and static host", text)

        (self.warp / "jira-map.json").write_text(json.dumps({"WV-01": {"key": "WAR-1", "source": "external", "confidence": "high"}}))
        prepared = jira_view.prepare_view(self.beam, "WV-01", args())
        self.assertIn("WV-01 -> WAR-1 (map, external id)", prepared)
        mapped = self.render("WV-01", {"issue": ISSUE, "transitions": []})
        self.assertIn("resolved: WAR-1 (map, external id)", mapped)
        self.assertIn("map: WV-01 = WAR-1 (external)", mapped)
        self.assertIn("beam: not stored", mapped)

    def test_beam_stores_the_key(self):
        self.beam.write_text(
            json.dumps(
                {
                    "tickets": {
                        "WV-01": {
                            "id": "WV-01",
                            "jiraKey": "WAR-1",
                            "jiraKeySource": "external",
                            "jira": {"externalId": "WV-01", "status": "To Do"},
                        }
                    }
                }
            )
        )
        text = self.render("WV-01", {"issue": ISSUE, "transitions": []})
        self.assertIn("resolved: WAR-1 (beam, external id)", text)
        self.assertIn("beam: WV-01 = WAR-1 (external)", text)
        again = json.loads(self.beam.read_text())
        self.assertEqual(again["tickets"]["WV-01"]["jiraKey"], "WAR-1")

    def test_ambiguous_lists_both_and_prints_neither(self):
        jql = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        data = {
            "searches": [{"jql": jql, "issues": [{"key": "WAR-1"}, {"key": "WAR-2", "fields": {"summary": "Viewer repo skeleton and static host"}}]}],
            "issue": ISSUE,
        }
        text = self.render("WV-01", data)
        self.assertIn("WAR-1", text)
        self.assertIn("WAR-2", text)
        self.assertIn("Neither is shown", text)
        self.assertNotIn("viewer serves the static host", text)
        self.assertNotIn("Ada Lovelace", text)

    def test_direct_miss_falls_back_to_external_id(self):
        (self.warp / "config.yaml").write_text('jiraProject: ""\n')
        jql = jira_lookup.jql_equals("externalId", "WV-01", None)
        data = {
            "direct": {"issue": None, "error": "Issue does not exist"},
            "searches": [{"jql": jql, "issues": [{"key": "WAR-1"}]}],
            "issue": ISSUE,
            "transitions": [],
        }
        text = self.render("WV-01", data)
        self.assertIn("getJiraIssue: Issue does not exist", text)
        self.assertIn("resolved: WAR-1 (external id)", text)

    def test_probe_one_several_and_none(self):
        (self.warp / "config.yaml").write_text('jiraProject: ""\n')
        war = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        abc = jira_lookup.jql_equals("externalId", "WV-01", "ABC")

        one = self.render(
            "WV-01",
            {
                "projects": ["WAR", "ABC"],
                "direct": {"error": "Issue does not exist"},
                "searches": [
                    {"jql": war, "project": "WAR", "issues": [{"key": "WAR-1"}]},
                    {"jql": abc, "project": "ABC", "issues": []},
                ],
                "issue": ISSUE,
                "transitions": [],
            },
        )
        self.assertIn("resolved: WAR-1 (external id)", one)

        several = self.render(
            "WV-01",
            {
                "projects": ["WAR", "ABC"],
                "direct": {"error": "Issue does not exist"},
                "searches": [
                    {"jql": war, "project": "WAR", "issues": [{"key": "WAR-12"}]},
                    {"jql": abc, "project": "ABC", "issues": [{"key": "ABC-3"}]},
                ],
                "issue": ISSUE,
            },
        )
        self.assertIn("WAR-12", several)
        self.assertIn("ABC-3", several)
        self.assertIn("Neither is shown", several)
        self.assertNotIn("viewer serves the static host", several)

        none = self.render(
            "WV-01",
            {
                "projects": ["WAR", "ABC"],
                "direct": {"error": "Issue does not exist"},
                "searches": [
                    {"jql": war, "project": "WAR", "issues": []},
                    {"jql": abc, "project": "ABC", "issues": []},
                ],
            },
        )
        self.assertIn("resolved: none", none)
        self.assertIn("No issue matched", none)

    def test_missing_external_field_uses_the_label(self):
        rows = jira_lookup.plan_queries("WV-01", "", "WAR", "externalId")
        searches = []
        for row in rows:
            if row["kind"] == "summary" or not row.get("jql"):
                continue
            if row["kind"] == "external" and row["field"] == "externalId":
                searches.append({"jql": row["jql"], "field": row["field"], "error": "field not found"})
            elif row["kind"] == "label":
                searches.append({"jql": row["jql"], "kind": "label", "issues": [{"key": "WAR-9"}]})
        text = self.render("WV-01", {"searches": searches, "issue": ISSUE, "transitions": []})
        self.assertIn("resolved: WAR-9 (label)", text)

    def test_help_lists_the_flags(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "jira_view.py"), "?"],
            cwd=self.tmp,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for needle in ("--results", "--all", "--json", "--full", "--verbose", "--comments", "--links", "--beam", "getJiraIssue"):
            self.assertIn(needle, proc.stdout)


if __name__ == "__main__":
    unittest.main()
