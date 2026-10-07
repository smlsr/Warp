"""Parent pipeline: review, fix, merge, alarms, and slot refill."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import orchestrator  # noqa: E402
import pipeline  # noqa: E402

NOW = "2026-10-07T12:00:00Z"


def ticket(tid, **extra):
    row = {
        "id": tid,
        "status": "queued",
        "locks": ["src/%s" % tid.lower()],
        "size": "S",
        "module": "app",
        "autoMerge": True,
        "jiraKey": "WAR-1",
        "branch": "warp/%s" % tid,
        "pr": {},
    }
    row.update(extra)
    return row


def beam(tickets, **config):
    cfg = {
        "maxAgents": 4,
        "maxLocalSubagents": 4,
        "bugbotRequired": True,
        "maxFixAttempts": 3,
        "maxRecoveries": 3,
        "autoMergeSizes": ["S", "M"],
        "checkCommand": "make ci",
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


class BugbotMergeTests(unittest.TestCase):
    def test_implement_fix_round_then_merge(self):
        data = beam([ticket("T-1", acs=["builds"])])
        first = pipeline.advance(data, now=NOW)
        self.assertIn("phase: T-1 implementing", text(first))
        self.assertIn("step=implement", text(first))
        self.assertNotEqual(data["tickets"]["T-1"]["status"], "merged")
        self.assertTrue(data["tickets"]["T-1"]["shuttle"]["pending"])

        opened = pipeline.advance(
            data,
            {
                "T-1": {
                    "returned": True,
                    "pr": "https://example.test/pull/7",
                    "acs": {"builds": "pass"},
                    "check": "green",
                    "ci": "green",
                    "rollup": "pending",
                    "bugbot": "",
                    "findings": [],
                }
            },
            now=NOW,
        )
        row = data["tickets"]["T-1"]
        self.assertIn("phase: T-1 pr-open", text(opened))
        self.assertIn("bugbot: request T-1 https://example.test/pull/7", text(opened))
        self.assertNotIn("merged", row["status"])
        self.assertNotEqual(row["phase"], "merged")
        self.assertTrue(row["pr"]["opened"])
        self.assertNotEqual(row["status"], "done")

        red = pipeline.advance(
            data,
            {
                "T-1": {
                    "bugbot": "fail",
                    "findings": ["unused import"],
                    "rollup": "red",
                    "ci": "red",
                    "check": "green",
                    "acs": {"builds": "pass"},
                }
            },
            now=NOW,
        )
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "fix")
        self.assertEqual(row["phase"], "fixing")
        self.assertIn("step=fix", text(red))
        self.assertIn("unused import", text(red))
        self.assertNotEqual(row["status"], "merged")

        again = pipeline.advance(
            data,
            {
                "T-1": {
                    "returned": True,
                    "bugbot": "running",
                    "findings": [],
                    "rollup": "pending",
                    "acs": {"builds": "pass"},
                    "check": "green",
                }
            },
            now=NOW,
        )
        self.assertIn("bugbot: request T-1", text(again))
        self.assertEqual(row["attempts"], 1)
        self.assertNotEqual(row["status"], "parked")

        merged = pipeline.advance(
            data,
            {
                "T-1": {
                    "bugbot": "pass",
                    "findings": [],
                    "rollup": "green",
                    "ci": "green",
                    "check": "green",
                    "acs": {"builds": "pass"},
                    "merge": "merged",
                    "sha": "abc123",
                }
            },
            now=NOW,
        )
        blob = text(merged)
        self.assertIn("phase: T-1 ready", blob)
        self.assertIn("phase: T-1 merging", blob)
        self.assertIn("phase: T-1 merged", blob)
        self.assertIn("merge: T-1 merged", blob)
        self.assertIn("herald: T-1 merged.", blob)
        self.assertIn("jira: MUST DO T-1 (WAR-1) transition to Done.", blob)
        self.assertIn("slack: T-1 merged https://example.test/pull/7", blob)
        self.assertIn("beam: push", blob)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["phase"], "merged")
        self.assertEqual(row["pr"]["sha"], "abc123")
        self.assertTrue(orchestrator.parent_may_exit(data))

    def test_failed_acceptance_criterion_starts_a_fix(self):
        data = beam(
            [
                ticket(
                    "AC-1",
                    status="review",
                    phase="reviewing",
                    pr={"url": "https://example.test/pull/3", "bugbot": "pass", "rollup": "green", "ci": "green", "check": "green"},
                )
            ]
        )
        lines = pipeline.advance(
            data,
            {"AC-1": {"acs": {"builds": "pass", "docs": "fail"}, "bugbot": "pass", "rollup": "green", "check": "green"}},
            now=NOW,
        )
        self.assertEqual(data["tickets"]["AC-1"]["status"], "fix")
        self.assertIn("ac docs fail", text(lines))
        self.assertIn("step=fix", text(lines))
        self.assertNotEqual(data["tickets"]["AC-1"]["status"], "merged")


class AlarmRecoveryTests(unittest.TestCase):
    def test_lock_escape_repairs_and_reruns_inside_supervise(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        warp = tmp / ".warp"
        warp.mkdir()
        path = warp / "beam.json"
        data = beam(
            [
                ticket(
                    "L-1",
                    status="alarm",
                    alarm="lock-escape",
                    escaped=["src/extra.py"],
                    locks=["src/a"],
                    phase="implementing",
                )
            ]
        )
        path.write_text(json.dumps(data) + "\n")
        lines = orchestrator.supervise(path, now=NOW)
        blob = text(lines)
        self.assertIn("alarm-repair: start L-1", blob)
        self.assertNotIn("step=implement", blob)
        saved = json.loads(path.read_text())
        locks = saved["tickets"]["L-1"]["locks"]
        self.assertIn("src/a", locks)
        self.assertIn("src/extra.py", locks)
        self.assertEqual(saved["tickets"]["L-1"]["status"], "claimed")

    def test_worker_died_restarts_and_stuck_stops_at_the_cap(self):
        data = beam([ticket("W-1", status="alarm", alarm="worker-died", recoveries=1)])
        lines = pipeline.advance(data, now=NOW)
        self.assertIn("step=restart", text(lines))
        self.assertIn("herald: W-1 worker died. A new Shuttle started.", text(lines))
        self.assertEqual(data["tickets"]["W-1"]["status"], "recovering")
        held = pipeline.advance(data, now=NOW)
        self.assertIn("shuttle: hold W-1", text(held))
        self.assertEqual(sum(1 for line in held if line.startswith("start ") and "step=restart" in line), 0)

        stuck = beam(
            [ticket("S-1", status="alarm", alarm="stuck", stuckRestarts=3)],
            maxRecoveries=3,
        )
        capped = pipeline.advance(stuck, now=NOW)
        self.assertIn("stuck: cap S-1", text(capped))
        self.assertNotIn("step=restart", text(capped))
        self.assertEqual(stuck["tickets"]["S-1"]["status"], "alarm")

    def test_ci_red_and_bugbot_failed_start_a_fix(self):
        data = beam(
            [
                ticket(
                    "C-1",
                    status="alarm",
                    alarm="ci-red",
                    pr={"url": "https://example.test/pull/9", "ci": "red", "rollup": "red"},
                )
            ]
        )
        lines = pipeline.advance(data, now=NOW)
        self.assertEqual(data["tickets"]["C-1"]["status"], "fix")
        self.assertEqual(data["tickets"]["C-1"]["attempts"], 1)
        self.assertIn("ci-red", text(lines))
        self.assertIn("step=fix", text(lines))

        failed = beam(
            [
                ticket(
                    "B-1",
                    status="alarm",
                    alarm="bugbot-failed",
                    attempts=2,
                    pr={"url": "https://example.test/pull/10", "bugbot": "fail", "bugbotFindings": ["race"]},
                )
            ]
        )
        parked = pipeline.advance(failed, now=NOW)
        self.assertEqual(failed["tickets"]["B-1"]["status"], "parked")
        self.assertNotIn("step=fix", text(parked))
        self.assertIn("phase: B-1 parked", text(parked))


class ParkAndRefillTests(unittest.TestCase):
    def test_three_reds_park_and_a_fourth_does_not_start(self):
        data = beam(
            [
                ticket(
                    "P-1",
                    status="review",
                    phase="reviewing",
                    pr={"url": "https://example.test/pull/4", "bugbot": "fail", "bugbotFindings": ["nit"], "rollup": "red"},
                )
            ]
        )
        starts = 0
        for round_ in range(3):
            lines = pipeline.advance(
                data,
                {"P-1": {"bugbot": "fail", "findings": ["nit"], "rollup": "red", "acs": {"builds": "fail"}}},
                now=NOW,
            )
            if any(line.startswith("start ") and "step=fix" in line for line in lines):
                starts += 1
                self.assertLess(data["tickets"]["P-1"]["attempts"], 3)
                pipeline.advance(data, {"P-1": {"returned": True, "bugbot": "running", "findings": [], "acs": {"builds": "fail"}}}, now=NOW)
            else:
                self.assertEqual(round_, 2)
        self.assertEqual(starts, 2)
        self.assertEqual(data["tickets"]["P-1"]["status"], "parked")
        self.assertEqual(data["tickets"]["P-1"]["attempts"], 3)
        extra = pipeline.advance(
            data,
            {"P-1": {"bugbot": "fail", "findings": ["nit"], "rollup": "red"}},
            now=NOW,
        )
        self.assertFalse(any(line.startswith("start ") for line in extra))
        self.assertEqual(data["tickets"]["P-1"]["attempts"], 3)

    def test_green_rollup_frees_the_slot_and_starts_the_next_ticket(self):
        data = beam(
            [
                ticket(
                    "R-1",
                    status="review",
                    phase="reviewing",
                    locks=["src/a"],
                    pr={"url": "https://example.test/pull/1", "rollup": "pending", "bugbot": "running"},
                ),
                ticket("R-2", locks=["src/b"]),
            ],
            maxAgents=1,
            maxLocalSubagents=1,
        )
        waiting = pipeline.advance(data, {"R-1": {"bugbot": "running", "rollup": "pending"}}, now=NOW)
        self.assertNotIn("start R-2", text(waiting))
        self.assertNotIn("step=implement", text(waiting))
        self.assertEqual(data["tickets"]["R-2"]["status"], "queued")
        freed = pipeline.advance(
            data,
            {"R-1": {"bugbot": "running", "rollup": "green", "ci": "green"}},
            now=NOW,
        )
        self.assertIn("step=implement", text(freed))
        self.assertIn("R-2", text(freed))
        self.assertEqual(data["tickets"]["R-2"]["status"], "coding")
        self.assertEqual(data["tickets"]["R-2"]["phase"], "implementing")
        self.assertEqual(data["tickets"]["R-1"]["status"], "review")
        self.assertNotEqual(data["tickets"]["R-1"]["status"], "merged")

    def test_resume_adopts_an_open_pull_request(self):
        data = beam(
            [
                ticket(
                    "E-1",
                    status="review",
                    pr={"url": "https://example.test/pull/18", "rollup": "pending", "bugbot": ""},
                )
            ]
        )
        lines = pipeline.advance(data, now=NOW)
        blob = text(lines)
        self.assertNotIn("step=implement", blob)
        self.assertIn("bugbot: request E-1 https://example.test/pull/18", blob)
        self.assertEqual(data["tickets"]["E-1"]["phase"], "reviewing")
        self.assertTrue(data["tickets"]["E-1"].get("adopted"))

        fixing = beam(
            [
                ticket(
                    "E-2",
                    status="review",
                    pr={
                        "url": "https://example.test/pull/19",
                        "bugbot": "fail",
                        "bugbotFindings": ["rename the helper"],
                        "rollup": "red",
                    },
                )
            ]
        )
        fixed = pipeline.advance(fixing, now=NOW)
        blob = text(fixed)
        self.assertNotIn("step=implement", blob)
        self.assertIn("step=fix", blob)
        self.assertIn("rename the helper", blob)
        self.assertEqual(fixing["tickets"]["E-2"]["phase"], "fixing")
        self.assertNotEqual(fixing["tickets"]["E-2"]["status"], "merged")


def _clean(findings):
    return ticket(
        "G-1",
        status="review",
        phase="ready",
        autoMerge=True,
        pr={
            "url": "https://example.test/pull/1",
            "bugbot": "pass",
            "rollup": "green",
            "ci": "green",
            "check": "green",
            "acResults": {"builds": "pass"},
            "bugbotFindings": findings,
        },
    )


class FindingsTests(unittest.TestCase):
    def test_findings_of_accepts_every_stored_shape(self):
        def of(raw):
            return pipeline.findings_of({"pr": {"bugbotFindings": raw}})

        self.assertEqual(of(2), ["2 Bugbot findings (details not loaded)"])
        self.assertEqual(of(0), [])
        self.assertEqual(of(False), [])
        self.assertEqual(of("  unused import  "), ["unused import"])
        self.assertEqual(of([" a ", "", "b"]), ["a", "b"])
        self.assertEqual(
            of([{"message": "race", "body": "hidden"}, {"message": "  ", "body": "nit"}, {"other": "x"}]),
            ["race", "nit"],
        )
        self.assertEqual(of([{"other": "x"}]), [])
        self.assertEqual(of(None), [])
        self.assertEqual(of(True), [])
        self.assertEqual(of(1.5), [])
        self.assertEqual(of({"message": "race"}), [])
        self.assertEqual(pipeline.findings_of({}), [])

    def test_count_above_zero_blocks_the_merge_gate_and_starts_a_fix(self):
        cfg = {"bugbotRequired": True, "checkCommand": "make ci", "autoMergeSizes": ["S"]}
        self.assertFalse(pipeline.gate_open(_clean(2), cfg))
        self.assertTrue(pipeline.gate_open(_clean(0), cfg))
        self.assertTrue(pipeline.gate_open(_clean(False), cfg))
        self.assertTrue(pipeline.gate_open(_clean(None), cfg))

        data = beam([_clean(2)])
        data["tickets"]["G-1"]["phase"] = "reviewing"
        lines = pipeline.advance(data, now=NOW)
        blob = text(lines)
        self.assertIn("step=fix", blob)
        self.assertIn("2 Bugbot findings (details not loaded)", blob)
        self.assertEqual(data["tickets"]["G-1"]["status"], "fix")
        self.assertNotEqual(data["tickets"]["G-1"]["status"], "merged")

    def test_writers_store_a_list(self):
        import jira_sync

        row = ticket("S-1", pr={"url": "https://example.test/pull/1"})
        pipeline._apply_snapshot(row, {"findings": "unused import"})
        self.assertEqual(row["pr"]["bugbotFindings"], ["unused import"])
        pipeline._apply_snapshot(row, {"findings": 4})
        self.assertEqual(row["pr"]["bugbotFindings"], ["4 Bugbot findings (details not loaded)"])
        pipeline._apply_snapshot(row, {"findings": [{"body": "nit"}]})
        self.assertEqual(row["pr"]["bugbotFindings"], ["nit"])

        counted = {"id": "H-1", "status": "review", "pr": {"bugbotFindings": 2}}
        jira_sync.mark_bugbot_fail(counted, {"maxFixAttempts": 5}, True)
        self.assertEqual(counted["pr"]["bugbotFindings"], ["3 Bugbot findings (details not loaded)"])
        jira_sync.mark_bugbot_pass(counted)
        self.assertEqual(counted["pr"]["bugbotFixed"], 3)
        self.assertEqual(counted["pr"]["bugbotFindings"], ["3 Bugbot findings (details not loaded)"])

        text_row = {"id": "H-2", "pr": {"bugbotFindings": "unused import"}}
        jira_sync.mark_bugbot_fail(text_row, {"maxFixAttempts": 5}, False)
        self.assertEqual(
            text_row["pr"]["bugbotFindings"],
            ["unused import", "Bugbot finding (details not loaded)"],
        )


if __name__ == "__main__":
    unittest.main()
