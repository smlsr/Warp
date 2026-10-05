"""Manual and auto tickets share the Bugbot and CI gate. Only the last step differs."""

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


def run(repo, script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


class BugbotGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "jira").mkdir(parents=True)
        (self.repo / "jira/tickets.json").write_text(
            json.dumps({"tickets": [{"key": "HOS-1", "summary": "a"}, {"key": "HOS-9", "summary": "manual"}]})
        )
        proc = run(self.repo, "scan.py", "scan")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.beam = self.repo / ".warp/beam.json"
        self._auto("HOS-1", True)
        self._auto("HOS-9", False)

    def load(self):
        return json.loads(self.beam.read_text())

    def save(self, beam):
        self.beam.write_text(json.dumps(beam))

    def _auto(self, tid, auto):
        beam = self.load()
        beam["tickets"][tid]["autoMerge"] = auto
        beam["tickets"][tid]["size"] = "S" if auto else "L"
        self.save(beam)

    def ticket(self, tid):
        return self.load()["tickets"][tid]

    def set(self, tid, *args):
        return run(self.repo, "beam.py", "set", "--beam", ".warp/beam.json", "--id", tid, *args)

    def test_manual_path_order_and_qa_comment(self):
        opened = self.set("HOS-9", "--status", "review", "--pr", "https://github.com/acme/app/pull/9")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        self.assertNotIn("QA Ready", opened.stdout)
        self.assertEqual(self.ticket("HOS-9")["status"], "review")
        first = self.set("HOS-9", "--bugbot", "fail")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.ticket("HOS-9")["status"], "fix")
        self.assertEqual(self.ticket("HOS-9")["attempts"], 1)
        self.assertIn("Bugbot: fail", first.stdout)
        clean = self.set("HOS-9", "--bugbot", "pass", "--ci", "green")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(self.ticket("HOS-9")["pr"]["bugbotFixed"], 1)
        waiting = self.set("HOS-9", "--status", "awaiting_approval")
        self.assertEqual(waiting.returncode, 0, waiting.stdout + waiting.stderr)
        self.assertEqual(self.ticket("HOS-9")["status"], "awaiting_approval")
        self.assertIn('move to "QA Ready"', waiting.stdout)
        self.assertIn("Bugbot clean, ready for manual review", waiting.stdout)
        self.assertIn("Findings fixed: 1", waiting.stdout)
        self.assertIn("warp:proceed HOS-9", waiting.stdout)

    def test_qa_ready_is_refused_until_bugbot_and_ci(self):
        self.set("HOS-9", "--status", "review")
        refused = self.set("HOS-9", "--status", "awaiting_approval")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refusing awaiting_approval", refused.stdout)
        self.assertIn("bugbot is not run", refused.stdout)
        self.assertIn("ci is not run", refused.stdout)
        self.assertEqual(self.ticket("HOS-9")["status"], "review")
        ci_only = self.set("HOS-9", "--ci", "green", "--status", "awaiting_approval")
        self.assertNotEqual(ci_only.returncode, 0)
        self.assertIn("bugbot is not run", ci_only.stdout)
        self.assertEqual(self.ticket("HOS-9")["status"], "review")
        self.assertIsNone(self.ticket("HOS-9")["jira"].get("qaReadyAt"))

    def test_auto_merge_uses_the_same_gate(self):
        refused = self.set("HOS-1", "--status", "merged")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refusing merged", refused.stdout)
        self.assertNotEqual(self.ticket("HOS-1")["status"], "merged")
        self.set("HOS-1", "--bugbot", "pass", "--ci", "green")
        merged = self.set("HOS-1", "--status", "merged", "--sha", "abc1234")
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual(self.ticket("HOS-1")["status"], "merged")
        self.assertIn('move to "Done"', merged.stdout)

    def test_fix_loop_exhausts_at_max_fix_attempts(self):
        self.set("HOS-9", "--status", "review")
        for n in (1, 2):
            proc = self.set("HOS-9", "--bugbot", "fail")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(self.ticket("HOS-9")["status"], "fix")
            self.assertEqual(self.ticket("HOS-9")["attempts"], n)
        last = self.set("HOS-9", "--bugbot", "fail")
        self.assertEqual(last.returncode, 0, last.stdout + last.stderr)
        self.assertEqual(self.ticket("HOS-9")["status"], "alarm")
        self.assertEqual(self.ticket("HOS-9")["alarm"], "bugbot-failed")
        self.assertEqual(self.ticket("HOS-9")["attempts"], 3)
        self.assertIn("status alarm", last.stdout)
        blocked = self.set("HOS-9", "--status", "awaiting_approval")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(self.ticket("HOS-9")["status"], "alarm")
        auto = self.set("HOS-1", "--bugbot", "fail")
        self.assertEqual(self.ticket("HOS-1")["status"], "fix")
        self.set("HOS-1", "--bugbot", "fail")
        self.set("HOS-1", "--bugbot", "fail")
        self.assertEqual(self.ticket("HOS-1")["status"], "alarm")
        self.assertEqual(self.ticket("HOS-1")["alarm"], "bugbot-failed")

    def test_bugbot_manual_false_skips_bugbot_on_the_manual_path_only(self):
        (self.repo / ".warp/config.yaml").write_text("bugbotManual: false\n")
        self.set("HOS-9", "--ci", "green")
        waiting = self.set("HOS-9", "--status", "awaiting_approval")
        self.assertEqual(waiting.returncode, 0, waiting.stdout + waiting.stderr)
        self.assertIn("Bugbot was not required", waiting.stdout)
        self.assertNotIn("Bugbot clean, ready for manual review", waiting.stdout)
        refused = self.set("HOS-1", "--ci", "green", "--status", "merged")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("bugbot is not run", refused.stdout)
        self.assertNotEqual(self.ticket("HOS-1")["status"], "merged")

    def test_bugbot_required_false_skips_bugbot_on_both_paths(self):
        (self.repo / ".warp/config.yaml").write_text("bugbotRequired: false\n")
        ci = self.set("HOS-1", "--ci", "green", "--status", "merged")
        self.assertEqual(ci.returncode, 0, ci.stdout + ci.stderr)
        self.assertEqual(self.ticket("HOS-1")["status"], "merged")
        still = self.set("HOS-9", "--status", "awaiting_approval")
        self.assertNotEqual(still.returncode, 0)
        self.assertIn("ci is not run", still.stdout)

    def test_new_commits_after_qa_ready_rerun_bugbot_and_keep_jira(self):
        self.set("HOS-9", "--bugbot", "pass", "--ci", "green", "--status", "awaiting_approval")
        recorded = run(
            self.repo,
            "jira_sync.py",
            "record",
            "--beam",
            ".warp/beam.json",
            "--id",
            "HOS-9",
            "--event",
            "qa-ready",
            "--result",
            "moved",
            "--to",
            "QA Ready",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertTrue(self.ticket("HOS-9")["jira"].get("qaReadyAt"))
        rerun = self.set("HOS-9", "--sha", "abc1234")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.ticket("HOS-9")["status"], "bugbot_running")
        self.assertIsNone(self.ticket("HOS-9")["pr"].get("bugbot"))
        self.assertIsNone(self.ticket("HOS-9")["pr"].get("ci"))
        self.assertIn("new commits, re-running Bugbot", rerun.stdout)
        self.assertNotIn("move to", rerun.stdout)
        self.assertTrue(self.ticket("HOS-9")["jira"].get("qaReadyAt"))
        failed = self.set("HOS-9", "--bugbot", "fail")
        self.assertEqual(self.ticket("HOS-9")["status"], "fix")
        self.assertTrue(self.ticket("HOS-9")["jira"].get("qaReadyAt"))
        self.assertIn("Bugbot: fail", failed.stdout)
        self.assertNotIn('move to "', failed.stdout)
        again = self.set("HOS-9", "--bugbot", "pass", "--ci", "green", "--status", "awaiting_approval")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.ticket("HOS-9")["status"], "awaiting_approval")
        self.assertIn("Bugbot clean, ready for manual review", again.stdout)
        self.assertNotIn('move to "QA Ready"', again.stdout)
        self.assertTrue(self.ticket("HOS-9")["jira"].get("qaReadyAt"))

    def test_status_and_jira_check_show_bugbot_for_manual_tickets(self):
        self.set("HOS-9", "--status", "review", "--bugbot", "fail")
        status = run(self.repo, "scan.py", "status", "--beam", ".warp/beam.json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        text = (self.repo / ".warp/STATUS.md").read_text()
        self.assertIn("HOS-9", text)
        self.assertIn("bugbot=fail", text)
        posted = run(self.repo, "status_post.py", "--beam", ".warp/beam.json", "--out", ".warp/status-post.json")
        self.assertEqual(posted.returncode, 0, posted.stdout + posted.stderr)
        self.assertIn("HOS-9", posted.stdout)
        self.assertIn("bugbot=fail", posted.stdout)
        report = run(self.repo, "jira_sync.py", "verify", "--beam", ".warp/beam.json", "--id", "HOS-9")
        self.assertIn("bugbot: fail", report.stdout)
        self.assertIn("findings fixed:", report.stdout)

    def test_init_backfills_bugbot_manual(self):
        sys.path.insert(0, str(SCRIPTS))
        import install

        example = (ROOT / "assets/config.example.yaml").read_text()
        text, added = install.add_missing_keys("bugbotRequired: true\nmaxFixAttempts: 3\n", example)
        self.assertIn("bugbotManual", added)
        self.assertIn("bugbotManual: true", text)
        self.assertIn("skip Bugbot on that path", text)
