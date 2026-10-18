"""Bulk External ID write-back. No live Jira calls."""

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


FIELDS = [{"id": "customfield_10050", "name": "External ID"}]
EDITMETA = {"fields": {"customfield_10050": {"name": "External ID"}}}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.beam = self.warp / "beam.json"
        self.config("true")
        self.write(
            [
                ticket("WV-01", "Build the widget", jiraKey="WAR-1", jiraKeySource="map", jiraKeyConfidence="high", jiraMapping="mapped"),
                ticket("WV-02", "Second", jiraKey="WAR-2", jiraKeySource="plan", jiraKeyConfidence="high", jiraMapping="mapped"),
                ticket("WV-03", "From Jira", jiraKey="WAR-3", jiraKeySource="external", jiraKeyConfidence="high", jiraMapping="mapped"),
                ticket("WV-04", "No key"),
            ]
        )

    def config(self, flag):
        (self.warp / "config.yaml").write_text(f'jiraProject: "WAR"\njiraWriteExternalId: {flag}\n')

    def write(self, tickets):
        self.beam.write_text(json.dumps({"tickets": {row["id"]: row for row in tickets}, "config": {}}))

    def load(self):
        return json.loads(self.beam.read_text())

    def test_scan_queues_every_mapped_ticket_except_external_id_hits(self):
        text = jira_sync.prepare_resolve(self.beam)
        self.assertIn("MUST DO write External ID", text)
        self.assertIn("editJiraIssue WAR-1 field", text)
        self.assertIn("editJiraIssue WAR-2 field", text)
        self.assertNotIn("editJiraIssue WAR-3", text)
        self.assertIn("WV-01 -> WAR-1: External ID currently unknown -> would set WV-01", text)
        self.assertIn("WV-02 -> WAR-2: External ID currently unknown -> would set WV-02", text)
        self.assertIn("already equal (external id match)", text)
        self.assertIn("would write 2, already equal 1, skipped 1 (WV-04 no key)", text)
        self.assertIn("record-external-id", text)
        self.assertIn("does not post a comment", text)
        self.assertNotIn("externalIdWritten", json.dumps(self.load()))
        saved = json.loads((self.warp / "jira-external-id.json").read_text())
        self.assertEqual([row["key"] for row in saved["pairs"]], ["WAR-1", "WAR-2"])

    def test_flag_off_emits_nothing(self):
        self.config("false")
        text = jira_sync.prepare_resolve(self.beam)
        self.assertNotIn("MUST DO write External ID", text)
        self.assertNotIn("editJiraIssue", text)
        catch = jira_sync.catchup(self.beam, None, False)
        self.assertNotIn("editJiraIssue", catch)

    def test_catchup_emits_the_same_todo(self):
        text = jira_sync.catchup(self.beam, None, False)
        self.assertIn("MUST DO write External ID", text)
        self.assertIn("editJiraIssue WAR-1", text)
        self.assertIn("editJiraIssue WAR-2", text)

    def test_idempotent_second_run_and_force(self):
        for tid in ("WV-01", "WV-02"):
            self.load  # keep lints quiet
        beam = self.load()
        for tid in ("WV-01", "WV-02"):
            beam["tickets"][tid]["jira"]["externalIdWritten"] = {"value": tid, "at": "t0"}
        self.beam.write_text(json.dumps(beam))
        quiet = jira_match.external_id_backfill(self.beam, mode="auto")
        self.assertNotIn("MUST DO", quiet)
        self.assertIn("would write 0, already equal 3", quiet)
        forced = jira_match.external_id_backfill(self.beam, mode="must", force=True)
        self.assertIn("MUST DO write External ID", forced)
        self.assertIn("editJiraIssue WAR-1", forced)
        self.assertIn("editJiraIssue WAR-3", forced)

    def test_different_value_is_protected(self):
        beam = self.load()
        beam["tickets"]["WV-01"]["jira"]["externalIdOnIssue"] = "ZZ-9"
        self.beam.write_text(json.dumps(beam))
        text = jira_match.external_id_backfill(self.beam, mode="dry")
        self.assertIn("currently ZZ-9 -> would set WV-01", text)
        self.assertIn("Pass --force-external-id", text)
        self.assertNotIn("editJiraIssue WAR-1", text)
        self.assertIn("WV-01 different value", text)
        allowed = jira_match.external_id_backfill(self.beam, mode="must", force_value=True)
        self.assertIn("editJiraIssue WAR-1 field", allowed)
        self.assertNotIn("externalIdWritten", json.dumps(self.load()["tickets"]["WV-01"]["jira"]))

    def test_dry_run_writes_nothing_on_the_beam(self):
        before = self.beam.read_text()
        text = jira_match.external_id_backfill(self.beam, mode="dry")
        self.assertIn("dry-run", text)
        self.assertIn("would write 2", text)
        self.assertEqual(self.beam.read_text(), before)
        self.assertFalse((self.warp / "jira-external-id.json").is_file())

    def test_record_success_missing_readonly_and_different(self):
        data = {
            "fields": FIELDS,
            "editmeta": EDITMETA,
            "issues": [
                {"key": "WAR-1", "fields": {"customfield_10050": ""}},
                {"key": "WAR-2", "fields": {"customfield_10050": "WV-02"}},
            ],
            "edits": [
                {"id": "WV-01", "key": "WAR-1", "before": "", "result": "updated"},
                {"id": "WV-02", "key": "WAR-2", "before": "WV-02", "result": "already"},
            ],
        }
        text = jira_match.record_external_id(self.beam, data)
        self.assertIn("WV-01 WAR-1 before empty after WV-01. Recorded.", text)
        self.assertIn("already equal", text)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["value"], "WV-01")
        self.assertEqual(self.load()["tickets"]["WV-02"]["jira"]["externalIdWritten"]["value"], "WV-02")
        again = jira_match.external_id_backfill(self.beam, mode="auto")
        self.assertNotIn("MUST DO", again)

        self.write(
            [ticket("WV-01", "Build", jiraKey="WAR-1", jiraKeySource="map", jiraKeyConfidence="high", jiraMapping="mapped")]
        )
        missing = jira_match.record_external_id(
            self.beam,
            {"fields": [{"id": "summary", "name": "Summary"}], "editmeta": {"fields": {"summary": {"name": "Summary"}}}, "edits": []},
        )
        self.assertIn("field is missing", missing)
        self.assertIn("written 0", missing)
        self.assertIn("--create-field --yes", missing)
        self.assertNotIn("WV-01 field missing", missing)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])
        state = json.loads((self.warp / "jira-field.json").read_text())
        self.assertEqual(state["status"], "missing")
        listed = jira_match.external_id_backfill(self.beam, mode="dry")
        self.assertIn("method label", listed)
        self.assertIn("warp:WV-01", listed)
        self.assertNotIn("WV-01 field missing", listed)
        report = jira_sync.key_report(self.load()["tickets"]["WV-01"], jira_sync.settings(self.beam, self.load()))
        self.assertIn("jira mapping: none", report)
        self.assertNotIn("externalIdAttempt", report)

        self.write(
            [ticket("WV-01", "Build", jiraKey="WAR-1", jiraKeySource="map", jiraKeyConfidence="high", jiraMapping="mapped")]
        )
        readonly = jira_match.record_external_id(
            self.beam,
            {"fields": FIELDS, "editmeta": {"fields": {"summary": {"name": "Summary"}}}, "edits": [{"id": "WV-01", "key": "WAR-1", "result": "updated"}]},
        )
        self.assertIn("not editable", readonly)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])

        self.write(
            [ticket("WV-01", "Build", jiraKey="WAR-1", jiraKeySource="manual", jiraKeyConfidence="high", jiraMapping="mapped")]
        )
        blocked = jira_match.record_external_id(
            self.beam,
            {
                "fields": FIELDS,
                "editmeta": EDITMETA,
                "edits": [{"id": "WV-01", "key": "WAR-1", "before": "ZZ-9", "result": "updated"}],
            },
        )
        self.assertIn("before ZZ-9 after WV-01", blocked)
        self.assertIn("Not written", blocked)
        self.assertNotIn("externalIdWritten", self.load()["tickets"]["WV-01"]["jira"])
        self.assertIn("different value", blocked)
        forced = jira_match.record_external_id(
            self.beam,
            {
                "fields": FIELDS,
                "editmeta": EDITMETA,
                "edits": [{"id": "WV-01", "key": "WAR-1", "before": "ZZ-9", "result": "updated"}],
            },
            force_value=True,
        )
        self.assertIn("Recorded.", forced)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["value"], "WV-01")

    def test_cli_dry_run_apply_and_record(self):
        dry = run("jira_external_id.py", "--beam", str(self.beam), cwd=self.tmp)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("dry-run", dry.stdout)
        self.assertNotIn("MUST DO", dry.stdout)
        self.assertNotIn("externalIdWritten", self.beam.read_text())
        queued = run("jira_external_id.py", "--beam", str(self.beam), "--apply", "--yes", "--ticket", "WV-01", cwd=self.tmp)
        self.assertEqual(queued.returncode, 0, queued.stdout + queued.stderr)
        self.assertIn("MUST DO write External ID", queued.stdout)
        self.assertIn("editJiraIssue WAR-1", queued.stdout)
        self.assertNotIn("editJiraIssue WAR-2", queued.stdout)
        edits = self.tmp / "edits.json"
        edits.write_text(
            json.dumps(
                {
                    "fields": FIELDS,
                    "editmeta": EDITMETA,
                    "edits": [{"id": "WV-01", "key": "WAR-1", "before": "", "result": "updated"}],
                }
            )
        )
        recorded = run(
            "jira_sync.py",
            "record-external-id",
            "--beam",
            str(self.beam),
            "--results",
            str(edits),
            cwd=self.tmp,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertIn("written 1", recorded.stdout)
        self.assertEqual(self.load()["tickets"]["WV-01"]["jira"]["externalIdWritten"]["value"], "WV-01")
        help_text = run("jira_external_id.py", "?", cwd=self.tmp)
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        for needle in ("--apply", "--yes", "--force", "--force-external-id", "--ticket", "--results", "editJiraIssue", "record-external-id"):
            self.assertIn(needle, help_text.stdout)
