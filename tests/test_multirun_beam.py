"""1.7.26: every subcommand that takes --beam reads and writes that exact file. In a multi-pack deployment
(.warp/runs/<pack>/beam.json per coordinator) nothing lands on the live root beam of another run."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MultiRunBeamTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q",
                        "--allow-empty", "-m", "base"], cwd=self.repo, check=True)
        warp = self.repo / ".warp"
        (warp / "runs" / "beta").mkdir(parents=True)
        (warp / "config.yaml").write_text("run: alpha\nrunner: local\nlaunch: worktree\n")
        self.root = warp / "beam.json"
        self.beta = warp / "runs" / "beta" / "beam.json"
        self.root.write_text(json.dumps({"version": 1, "run": "alpha", "generation": 42, "runState": "running",
                                         "config": {"run": "alpha"}, "tickets": {"A-1": {"id": "A-1", "status": "queued"}}},
                                        indent=2) + "\n")
        self.write_beta()
        self.before = sha(self.root)

    def write_beta(self, run="beta", **ticket):
        row = {"id": "B-1", "status": "claimed", "claimedAt": "2026-11-11T20:00:00Z", "size": "S", "locks": ["src/b"],
               "summary": "b", "acceptance": ["AC-1: x"], "shuttle": {"step": "implement"}}
        row.update(ticket)
        self.beta.write_text(json.dumps({"version": 1, "run": run, "generation": 1, "runState": "running",
                                         "config": {"run": run, "runner": "local", "launch": "worktree"},
                                         "tickets": {"B-1": row}}, indent=2) + "\n")

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=self.repo, capture_output=True,
                              text=True, timeout=120)

    def beta_doc(self):
        return json.loads(self.beta.read_text())

    def assert_root_untouched(self, done):
        self.assertEqual(sha(self.root), self.before, "the root beam changed: " + done.stdout + done.stderr)

    def test_parent_alive_writes_the_pack_beam_only(self):
        done = self.cli("orchestrator.py", "parent-alive", "--beam", ".warp/runs/beta/beam.json", "--every", "600",
                        "--session", "s1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_root_untouched(done)
        listener = self.beta_doc().get("listener") or {}
        self.assertTrue(listener.get("parentSeenAt"), self.beta_doc())
        self.assertEqual(listener.get("parentEvery"), 600)
        self.assertEqual(listener.get("parentSeenSession"), "s1")

    def test_parent_exit_writes_the_pack_beam_only(self):
        done = self.cli("orchestrator.py", "parent-exit", "--beam", ".warp/runs/beta/beam.json", "--session", "s1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_root_untouched(done)
        self.assertNotEqual(self.beta_doc(), {})

    def test_checkout_launch_claims_on_the_pack_beam_with_pack_branches(self):
        done = self.cli("checkout.py", "launch", "--beam", ".warp/runs/beta/beam.json", "--id", "B-1", "--root", ".")
        self.assert_root_untouched(done)
        ticket = self.beta_doc()["tickets"]["B-1"]
        self.assertTrue(str(ticket.get("branch") or "").startswith("warp/beta/"), done.stdout + done.stderr)
        self.assertIn("warp/beta/B-1", done.stdout)
        self.assertNotIn("warp/alpha/", done.stdout)
        self.assertIn("agents.py reap --beam %s" % self.beta.resolve(), done.stdout)

    def test_checkout_launch_refuses_another_packs_branch(self):
        self.write_beta(branch="warp/desktop/B-1")
        done = self.cli("checkout.py", "launch", "--beam", ".warp/runs/beta/beam.json", "--id", "B-1", "--root", ".")
        self.assertEqual(done.returncode, 2)
        self.assertIn("is not a warp/beta/ branch", done.stdout)
        self.assert_root_untouched(done)

    def test_checkout_verify_and_agents_reap_touch_the_pack_beam_only(self):
        done = self.cli("checkout.py", "verify", "--beam", ".warp/runs/beta/beam.json", "--id", "B-1", "--root", ".")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_root_untouched(done)
        done = self.cli("agents.py", "reap", "--beam", ".warp/runs/beta/beam.json", "--id", "subagent:B-1", "--ticket", "B-1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("reap: continue", done.stdout)
        self.assert_root_untouched(done)

    def test_a_beam_whose_run_does_not_match_its_folder_is_refused(self):
        self.write_beta(run="desktop")
        frozen = sha(self.beta)
        done = self.cli("orchestrator.py", "parent-alive", "--beam", ".warp/runs/beta/beam.json", "--every", "600")
        self.assertEqual(done.returncode, 2)
        self.assertIn("carries run: desktop, but its folder is .warp/runs/beta/", done.stderr)
        self.assertEqual(sha(self.beta), frozen)
        self.assert_root_untouched(done)

    def test_mirror_root_only_onto_the_same_run(self):
        done = self.cli("orchestrator.py", "parent-alive", "--beam", ".warp/runs/beta/beam.json", "--every", "600",
                        "--mirror-root")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("not mirrored: the root beam is run alpha, not beta", done.stderr)
        self.assert_root_untouched(done)
        # The root beam holds beta (a single-pack run on another checkout layout): the same write is mirrored.
        root = json.loads(self.root.read_text())
        root.update(run="beta", config={"run": "beta"})
        self.root.write_text(json.dumps(root, indent=2) + "\n")
        (self.repo / ".warp" / "config.yaml").write_text("run: alpha\nrunner: local\n")
        done = self.cli("orchestrator.py", "parent-alive", "--beam", ".warp/runs/beta/beam.json", "--every", "900",
                        "--mirror-root")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("beam: mirrored to", done.stderr)
        self.assertEqual(json.loads(self.root.read_text())["listener"]["parentEvery"], 900)
        self.assertEqual(self.beta_doc()["listener"]["parentEvery"], 900)

    def test_a_solo_run_without_beam_or_with_the_root_still_writes_the_root(self):
        done = self.cli("orchestrator.py", "parent-alive", "--beam", ".warp/beam.json", "--every", "600")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(sha(self.root), self.before)


if __name__ == "__main__":
    unittest.main()
