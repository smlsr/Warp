"""Plan ids are not Jira keys. Real keys come from the plan, the map, or one external id."""

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
import jira_sync  # noqa: E402
import scan  # noqa: E402

INSTALL = SCRIPTS / "install.py"


def run(script, *args, cwd):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


def schedule(tickets):
    return json.dumps({"tickets": tickets})


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / ".warp").mkdir()
        self.beam = ".warp/beam.json"

    def cfg(self, text="jiraProject: WAR\n"):
        (self.repo / ".warp/config.yaml").write_text(text)

    def scan_text(self, name, text):
        (self.repo / name).write_text(text)
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def tickets(self):
        return json.loads((self.repo / self.beam).read_text())["tickets"]

    def key(self, tid):
        return self.tickets()[tid].get("jiraKey")


class PlanPathTests(Repo):
    def test_schedule_jira_key(self):
        self.cfg()
        body = schedule(
            [
                {"id": "WV-01", "summary": "Build the widget", "size": "S", "jiraKey": "WAR-1"},
                {"id": "WV-02", "summary": "Second", "size": "S"},
            ]
        )
        r = self.scan_text("schedule.json", body)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertIsNone(self.key("WV-02"))
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "plan")
        self.assertIn("2 tickets: 1 keyed, 1 need mapping", r.stdout)
        self.assertIn("RESOLVE WV-02", r.stdout)
        self.assertIn("only for tickets that stay unmapped", r.stdout)

    def test_plan_id_is_not_stored_when_the_prefix_does_not_match(self):
        self.cfg()
        body = schedule(
            [
                {"id": "WV-01", "summary": "Build", "size": "S", "jiraKey": "WV-01"},
                {"id": "WV-02", "summary": "Second", "size": "S"},
            ]
        )
        self.scan_text("schedule.json", body)
        self.assertIsNone(self.key("WV-01"))
        self.assertEqual(self.tickets()["WV-01"]["jiraMapping"], "needs mapping")

    def test_markdown_jira_field_table_and_heading(self):
        self.cfg()
        field = self.scan_text(
            "CURSOR_PLAN.md",
            "- WV-01 Build the widget\nJira: WAR-1\n- WV-02 Second ticket\n",
        )
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertNotIn("WAR-1", self.tickets())
        self.assertIn("1 keyed, 1 need mapping", field.stdout)

        repo2 = self.tmp / "table"
        repo2.mkdir()
        (repo2 / ".warp").mkdir()
        (repo2 / ".warp/config.yaml").write_text("jiraProject: WAR\n")
        (repo2 / "CURSOR_PLAN.md").write_text(
            "| id | summary | Jira Key |\n| --- | --- | --- |\n| WV-01 | Build the widget | WAR-1 |\n| WV-02 | Second ticket | |\n"
        )
        r = run("scan.py", "scan", cwd=repo2)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tickets = json.loads((repo2 / ".warp/beam.json").read_text())["tickets"]
        self.assertEqual(tickets["WV-01"]["jiraKey"], "WAR-1")
        self.assertNotIn("WAR-1", tickets)

        repo3 = self.tmp / "head"
        repo3.mkdir()
        (repo3 / ".warp").mkdir()
        (repo3 / ".warp/config.yaml").write_text("jiraProject: WAR\n")
        (repo3 / "CURSOR_PLAN.md").write_text("### WV-01 [WAR-1] Build the widget\n### WV-02 Second ticket\n")
        r = run("scan.py", "scan", cwd=repo3)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tickets = json.loads((repo3 / ".warp/beam.json").read_text())["tickets"]
        self.assertEqual(tickets["WV-01"]["jiraKey"], "WAR-1")
        self.assertEqual(sorted(tickets), ["WV-01", "WV-02"])

    def test_map_file_and_config_map_survive_rescan(self):
        self.cfg("jiraProject: WAR\njiraKeyMap: {\"WV-01\": \"WAR-1\"}\n")
        body = schedule(
            [
                {"id": "WV-01", "summary": "Build", "size": "S"},
                {"id": "WV-02", "summary": "Second", "size": "S"},
            ]
        )
        self.scan_text("schedule.json", body)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "map")
        (self.repo / ".warp/config.yaml").write_text("jiraProject: WAR\n")
        (self.repo / ".warp/jira-map.json").write_text(json.dumps({"WV-02": "WAR-2"}) + "\n")
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.key("WV-02"), "WAR-2")
        self.assertIsNone(self.key("WV-01"))

    def test_manual_key_survives_rescan_without_the_map_file(self):
        self.cfg()
        body = schedule(
            [
                {"id": "WV-01", "summary": "Build", "size": "S"},
                {"id": "WV-02", "summary": "Second", "size": "S"},
            ]
        )
        self.scan_text("schedule.json", body)
        r = run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--jira", "WAR-1", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (self.repo / ".warp/jira-map.json").unlink()
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "manual")


class CommandTests(Repo):
    def seed(self):
        self.cfg()
        for name in ("beam.json", "jira-map.json", "jira-resolve.json", "jira-todo.json", "outbox.md"):
            path = self.repo / ".warp" / name
            if path.exists():
                path.unlink()
        self.scan_text(
            "schedule.json",
            schedule(
                [
                    {"id": "WV-01", "summary": "Build the widget", "size": "M"},
                    {"id": "WV-02", "summary": "Second ticket", "size": "S"},
                ]
            ),
        )

    def test_set_import_and_prefix_checks(self):
        self.seed()
        bad = run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--jira", "WV-01", cwd=self.repo)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("does not match", bad.stderr)
        self.assertIsNone(self.key("WV-01"))
        ok = run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--jira", "WAR-1", cwd=self.repo)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        forced = run(
            "beam.py", "set", "--beam", self.beam, "--id", "WV-02", "--jira", "WV-02", "--force", cwd=self.repo
        )
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(self.key("WV-02"), "WV-02")
        self.assertTrue(self.tickets()["WV-02"]["jiraKeyForced"])

        self.seed()
        rejected = run("jira_sync.py", "map", "--beam", self.beam, "--set", "WV-01=HOS-9", cwd=self.repo)
        self.assertIn("does not match", rejected.stdout)
        self.assertIsNone(self.key("WV-01"))
        accepted = run("jira_sync.py", "map", "--beam", self.beam, "--set", "WV-01=WAR-1", cwd=self.repo)
        self.assertIn("WV-01 -> WAR-1", accepted.stdout)
        self.assertEqual(json.loads((self.repo / ".warp/jira-map.json").read_text())["WV-01"], "WAR-1")

        for name, text in (
            ("m.csv", "id,jiraKey\nWV-01,WAR-1\nWV-02,WAR-2\n"),
            ("m.json", json.dumps({"WV-01": "WAR-1", "WV-02": "WAR-2"})),
            ("m.md", "| id | jira key |\n| --- | --- |\n| WV-01 | WAR-1 |\n| WV-02 | WAR-2 |\n"),
        ):
            self.seed()
            (self.repo / name).write_text(text)
            r = run("jira_sync.py", "map", "--beam", self.beam, "--import", name, cwd=self.repo)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.key("WV-01"), "WAR-1")
            self.assertEqual(self.key("WV-02"), "WAR-2")

    def test_summary_search_waits_for_yes(self):
        self.seed()
        r = run("jira_sync.py", "map", "--beam", self.beam, "--search", cwd=self.repo)
        self.assertIn("searchJiraIssuesUsingJql", r.stdout)
        self.assertIsNone(self.key("WV-01"))
        payload = json.loads((self.repo / ".warp/jira-search.json").read_text())
        self.assertTrue(payload["confirm"])
        results = self.repo / "found.json"
        results.write_text(json.dumps({"WV-01": [{"key": "WAR-1", "summary": "Build the widget"}], "WV-02": []}))
        quiet = run("jira_sync.py", "map", "--beam", self.beam, "--match", "found.json", cwd=self.repo)
        self.assertIn("not written", quiet.stdout)
        self.assertIsNone(self.key("WV-01"))
        stored = run("jira_sync.py", "map", "--beam", self.beam, "--match", "found.json", "--yes", cwd=self.repo)
        self.assertIn("WV-01 -> WAR-1", stored.stdout)
        self.assertEqual(self.key("WV-01"), "WAR-1")

    def test_unmapped_claim_flags_only_after_a_miss_and_catchup_uses_the_real_key(self):
        self.seed()
        box = self.repo / ".warp/outbox.md"
        if box.exists():
            box.unlink()
        claim = run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--status", "claimed", "--agent", "s", cwd=self.repo)
        self.assertIn("RESOLVE", claim.stdout)
        jql = json.loads((self.repo / ".warp/jira-resolve.json").read_text())["tickets"][0]["jql"]
        self.assertEqual(jql, 'project = WAR AND externalId = "WV-01"')
        self.assertNotIn("MUST DO", claim.stdout)
        self.assertFalse((self.repo / ".warp/outbox.md").exists())
        todo = json.loads((self.repo / ".warp/jira-todo.json").read_text())
        self.assertEqual(todo["tickets"], [])

        missed = self.repo / "none.json"
        missed.write_text(json.dumps({"WV-01": [{"key": "WAR-4"}, {"key": "WAR-5"}]}))
        amb = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "none.json", cwd=self.repo)
        self.assertIn("ambiguous", amb.stdout)
        self.assertIn("needs mapping", amb.stdout)
        self.assertIsNone(self.key("WV-01"))
        self.assertIn("needs mapping", (self.repo / ".warp/outbox.md").read_text())
        self.assertNotIn("MUST DO", amb.stdout)

        mapped = run("jira_sync.py", "map", "--beam", self.beam, "--set", "WV-01=WAR-1", cwd=self.repo)
        self.assertIn("WV-01 -> WAR-1", mapped.stdout)
        caught = run("jira_sync.py", "catchup", "--beam", self.beam, "--id", "WV-01", cwd=self.repo)
        self.assertIn("MUST DO", caught.stdout)
        self.assertIn("WAR-1", caught.stdout)
        self.assertIn("In Progress", caught.stdout)
        self.assertNotIn("(WV-01)", caught.stdout)
        todo = json.loads((self.repo / ".warp/jira-todo.json").read_text())
        self.assertEqual(todo["tickets"][0]["jiraKey"], "WAR-1")

    def test_external_id_match_on_a_claim_stores_the_key_and_requests_the_transition(self):
        self.seed()
        run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--status", "claimed", "--agent", "s", cwd=self.repo)
        results = self.repo / "hit.json"
        results.write_text(json.dumps({"WV-01": [{"key": "WAR-1", "externalId": "WV-01"}]}))
        r = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "hit.json", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "external")
        self.assertIn("1 keyed, 1 need mapping", r.stdout)
        self.assertIn("MUST DO", r.stdout)
        self.assertIn("WAR-1", r.stdout)
        todo = json.loads((self.repo / ".warp/jira-todo.json").read_text())
        self.assertEqual(todo["tickets"][0]["jiraKey"], "WAR-1")
        self.assertEqual(todo["tickets"][0]["actions"][0]["plan"]["jiraKey"], "WAR-1")
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(self.key("WV-01"), "WAR-1")

    def test_queued_external_match_does_not_flag(self):
        self.seed()
        run("jira_sync.py", "resolve", "--beam", self.beam, cwd=self.repo)
        results = self.repo / "hit.json"
        results.write_text(
            json.dumps(
                {
                    "WV-01": [{"key": "WAR-1", "fields": {"externalId": "WV-01"}}],
                    "WV-02": [{"key": "WAR-8", "externalId": "OTHER"}, {"key": "WAR-9", "externalId": "OTHER"}],
                }
            )
        )
        box = self.repo / ".warp/outbox.md"
        if box.exists():
            box.unlink()
        r = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "hit.json", cwd=self.repo)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertIsNone(self.key("WV-02"))
        self.assertIn("2 tickets: 1 keyed, 1 need mapping", r.stdout)
        self.assertIn("no external-id match", r.stdout)
        self.assertNotIn("MUST DO", r.stdout)
        self.assertFalse((self.repo / ".warp/outbox.md").exists())

    def test_verify_lists_unmapped(self):
        self.seed()
        r = run("jira_sync.py", "verify", "--beam", self.beam, cwd=self.repo)
        self.assertIn("jira: unmapped: WV-01, WV-02", r.stdout)
        self.assertIn("needs mapping", r.stdout)


class MatchUnitTests(unittest.TestCase):
    def test_exact_single_external_id(self):
        key, how = jira_sync.match_external(
            "WV-01",
            [{"key": "WAR-1", "externalId": "WV-01"}, {"key": "WAR-2", "externalId": "WV-02"}],
            "externalId",
            ["WAR"],
        )
        self.assertEqual((key, how), ("WAR-1", "matched"))
        key, how = jira_sync.match_external("WV-01", [{"key": "WAR-1"}, {"key": "WAR-2"}], "externalId", ["WAR"])
        self.assertEqual(key, None)
        self.assertTrue(how.startswith("ambiguous"))
        key, how = jira_sync.match_external("WV-01", [{"key": "WAR-1"}], "externalId", ["WAR"])
        self.assertEqual((key, how), ("WAR-1", "matched"))
        key, how = jira_sync.match_external("WV-01", [{"key": "HOS-1", "externalId": "WV-01"}], "externalId", ["WAR"])
        self.assertEqual(how, "none")

    def test_markdown_parser_reads_each_shape(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        field = d / "a.md"
        field.write_text("- WV-01 Build\nJira: WAR-1\n- WV-02 Other\n")
        got = {t["id"]: t.get("jiraKey") for t in scan.from_markdown(field)["tickets"]}
        self.assertEqual(got["WV-01"], "WAR-1")
        table = d / "b.md"
        table.write_text("| id | jiraKey |\n| --- | --- |\n| WV-01 | WAR-1 |\n| WV-02 | |\n")
        got = {t["id"]: t.get("jiraKey") for t in scan.from_markdown(table)["tickets"]}
        self.assertEqual(got["WV-01"], "WAR-1")
        self.assertNotIn("WAR-1", got)


class BackfillTests(unittest.TestCase):
    def test_init_adds_the_new_jira_keys(self):
        repo = Path(tempfile.mkdtemp()) / "repo"
        self.addCleanup(shutil.rmtree, repo.parent, True)
        (repo / ".warp").mkdir(parents=True)
        cfg = repo / ".warp/config.yaml"
        cfg.write_text('slackChannel: "warp"\nteamsChannel: "warp"\n')
        r = subprocess.run([sys.executable, "-B", str(INSTALL), "init", "--root", "."], cwd=repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("jiraKeyPrefixes", r.stdout)
        self.assertIn("jiraKeyMap", r.stdout)
        self.assertIn("jiraExternalIdField", r.stdout)
        text = cfg.read_text()
        self.assertRegex(text, r"(?m)^jiraKeyPrefixes:")
        self.assertRegex(text, r"(?m)^jiraKeyMap:")
        self.assertRegex(text, r"(?m)^jiraExternalIdField:")


class LookupTests(CommandTests):
    def _apply(self, data, **kwargs):
        return jira_sync.apply_jira_lookup(self.repo / self.beam, jira_lookup.ReplayClient(data), **kwargs)

    def test_custom_field_hit_is_stored_and_survives_rescan(self):
        self.seed()
        self.cfg("jiraProject: WAR\njiraExternalIdField: customfield_10050\n")
        jql = jira_lookup.jql_equals(jira_lookup.clause_for("customfield_10050"), "WV-01", "WAR")
        self.assertEqual(jql, 'project = WAR AND cf[10050] = "WV-01"')
        text = self._apply(
            {
                "fields": [{"name": "External ID", "id": "customfield_10050", "clauseNames": ["cf[10050]"]}],
                "searches": [{"jql": jql, "issues": [{"key": "WAR-1", "fields": {"customfield_10050": "WV-01"}}]}],
            }
        )
        self.assertIn("WV-01 -> WAR-1 (external, high)", text)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        ticket = self.tickets()["WV-01"]
        self.assertEqual(ticket["jiraKeySource"], "external")
        self.assertEqual(ticket["jiraKeyConfidence"], "high")
        saved = json.loads((self.repo / ".warp/jira-map.json").read_text())["WV-01"]
        self.assertEqual(saved, {"key": "WAR-1", "source": "external", "confidence": "high"})
        run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "external")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeyConfidence"], "high")

    def test_manual_key_is_not_overwritten(self):
        self.seed()
        run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--jira", "WAR-9", cwd=self.repo)
        jql = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        text = self._apply({"searches": [{"jql": jql, "issues": [{"key": "WAR-1", "externalId": "WV-01"}]}]})
        self.assertEqual(self.key("WV-01"), "WAR-9")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "manual")
        self.assertNotIn("WAR-1", text)
        self.assertEqual(json.loads((self.repo / ".warp/jira-map.json").read_text())["WV-01"], "WAR-9")

    def test_miss_leaves_the_key_empty(self):
        self.seed()
        text = self._apply({"searches": []})
        self.assertIsNone(self.key("WV-01"))
        self.assertIn("WV-01 no match", text)
        self.assertFalse((self.repo / ".warp/jira-map.json").exists())

    def test_ambiguous_external_id_does_not_fall_through_to_a_label(self):
        self.seed()
        external = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        label = jira_lookup.jql_equals("labels", "warp:WV-01", "WAR")
        text = self._apply(
            {
                "searches": [
                    {"jql": external, "issues": [{"key": "WAR-1"}, {"key": "WAR-2"}]},
                    {"jql": label, "issues": [{"key": "WAR-9", "labels": ["warp:WV-01"]}]},
                ]
            }
        )
        self.assertIsNone(self.key("WV-01"))
        self.assertIn("ambiguous (WAR-1, WAR-2)", text)
        self.assertNotIn("WAR-9", text)
        self.assertEqual(self.tickets()["WV-01"]["jiraKeyCandidates"], ["WAR-1", "WAR-2"])
        self.assertFalse((self.repo / ".warp/jira-map.json").exists())

    def test_missing_field_is_skipped(self):
        self.seed()
        self.cfg('jiraProject: WAR\njiraExternalIdField: "External ID"\n')
        tempting = jira_lookup.jql_equals(jira_lookup.clause_for("External ID"), "WV-01", "WAR")
        text = self._apply(
            {
                "fields": [{"name": "Summary", "id": "summary"}],
                "searches": [{"jql": tempting, "issues": [{"key": "WAR-1", "fields": {"External ID": "WV-01"}}]}],
            }
        )
        self.assertIsNone(self.key("WV-01"))
        self.assertIn("field not found: External ID", text)
        self.assertNotIn("-> WAR-1", text)

    def test_unknown_field_error_continues_to_the_next_name(self):
        self.seed()
        first = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        second = jira_lookup.jql_equals(jira_lookup.clause_for("External ID"), "WV-01", "WAR")
        text = self._apply(
            {
                "searches": [
                    {"jql": first, "error": "no such field", "field": "externalId"},
                    {"jql": second, "issues": [{"key": "WAR-1", "fields": {"External ID": "WV-01"}}]},
                ]
            }
        )
        self.assertIn("field not found: externalId", text)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "external")

    def test_label_then_remote_link_then_summary(self):
        self.seed()
        label = jira_lookup.jql_equals("labels", "warp:WV-01", "WAR")
        text = self._apply({"searches": [{"jql": label, "issues": [{"key": "WAR-3", "labels": ["warp:WV-01"]}]}]})
        self.assertEqual(self.key("WV-01"), "WAR-3")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "label")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeyConfidence"], "medium")

        self.seed()
        text = self._apply({"remoteLinks": [{"key": "WAR-4", "ids": ["https://plans.example/WV-01"]}]})
        self.assertIn("link, medium", text)
        self.assertEqual(self.key("WV-01"), "WAR-4")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "link")

        self.seed()
        summary = jira_lookup.summary_jql("Build the widget", "WAR")
        quiet = self._apply({"searches": [{"jql": summary, "issues": [{"key": "WAR-6", "summary": "Build the widget"}]}]})
        self.assertIn("propose WV-01=WAR-6", quiet)
        self.assertIn("Not written", quiet)
        self.assertIsNone(self.key("WV-01"))
        stored = self._apply(
            {"searches": [{"jql": summary, "issues": [{"key": "WAR-6", "summary": "Build the widget"}]}]},
            confirm_summary=True,
        )
        self.assertIn("summary, low", stored)
        self.assertEqual(self.key("WV-01"), "WAR-6")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeyConfidence"], "low")

    def test_from_jira_dry_run_and_auto(self):
        self.seed()
        listed = run("jira_sync.py", "map", "--beam", self.beam, "--from-jira", cwd=self.repo)
        self.assertIn("nothing written", listed.stdout)
        self.assertIn("external id", listed.stdout)
        payload = json.loads((self.repo / ".warp/jira-resolve.json").read_text())
        self.assertEqual(payload["tickets"][0]["jql"], 'project = WAR AND externalId = "WV-01"')
        kinds = [row["kind"] for row in payload["tickets"][0]["queries"]]
        self.assertEqual(kinds[0], "external")
        self.assertIn("label", kinds)
        self.assertIn("link", kinds)
        self.assertIsNone(self.key("WV-01"))

        jql = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        results = self.repo / "results.json"
        results.write_text(json.dumps({"searches": [{"jql": jql, "issues": [{"key": "WAR-1", "externalId": "WV-01"}]}]}))
        dry = run(
            "jira_sync.py",
            "map",
            "--beam",
            self.beam,
            "--from-jira",
            "--results",
            "results.json",
            "--dry-run",
            cwd=self.repo,
        )
        self.assertIn("dry-run WV-01 -> WAR-1 (external, high)", dry.stdout)
        self.assertIn("Not written", dry.stdout)
        self.assertIsNone(self.key("WV-01"))
        self.assertFalse((self.repo / ".warp/jira-map.json").exists())
        stored = run("jira_sync.py", "map", "--beam", self.beam, "--auto", "--results", "results.json", cwd=self.repo)
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertEqual(self.tickets()["WV-01"]["jiraKeySource"], "external")

    def test_resolve_transcript_on_a_claimed_ticket_requests_the_transition(self):
        self.seed()
        run("beam.py", "set", "--beam", self.beam, "--id", "WV-01", "--status", "claimed", "--agent", "s", cwd=self.repo)
        jql = jira_lookup.jql_equals("externalId", "WV-01", "WAR")
        results = self.repo / "results.json"
        results.write_text(json.dumps({"searches": [{"jql": jql, "issues": [{"key": "WAR-1"}]}]}))
        r = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "results.json", cwd=self.repo)
        self.assertEqual(self.key("WV-01"), "WAR-1")
        self.assertIn("MUST DO", r.stdout)
        self.assertIn("WAR-1", r.stdout)


if __name__ == "__main__":
    unittest.main()
