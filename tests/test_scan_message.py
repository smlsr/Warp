"""`notify.py scan` on its own rebuilds the scan message from the beam, with the Jira counts as they are now."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "build_schedule_v2.json"
IDS = ["AR-00", "AD-01", "AD-02", "AD-03", "AU-01", "AD-04"]


def run(script, *args, cwd):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], cwd=cwd, capture_output=True, text=True)


class ScanMessageTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.dir, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/App.git"], cwd=self.dir, check=True)
        (self.dir / "plan").mkdir()
        shutil.copy(FIXTURE, self.dir / "plan" / "schedule.json")
        self.post = self.dir / ".warp" / "notify-post.json"

    def scan(self):
        out = run("scan.py", "scan", cwd=self.dir)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("jira: 6 tickets: 0 keyed, 6 need mapping", out.stdout)
        return json.loads(self.post.read_text())["text"]

    def resolve(self, ids):
        lines = []
        for n, tid in enumerate(ids, 1):
            out = run("jira_sync.py", "resolve", "--ticket", tid, "--key", f"WAR-{n}", "--issue-id", f"1000{n}",
                      "--cloud-id", "c1", cwd=self.dir)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            lines.append(out.stdout)
        return lines

    def test_rebuild_after_resolve_keeps_the_details_and_shows_the_new_counts(self):
        first = self.scan()
        self.assertIn("Jira: 0 keyed, 6 need mapping", first)
        lines = self.resolve(IDS)
        self.assertIn("jira: 1 keyed now (+1). next: python3 <plugin>/scripts/notify.py scan --root .", lines[0])
        self.assertIn("jira: 6 keyed now (+1).", lines[-1])
        out = run("notify.py", "scan", "--root", ".", cwd=self.dir)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        text = json.loads(self.post.read_text())["text"]
        for line in ("Format: build-schedule/2", "Tickets: 6", "Gates: 4", "Run: stopped",
                     "Estimate: agent 37.0h, human 1.5h, elapsed 30.5h", "Jira: 6 keyed, 0 need mapping"):
            self.assertIn(line, text.splitlines())
        self.assertIn("Plan used: plan/schedule.json", text)
        self.assertNotIn("None", text)
        self.assertNotIn("Needs a Jira key", text)

    def test_partial_resolve_lists_what_is_left(self):
        self.scan()
        self.resolve(IDS[:4])
        run("notify.py", "scan", "--root", ".", cwd=self.dir)
        text = json.loads(self.post.read_text())["text"]
        self.assertIn("Jira: 4 keyed, 2 need mapping", text)
        self.assertIn("Needs a Jira key: AU-01, AD-04", text)

    def test_a_resolve_that_stores_nothing_prints_no_hint(self):
        self.scan()
        self.resolve(IDS[:1])
        again = self.resolve(IDS[:1])  # same key again: nothing new
        self.assertNotIn("keyed now", again[0])

    def test_no_beam_writes_nothing(self):
        warp = self.dir / ".warp"
        warp.mkdir()
        self.post.write_text('{"kind": "scan", "text": "earlier"}\n')
        out = run("notify.py", "scan", "--root", ".", cwd=self.dir)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("no scan on the beam", out.stdout)
        self.assertEqual(self.post.read_text(), '{"kind": "scan", "text": "earlier"}\n')
        (warp / "beam.json").write_text('{"tickets": {}}\n')
        out = run("notify.py", "scan", "--root", ".", cwd=self.dir)
        self.assertIn("no scan on the beam", out.stdout)
        self.assertEqual(self.post.read_text(), '{"kind": "scan", "text": "earlier"}\n')

    def test_scan_details_passed_in_are_used_as_given(self):
        sys.path.insert(0, str(SCRIPTS))
        import notify

        self.scan()
        payload = notify.build("scan", self.dir, {"format": "markdown", "tickets": 2, "gates": 0})
        self.assertIn("Format: markdown", payload["text"])
        self.assertIn("Tickets: 2", payload["text"])


if __name__ == "__main__":
    unittest.main()
