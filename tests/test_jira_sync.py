"""Run with: python3 -m unittest discover -s tests"""

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
import jira_sync  # noqa: E402

PLAN = ROOT / "examples" / "CURSOR_PLAN.sample.md"
CFG = {"jiraTransition": True, "jiraInProgressStatus": "In Progress", "jiraRestoreOnRelease": False}

T_START = {"id": "A", "name": "Start progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}
T_REVIEW = {"id": "B", "name": "Send to review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}}
T_DONE = {"id": "C", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}}
T_TODO = {"id": "D", "name": "Back to To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}}


def run(script, *args, cwd):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


class PlanTests(unittest.TestCase):
    def test_ticket_without_key_is_skipped(self):
        p = jira_sync.plan({"id": "API-01", "jiraKey": None}, "claim", CFG)
        self.assertEqual(p["action"], "skip")
        self.assertIn("no Jira key", p["reason"])

    def test_plan_ids_that_look_like_keys_are_not_keys(self):
        self.assertIsNone(jira_sync.jira_key({"id": "HOS-1", "jiraKey": None}))
        self.assertIsNone(jira_sync.jira_key({"jiraKey": "not a key"}))
        self.assertEqual(jira_sync.jira_key({"jiraKey": "HOS-12"}), "HOS-12")

    def test_claim_transitions_to_configured_status(self):
        p = jira_sync.plan({"id": "A", "jiraKey": "HOS-1"}, "claim", {**CFG, "jiraInProgressStatus": "Doing"})
        self.assertEqual((p["action"], p["target"]), ("transition", "Doing"))

    def test_claim_is_idempotent_and_can_be_turned_off(self):
        t = {"id": "A", "jiraKey": "HOS-1", "jira": {"startedAt": "2026-01-01T00:00:00Z"}}
        self.assertEqual(jira_sync.plan(t, "claim", CFG)["action"], "skip")
        self.assertEqual(jira_sync.plan({"id": "A", "jiraKey": "HOS-1"}, "claim", {**CFG, "jiraTransition": False})["action"], "skip")

    def test_release_leaves_issue_unless_configured(self):
        t = {"id": "A", "jiraKey": "HOS-1", "jira": {"startedAt": "x", "previousStatus": "To Do"}}
        self.assertEqual(jira_sync.plan(t, "release", CFG)["action"], "skip")
        p = jira_sync.plan(t, "release", {**CFG, "jiraRestoreOnRelease": True})
        self.assertEqual((p["action"], p["target"]), ("transition", "To Do"))

    def test_release_does_not_restore_what_warp_did_not_move(self):
        t = {"id": "A", "jiraKey": "HOS-1", "jira": {}}
        self.assertEqual(jira_sync.plan(t, "release", {**CFG, "jiraRestoreOnRelease": True})["action"], "skip")


class PickTests(unittest.TestCase):
    def test_matches_transition_name_then_target_status_case_insensitively(self):
        r = jira_sync.pick([T_REVIEW, T_START], "start PROGRESS")
        self.assertEqual(r["transition"]["id"], "A")
        r = jira_sync.pick([T_REVIEW, T_START], "in progress")
        self.assertEqual((r["transition"]["id"], r["how"]), ("A", "status"))

    def test_falls_back_to_status_category_when_name_differs(self):
        r = jira_sync.pick([T_DONE, T_TODO, T_START], "Doing")
        self.assertEqual((r["transition"]["id"], r["how"]), ("A", "category"))

    def test_several_in_progress_like_prefers_progress_named(self):
        r = jira_sync.pick([T_REVIEW, T_START], "Doing")
        self.assertEqual(r["transition"]["id"], "A")
        r = jira_sync.pick([T_REVIEW, {**T_REVIEW, "id": "Z", "name": "QA", "to": {"name": "QA", "statusCategory": {"key": "indeterminate"}}}], "Doing")
        self.assertIsNone(r["transition"])
        self.assertEqual(r["result"], "no-transition")

    def test_no_matching_transition(self):
        r = jira_sync.pick([T_DONE, T_TODO], "In Progress")
        self.assertIsNone(r["transition"])
        self.assertEqual(r["result"], "no-transition")
        self.assertIn("offered", r["reason"])

    def test_already_in_progress_or_done_is_not_touched(self):
        self.assertEqual(jira_sync.pick([T_START], "In Progress", {"name": "In Progress", "category": "indeterminate"})["result"], "already")
        self.assertEqual(jira_sync.pick([T_START], "In Progress", {"name": "In Review", "category": "indeterminate"})["result"], "already")
        r = jira_sync.pick([T_START], "In Progress", {"name": "Done", "category": "done"})
        self.assertEqual(r["result"], "skipped")

    def test_flat_transition_shape(self):
        r = jira_sync.pick([{"id": "9", "name": "Go", "toName": "Working", "category": "indeterminate"}], "In Progress")
        self.assertEqual(r["transition"]["id"], "9")

    def test_restore_matches_by_name_only(self):
        r = jira_sync.pick([T_START, T_TODO], "To Do", allow_category=False)
        self.assertEqual(r["transition"]["id"], "D")
        r = jira_sync.pick([T_START], "Backlog", allow_category=False)
        self.assertIsNone(r["transition"])

    def test_cli_pick(self):
        r = run("jira_sync.py", "pick", "--target", "In Progress", "--transitions", json.dumps({"transitions": [T_START]}), cwd=ROOT)
        self.assertEqual(json.loads(r.stdout)["transition"]["id"], "A")


class SettingsTests(unittest.TestCase):
    def test_defaults_beam_copy_then_yaml(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        beam = d / "beam.json"
        self.assertEqual(jira_sync.settings(beam, {}), CFG)
        self.assertEqual(jira_sync.settings(beam, {"config": {"jiraInProgressStatus": "Doing"}})["jiraInProgressStatus"], "Doing")
        (d / "config.yaml").write_text('jiraTransition: false\njiraInProgressStatus: "Working"  # name\njiraRestoreOnRelease: true\n')
        s = jira_sync.settings(beam, {"config": {"jiraInProgressStatus": "Doing"}})
        self.assertEqual(s, {"jiraTransition": False, "jiraInProgressStatus": "Working", "jiraRestoreOnRelease": True})


class BeamFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "jira").mkdir(parents=True)
        (self.repo / "jira/tickets.json").write_text(
            json.dumps({"tickets": [{"key": "HOS-1", "summary": "a"}, {"key": "HOS-2", "summary": "b", "blockedBy": ["HOS-1"]}, {"tempId": "T-3", "summary": "c"}]})
        )
        r = run("scan.py", "scan", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.beam = ".warp/beam.json"

    def beam_json(self):
        return json.loads((self.repo / self.beam).read_text())

    def set(self, tid, status, *extra):
        return run("beam.py", "set", "--beam", self.beam, "--id", tid, "--status", status, *extra, cwd=self.repo)

    def test_jira_export_gives_keys_and_markdown_does_not(self):
        keys = {k: v["jiraKey"] for k, v in self.beam_json()["tickets"].items()}
        self.assertEqual(keys, {"HOS-1": "HOS-1", "HOS-2": "HOS-2", "T-3": None})
        md = self.tmp / "md"
        md.mkdir()
        shutil.copy(PLAN, md / "CURSOR_PLAN.md")
        run("scan.py", "scan", cwd=md)
        self.assertTrue(all(t["jiraKey"] is None for t in json.loads((md / ".warp/beam.json").read_text())["tickets"].values()))

    def test_claim_prints_jira_instruction_for_keyed_ticket(self):
        r = self.set("HOS-1", "claimed", "--agent", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertIn('move to "In Progress"', r.stdout)
        self.assertIn("connected Jira MCP", r.stdout)
        self.assertIn("The claim stands", r.stdout)
        self.assertEqual(self.beam_json()["tickets"]["HOS-1"]["status"], "claimed")

    def test_claim_without_key_skips_but_succeeds(self):
        r = self.set("T-3", "claimed", "--agent", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no Jira key", r.stdout)
        self.assertEqual(self.beam_json()["tickets"]["T-3"]["status"], "claimed")

    def test_only_the_claim_triggers_it(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        r = self.set("HOS-1", "planning")
        self.assertNotIn("jira:", r.stdout)

    def test_record_moved_then_no_second_instruction(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        r = run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "claim", "--result", "moved", "--from", "To Do", "--to", "In Progress", cwd=self.repo)
        self.assertEqual(r.returncode, 0)
        j = self.beam_json()["tickets"]["HOS-1"]["jira"]
        self.assertEqual((j["status"], j["previousStatus"]), ("In Progress", "To Do"))
        self.assertTrue(j["startedAt"])
        p = run("jira_sync.py", "plan", "--beam", self.beam, "--id", "HOS-1", "--event", "claim", cwd=self.repo)
        self.assertIn("already moved by Warp", p.stdout)

    def test_unavailable_jira_is_logged_to_outbox_and_exits_zero(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        for result in ("unavailable", "no-transition", "failed"):
            r = run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "claim", "--result", result, cwd=self.repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("The claim was not affected", r.stdout)
        box = (self.repo / ".warp/outbox.md").read_text()
        self.assertIn("Jira is not connected", box)
        self.assertIn("no matching transition", box)
        self.assertIn("HOS-1", box)
        self.assertEqual(self.beam_json()["tickets"]["HOS-1"]["status"], "claimed")
        self.assertIsNone(self.beam_json()["tickets"]["HOS-1"]["jira"].get("startedAt"))

    def test_release_leaves_issue_by_default(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "claim", "--result", "moved", "--from", "To Do", "--to", "In Progress", cwd=self.repo)
        r = self.set("HOS-1", "queued", "--agent", "")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("jira:", r.stdout)

    def test_release_restores_when_configured(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "claim", "--result", "moved", "--from", "To Do", "--to", "In Progress", cwd=self.repo)
        (self.repo / ".warp/config.yaml").write_text("jiraRestoreOnRelease: true\n")
        r = self.set("HOS-1", "queued", "--agent", "")
        self.assertIn('move back to "To Do"', r.stdout)
        self.assertIn("--no-category", r.stdout)
        run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "release", "--result", "moved", "--to", "To Do", cwd=self.repo)
        j = self.beam_json()["tickets"]["HOS-1"]["jira"]
        self.assertNotIn("startedAt", j)
        self.assertEqual(j["status"], "To Do")

    def test_pause_and_stop_never_touch_jira(self):
        self.set("HOS-1", "claimed", "--agent", "s1")
        for cmd in ("pause", "stop"):
            r = run("scan.py", cmd, "--beam", self.beam, cwd=self.repo)
            self.assertNotIn("jira:", r.stdout)
        self.assertEqual(self.beam_json()["tickets"]["HOS-1"]["status"], "claimed")

    def test_disabled_by_config(self):
        (self.repo / ".warp/config.yaml").write_text("jiraTransition: false\n")
        r = self.set("HOS-1", "claimed", "--agent", "s1")
        self.assertIn("jiraTransition is false", r.stdout)

    def test_jira_key_survives_export_and_import(self):
        run("scan.py", "export", cwd=self.repo)
        r = run("scan.py", "import", "--plan", ".warp/WARP_PLAN.json", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.beam_json()["tickets"]["HOS-2"]["jiraKey"], "HOS-2")


if __name__ == "__main__":
    unittest.main()
