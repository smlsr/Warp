"""1.7.17: owned pull requests get instructions, not a second pusher; every write retries the other ways."""

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

import attempt  # noqa: E402
import drive  # noqa: E402
import lane  # noqa: E402
import ownership  # noqa: E402
import pipeline  # noqa: E402

NOW = "2026-11-08T21:40:00Z"
URL = "https://github.com/o/r/pull/%s"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp" / "outbox").mkdir(parents=True)
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.write_text(json.dumps({"tickets": {}}))
        ownership._CACHE.clear()


class OwnedFixTest(Base):
    def red(self, tid, number, **extra):
        row = {"id": tid, "status": "review", "phase": "reviewing", "locks": ["src/x"], "size": "S", "module": "m",
               "pr": {"url": URL % number, "headSha": "h%s" % number, "ci": "red", "ciSha": "h%s" % number,
                      "conflict": True, "mergeStateStatus": "DIRTY"}}
        row.update(extra)
        return row

    def test_owned_prs_are_told_and_no_agent_starts(self):
        (self.tmp / ".warp" / "outbox" / "coord-e49.md").write_text("- WORKER bc-77 #548\n- READY #1418 abcdef1\n")
        data = {"_repoRoot": str(self.tmp), "config": {"maxAgents": 30}, "tickets": {
            "A-1": self.red("A-1", 685), "A-2": self.red("A-2", 548), "A-3": self.red("A-3", 1418),
            "A-4": self.red("A-4", 1442, parkReason="owner outside Warp (e49-restart coordinator)"),
            "A-5": self.red("A-5", 9)},
            "prOwners": {"685": {"owner": "coord-ui", "coordinator": "coord-ui", "agent": "bc-5"}}}
        lines = []
        for tid in ("A-1", "A-2", "A-3", "A-4", "A-5"):
            item = pipeline._fix_class(data["tickets"][tid], {})
            pipeline._launch_fix(data, data["tickets"][tid], item, lines)
        text = "\n".join(lines)
        for number, who in (("685", "coord-ui"), ("548", "coord-e49"), ("1418", "coord-e49"), ("1442", "e49-restart coordinator")):
            self.assertIn("#%s is owned by %s" % (number, who), text)
        starts = [l for l in lines if l.startswith("start ")]
        self.assertEqual([s.split()[1] for s in starts], ["A-5"])  # only the one Warp owns
        before = len(lines)
        pipeline._launch_fix(data, data["tickets"]["A-1"], pipeline._fix_class(data["tickets"]["A-1"], {}), lines)
        self.assertEqual(len(lines), before)  # once per head and kind

    def test_owner_recording_never_claims_a_coordinators_pr(self):
        data = {"_repoRoot": str(self.tmp), "tickets": {
            "A-4": self.red("A-4", 1442, parkReason="owner outside Warp (e49-restart coordinator)"),
            "W-1": self.red("W-1", 7, workerStartedAt=NOW)}}
        pipeline._record_owners(data, now=NOW)
        self.assertEqual(data["prOwners"]["1442"]["coordinator"], "e49-restart coordinator")
        self.assertEqual(data["prOwners"]["7"]["coordinator"], "warp")


class LaneOwnedTest(Base):
    def test_an_owned_child_is_told_to_rebuild_and_warp_does_not_push(self):
        import intake

        intake.save_lane(self.path, {"queue": {"1@a": {"pr": "1", "url": URL % 1, "sha": "a" * 40, "branch": "p", "queuedAt": NOW}}})
        told, rebuilt = [], []
        lines = lane.run_lane(self.path, {"tickets": {}}, merge=lambda e: ("merged", "m" * 40),
                              list_children=lambda b: [{"number": "2", "url": URL % 2, "branch": "c", "ownedBy": "coord-ui"}],
                              retarget=lambda url, to: (True, ""), rebuild=lambda *a: rebuilt.append(a) or ("rebuilt", "x"),
                              send=lambda owner, text, agent="": told.append((owner, text)) or ["relay: x"], now=NOW)
        self.assertEqual(rebuilt, [])
        self.assertEqual(told[0][0], "coord-ui")
        self.assertIn("Rebuild it once", told[0][1])


class LadderTest(Base):
    def test_rest_then_gh_then_parent_then_coordinator(self):
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=self.tmp, check=True)
        calls = []
        merged = {"v": False}

        def rest(cmd, cwd=None):
            calls.append(cmd[:4])
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="GraphQL: Resource not accessible by integration")
            if cmd[:2] == ["gh", "api"] and cmd[3] == "PUT":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403: Resource not accessible")
            if cmd[:2] == ["gh", "api"] and cmd[3] == "GET":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"merged": merged["v"]}), stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no")

        env = drive.Env(rest=rest, git=rest, env={})
        data = {"tickets": {}, "prOwners": {"5": {"owner": "coord-ui", "coordinator": "coord-ui", "agent": "bc-9"}}}
        writes = drive.Writes(self.path, data, self.tmp, "o/r", env, NOW)
        outcome, detail = writes.merge({"pr": "5", "url": URL % 5, "sha": "a" * 40}, "squash")
        self.assertEqual(outcome, "rejected")
        self.assertIn(["gh", "api", "-X", "PUT"], calls)
        self.assertIn(["gh", "pr", "merge", "5"], calls)
        self.assertTrue(writes.lines[0].startswith("merge: MUST DO #5 with the GitHub MCP merge_pull_request"), writes.lines)
        told = []
        send = lambda owner, text, agent="": told.append((owner, text, agent)) or ["relay: sent"]
        attempt.check(self.path, writes.happened, send, now=NOW)
        self.assertEqual(told[0][0], "coord-ui")
        self.assertIn("You have the permissions for it", told[0][1])
        merged["v"] = True
        self.assertEqual(attempt.check(self.path, writes.happened, send, now=NOW), ["merge: #5 done by its coordinator"])

    def test_a_definite_refusal_is_not_retried(self):
        steps = [("REST", lambda: (False, "the head moved past aaaa (sha guard); not merged", True)),
                 ("gh", lambda: (True, "", False))]
        ok, how, detail, tried = attempt.chain(steps)
        self.assertFalse(ok)
        self.assertEqual(how, "REST")
        self.assertEqual(len(tried), 1)


if __name__ == "__main__":
    unittest.main()
