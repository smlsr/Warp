"""autoMergeSizes is the only cut between auto-merge and manual review.

L and XL are not special. A size in the list auto-merges after Bugbot and CI.
A size left out stays manual. The default list is S, M.
"""

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
import beam  # noqa: E402
import scan  # noqa: E402

PLAN = """# Tickets

| id | size | summary | jira key |
|---|---|---|---|
| T-1 | S | small | WAR-1 |
| T-2 | L | large | WAR-2 |
| T-3 | XL | huge | WAR-3 |
| T-4 | size:L | prefixed | WAR-4 |
| T-5 | L — checkout | dashed | WAR-5 |
"""


def run(repo, script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


class NormalizeTests(unittest.TestCase):
    def test_labels_match_the_list_token(self):
        for raw in ("L", "l", "size:L", "Size: L", "L — checkout", "L - checkout", "HIGH"):
            self.assertEqual(beam.normalize_size(raw), "L", raw)
        self.assertEqual(beam.normalize_size("XL"), "XL")
        self.assertEqual(beam.normalize_size("size:XL"), "XL")
        self.assertEqual(beam.normalize_size("S"), "S")
        self.assertTrue(beam.auto_merge("size:L", ["S", "M", "L", "XL"]))
        self.assertTrue(beam.auto_merge("L — checkout", ["S", "M", "L", "XL"]))
        self.assertFalse(beam.auto_merge("L", ["S", "M"]))
        self.assertFalse(beam.auto_merge("XL", ["S", "M", "L"]))


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / ".warp").mkdir(parents=True)
        self.sched = self.repo / "schedule.json"
        self.sched.write_text(
            json.dumps(
                {
                    "tickets": [
                        {"id": "S-1", "size": "S", "summary": "small", "autoMerge": False},
                        {"id": "L-1", "size": "L", "summary": "large", "autoMerge": False},
                        {"id": "L-2", "size": "size:L", "summary": "prefixed"},
                        {"id": "L-3", "size": "L — checkout", "summary": "dashed"},
                        {"id": "X-1", "size": "XL", "summary": "huge"},
                    ]
                }
            )
        )
        self.out = self.repo / ".warp" / "beam.json"

    def ingest(self, sizes):
        cfg = beam.default_config()
        cfg["autoMergeSizes"] = sizes
        beam.ingest(self.sched, None, self.out, cfg)
        return json.loads(self.out.read_text())["tickets"]

    def test_l_and_xl_in_the_list_auto_merge(self):
        tickets = self.ingest(["S", "M", "L", "XL"])
        for tid in ("S-1", "L-1", "L-2", "L-3", "X-1"):
            self.assertTrue(tickets[tid]["autoMerge"], tid)
            self.assertIn(tickets[tid]["size"], {"S", "L", "XL"})
        self.assertEqual(tickets["L-2"]["size"], "L")
        self.assertEqual(tickets["L-3"]["size"], "L")

    def test_l_removed_from_the_list_stays_manual(self):
        tickets = self.ingest(["S", "M", "XL"])
        self.assertTrue(tickets["S-1"]["autoMerge"])
        self.assertTrue(tickets["X-1"]["autoMerge"])
        self.assertFalse(tickets["L-1"]["autoMerge"])
        self.assertFalse(tickets["L-2"]["autoMerge"])
        self.assertFalse(tickets["L-3"]["autoMerge"])

    def test_default_config_sends_l_to_manual(self):
        cfg = beam.default_config()
        self.assertEqual(cfg["autoMergeSizes"], ["S", "M"])
        tickets = self.ingest(cfg["autoMergeSizes"])
        self.assertTrue(tickets["S-1"]["autoMerge"])
        self.assertFalse(tickets["L-1"]["autoMerge"])
        self.assertFalse(tickets["X-1"]["autoMerge"])


class ScanConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "CURSOR_PLAN.md").write_text(PLAN)
        (self.repo / ".warp").mkdir()

    def tickets(self):
        return json.loads((self.repo / ".warp" / "beam.json").read_text())["tickets"]

    def test_scan_reads_the_yaml_list(self):
        (self.repo / ".warp" / "config.yaml").write_text(
            'jiraProject: WAR\nautoMergeSizes: [S, M, L, XL]\n'
        )
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tickets = self.tickets()
        for tid in ("T-1", "T-2", "T-3", "T-4", "T-5"):
            self.assertTrue(tickets[tid]["autoMerge"], tid)
        est = json.loads((self.repo / ".warp" / "beam.json").read_text())["program"]["estimate"]
        self.assertEqual(est["reviewTickets"], 0)

    def test_block_list_and_l_removed(self):
        (self.repo / ".warp" / "config.yaml").write_text(
            "jiraProject: WAR\nautoMergeSizes:\n  - S\n  - M\n  - XL\n"
        )
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tickets = self.tickets()
        self.assertTrue(tickets["T-1"]["autoMerge"])
        self.assertTrue(tickets["T-3"]["autoMerge"])
        self.assertFalse(tickets["T-2"]["autoMerge"])
        self.assertFalse(tickets["T-4"]["autoMerge"])
        self.assertFalse(tickets["T-5"]["autoMerge"])

    def test_default_yaml_without_the_key_keeps_l_manual(self):
        (self.repo / ".warp" / "config.yaml").write_text("jiraProject: WAR\n")
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tickets = self.tickets()
        self.assertTrue(tickets["T-1"]["autoMerge"])
        self.assertFalse(tickets["T-2"]["autoMerge"])
        self.assertFalse(tickets["T-3"]["autoMerge"])

    def test_clean_bugbot_merges_l_and_does_not_open_qa_ready(self):
        (self.repo / ".warp" / "config.yaml").write_text(
            'jiraProject: WAR\nautoMergeSizes: [S, M, L, XL]\n'
        )
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        waiting = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "T-2",
            "--bugbot",
            "pass",
            "--ci",
            "green",
            "--status",
            "awaiting_approval",
        )
        self.assertNotEqual(waiting.returncode, 0)
        self.assertIn("refusing awaiting_approval", waiting.stdout)
        self.assertIn("autoMergeSizes", waiting.stdout)
        self.assertNotIn("QA Ready", waiting.stdout)
        self.assertNotIn("warp:proceed", waiting.stdout)
        beam_data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertTrue(beam_data["tickets"]["T-2"]["autoMerge"])
        self.assertNotEqual(beam_data["tickets"]["T-2"]["status"], "awaiting_approval")
        merged = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "T-2",
            "--status",
            "merged",
            "--sha",
            "abc1234",
        )
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertIn('move to "Done"', merged.stdout)
        self.assertNotIn("QA Ready", merged.stdout)
        self.assertNotIn("warp:proceed", merged.stdout)

    def test_l_left_out_still_waits_for_approval(self):
        (self.repo / ".warp" / "config.yaml").write_text(
            "jiraProject: WAR\nautoMergeSizes: [S, M]\n"
        )
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        waiting = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "T-2",
            "--bugbot",
            "pass",
            "--ci",
            "green",
            "--status",
            "awaiting_approval",
        )
        self.assertEqual(waiting.returncode, 0, waiting.stdout + waiting.stderr)
        self.assertIn('move to "QA Ready"', waiting.stdout)
        self.assertIn("warp:proceed T-2", waiting.stdout)
        self.assertEqual(self.tickets()["T-2"]["status"], "awaiting_approval")


class EstimateTests(unittest.TestCase):
    def test_listed_l_adds_no_human_wait(self):
        tickets = [
            {"id": "L-1", "size": "L — checkout", "deps": []},
            {"id": "X-1", "size": "XL", "deps": []},
        ]
        listed = scan.estimate(tickets, [], [], 1, ["S", "M", "L", "XL"])
        self.assertEqual(listed["reviewTickets"], 0)
        self.assertEqual(listed["humanHours"], 0.0)
        default = scan.estimate(tickets, [], [], 1)
        self.assertEqual(default["reviewTickets"], 2)
        self.assertGreater(default["humanHours"], 0)
