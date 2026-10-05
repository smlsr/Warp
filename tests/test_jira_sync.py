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
CFG = dict(jira_sync.DEFAULTS)

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

    def test_infer_key_from_id_summary_or_branch(self):
        abc = ["ABC"]
        self.assertIsNone(jira_sync.infer_key({"id": "WV-01", "jiraKey": None}))
        self.assertIsNone(jira_sync.infer_key({"id": "WV-01", "jiraKey": None}, ["WAR"]))
        self.assertEqual(jira_sync.infer_key({"id": "ABC-123", "jiraKey": None}, abc), "ABC-123")
        self.assertEqual(jira_sync.infer_key({"id": "T-9", "summary": "Ship ABC-123"}, abc), "ABC-123")
        self.assertEqual(jira_sync.infer_key({"id": "T-8", "branch": "warp/T-8-ABC-123"}, abc), "ABC-123")
        self.assertIsNone(jira_sync.infer_key({"id": "T-3", "summary": "no key here"}, abc))
        self.assertIsNone(jira_sync.jira_key({"id": "ABC-123", "jiraKey": None}))
        self.assertIsNone(jira_sync.jira_key({"id": "WV-01", "jiraKey": "WV-01"}, ["WAR"]))

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

    def test_manual_path_goes_to_qa_ready_then_done(self):
        manual = {"id": "L-01", "jiraKey": "HOS-9", "autoMerge": False}
        p = jira_sync.plan(manual, "qa-ready", CFG)
        self.assertEqual((p["action"], p["target"], p["kind"]), ("transition", "QA Ready", "qa"))
        p = jira_sync.plan(manual, "done", CFG)
        self.assertEqual((p["action"], p["target"], p["kind"]), ("transition", "Done", "done"))
        waiting = {**manual, "jira": {"qaReadyAt": "2026-01-01T00:00:00Z"}}
        self.assertEqual(jira_sync.plan(waiting, "done", CFG)["action"], "transition")
        p = jira_sync.plan(manual, "done", {**CFG, "jiraDoneOnManualMerge": False, "jiraQaReadyStatus": "Ready for QA"})
        self.assertEqual(p["action"], "skip")
        self.assertIn("jiraDoneOnManualMerge is false", p["reason"])
        self.assertIn("Ready for QA", p["reason"])

    def test_auto_path_goes_to_done_and_not_qa_ready(self):
        auto = {"id": "S-01", "jiraKey": "HOS-3", "autoMerge": True}
        p = jira_sync.plan(auto, "done", {**CFG, "jiraDoneStatus": "Complete"})
        self.assertEqual((p["action"], p["target"], p["kind"]), ("transition", "Complete", "done"))
        self.assertEqual(jira_sync.plan(auto, "qa-ready", CFG)["action"], "skip")
        done = {**auto, "jira": {"doneAt": "2026-01-01T00:00:00Z"}}
        self.assertIn("already moved to Done", jira_sync.plan(done, "done", CFG)["reason"])


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

    def test_qa_ready_is_name_only_and_done_uses_category(self):
        qa = {"id": "Q", "name": "Send to QA", "to": {"name": "QA Ready", "statusCategory": {"key": "indeterminate"}}}
        r = jira_sync.pick([T_START, qa], "Ready for QA", kind="qa")
        self.assertEqual(r["result"], "no-transition")
        r = jira_sync.pick([qa], "QA Ready", kind="qa")
        self.assertEqual(r["transition"]["id"], "Q")
        r = jira_sync.pick([qa], "QA Ready", {"name": "Done", "category": "done"}, kind="qa")
        self.assertEqual(r["result"], "skipped")
        r = jira_sync.pick([T_DONE, T_START], "Complete", kind="done")
        self.assertEqual((r["transition"]["id"], r["how"]), ("C", "category"))
        r = jira_sync.pick([T_DONE], "Done", {"name": "Done", "category": "done"}, kind="done")
        self.assertEqual(r["result"], "already")
        r = jira_sync.pick([qa, T_DONE], "Done", {"name": "QA Ready", "category": "indeterminate"}, kind="done")
        self.assertEqual((r["transition"]["id"], r["how"]), ("C", "name"))
        r = jira_sync.pick([qa], "Done", {"name": "QA Ready", "category": "indeterminate"}, kind="done")
        self.assertEqual(r["result"], "no-transition")
        self.assertIn("no transition to Done", r["reason"])

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
        self.assertFalse(s["jiraTransition"])
        self.assertEqual(s["jiraInProgressStatus"], "Working")
        self.assertTrue(s["jiraRestoreOnRelease"])
        self.assertEqual(s["jiraQaReadyStatus"], "QA Ready")
        self.assertEqual(s["jiraDoneStatus"], "Done")
        self.assertEqual(s["jiraMcp"], "atlassian")
        import beam
        import herald_fmt

        for key, value in jira_sync.DEFAULTS.items():
            self.assertEqual(beam.default_config()[key], value)
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        heard = herald_fmt.read_config(empty)
        self.assertEqual(heard["jiraInProgressStatus"], "In Progress")
        self.assertEqual(heard["jiraQaReadyStatus"], "QA Ready")
        self.assertEqual(heard["jiraDoneStatus"], "Done")
        self.assertEqual(heard["jiraTransition"], "true")


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

    def test_jira_export_and_markdown_plan_both_get_keys(self):
        keys = {k: v["jiraKey"] for k, v in self.beam_json()["tickets"].items()}
        self.assertEqual(keys, {"HOS-1": "HOS-1", "HOS-2": "HOS-2", "T-3": None})
        md = self.tmp / "md"
        md.mkdir()
        shutil.copy(PLAN, md / "CURSOR_PLAN.md")
        run("scan.py", "scan", cwd=md)
        keys = {t["id"]: t["jiraKey"] for t in json.loads((md / ".warp/beam.json").read_text())["tickets"].values()}
        self.assertIsNone(keys["API-01"])
        self.assertIsNone(keys["BILL-01"])
        self.assertTrue(all(v is None for v in keys.values()))

    def test_claim_prints_jira_instruction_for_keyed_ticket(self):
        r = self.set("HOS-1", "claimed", "--agent", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertIn('move to "In Progress"', r.stdout)
        self.assertIn("connected Jira MCP", r.stdout)
        self.assertIn("Nothing else is blocked", r.stdout)
        self.assertIn("jira: MUST DO", r.stdout)
        self.assertIn("getTransitionsForJiraIssue", r.stdout)
        self.assertIn("addOrEditJiraIssueComment", r.stdout)
        self.assertIn("getAccessibleAtlassianResources", r.stdout)
        self.assertEqual(self.beam_json()["tickets"]["HOS-1"]["status"], "claimed")

    def test_claim_without_key_tries_external_id_before_it_flags(self):
        box = self.repo / ".warp/outbox.md"
        if box.exists():
            box.unlink()
        r = self.set("T-3", "claimed", "--agent", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("RESOLVE", r.stdout)
        self.assertIn("external id", r.stdout)
        self.assertNotIn("MUST DO", r.stdout)
        self.assertFalse((self.repo / ".warp/outbox.md").exists())
        self.assertEqual(self.beam_json()["tickets"]["T-3"]["status"], "claimed")
        results = self.repo / "results.json"
        results.write_text(json.dumps({"T-3": []}))
        missed = run("jira_sync.py", "resolve", "--beam", self.beam, "--apply", "results.json", cwd=self.repo)
        self.assertEqual(missed.returncode, 0, missed.stdout + missed.stderr)
        self.assertIn("no Jira key", missed.stdout)
        self.assertIn("needs mapping", missed.stdout)
        self.assertNotIn("MUST DO", missed.stdout)
        self.assertIn("needs mapping", (self.repo / ".warp/outbox.md").read_text())

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
        self.assertIn("--kind restore", r.stdout)
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

    def _auto(self, tid, auto):
        beam = self.beam_json()
        beam["tickets"][tid]["autoMerge"] = auto
        beam["tickets"][tid]["size"] = "S" if auto else "L"
        (self.repo / ".warp/beam.json").write_text(json.dumps(beam))

    def _green(self, tid):
        r = run("beam.py", "set", "--beam", self.beam, "--id", tid, "--bugbot", "pass", "--ci", "green", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_awaiting_approval_moves_manual_ticket_to_qa_ready(self):
        self._auto("HOS-1", False)
        self._green("HOS-1")
        r = self.set("HOS-1", "awaiting_approval")
        self.assertIn('ready for manual review and merge, so move to "QA Ready"', r.stdout)
        self.assertIn("Bugbot clean, ready for manual review", r.stdout)
        self.assertIn("--kind qa", r.stdout)
        r = self.set("HOS-1", "merged", "--sha", "abc1234", "--via", "connected", "--proceeded-by", "alex")
        self.assertIn('merged, so move to "Done"', r.stdout)
        self.assertIn("post-merge MUST DO", r.stdout)
        self.assertIn("Approved by alex", r.stdout)
        self.assertIn("slack reply: Merged HOS-1 sha abc1234. Jira status Done.", r.stdout)
        r = self.set("HOS-1", "done")
        self.assertNotIn("jira:", r.stdout)

    def test_merge_moves_auto_ticket_to_done(self):
        self._auto("HOS-1", True)
        self._green("HOS-1")
        r = self.set("HOS-1", "awaiting_approval")
        self.assertNotIn("jira:", r.stdout)
        r = self.set("HOS-1", "merged")
        self.assertIn('merged, so move to "Done"', r.stdout)
        self.assertIn("--kind done", r.stdout)
        run("jira_sync.py", "record", "--beam", self.beam, "--id", "HOS-1", "--event", "done", "--result", "moved", "--to", "Done", cwd=self.repo)
        self.assertTrue(self.beam_json()["tickets"]["HOS-1"]["jira"]["doneAt"])
        r = self.set("HOS-1", "done")
        self.assertNotIn("jira:", r.stdout)

    def test_jira_key_survives_export_and_import(self):
        run("scan.py", "export", cwd=self.repo)
        r = run("scan.py", "import", "--plan", ".warp/WARP_PLAN.json", cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.beam_json()["tickets"]["HOS-2"]["jiraKey"], "HOS-2")


if __name__ == "__main__":
    unittest.main()
