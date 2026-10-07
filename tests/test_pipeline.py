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


ROUTES = ["cmd/qa-api/routes.go", "pkg/lib/auth/routes.go"]
SHA = "a" * 40
SHA2 = "b" * 40


def reviewing(tid, **pr):
    base = {
        "url": "https://example.test/pull/%s" % tid,
        "bugbot": "pass",
        "rollup": "pending",
        "ci": "pending",
        "check": "green",
    }
    base.update(pr)
    return ticket(tid, status="review", phase="reviewing", pr=base)


class LivePullRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        self.path = warp / "beam.json"

    def _result(self, tid, doc):
        folder = self.tmp / "tasks" / tid
        folder.mkdir(parents=True)
        (folder / "RESULT.json").write_text(json.dumps(doc) + "\n")

    def test_green_conflicted_pull_request_rebases_then_merges(self):
        self._result("A-001", {"acResults": {"routes": "pass"}})
        data = beam([reviewing("A-001")])
        self.path.write_text(json.dumps(data) + "\n")
        first = pipeline.advance(
            data,
            {
                "A-001": {
                    "headSha": SHA,
                    "headAgeMinutes": 30,
                    "rollup": "green",
                    "ci": "green",
                    "check": "green",
                    "checkCount": 12,
                    "bugbot": "pass",
                    "findings": [],
                    "mergeable": "CONFLICTING",
                    "mergeStateStatus": "DIRTY",
                    "conflictFiles": ROUTES,
                }
            },
            beam_path=self.path,
            now=NOW,
        )
        blob = text(first)
        row = data["tickets"]["A-001"]
        self.assertNotIn("review: wait", blob)
        self.assertIn("fixer: A-001 rebase", blob)
        self.assertIn("cmd/qa-api/routes.go", blob)
        self.assertIn("pkg/lib/auth/routes.go", blob)
        self.assertIn("route registration files keep both sides", blob)
        self.assertIn("run checkCommand and push", blob)
        self.assertEqual(row["status"], "fix")
        self.assertEqual(row["phase"], "fixing")
        self.assertEqual(row["pr"]["conflict"], ROUTES)
        self.assertEqual(row["pr"]["mergeable"], "CONFLICTING")
        self.assertEqual(row["pr"]["headSha"], SHA)
        self.assertEqual(row["pr"]["rollup"], "green")
        self.assertEqual(row["pr"]["rollupSha"], SHA)
        self.assertEqual(row["pr"]["acResults"], {"routes": "pass"})
        self.assertNotEqual(row["status"], "merged")
        self.assertNotIn("_repoRoot", data)

        second = pipeline.advance(
            data,
            {
                "A-001": {
                    "returned": True,
                    "headSha": SHA2,
                    "rollup": "green",
                    "ci": "green",
                    "check": "green",
                    "checkCount": 12,
                    "bugbot": "pass",
                    "findings": [],
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "conflict": False,
                    "conflictFiles": [],
                }
            },
            beam_path=self.path,
            now=NOW,
        )
        self.assertIn("bugbot: request A-001", text(second))
        self.assertNotIn("review: wait", text(second))
        self.assertNotEqual(row["status"], "merged")
        self.assertFalse(row["pr"].get("conflict"))
        self.assertEqual(row["pr"]["bugbot"], "")

        third = pipeline.advance(
            data,
            {
                "A-001": {
                    "headSha": SHA2,
                    "bugbot": "pass",
                    "findings": [],
                    "rollup": "green",
                    "ci": "green",
                    "check": "green",
                    "checkCount": 12,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "conflict": False,
                    "acs": {"routes": "pass"},
                    "merge": "merged",
                    "sha": SHA2,
                }
            },
            beam_path=self.path,
            now=NOW,
        )
        done = text(third)
        self.assertNotIn("review: wait", done)
        self.assertIn("merge: A-001 merged", done)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["phase"], "merged")

    def test_no_checks_and_a_stale_finding_rebase_instead_of_waiting(self):
        data = beam(
            [
                reviewing(
                    "A-004",
                    bugbot="fail",
                    bugbotFindings=1,
                    check="pending",
                    rollup="pending",
                    ci="pending",
                )
            ]
        )
        data["tickets"]["A-004"]["pr"]["bugbotFindings"] = "routes registered twice"
        lines = pipeline.advance(
            data,
            {
                "A-004": {
                    "headSha": SHA2,
                    "headAgeMinutes": 10,
                    "rollup": "absent",
                    "ci": "absent",
                    "checkCount": 0,
                    "checks": [],
                    "mergeable": "CONFLICTING",
                    "mergeStateStatus": "DIRTY",
                    "conflictFiles": ROUTES,
                }
            },
            now=NOW,
        )
        blob = text(lines)
        row = data["tickets"]["A-004"]
        self.assertNotIn("review: wait", blob)
        self.assertNotIn("ci: start", blob)
        self.assertNotIn("routes registered twice", blob)
        self.assertIn("fixer: A-004 rebase", blob)
        self.assertEqual(pipeline.findings_of(row), [])
        self.assertEqual(row["pr"]["bugbotFindings"], [])
        self.assertTrue(row["pr"]["ciAbsent"])
        self.assertEqual(row["pr"]["conflict"], ROUTES)
        self.assertNotEqual(row["status"], "merged")

    def test_a_stale_sha_drops_green_results_and_requests_bugbot(self):
        old = "c" * 40
        row = reviewing(
            "S-9",
            headSha=old,
            rollup="green",
            rollupSha=old,
            ci="green",
            ciSha=old,
            check="green",
            checkSha=old,
            bugbot="pass",
            bugbotSha=old,
            bugbotFindings=["old race"],
            bugbotFindingsSha=old,
            acResults={"builds": "pass"},
        )
        data = beam([row])
        lines = pipeline.advance(
            data,
            {
                "S-9": {
                    "headSha": SHA,
                    "rollup": "green",
                    "ci": "green",
                    "check": "green",
                    "checkCount": 12,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "conflict": False,
                }
            },
            now=NOW,
        )
        saved = data["tickets"]["S-9"]
        self.assertEqual(saved["pr"]["rollup"], "green")
        self.assertEqual(saved["pr"]["rollupSha"], SHA)
        self.assertEqual(saved["pr"]["bugbot"], "pending")
        self.assertEqual(saved["pr"]["bugbotFindings"], [])
        self.assertEqual(pipeline.findings_of(saved), [])
        self.assertIn("bugbot: request S-9", text(lines))
        self.assertNotIn("old race", text(lines))
        self.assertNotEqual(saved["status"], "merged")

    def test_a_green_check_beats_a_pending_rollup_for_the_same_head(self):
        same = {
            "id": "C-1",
            "status": "review",
            "pr": {
                "headSha": SHA,
                "rollup": "pending",
                "rollupSha": SHA,
                "check": "green",
                "checkSha": SHA,
                "ci": "pending",
                "ciSha": SHA,
            },
        }
        self.assertTrue(orchestrator.checks_green(same, {"checkCommand": "make ci"}))
        same["pr"]["rollup"] = "red"
        self.assertFalse(orchestrator.checks_green(same, {"checkCommand": "make ci"}))
        same["pr"]["rollup"] = "pending"
        same["pr"]["rollupSha"] = "old"
        same["pr"]["check"] = "green"
        same["pr"]["checkSha"] = SHA
        self.assertTrue(orchestrator.checks_green(same, {"checkCommand": "make ci"}))

    def test_result_json_fills_empty_acceptance_results(self):
        self._result("R-1", {"builds": "pass"})
        row = reviewing("R-1")
        self.assertTrue(pipeline.acs_pass(row, self.tmp))
        self.assertEqual(row["pr"]["acResults"], {"builds": "pass"})

        other = reviewing("R-2")
        folder = self.tmp / ".warp" / "tickets" / "R-2"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"acs": {"docs": "pass"}}) + "\n")
        data = beam([other])
        pipeline.advance(data, {"R-2": {"headSha": SHA}}, beam_path=self.path, now=NOW)
        self.assertEqual(data["tickets"]["R-2"]["pr"]["acResults"], {"docs": "pass"})

    def test_ci_inside_the_grace_period_waits_and_past_it_starts(self):
        waiting = beam([reviewing("W-9", bugbot="pass", rollup="absent", ci="absent", check="")])
        held = pipeline.advance(
            waiting,
            {"W-9": {"headSha": SHA, "headAgeMinutes": 1, "rollup": "absent", "ci": "absent", "checkCount": 0, "checks": []}},
            now=NOW,
        )
        self.assertIn("review: wait W-9", text(held))
        self.assertNotIn("ci: start", text(held))
        self.assertFalse(waiting["tickets"]["W-9"]["pr"].get("ciAbsent"))

        late = beam(
            [reviewing("L-9", bugbot="pass", rollup="absent", ci="absent", check="")],
            ciStartGraceMinutes=5,
        )
        started = pipeline.advance(
            late,
            {
                "L-9": {
                    "headSha": SHA,
                    "headAgeMinutes": 5,
                    "rollup": "absent",
                    "ci": "absent",
                    "checkCount": 0,
                    "checks": [],
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            },
            now=NOW,
        )
        self.assertIn("ci: start L-9", text(started))
        self.assertNotIn("review: wait", text(started))
        self.assertTrue(late["tickets"]["L-9"]["pr"]["ciAbsent"])

    def test_supervise_fetches_github_when_no_provider_file_is_passed(self):
        import provider

        data = beam(
            [
                reviewing(
                    "A-001",
                    url="https://github.com/acme/app/pull/82",
                    bugbot="",
                    check="",
                    rollup="",
                    ci="",
                )
            ]
        )
        self.path.write_text(json.dumps(data) + "\n")
        calls = []

        def fake(root, pr):
            calls.append(str(pr))
            return {
                "headSha": SHA,
                "rollup": "green",
                "ci": "green",
                "check": "green",
                "checkCount": 12,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "bugbot": "pass",
                "conflict": False,
                "conflictFiles": [],
            }

        original = provider.fetch_pr_state
        provider.fetch_pr_state = fake
        try:
            orchestrator.supervise(self.path, now=NOW)
        finally:
            provider.fetch_pr_state = original
        self.assertEqual(calls, ["https://github.com/acme/app/pull/82"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["tickets"]["A-001"]["pr"]["headSha"], SHA)
        self.assertEqual(saved["tickets"]["A-001"]["pr"]["rollup"], "green")
        self.assertNotIn("_repoRoot", saved)

        calls.clear()
        provider.fetch_pr_state = fake
        try:
            orchestrator.supervise(
                self.path,
                now=NOW,
                provider={"A-001": {"rollup": "pending", "headSha": SHA}},
            )
        finally:
            provider.fetch_pr_state = original
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
