"""1.7.12: every path that frees work prints its `start` line in the same pass.

Run `ui` stalled after a merge: the live pass merged a done ticket (run_merges, after `advance` had already refilled),
printed the Jira move, and no `start` line, so the ticket its merge unblocked waited for a pass that did not come.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import orchestrator  # noqa: E402
import pipeline  # noqa: E402

NOW = "2026-10-26T13:30:00Z"


def done_ticket(tid, rank=1):
    return {"id": tid, "status": "review", "phase": "ready", "locks": ["src/%s" % tid.lower()], "size": "S",
            "module": "UI", "autoMerge": True, "jiraKey": "HOS-%d" % rank, "branch": "warp/ui/%s" % tid, "rank": rank,
            "summary": tid, "plan": "p", "result": "r", "deps": [],
            "pr": {"url": "https://github.com/o/r/pull/%d" % rank, "headSha": "h%d" % rank, "rollup": "green",
                   "rollupSha": "h%d" % rank, "ci": "green", "ciSha": "h%d" % rank, "bugbot": "pass",
                   "bugbotSha": "h%d" % rank, "acResults": {"AC-1": "pass"}, "acSha": "h%d" % rank}}


def queued(tid, deps=(), rank=5, **extra):
    row = {"id": tid, "status": "queued", "locks": ["src/%s" % tid.lower()], "size": "S", "module": "UI",
           "autoMerge": True, "jiraKey": "HOS-%d" % rank, "rank": rank, "summary": tid, "deps": list(deps), "pr": {}}
    row.update(extra)
    return row


class LiveRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=self.tmp, check=True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text(
            "gitProvider: github\njiraProject: HOS\njiraKeyPrefixes: HOS\nbugbotRequired: false\nbaseCheckSeconds: 0\n"
            "stallNoPushMinutes: 0\nautoScan: 0\nwatchSeconds: 0\n")
        self.path = self.tmp / ".warp" / "beam.json"
        import provider

        # No network: the provider's reads and the merge are stubbed; the pass logic is the real one.
        self.patch(pipeline, "live_provider", lambda data, path: {})
        self.patch(provider, "pre_merge_check", lambda root, ticket, cfg: ("skipped", "stubbed"))
        self.patch(provider, "merge_with_state", lambda root, path, data, ticket, number, cfg: ("merged", "c" * 40))
        self.patch(orchestrator, "publish_on_change", lambda *a, **k: [])

    def patch(self, mod, name, value):
        old = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, old)

    def write(self, *tickets, **cfg):
        config = {"maxAgents": 2, "runner": "local", "launch": "worktree", "subagentVm": False, "bugbotRequired": False,
                  "autoMergeSizes": ["S", "M"]}
        config.update(cfg)
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "baseGreen": True, "gates": [],
                                         "instance": {"id": "db7970"}, "config": config,
                                         "tickets": {t["id"]: t for t in tickets}}, indent=2) + "\n")

    def beam(self):
        return json.loads(self.path.read_text())


class MergeThenStartTest(LiveRepo):
    def test_a_merge_starts_the_ticket_it_unblocks_in_the_same_pass(self):
        self.write(done_ticket("UI-1"), queued("UI-2", deps=["UI-1"]))
        lines = orchestrator.supervise(self.path, now=NOW)
        text = "\n".join(lines)
        self.assertIn("merge: UI-1 merged", lines)
        self.assertTrue(any(line.startswith("start UI-2 ") for line in lines), text)
        self.assertIn("dispatch: after merging UI-1 in this pass: started UI-2", lines)
        # And its Jira move to In Progress, after UI-1's move to Done.
        self.assertIn("jira: MUST DO UI-2 (HOS-5)", text)
        self.assertIn("jira: MUST DO UI-1 (HOS-1)", text)
        self.assertEqual(self.beam()["tickets"]["UI-2"]["status"], "coding")

    def test_a_merge_frees_a_slot_for_a_waiting_ticket(self):
        busy = queued("UI-9", status="coding", phase="implementing", lastSeenAt=NOW, workerStartedAt=NOW,
                      shuttle={"pending": True, "step": "implement", "startedAt": NOW})
        self.write(done_ticket("UI-1"), busy, queued("UI-3"), maxAgents=2, maxInProgress=2)
        lines = orchestrator.supervise(self.path, now=NOW)
        self.assertIn("merge: UI-1 merged", lines)
        self.assertTrue(any(line.startswith("start UI-3 ") for line in lines), "\n".join(lines))


class RefillTest(unittest.TestCase):
    def data(self, *tickets, **cfg):
        config = {"maxAgents": 4, "runner": "local", "launch": "worktree", "subagentVm": False}
        config.update(cfg)
        return {"version": 1, "runState": "running", "baseGreen": True, "gates": [], "config": config,
                "tickets": {t["id"]: t for t in tickets}}

    def test_a_queued_ticket_with_a_leftover_phase_starts_and_says_so(self):
        data = self.data(queued("UI-4", phase="pr-open"), queued("UI-5", shuttle={"pending": True, "step": "fix"}))
        lines = []
        pipeline._refill(data, lines)
        self.assertIn("dispatch: UI-4 was queued with a leftover phase pr-open; cleared, and it starts now", lines)
        self.assertIn("dispatch: UI-5 was queued with a leftover Shuttle mark; cleared, and it starts now", lines)
        self.assertTrue(any(line.startswith("start UI-4 ") for line in lines))
        self.assertTrue(any(line.startswith("start UI-5 ") for line in lines))

    def test_a_held_start_goes_back_to_queued_and_is_retried(self):
        import agents

        data = self.data(queued("UI-6"))
        real = agents.prepare_launch
        agents.prepare_launch = lambda *a, **k: {"action": "hour"}
        try:
            lines = []
            pipeline._refill(data, lines)
        finally:
            agents.prepare_launch = real
        row = data["tickets"]["UI-6"]
        self.assertEqual((row["status"], row.get("phase")), ("queued", None))
        self.assertTrue(any(line.startswith("dispatch: UI-6 not started (spawn: hour UI-6") for line in lines), lines)
        lines = []
        pipeline._refill(data, lines)
        self.assertTrue(any(line.startswith("start UI-6 ") for line in lines), lines)

    def test_an_orphaned_coding_ticket_restarts_with_its_jira_move(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "config.yaml").write_text("jiraProject: HOS\njiraKeyPrefixes: HOS\n")
        path = tmp / ".warp" / "beam.json"
        data = self.data(queued("UI-7", status="coding", phase="implementing"))
        path.write_text(json.dumps(data))
        lines = pipeline.advance(data, {}, beam_path=path, now=NOW)
        self.assertTrue(any(line.startswith("start UI-7 ") for line in lines), lines)
        self.assertIn("jira: MUST DO UI-7 (HOS-5)", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
