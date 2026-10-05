"""Claim lookup must be recorded before a transition. Import JSON is not a Jira key."""

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
import jira_project  # noqa: E402
import jira_sync  # noqa: E402

IMPORT = {
    "externalId": "WV-01",
    "summary": "Viewer repo skeleton and static host",
    "description": (
        "h2. What\n...\n"
        "h2. Size\nS — auto-merge after Bugbot\n\n"
        "h2. Locks\napps/viewer\n\n"
        "h2. Blocked by\nnone\n\n"
        "h2. Acceptance\n* viewer serves the static host\n"
    ),
    "issueType": "Story",
    "status": "To Do",
    "priority": "Medium",
    "labels": ["warp-harness", "size:S", "auto-merge", "area:platform"],
}


def run(script, *args, cwd):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


class ImportAndClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "jira").mkdir(parents=True)
        (self.repo / "jira" / "import.json").write_text(json.dumps([IMPORT]))
        self.beam = ".warp/beam.json"

    def scan(self):
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def ticket(self):
        return json.loads((self.repo / self.beam).read_text())["tickets"]["WV-01"]

    def test_import_external_id_is_the_plan_id_not_the_jira_key(self):
        self.scan()
        t = self.ticket()
        self.assertEqual(t["summary"], "Viewer repo skeleton and static host")
        self.assertIsNone(t.get("jiraKey"))
        self.assertNotEqual(t.get("jiraKey"), "WV-01")
        self.assertEqual(t["size"], "S")
        self.assertTrue(t["autoMerge"])
        self.assertEqual(t["locks"], ["apps/viewer"])
        self.assertEqual(t["deps"], [])
        self.assertEqual(t["module"], "platform")
        self.assertIn("viewer serves the static host", t["acs"])
        self.assertEqual(t["status"], "queued")
        self.assertEqual(t["jira"]["status"], "To Do")
        self.assertEqual(t["jira"]["externalId"], "WV-01")
        self.assertNotIn("startedAt", t["jira"])

    def test_claim_does_not_transition_until_the_lookup_is_recorded(self):
        self.scan()
        (self.repo / ".warp" / "config.yaml").write_text('jiraProject: "WAR"\n')
        claim = run(
            "beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--status", "claimed", "--agent", "s", cwd=self.repo
        )
        self.assertEqual(claim.returncode, 0, claim.stdout + claim.stderr)
        self.assertNotIn("MUST DO", claim.stdout)
        self.assertIn("resolve --ticket", claim.stdout)
        self.assertIn("Do not pass the plan id", claim.stdout)
        self.assertIsNone(self.ticket().get("jiraKey"))
        todo = json.loads((self.repo / ".warp" / "jira-todo.json").read_text())
        self.assertEqual(todo["tickets"], [])

        refused = run(
            "jira_sync.py",
            "resolve",
            "--beam",
            self.beam,
            "--ticket",
            "WV-01",
            "--key",
            "WV-01",
            "--issue-id",
            "10001",
            cwd=self.repo,
        )
        self.assertIn("is the plan id", refused.stdout)
        self.assertIsNone(self.ticket().get("jiraKey"))

        recorded = run(
            "jira_sync.py",
            "resolve",
            "--beam",
            self.beam,
            "--ticket",
            "WV-01",
            "--key",
            "WAR-1",
            "--issue-id",
            "10001",
            "--cloud-id",
            "cloud-1",
            "--status",
            "To Do",
            cwd=self.repo,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertIn("MUST DO WV-01 (WAR-1)", recorded.stdout)
        self.assertIn("issue key WAR-1", recorded.stdout)
        self.assertIn("Do not pass WV-01", recorded.stdout)
        t = self.ticket()
        self.assertEqual(t["jiraKey"], "WAR-1")
        self.assertEqual(t["jiraKeySource"], "external")
        self.assertEqual(t["jira"]["id"], "10001")
        self.assertEqual(t["jira"]["cloudId"], "cloud-1")
        self.assertNotIn("startedAt", t["jira"])
        saved = json.loads((self.repo / ".warp" / "jira-map.json").read_text())["WV-01"]
        self.assertEqual(saved["key"], "WAR-1")
        self.assertEqual(saved["source"], "external")
        todo = json.loads((self.repo / ".warp" / "jira-todo.json").read_text())
        self.assertEqual(todo["tickets"][0]["jiraKey"], "WAR-1")
        self.assertEqual(todo["tickets"][0]["issueId"], "10001")
        self.assertNotEqual(todo["tickets"][0]["jiraKey"], "WV-01")

        failed = run(
            "jira_sync.py",
            "record",
            "--beam",
            self.beam,
            "--id",
            "WV-01",
            "--event",
            "claim",
            "--result",
            "failed",
            "--error",
            "HTTP 400 no transition",
            cwd=self.repo,
        )
        self.assertIn("HTTP 400 no transition", failed.stdout)
        t = self.ticket()
        self.assertNotIn("startedAt", t["jira"])
        self.assertEqual(t["jira"]["lastAttempt"]["result"], "failed")
        self.assertEqual(t["jira"]["lastAttempt"]["error"], "HTTP 400 no transition")

        check = run("jira_sync.py", "verify", "--beam", self.beam, "--id", "WV-01", cwd=self.repo)
        self.assertIn("key stored: WAR-1 (from external id)", check.stdout)
        self.assertIn("jira.id: 10001", check.stdout)
        self.assertIn("lastAttempt: claim failed — HTTP 400 no transition", check.stdout)
        self.assertIn("status: keyed", check.stdout)

    def test_unrecorded_claim_says_the_lookup_was_not_stored(self):
        self.scan()
        check = run("jira_sync.py", "verify", "--beam", self.beam, "--id", "WV-01", cwd=self.repo)
        self.assertIn("key missing: claim lookup result was not recorded", check.stdout)
        self.assertIn("status: unmapped", check.stdout)

    def test_lookup_key_is_used_even_when_the_prefix_list_is_wrong(self):
        self.assertEqual(
            jira_sync.jira_key({"id": "WV-01", "jiraKey": "WAR-1", "jiraKeySource": "external"}, ["HOS"]),
            "WAR-1",
        )
        self.assertIsNone(jira_sync.jira_key({"id": "WV-01", "jiraKey": "WV-01", "jiraKeySource": "external"}, ["WV"]))
        text = jira_sync.render(
            {"id": "WV-01", "jiraKey": "WV-01", "jiraKeySource": "plan", "status": "claimed"},
            [{"type": "comment", "where": "jira", "event": "claim", "body": "x"}],
            jira_sync.DEFAULTS,
        )
        self.assertIn("no transition", text)
        self.assertIn("claim lookup result was not recorded", text)
        self.assertNotIn("MUST DO", text)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".warp").mkdir(parents=True)
        (self.repo / ".warp" / "config.yaml").write_text('jiraProject: ""\n')

    def _searches(self, issues_for):
        rows = []
        for query in jira_project.probe_queries(["WAR", "ABC"], ["WV-01"], "externalId"):
            row = dict(query)
            row["issues"] = issues_for(query)
            rows.append(row)
        return rows

    def test_one_project_hit_is_stored(self):
        def issues_for(query):
            if query["project"] == "WAR" and query["field"] == "externalId":
                return [{"key": "WAR-12"}]
            return []

        note = jira_project.record_probe(
            self.repo, {"projects": ["WAR", "ABC"], "searches": self._searches(issues_for)}
        )
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn("WV-01", note)
        self.assertIn("WAR-12", note)
        text = (self.repo / ".warp" / "config.yaml").read_text()
        self.assertIn('jiraProject: "WAR"', text)
        self.assertIn('jiraKeyPrefixes: "WAR"', text)

    def test_several_hits_are_listed_and_not_set(self):
        def issues_for(query):
            if query["field"] != "externalId":
                return []
            return [{"key": "WAR-12"}] if query["project"] == "WAR" else [{"key": "ABC-3"}]

        note = jira_project.record_probe(
            self.repo, {"projects": ["WAR", "ABC"], "searches": self._searches(issues_for)}
        )
        self.assertIn("several projects matched", note)
        self.assertIn("WV-01=WAR-12", note)
        self.assertIn("WV-01=ABC-3", note)
        self.assertIn("project --set WAR", note)
        self.assertIn("project --set ABC", note)
        self.assertNotIn('jiraProject: "WAR"', (self.repo / ".warp" / "config.yaml").read_text())

    def test_no_hit_lists_every_project(self):
        note = jira_project.record_probe(
            self.repo, {"projects": ["WAR", "ABC"], "searches": self._searches(lambda _q: [])}
        )
        self.assertIn("no project matched an external id", note)
        self.assertIn("candidates: ABC, WAR", note)
        self.assertIn('jiraProject: ""', (self.repo / ".warp" / "config.yaml").read_text())

    def test_missing_field_falls_through_to_the_label(self):
        queries = jira_project.probe_queries(["WAR", "ABC"], ["WV-01"], "externalId")
        searches = []
        for query in queries:
            row = dict(query)
            if query["kind"] == "external":
                row["error"] = "field not found"
                row["issues"] = []
            elif query["project"] == "WAR" and query["kind"] == "label":
                row["issues"] = [{"key": "WAR-9"}]
            else:
                row["issues"] = []
            searches.append(row)
        client = jira_project.ProbeClient({"searches": searches})
        note = jira_project.run_probe(self.repo, client, ["WAR", "ABC"], ["WV-01"])
        self.assertIn("set jiraProject to WAR", note)
        self.assertIn("WAR-9", note)
        self.assertIn('jiraProject: "WAR"', (self.repo / ".warp" / "config.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
