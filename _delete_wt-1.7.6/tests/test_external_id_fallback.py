"""Missing External ID field: create when allowed, otherwise label or remote-link."""

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
import jira_match  # noqa: E402
import jira_sync  # noqa: E402
import jira_view  # noqa: E402


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


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


MAPPED = dict(jiraKey="WAR-1", jiraKeySource="map", jiraKeyConfidence="high", jiraMapping="mapped")


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        self.config('jiraProject: "WAR"\njiraWriteExternalId: true\njiraExternalIdField: externalId\n')
        self.write([ticket("WV-01", "Build the widget", **MAPPED)])

    def config(self, text):
        (self.warp / "config.yaml").write_text(text if text.endswith("\n") else text + "\n")

    def write(self, tickets):
        self.beam.write_text(json.dumps({"tickets": {row["id"]: row for row in tickets}, "config": {}}))

    def load(self):
        return json.loads(self.beam.read_text())

    def plant_missing(self):
        beam = self.load()
        for row in beam["tickets"].values():
            row["jira"]["externalIdAttempt"] = {
                "at": "t0",
                "result": "skipped",
                "error": "field missing",
                "event": "external-id",
            }
        self.beam.write_text(json.dumps(beam))

    def test_create_tool_records_id_and_does_not_overwrite_yaml(self):
        data = {
            "tools": ["createJiraField", "updateJiraScreen", "editJiraIssue", "getJiraIssueEditmeta"],
            "createField": {"tool": "createJiraField", "result": "created", "id": "customfield_10200", "name": "External ID"},
            "screen": {"tool": "updateJiraScreen", "result": "updated"},
            "fields": [{"id": "customfield_10200", "name": "External ID"}],
            "editmeta": {"fields": {"customfield_10200": {"name": "External ID"}}},
            "edits": [{"id": "WV-01", "key": "WAR-1", "before": "", "result": "updated", "method": "field"}],
        }
        text = jira_match.record_external_id(self.beam, data)
        self.assertIn("left as is", text)
        self.assertIn("jiraExternalIdField: customfield_10200", text)
        yaml = (self.warp / "config.yaml").read_text()
        self.assertIn("jiraExternalIdField: externalId", yaml)
        self.assertNotIn("customfield_10200", yaml)
        written = self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]
        self.assertEqual(written["value"], "WV-01")
        self.assertEqual(written["method"], "field")
        state = json.loads((self.warp / "jira-field.json").read_text())
        self.assertEqual(state["status"], "present")
        self.assertEqual(state["id"], "customfield_10200")

    def test_create_denied_falls_back_to_label(self):
        data = {
            "tools": ["createJiraField", "editJiraIssue"],
            "createField": {"tool": "createJiraField", "result": "denied", "error": "permission"},
            "fields": [{"id": "customfield_10011", "name": "Rank"}],
            "edits": [{"id": "WV-01", "key": "WAR-1", "method": "label", "result": "updated"}],
        }
        text = jira_match.record_external_id(self.beam, data)
        self.assertIn("not a failure", text)
        self.assertIn("permission", text)
        self.assertIn("Short text", text)
        written = self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]
        self.assertEqual(written["method"], "label")
        self.assertEqual(written["value"], "WV-01")
        self.assertNotIn("externalIdAttempt", self.load()["tickets"]["WV-01"]["jira"])

    def test_label_is_additive_and_idempotent(self):
        self.plant_missing()
        text = jira_match.external_id_backfill(self.beam, mode="must")
        self.assertIn("editJiraIssue WAR-1 update.labels add warp:WV-01", text)
        self.assertNotIn("fields.labels =", text)
        self.assertIn("Do not set fields.labels", text)
        self.assertNotIn("MUST DO ensure field", text)
        recorded = jira_match.record_external_id(
            self.beam,
            {
                "fields": [{"id": "customfield_10011", "name": "Rank"}],
                "edits": [{"id": "WV-01", "key": "WAR-1", "method": "label", "result": "updated"}],
            },
        )
        self.assertIn("added. Recorded.", recorded)
        again = jira_match.external_id_backfill(self.beam, mode="auto")
        self.assertNotIn("MUST DO write External ID", again)
        self.assertIn("already equal", again)
        self.assertIn("would write 0", again)
        report = jira_sync.key_report(self.load()["tickets"]["WV-01"], jira_sync.settings(self.beam, self.load()))
        self.assertIn("jira mapping: label warp:WV-01", report)

    def test_remote_link_method(self):
        self.config(
            'jiraProject: "WAR"\njiraWriteExternalId: true\njiraExternalIdField: externalId\n'
            "jiraExternalIdFallback: remote-link\n"
        )
        self.plant_missing()
        text = jira_match.external_id_backfill(self.beam, mode="must")
        self.assertIn("createJiraIssueRemoteIssueLink WAR-1 globalId warp:WV-01", text)
        self.assertNotIn("update.labels add", text)
        recorded = jira_match.record_external_id(
            self.beam,
            {
                "fields": [{"name": "Rank"}],
                "edits": [{"id": "WV-01", "key": "WAR-1", "method": "remote-link", "result": "updated"}],
            },
        )
        self.assertIn("remote link", recorded)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["method"], "remote-link")

    def test_noise_is_one_summary(self):
        self.write(
            [
                ticket("WV-01", "One", jiraKey="WAR-1", jiraKeySource="map", jiraKeyConfidence="high", jiraMapping="mapped"),
                ticket("WV-02", "Two", jiraKey="WAR-2", jiraKeySource="plan", jiraKeyConfidence="high", jiraMapping="mapped"),
            ]
        )
        self.plant_missing()
        text = jira_sync.prepare_resolve(self.beam)
        self.assertEqual(text.count("External ID field is missing"), 1)
        self.assertNotIn("WV-01 field missing", text)
        self.assertNotIn("WV-02 field missing", text)
        self.assertIn("update.labels add warp:WV-01", text)
        self.assertIn("update.labels add warp:WV-02", text)
        again = jira_sync.prepare_resolve(self.beam)
        self.assertEqual(again.count("External ID field is missing"), 1)
        self.assertNotIn("externalIdAttempt", self.beam.read_text())

    def test_config_gates_creation_and_fallback_none(self):
        self.plant_missing()
        off = jira_match.external_id_backfill(self.beam, mode="must")
        self.assertIn("--create-field --yes", off)
        self.assertNotIn("MUST DO ensure field", off)
        allowed = jira_match.external_id_backfill(self.beam, mode="must", create=True, recheck=True)
        self.assertIn("MUST DO ensure field", allowed)
        self.assertIn("createJiraField", allowed)
        self.config(
            'jiraProject: "WAR"\njiraWriteExternalId: true\njiraExternalIdField: externalId\n'
            "jiraExternalIdFallback: none\n"
        )
        self.plant_missing()
        quiet = jira_match.external_id_backfill(self.beam, mode="must")
        self.assertNotIn("update.labels", quiet)
        self.assertIn("jiraExternalIdFallback is none", quiet)
        self.assertNotIn("editJiraIssue WAR-1", quiet)

    def test_dry_run_shows_method_and_writes_nothing(self):
        self.plant_missing()
        before = self.beam.read_text()
        text = jira_match.external_id_backfill(self.beam, mode="dry")
        self.assertIn("method label", text)
        self.assertIn("dry-run", text)
        self.assertNotIn("MUST DO", text)
        self.assertEqual(self.beam.read_text(), before)
        self.assertFalse((self.warp / "jira-field.json").is_file())

    def test_recheck_ignores_the_saved_miss(self):
        cfg = jira_sync.settings(self.beam, self.load())
        (self.warp / "jira-field.json").write_text(
            json.dumps(
                {
                    "status": "missing",
                    "project": "WAR",
                    "checkedAt": "t0",
                    "fingerprint": jira_match.field_fingerprint(cfg),
                }
            )
        )
        cached = jira_match.external_id_backfill(self.beam, mode="dry")
        self.assertIn("method label", cached)
        text = jira_match.external_id_backfill(self.beam, mode="dry", recheck=True)
        self.assertIn("(method field)", text)
        self.assertNotIn("method label", text)

    def test_view_shows_how_the_mapping_is_stored(self):
        beam = self.load()
        beam["tickets"]["WV-01"]["jira"]["externalIdWritten"] = {"value": "WV-01", "at": "t0", "method": "label"}
        self.beam.write_text(json.dumps(beam))
        cfg = jira_sync.settings(self.beam, self.load())
        lines = "\n".join(jira_view.stored_lines("WV-01", "WAR-1", self.beam, cfg))
        self.assertIn("stored in Jira: WV-01: label warp:WV-01", lines)
        report = jira_sync.key_report(self.load()["tickets"]["WV-01"], cfg)
        self.assertIn("jira mapping: label warp:WV-01", report)

    def test_cli_help_and_label_apply(self):
        self.plant_missing()
        queued = run("jira_external_id.py", "--beam", str(self.beam), "--apply", "--yes", cwd=self.tmp)
        self.assertEqual(queued.returncode, 0, queued.stdout + queued.stderr)
        self.assertIn("update.labels add warp:WV-01", queued.stdout)
        help_text = run("jira_external_id.py", "?", cwd=self.tmp)
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        for needle in ("--create-field", "--recheck", "update.labels", "createJiraField", "Short text"):
            self.assertIn(needle, help_text.stdout)
