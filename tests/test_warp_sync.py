"""Jira-to-beam sync. Run with: python3 -m unittest tests.test_warp_sync"""

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


def run(cwd, *args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / "jira_sync.py"), *args], cwd=cwd, capture_output=True, text=True)


def ticket(tid, key, status="queued", **extra):
    row = {
        "id": tid,
        "status": status,
        "jiraKey": key,
        "summary": tid,
        "size": "S",
        "module": "app",
        "hours": 1,
        "autoMerge": extra.pop("autoMerge", True),
        "pr": extra.pop("pr", {}),
        "jira": extra.pop("jira", {}),
        "phase": extra.pop("phase", None),
    }
    row.update(extra)
    return row


def workspace(tickets, config=None):
    tmp = Path(tempfile.mkdtemp())
    warp = tmp / ".warp"
    warp.mkdir()
    beam = {
        "tickets": tickets,
        "gates": [],
        "config": config or {},
        "paused": False,
        "program": {},
    }
    path = warp / "beam.json"
    path.write_text(json.dumps(beam, indent=2) + "\n")
    return tmp, path


class MapTests(unittest.TestCase):
    def test_each_configured_status_maps(self):
        self.assertEqual(jira_sync.map_jira_status("To Do", "new", CFG), "queued")
        self.assertEqual(jira_sync.map_jira_status("In Progress", "indeterminate", CFG), "shuttle")
        self.assertEqual(jira_sync.map_jira_status("In Review", "indeterminate", CFG), "reviewing")
        self.assertEqual(jira_sync.map_jira_status("QA Ready", "indeterminate", CFG), "awaiting_approval")
        self.assertEqual(jira_sync.map_jira_status("Done", "done", CFG), "merged")
        custom = {**CFG, "jiraInProgressStatus": "Doing", "jiraInReviewStatus": "Review", "jiraQaReadyStatus": "Ready", "jiraDoneStatus": "Complete"}
        self.assertEqual(jira_sync.map_jira_status("Doing", "", custom), "shuttle")
        self.assertEqual(jira_sync.map_jira_status("Review", "", custom), "reviewing")
        self.assertEqual(jira_sync.map_jira_status("Ready", "", custom), "awaiting_approval")
        self.assertEqual(jira_sync.map_jira_status("Complete", "", custom), "merged")

    def test_empty_in_review_maps_nothing_and_an_open_pr_stays_reviewing(self):
        cfg = {**CFG, "jiraInReviewStatus": ""}
        self.assertIsNone(jira_sync.map_jira_status("In Review", "indeterminate", cfg))
        self.assertIsNone(jira_sync.desired_intent("In Review", "indeterminate", None, cfg))
        pr = {"number": 3, "open": True, "merged": False}
        self.assertEqual(jira_sync.desired_intent("In Review", "indeterminate", pr, cfg), "reviewing")
        self.assertEqual(jira_sync.desired_intent("In Progress", "indeterminate", pr, cfg), "reviewing")
        self.assertEqual(jira_sync.desired_intent("QA Ready", "indeterminate", pr, cfg), "awaiting_approval")

    def test_merged_pr_wins(self):
        pr = {"number": 12, "merged": True, "open": False}
        self.assertEqual(jira_sync.desired_intent("In Progress", "indeterminate", pr, CFG), "merged")


class SyncCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.beam = workspace(
            {
                "T-1": ticket("T-1", "WAR-1", "queued"),
                "T-2": ticket("T-2", "WAR-2", "queued"),
                "T-3": ticket("T-3", "WAR-3", "queued"),
                "T-4": ticket("T-4", "WAR-4", "queued"),
                "T-5": ticket("T-5", "WAR-5", "claimed", phase="implementing"),
                "T-6": ticket("T-6", "WAR-6", "awaiting_approval", phase="ready"),
            }
        )
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.jira = self.tmp / "jira.json"
        self.jira.write_text(
            json.dumps(
                {
                    "issues": [
                        {"key": "WAR-1", "fields": {"status": {"name": "To Do", "statusCategory": {"key": "new"}}}},
                        {"key": "WAR-2", "fields": {"status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}},
                        {"key": "WAR-3", "fields": {"status": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}}},
                        {"key": "WAR-4", "fields": {"status": {"name": "QA Ready", "statusCategory": {"key": "indeterminate"}}}},
                        {"key": "WAR-5", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
                        {"key": "WAR-6", "fields": {"status": {"name": "To Do", "statusCategory": {"key": "new"}}}},
                    ]
                }
            )
        )
        self.prs = self.tmp / "prs.json"
        self.prs.write_text(json.dumps({}))

    def test_dry_run_prints_one_line_each_and_writes_nothing(self):
        before = self.beam.read_text()
        result = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.beam.read_text(), before)
        self.assertIn("sync: dry-run", result.stdout)
        self.assertIn("sync: no dispatch", result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.startswith("sync: T-")]
        self.assertEqual(
            lines,
            [
                "sync: T-2: queued -> claimed (needs a Shuttle)",
                "sync: T-3: queued -> reviewing (jira In Review)",
                "sync: T-4: queued -> awaiting_approval (jira QA Ready)",
                "sync: T-5: claimed -> merged (jira Done)",
                "sync: T-6: awaiting_approval -> queued refused without --force",
            ],
        )
        self.assertIn("sync: 4 differ, 1 refused, 1 unchanged", result.stdout)

    def test_apply_writes_and_force_moves_backward(self):
        refused = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply", "--id", "T-6")
        self.assertIn("refused without --force", refused.stdout)
        self.assertEqual(json.loads(self.beam.read_text())["tickets"]["T-6"]["status"], "awaiting_approval")
        forced = run(
            self.tmp,
            "sync",
            "--beam",
            str(self.beam),
            "--jira",
            str(self.jira),
            "--prs",
            str(self.prs),
            "--apply",
            "--force",
            "--id",
            "T-6",
        )
        self.assertIn("sync: applied", forced.stdout)
        self.assertEqual(json.loads(self.beam.read_text())["tickets"]["T-6"]["status"], "queued")
        applied = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply")
        self.assertIn("sync: applied", applied.stdout)
        tickets = json.loads(self.beam.read_text())["tickets"]
        self.assertEqual(tickets["T-2"]["status"], "claimed")
        self.assertTrue(tickets["T-2"]["needsReplacement"])
        self.assertTrue(tickets["T-2"]["haltResume"])
        self.assertEqual(tickets["T-2"]["phase"], "implementing")
        self.assertEqual(tickets["T-3"]["status"], "review")
        self.assertEqual(tickets["T-3"]["phase"], "reviewing")
        self.assertEqual(tickets["T-4"]["status"], "awaiting_approval")
        self.assertEqual(tickets["T-5"]["status"], "merged")
        journal = (self.tmp / ".warp/journal.jsonl").read_text()
        self.assertIn('"type": "jira-sync"', journal)

    def test_merged_pr_overrides_jira(self):
        self.prs.write_text(
            json.dumps(
                {
                    "T-2": {
                        "number": 12,
                        "url": "https://github.com/acme/app/pull/12",
                        "state": "MERGED",
                        "sha": "abc1234",
                        "branch": "warp/T-2-WAR-2",
                    }
                }
            )
        )
        result = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply", "--id", "T-2")
        self.assertIn("sync: T-2: queued -> merged (pr #12 merged)", result.stdout)
        row = json.loads(self.beam.read_text())["tickets"]["T-2"]
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["pr"]["url"], "https://github.com/acme/app/pull/12")
        self.assertEqual(row["pr"]["sha"], "abc1234")
        self.assertEqual(row["branch"], "warp/T-2-WAR-2")

    def test_empty_in_review_keeps_an_open_pr_reviewing(self):
        (self.tmp / ".warp/config.yaml").write_text('jiraInReviewStatus: ""\n')
        self.jira.write_text(
            json.dumps(
                {
                    "WAR-2": {"name": "In Review", "category": "indeterminate"},
                    "WAR-3": {"name": "In Progress", "category": "indeterminate"},
                }
            )
        )
        self.prs.write_text(
            json.dumps(
                {
                    "T-3": {"number": 8, "url": "https://github.com/acme/app/pull/8", "state": "OPEN", "sha": "fff", "branch": "warp/T-3"}
                }
            )
        )
        result = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply")
        self.assertNotIn("T-2: queued -> reviewing", result.stdout)
        self.assertIn("sync: T-3: queued -> reviewing (pr #8 open)", result.stdout)
        tickets = json.loads(self.beam.read_text())["tickets"]
        self.assertEqual(tickets["T-2"]["status"], "queued")
        self.assertEqual(tickets["T-3"]["status"], "review")
        self.assertEqual(tickets["T-3"]["phase"], "reviewing")
        self.assertNotIn("inReviewAt", tickets["T-3"].get("jira") or {})

    def test_fix_send_back_stays_while_jira_is_in_review(self):
        beam = json.loads(self.beam.read_text())
        beam["tickets"]["T-3"]["status"] = "fix"
        beam["tickets"]["T-3"]["phase"] = "fixing"
        self.beam.write_text(json.dumps(beam, indent=2) + "\n")
        result = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply", "--id", "T-3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("T-3:", result.stdout)
        row = json.loads(self.beam.read_text())["tickets"]["T-3"]
        self.assertEqual(row["status"], "fix")
        self.assertEqual(row["phase"], "fixing")

    def test_merged_pull_request_wins_over_an_open_one(self):
        self.prs.write_text(
            json.dumps(
                [
                    {"id": "T-2", "number": 1, "state": "OPEN", "url": "https://github.com/acme/app/pull/1", "sha": "old"},
                    {
                        "id": "T-2",
                        "number": 12,
                        "state": "MERGED",
                        "url": "https://github.com/acme/app/pull/12",
                        "sha": "abc1234",
                        "branch": "warp/T-2-WAR-2",
                    },
                ]
            )
        )
        result = run(self.tmp, "sync", "--beam", str(self.beam), "--jira", str(self.jira), "--prs", str(self.prs), "--apply", "--id", "T-2")
        self.assertIn("sync: T-2: queued -> merged (pr #12 merged)", result.stdout)
        row = json.loads(self.beam.read_text())["tickets"]["T-2"]
        self.assertEqual(row["pr"]["number"], 12)
        self.assertEqual(row["status"], "merged")

    def test_refused_status_still_records_the_pull_request(self):
        self.prs.write_text(
            json.dumps(
                {"T-6": {"number": 9, "url": "https://github.com/acme/app/pull/9", "state": "OPEN", "sha": "abc"}}
            )
        )
        result = run(
            self.tmp,
            "sync",
            "--beam",
            str(self.beam),
            "--jira",
            str(self.jira),
            "--prs",
            str(self.prs),
            "--apply",
            "--id",
            "T-6",
        )
        self.assertIn("refused without --force", result.stdout)
        self.assertIn("sync: applied", result.stdout)
        row = json.loads(self.beam.read_text())["tickets"]["T-6"]
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertEqual(row["pr"]["url"], "https://github.com/acme/app/pull/9")
        self.assertEqual(row["pr"]["sha"], "abc")


if __name__ == "__main__":
    unittest.main()
