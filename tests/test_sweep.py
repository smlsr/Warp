"""Broken-state sweep: find, skip, unlock, and escalate."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import alarm_repair  # noqa: E402
import beam  # noqa: E402
import sweep  # noqa: E402
import watch  # noqa: E402

NOW = "2026-10-07T12:00:00Z"
OLD = "2026-10-07T09:00:00Z"
SOON = "2026-10-07T12:10:00Z"
LATER = "2026-10-07T12:15:00Z"


def ticket(tid, **extra):
    row = {
        "id": tid,
        "status": "review",
        "phase": "reviewing",
        "locks": ["src/%s" % tid.lower()],
        "size": "S",
        "module": "app",
        "autoMerge": True,
        "jiraKey": "WAR-1",
        "branch": "warp/%s" % tid,
        "summary": "ship %s" % tid,
        "agent": None,
        "pr": {"bugbot": "pass", "rollup": "green", "ci": "green"},
    }
    row.update(extra)
    return row


def data_of(tickets, **config):
    cfg = {
        "maxAgents": 4,
        "bugbotRequired": True,
        "maxFixAttempts": 5,
        "maxRecoveries": 5,
        "maxStallFixes": 5,
        "maxAlarmRepairs": 5,
        "repairSweepMinutes": 15,
        "autoMergeSizes": ["S", "M"],
        "runner": "cloud",
        "launch": "worktree",
        "subagentVm": False,
    }
    cfg.update(config)
    return {
        "version": 1,
        "runState": "running",
        "baseGreen": True,
        "tickets": {row["id"]: row for row in tickets},
        "gates": [],
        "config": cfg,
    }


def text(lines):
    return "\n".join(lines)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.path = warp / "beam.json"

    def test_defaults_are_five_and_the_sweep_is_fifteen_minutes(self):
        cfg = beam.default_config()
        self.assertEqual(cfg["maxFixAttempts"], 5)
        self.assertEqual(cfg["maxRecoveries"], 5)
        self.assertEqual(cfg["maxAlarmRepairs"], 5)
        self.assertEqual(cfg["maxStallFixes"], 5)
        self.assertEqual(cfg["repairSweepMinutes"], 15)
        self.assertEqual(alarm_repair.DEFAULT_MAX_ALARM_REPAIRS, 5)
        self.assertEqual(beam.DEFAULT_MAX_RECOVERIES, 5)

    def test_sweep_finds_broken_work_and_logs_it(self):
        members = ["P-002", "P-003", "P-004", "P-018"]
        rows = [ticket(tid, status="merged", phase="merged", locks=[]) for tid in members]
        rows.extend(
            [
                ticket("C-1", pr={"bugbot": "pass", "rollup": "green", "ci": "red", "url": "https://example.test/pull/1"}),
                ticket(
                    "R-1",
                    pr={"bugbot": "pass", "rollup": "green", "ci": "green", "conflict": True, "url": "https://example.test/pull/2"},
                ),
                ticket(
                    "B-1",
                    pr={"bugbot": "fail", "rollup": "green", "ci": "green", "bugbotFindings": ["unused import"], "url": "https://example.test/pull/3"},
                ),
                ticket("M-1", status="merged", phase="merged", locks=["src/old"]),
                ticket("L-1", locks=["src/live"]),
            ]
        )
        data = data_of(rows)
        data["baseGreen"] = False
        data["beamSync"] = "behind"
        data["listener"] = {"lastSeenAt": OLD, "agentId": "listener-1"}
        data["gates"] = [{"key": "G1", "status": "red", "members": members, "evidence": "stale"}]
        self.path.write_text(json.dumps(data) + "\n")
        lines = sweep.run(data, beam_path=self.path, now=NOW)
        blob = text(lines)
        self.assertIn("ci: rerun C-1", blob)
        self.assertIn("fixer: R-1 rebase", blob)
        self.assertIn("rebase onto main", blob)
        self.assertIn("unused import", blob)
        self.assertIn("step=fix", blob)
        self.assertIn("lock: remove M-1", blob)
        self.assertEqual(data["tickets"]["M-1"]["locks"], [])
        self.assertEqual(data["tickets"]["L-1"]["locks"], ["src/live"])
        self.assertIn("listener: poll", blob)
        self.assertIn("sweep: action listener poll", blob)
        self.assertEqual(data["listener"]["pollStartedAt"], NOW)
        self.assertIn("beam: sync", blob)
        self.assertIn("herald: G1 red cleared. Members merged. Tick ran.", blob)
        self.assertIn("dispatch-base-fix checkout=worktree", blob)
        self.assertEqual(blob.count("slack: sweep started"), 1)
        self.assertIn("findings=", blob)
        log = json.loads((self.path.parent / "sweep.jsonl").read_text().splitlines()[-1])
        kinds = {item["kind"] for item in log["findings"]}
        self.assertTrue({"ci", "rebase", "fix", "lock", "listener", "beam", "gate", "base"} <= kinds)
        self.assertEqual(data["repairSweep"]["at"], NOW)
        shown = text(watch.snapshot(data, now=NOW))
        self.assertIn("sweep: at=%s" % NOW, shown)
        self.assertIn("sweep: none", text(watch.snapshot(data_of([ticket("Q-1", status="queued")]), now=NOW)))

    def test_sweep_skips_work_already_running_or_out_of_slots(self):
        data = data_of(
            [
                ticket(
                    "S-1",
                    shuttle={"pending": True, "step": "fix"},
                    pr={"bugbot": "pass", "rollup": "green", "ci": "green", "conflict": True, "url": "https://example.test/pull/4"},
                ),
                ticket(
                    "R-1",
                    pr={"bugbot": "pass", "rollup": "green", "ci": "green", "behind": True, "url": "https://example.test/pull/5"},
                ),
                ticket("C-1", pr={"bugbot": "pass", "rollup": "green", "ci": "red", "url": "https://example.test/pull/6"}),
                ticket("H-1", status="claimed", phase="implementing"),
            ],
            maxAgents=1,
        )
        # A poll is already out, so the sweep does not issue a second one.
        data["listener"] = {"lastSeenAt": OLD, "pollStartedAt": NOW, "agentId": "listener"}
        data["beamSync"] = "behind"
        data["repairSweep"] = {"at": OLD, "pending": {"beam": True}}
        self.path.write_text(json.dumps(data) + "\n")
        lines = sweep.run(data, beam_path=self.path, now=NOW)
        blob = text(lines)
        self.assertIn("sweep: skip S-1 running", blob)
        self.assertNotIn("fixer: S-1 rebase", blob)
        self.assertIn("sweep: skip R-1 slot", blob)
        self.assertNotIn("fixer: R-1 rebase", blob)
        self.assertIn("ci: rerun C-1", blob)
        self.assertIn("sweep: skip listener running", blob)
        self.assertNotIn("listener: poll", blob)
        self.assertIn("sweep: skip beam running", blob)
        self.assertNotIn("beam: sync", blob)

    def test_orphaned_locks_go_and_a_second_sweep_waits(self):
        data = data_of(
            [
                ticket("M-1", status="merged", phase="merged", locks=["src/old"]),
                ticket("L-1", locks=["src/live"]),
            ]
        )
        self.path.write_text("{}\n")
        first = sweep.run(data, beam_path=self.path, now=NOW)
        self.assertEqual(data["tickets"]["M-1"]["locks"], [])
        self.assertEqual(data["tickets"]["L-1"]["locks"], ["src/live"])
        self.assertIn("lock: remove M-1", text(first))
        self.assertEqual(sweep.run(data, beam_path=self.path, now=SOON), [])
        self.assertEqual((self.path.parent / "sweep.jsonl").read_text().count("\n"), 1)
        later = sweep.run(data, beam_path=self.path, now=LATER)
        self.assertIn("findings=0", text(later))
        self.assertNotIn("slack: sweep", text(later))
        self.assertEqual(data["tickets"]["L-1"]["locks"], ["src/live"])

    def test_escalate_parks_once_and_does_not_repost(self):
        row = ticket(
            "T-1",
            locks=[],
            pr={"bugbot": "pass", "rollup": "green", "ci": "red", "url": "https://example.test/pull/9"},
        )
        row["watch"] = {"fixerAttempts": 5, "state": "reviewing", "since": OLD, "progressAt": NOW}
        data = data_of([row], maxStallFixes=5)
        lines = sweep.run(data, now=NOW)
        blob = text(lines)
        self.assertEqual(data["tickets"]["T-1"]["status"], "parked")
        self.assertEqual(data["tickets"]["T-1"]["parkReason"], "fixer cap: ci")
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 5)
        self.assertEqual(blob.count("slack: sweep started 0 escalated 1"), 1)
        self.assertIn("slack: alarm T-1 fixer cap: ci", blob)
        again = sweep.run(data, now=LATER)
        self.assertNotIn("slack:", text(again))
        self.assertEqual(data["tickets"]["T-1"]["status"], "parked")

    def test_a_finding_count_or_string_starts_a_fix(self):
        data = data_of(
            [
                ticket(
                    "B-2",
                    pr={
                        "bugbot": "pass",
                        "rollup": "green",
                        "ci": "green",
                        "bugbotFindings": 2,
                        "url": "https://example.test/pull/9",
                    },
                )
            ]
        )
        self.path.write_text(json.dumps(data) + "\n")
        lines = sweep.run(data, beam_path=self.path, now=NOW)
        blob = text(lines)
        self.assertIn("2 Bugbot findings (details not loaded)", blob)
        self.assertIn("step=fix", blob)
        self.assertEqual(data["tickets"]["B-2"]["status"], "fix")

        stringy = data_of(
            [
                ticket(
                    "B-3",
                    pr={
                        "bugbot": "fail",
                        "rollup": "green",
                        "ci": "green",
                        "bugbotFindings": "unused import",
                        "url": "https://example.test/pull/10",
                    },
                )
            ]
        )
        detail = sweep._detail(stringy["tickets"]["B-3"], "fix", {})
        self.assertEqual(detail, "bugbot findings")
        reasons = [
            "bugbot: %s" % item
            for item in sweep._bugbot_findings(stringy["tickets"]["B-3"]["pr"])
        ]
        self.assertEqual(reasons, ["bugbot: unused import"])
