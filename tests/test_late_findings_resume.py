"""1.6.15: a run whose tickets are all merged still queues and starts follow-ups for late Bugbot findings."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
from test_bugbot_heal import Repo, row  # noqa: E402
from test_real_merge import Live  # noqa: E402

PRS = {"WV-11": 12, "WV-12": 13, "WV-16": 18}


def merged(tid, number, **pr):
    ticket = row(tid, status="merged", phase="merged", locks=["app/src/%s/" % tid.lower()], gate="G2")
    ticket["pr"] = dict({"url": "https://github.com/smlsr/WarpDemo/pull/%d" % number, "headSha": "a" * 40,
                         "mergeConfirmed": True, "mergeAnnounced": True}, **pr)
    return ticket


class FinishedRunTest(Repo):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/smlsr/WarpDemo.git"], cwd=self.tmp, check=True)
        self.config.write_text("bugbotRequired: true\ngitProvider: github\nmaxAgents: 4\n")
        data = self.write([merged(tid, n) for tid, n in PRS.items()], bugbotRequired=True, maxAgents=4)
        data["gates"] = [{"key": "G2", "members": list(PRS), "status": "green", "blocking": True}]
        data["bugbotSeen"] = True
        self.beam_path.write_text(json.dumps(data))
        self.saved = provider.fetch_pr_state

    def tearDown(self):
        provider.fetch_pr_state = self.saved
        super().tearDown()

    def findings(self, root, url):
        number = int(str(url).rsplit("/", 1)[-1])
        return {"prState": "MERGED", "bugbot": "fail",
                "findings": ["app/src/x%d.mjs:%d Finding on #%d (Medium)" % (number, number, number)]}

    def test_resume_on_a_finished_run_queues_and_starts_all_three(self):
        self.assertTrue(orchestrator.parent_may_exit(self.load()))  # the run looks finished
        (self.warp / "agents.json").write_text(json.dumps({"finishedAt": "2026-10-14T01:00:00Z", "agents": [
            {"id": "shuttle-WV-16-r1", "ticket": "WV-16", "role": "shuttle", "state": "ended", "halted": True}]}))
        provider.fetch_pr_state = self.findings
        with Live({}):
            lines = orchestrator.supervise(self.beam_path, now="2026-10-14T01:30:00Z")
        for tid, number in PRS.items():
            self.assertIn("bugbot: late %s https://github.com/smlsr/WarpDemo/pull/%d 1 finding(s) after the merge. "
                          "Follow-up ticket %s-BB1 queued." % (tid, number, tid), lines)
            self.assertTrue(any(line.startswith("start %s-BB1 " % tid) for line in lines), lines)
        self.assertIn("run: reopened for Bugbot follow-ups", lines)
        self.assertNotIn("finishedAt", json.loads((self.warp / "agents.json").read_text()))
        tickets = self.load()["tickets"]
        self.assertEqual(sorted(t for t in tickets if "-BB" in t), ["WV-11-BB1", "WV-12-BB1", "WV-16-BB1"])
        self.assertFalse(orchestrator.parent_may_exit(self.load()))
        # The next pass does not queue them again.
        provider.fetch_pr_state = lambda root, url: self.fail("fetched again")
        with Live({}):
            again = orchestrator.supervise(self.beam_path, now="2026-10-14T01:31:00Z")
        self.assertFalse(any(line.startswith("bugbot: late") for line in again), again)

    def test_a_merge_waiting_for_bugbot_keeps_the_run_open(self):
        data = self.load()
        import beam as beam_mod

        data["tickets"]["WV-16"]["pr"]["mergedAt"] = beam_mod.utcnow()
        self.assertFalse(orchestrator.parent_may_exit(data))
        data["tickets"]["WV-16"]["pr"]["lateBugbot"] = "pass"
        self.assertTrue(orchestrator.parent_may_exit(data))

    def test_a_paused_run_checks_nothing(self):
        data = self.load()
        data["runState"] = "paused"
        data["paused"] = True
        self.beam_path.write_text(json.dumps(data))
        provider.fetch_pr_state = lambda root, url: self.fail("fetched while paused")
        with Live({}):
            lines = orchestrator.supervise(self.beam_path, now="2026-10-14T01:30:00Z")
        self.assertFalse(any(line.startswith("bugbot: late") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
