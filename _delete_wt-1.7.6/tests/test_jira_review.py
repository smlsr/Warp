"""In Review moves. Run with: python3 -m unittest tests.test_jira_review"""

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

CFG = dict(jira_sync.DEFAULTS)
OFFERED = [
    {"id": "11", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "44", "name": "In Review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}},
    {"id": "22", "name": "QA Ready", "to": {"name": "QA Ready", "statusCategory": {"key": "indeterminate"}}},
    {"id": "33", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]


def run(cwd, script, *args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


def transitions(actions):
    return [a["plan"]["event"] for a in actions if a.get("type") == "transition"]


class InReviewPlanTests(unittest.TestCase):
    def test_missing_key_defaults_to_in_review(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        beam = d / "beam.json"
        self.assertEqual(jira_sync.settings(beam, {"config": {}})["jiraInReviewStatus"], "In Review")
        self.assertEqual(jira_sync.in_review_target(jira_sync.DEFAULTS), "In Review")

    def test_empty_string_and_null_turn_the_move_off(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        beam = d / "beam.json"
        for raw in ("", None, "null", "none", "~"):
            cfg = jira_sync.settings(beam, {"config": {"jiraInReviewStatus": raw}})
            self.assertEqual(cfg["jiraInReviewStatus"], "", raw)
            self.assertEqual(jira_sync.plan({"id": "T", "jiraKey": "WAR-1", "jira": {}}, "in-review", cfg)["action"], "skip")
        (d / "config.yaml").write_text('jiraInReviewStatus: ""\n')
        self.assertEqual(jira_sync.settings(beam, {"config": {}})["jiraInReviewStatus"], "")
        (d / "config.yaml").write_text("jiraInReviewStatus:\n")
        self.assertEqual(jira_sync.settings(beam, {"config": {}})["jiraInReviewStatus"], "")
        (d / "config.yaml").write_text("jiraInReviewStatus: null\n")
        self.assertEqual(jira_sync.settings(beam, {"config": {}})["jiraInReviewStatus"], "")

    def test_plan_is_idempotent_and_does_not_move_backward(self):
        ticket = {"id": "T", "jiraKey": "WAR-1", "autoMerge": True, "jira": {}}
        planned = jira_sync.plan(ticket, "in-review", CFG)
        self.assertEqual((planned["action"], planned["target"], planned["kind"]), ("transition", "In Review", "review"))
        again = {**ticket, "jira": {"inReviewAt": "2026-01-01T00:00:00Z"}}
        self.assertIn("already moved", jira_sync.plan(again, "in-review", CFG)["reason"])
        later = {**ticket, "jira": {"qaReadyAt": "2026-01-01T00:00:00Z"}}
        self.assertIn("already past", jira_sync.plan(later, "in-review", CFG)["reason"])

    def test_bugbot_request_is_the_trigger(self):
        ticket = {
            "id": "T",
            "jiraKey": "WAR-1",
            "autoMerge": True,
            "status": "review",
            "jira": {"startedAt": "2026-01-01T00:00:00Z"},
            "pr": {"url": "https://github.com/acme/app/pull/5", "bugbotRequested": True},
        }
        before = {"status": "coding", "pr_url": ticket["pr"]["url"], "bugbotRequested": False}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, CFG, "local", Path("."))), ["in-review"])
        before["bugbotRequested"] = True
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, CFG, "local", Path("."))), [])

    def test_pr_open_triggers_when_bugbot_is_off(self):
        ticket = {
            "id": "T",
            "jiraKey": "WAR-1",
            "autoMerge": True,
            "status": "review",
            "jira": {"startedAt": "2026-01-01T00:00:00Z"},
            "pr": {"url": "https://github.com/acme/app/pull/5"},
        }
        before = {"status": "coding", "pr_url": None, "bugbotRequested": False}
        off = {**CFG, "bugbotRequired": False}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, off, "local", Path("."))), ["in-review"])
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, CFG, "local", Path("."))), [])

    def test_manual_ticket_with_bugbot_manual_off_uses_the_pr(self):
        ticket = {
            "id": "T",
            "jiraKey": "WAR-9",
            "autoMerge": False,
            "status": "review",
            "jira": {},
            "pr": {"url": "https://github.com/acme/app/pull/9"},
        }
        before = {"status": "coding", "pr_url": None, "bugbotRequested": False}
        cfg = {**CFG, "bugbotManual": False}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, cfg, "local", Path("."))), ["in-review"])

    def test_send_back_stays_in_review(self):
        ticket = {
            "id": "T",
            "jiraKey": "WAR-1",
            "autoMerge": True,
            "status": "fix",
            "jira": {"startedAt": "2026-01-01T00:00:00Z", "inReviewAt": "2026-01-02T00:00:00Z", "status": "In Review"},
            "pr": {"url": "https://github.com/acme/app/pull/5", "bugbotRequested": True, "bugbot": "fail"},
        }
        before = {"status": "review", "pr_url": ticket["pr"]["url"], "bugbotRequested": True, "bugbot": None}
        self.assertIsNone(jira_sync.event_for(ticket, "review", "fix"))
        found = transitions(jira_sync.actions_for(ticket, before, CFG, "local", Path(".")))
        self.assertNotIn("claim", found)
        self.assertNotIn("in-review", found)
        self.assertEqual(jira_sync.plan(ticket, "claim", CFG)["action"], "skip")

    def test_manual_size_still_goes_to_qa_ready(self):
        ticket = {
            "id": "T",
            "jiraKey": "WAR-9",
            "autoMerge": False,
            "status": "awaiting_approval",
            "size": "L",
            "jira": {"startedAt": "2026-01-01T00:00:00Z", "inReviewAt": "2026-01-02T00:00:00Z"},
            "pr": {"url": "https://github.com/acme/app/pull/9", "bugbot": "pass", "ci": "green", "bugbotRequested": True},
        }
        before = {"status": "review", "pr_url": ticket["pr"]["url"], "bugbotRequested": True, "bugbot": "pass", "ci": "green"}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, CFG, "connected", Path("."))), ["qa-ready"])
        planned = jira_sync.plan(ticket, "qa-ready", CFG)
        self.assertEqual((planned["action"], planned["target"]), ("transition", "QA Ready"))

    def test_empty_status_does_not_transition_and_qa_ready_still_runs(self):
        cfg = {**CFG, "jiraInReviewStatus": ""}
        ticket = {
            "id": "T",
            "jiraKey": "WAR-9",
            "autoMerge": False,
            "status": "review",
            "jira": {"startedAt": "2026-01-01T00:00:00Z"},
            "pr": {"url": "https://github.com/acme/app/pull/9", "bugbotRequested": True},
        }
        before = {"status": "coding", "pr_url": ticket["pr"]["url"], "bugbotRequested": False}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, cfg, "local", Path("."))), [])
        ticket["status"] = "awaiting_approval"
        ticket["pr"]["bugbot"] = "pass"
        ticket["pr"]["ci"] = "green"
        before = {"status": "review", "pr_url": ticket["pr"]["url"], "bugbotRequested": True}
        self.assertEqual(transitions(jira_sync.actions_for(ticket, before, cfg, "local", Path("."))), ["qa-ready"])

    def test_pick_warns_when_the_workflow_has_no_in_review(self):
        picked = jira_sync.pick(OFFERED[:1], "In Review", {"name": "In Progress", "category": "indeterminate"}, kind="review")
        self.assertEqual(picked["result"], "no-transition")
        picked = jira_sync.pick(OFFERED, "In Review", {"name": "QA Ready", "category": "indeterminate"}, kind="review", ahead=["QA Ready", "Done"])
        self.assertEqual(picked["result"], "skipped")
        picked = jira_sync.pick(OFFERED, "In Review", {"name": "In Review", "category": "indeterminate"}, kind="review")
        self.assertEqual(picked["result"], "already")
        picked = jira_sync.pick(OFFERED, "In Review", {"name": "In Progress", "category": "indeterminate"}, kind="review")
        self.assertEqual(picked["transition"]["id"], "44")


class InReviewBeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "jira").mkdir()
        (self.tmp / "jira/tickets.json").write_text(json.dumps({"tickets": [{"key": "WAR-1", "summary": "a"}, {"key": "WAR-9", "summary": "b", "size": "L"}]}))
        scanned = run(self.tmp, "scan.py", "scan")
        self.assertEqual(scanned.returncode, 0, scanned.stdout + scanned.stderr)
        self.beam = ".warp/beam.json"

    def test_set_bugbot_requested_prints_in_review(self):
        claimed = run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-1", "--status", "claimed", "--agent", "s")
        self.assertIn('move to "In Progress"', claimed.stdout)
        opened = run(
            self.tmp,
            "beam.py",
            "set",
            "--beam",
            self.beam,
            "--id",
            "WAR-1",
            "--status",
            "review",
            "--pr",
            "https://github.com/acme/app/pull/5",
        )
        self.assertNotIn('move to "In Review"', opened.stdout)
        asked = run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-1", "--bugbot-requested")
        self.assertIn('move to "In Review"', asked.stdout)
        self.assertIn("in-review", asked.stdout)
        recorded = run(
            self.tmp,
            "jira_sync.py",
            "record",
            "--beam",
            self.beam,
            "--id",
            "WAR-1",
            "--event",
            "in-review",
            "--result",
            "no-transition",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertIn("no matching transition", recorded.stdout)
        self.assertIn("In Review", (self.tmp / ".warp/outbox.md").read_text())
        journal = (self.tmp / ".warp/journal.jsonl").read_text()
        self.assertIn('"event": "in-review"', journal)

    def test_bugbot_off_moves_when_the_pr_opens(self):
        cfg = (self.tmp / ".warp/config.yaml").read_text() + "bugbotRequired: false\n"
        (self.tmp / ".warp/config.yaml").write_text(cfg)
        run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-1", "--status", "claimed", "--agent", "s")
        opened = run(
            self.tmp,
            "beam.py",
            "set",
            "--beam",
            self.beam,
            "--id",
            "WAR-1",
            "--status",
            "review",
            "--pr",
            "https://github.com/acme/app/pull/5",
        )
        self.assertIn('move to "In Review"', opened.stdout)

    def test_send_back_and_qa_ready_on_the_beam(self):
        run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-9", "--status", "claimed", "--agent", "s")
        run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-9", "--status", "review", "--pr", "https://github.com/acme/app/pull/9", "--bugbot-requested")
        run(
            self.tmp,
            "jira_sync.py",
            "record",
            "--beam",
            self.beam,
            "--id",
            "WAR-9",
            "--event",
            "in-review",
            "--result",
            "moved",
            "--to",
            "In Review",
        )
        sent = run(self.tmp, "beam.py", "set", "--beam", self.beam, "--id", "WAR-9", "--status", "fix", "--bugbot", "fail")
        self.assertNotIn('move to "In Progress"', sent.stdout)
        self.assertNotIn('move to "In Review"', sent.stdout)
        ready = run(
            self.tmp,
            "beam.py",
            "set",
            "--beam",
            self.beam,
            "--id",
            "WAR-9",
            "--status",
            "awaiting_approval",
            "--bugbot",
            "pass",
            "--ci",
            "green",
        )
        self.assertIn('move to "QA Ready"', ready.stdout)
        self.assertNotIn('move to "In Progress"', ready.stdout)


if __name__ == "__main__":
    unittest.main()
