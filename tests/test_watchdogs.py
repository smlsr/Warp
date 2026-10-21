"""1.7.9: the six gaps that left a run looking busy while nothing moved.

1. A red main was never seen (nothing set baseGreen), so every pull request was "red, back to fix".
2. Acceptance results were dropped on a push and the gate shut without a word.
3. A ticket flagged for a fix with no Shuttle started raised nothing.
4. A Shuttle that kept heartbeating and never committed was not named.
5. A pull request that could not be opened (a refused token) printed a line each pass and nothing else.
6. A dead listener was only noticed when supervise ran.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import acceptance  # noqa: E402
import basecheck  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import progress  # noqa: E402

NOW = "2026-10-20T12:00:00Z"


def plus(stamp, minutes):
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(tid, **extra):
    row = {"id": tid, "status": "queued", "locks": ["src/%s" % tid.lower()], "size": "S", "module": "app",
           "autoMerge": True, "jiraKey": "WAR-1", "branch": "warp/%s" % tid, "pr": {}, "summary": tid}
    row.update(extra)
    return row


def beam(tickets, **config):
    cfg = {"maxAgents": 4, "bugbotRequired": False, "maxFixAttempts": 3, "maxRecoveries": 3,
           "autoMergeSizes": ["S", "M"], "runner": "cloud", "launch": "worktree", "subagentVm": False}
    cfg.update(config)
    return {"version": 1, "runState": "running", "baseGreen": True, "gates": [], "config": cfg,
            "tickets": {row["id"]: row for row in tickets}}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=w@example.com", "-c", "user.name=W",
                           "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], capture_output=True, text=True)


class RedMainTest(unittest.TestCase):
    def test_a_red_main_is_an_alarm_once_and_green_again_says_so(self):
        data = beam([])
        lines = basecheck.apply(data, {"sha": "a" * 40, "state": "red", "failed": ["unit", "lint"]}, NOW)
        self.assertIs(data["baseGreen"], False)
        self.assertEqual(lines[0], "base: main is red at aaaaaaaaaaaa (unit, lint)")
        self.assertTrue(any(line.startswith("herald: ALARM main is red at aaaaaaaaaaaa: unit, lint failing") for line in lines))
        again = basecheck.apply(data, {"sha": "a" * 40, "state": "red", "failed": ["unit", "lint"]}, plus(NOW, 5))
        self.assertFalse(any(line.startswith("herald:") for line in again))  # one alarm per red commit
        self.assertTrue(orchestrator.needs_base_fix(data))
        data["watch"] = {"baseFixes": 2}
        green = basecheck.apply(data, {"sha": "b" * 40, "state": "green", "failed": []}, plus(NOW, 10))
        self.assertIs(data["baseGreen"], True)
        self.assertIn("base: main is green again at bbbbbbbbbbbb (red since 12:00:00Z)", green)
        self.assertNotIn("baseFixes", data["watch"])

    def test_the_github_read_combines_check_runs_and_statuses_without_bugbot(self):
        import provider

        def runner(cmd):
            url = cmd[2]
            if url.endswith("/commits/main"):
                out = "f" * 40 + "\n"
            elif "check-runs" in url:
                out = json.dumps({"check_runs": [{"name": "unit", "status": "completed", "conclusion": "failure"},
                                                 {"name": "Cursor Bugbot", "status": "completed", "conclusion": "neutral"},
                                                 {"name": "lint", "status": "completed", "conclusion": "success"}]})
            else:
                out = json.dumps({"statuses": [{"context": "deploy", "state": "success"}]})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        got = provider.base_checks(Path("."), "main", runner=runner)
        self.assertEqual(got, {"sha": "f" * 40, "state": "red", "failed": ["unit"], "count": 3})

    def test_a_release_after_merge_does_not_make_main_red(self):
        import provider

        def runner(cmd):
            url = cmd[2]
            if url.endswith("/commits/main"):
                out = "f" * 40 + "\n"
            elif "actions/runs" in url:
                out = json.dumps({"workflow_runs": [{"event": "push", "check_suite_id": 1},
                                                    {"event": "workflow_run", "check_suite_id": 2}]})
            elif "check-runs" in url:
                out = json.dumps({"check_runs": [
                    {"name": "go-test", "status": "completed", "conclusion": "success", "check_suite": {"id": 1}},
                    {"name": "release", "status": "completed", "conclusion": "failure", "check_suite": {"id": 2}}]})
            else:
                out = json.dumps({"statuses": []})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        got = provider.base_checks(Path("."), "main", runner=runner)
        self.assertEqual(got, {"sha": "f" * 40, "state": "green", "failed": [], "count": 1})

    def test_pending_keeps_what_it_was(self):
        data = beam([])
        basecheck.apply(data, {"sha": "a" * 40, "state": "red", "failed": ["unit"]}, NOW)
        basecheck.apply(data, {"sha": "c" * 40, "state": "pending", "failed": []}, plus(NOW, 5))
        self.assertIs(data["baseGreen"], False)

    def test_a_pull_request_failing_only_mains_checks_waits_for_main_then_takes_it(self):
        row = ticket("R-1", status="review", phase="reviewing",
                     pr={"url": "https://example.test/pull/3", "headSha": "h1", "ci": "red", "ciSha": "h1",
                         "rollup": "red", "rollupSha": "h1", "failedChecks": ["unit"], "failedChecksSha": "h1",
                         "acResults": {"AC-1": "pass"}, "acSha": "h1"})
        data = beam([row])
        basecheck.apply(data, {"sha": "a" * 40, "state": "red", "failed": ["unit", "lint"]}, NOW)
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertTrue(any(line.startswith("ci: R-1 fails the same checks as main (unit); main is red") for line in lines), lines)
        self.assertNotEqual(row.get("phase"), "fixing")
        self.assertFalse(row.get("attempts"))  # no fix attempt spent
        basecheck.apply(data, {"sha": "b" * 40, "state": "green", "failed": []}, plus(NOW, 10))
        lines = pipeline.advance(data, {}, now=plus(NOW, 10))
        self.assertTrue(any("waited for the base" in line for line in lines), lines)
        self.assertTrue(any("fixer: R-1 rebase" in line for line in lines), lines)

    def test_a_pull_request_with_its_own_failure_still_goes_to_fix(self):
        row = ticket("R-2", status="review", phase="reviewing",
                     pr={"url": "https://example.test/pull/4", "headSha": "h2", "ci": "red", "ciSha": "h2",
                         "rollup": "red", "rollupSha": "h2", "failedChecks": ["unit", "e2e"], "failedChecksSha": "h2"})
        data = beam([row])
        basecheck.apply(data, {"sha": "a" * 40, "state": "red", "failed": ["unit"]}, NOW)
        lines = pipeline.advance(data, {}, now=NOW)
        self.assertFalse(any("fails the same checks as main" in line for line in lines), lines)
        self.assertEqual(row["phase"], "fixing")


class AcceptanceCarryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("1\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "warp/T-1")
        (self.repo / "b.txt").write_text("ticket\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "ticket")
        self.checked = git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def head(self):
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_a_merge_of_main_carries_the_results(self):
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "a.txt").write_text("2\n")
        git(self.repo, "commit", "-q", "-am", "main moved")
        git(self.repo, "checkout", "-q", "warp/T-1")
        git(self.repo, "merge", "-q", "--no-edit", "main")
        self.assertEqual(acceptance.carry_check(self.repo, self.checked, self.head(), "main"), (True, "only main was merged in"))

    def test_new_code_is_not_carried(self):
        (self.repo / "b.txt").write_text("changed\n")
        git(self.repo, "commit", "-q", "-am", "more")
        ok, why = acceptance.carry_check(self.repo, self.checked, self.head(), "main")
        self.assertFalse(ok)
        self.assertEqual(why, "1 new commit changed code")

    def test_only_warp_state_is_carried(self):
        (self.repo / ".warp").mkdir()
        (self.repo / ".warp" / "x.json").write_text("{}\n")
        git(self.repo, "add", ".warp/x.json")
        git(self.repo, "commit", "-q", "-m", "state")
        self.assertEqual(acceptance.carry_check(self.repo, self.checked, self.head(), "main"), (True, "only Warp state changed"))

    def test_the_pass_carries_or_asks_instead_of_dropping_them(self):
        row = ticket("T-1", status="review", phase="reviewing",
                     acceptanceCriteria=[{"id": "AC-1", "text": "works"}],
                     pr={"url": "https://example.test/pull/1", "headSha": "old", "acResults": {"AC-1": "pass"}, "acSha": "old"})
        pipeline._drop_stale_results(row["pr"], "new")
        row["pr"]["headSha"] = "new"
        self.assertEqual(row["pr"]["acCarried"]["results"], {"AC-1": "pass"})
        data = beam([row])
        lines = []
        pipeline._carry_acs(data, row, lines, check=lambda *a: (True, "only main was merged in"))
        self.assertEqual(row["pr"]["acResults"], {"AC-1": "pass"})
        self.assertEqual(row["pr"]["acSha"], "new")
        self.assertIn("acceptance: T-1 results from old carried to new (only main was merged in)", lines)
        # Code changed: not carried, and said once with the command.
        pipeline._drop_stale_results(row["pr"], "newer")
        row["pr"]["headSha"] = "newer"
        lines = []
        pipeline._carry_acs(data, row, lines, check=lambda *a: (False, "2 new commits changed code"))
        self.assertEqual(row["pr"]["acResults"], {})
        self.assertTrue(lines[0].startswith("acceptance: T-1 head moved from new to newer after its results were recorded (all pass); "
                                            "they are not carried (2 new commits changed code). Record them on newer:"), lines)
        self.assertIn("--sha newer --ac AC-1=pass|fail", lines[0])
        self.assertTrue(lines[1].startswith("herald: T-1 acceptance results from new need recording again on newer"))
        again = []
        pipeline._carry_acs(data, row, again, check=lambda *a: (False, "x"))
        self.assertEqual(again, [])

    def test_results_record_the_commit_the_shuttle_checked(self):
        import ticket_state

        root = self.tmp / "parent"
        (root / ".warp").mkdir(parents=True)
        ticket_state.append_status(root, "T-1", "done", agent="a1", sha="abc123", acs={"AC-1": "pass"})
        log = [json.loads(line) for line in (root / ".warp" / "tickets" / "T-1" / "log.jsonl").read_text().splitlines()]
        self.assertEqual(log[-1]["acSha"], "abc123")
        row = ticket("T-1", pr={"headSha": "older"})
        ticket_state._apply_acs(row, log[-1], [], set())
        self.assertEqual(row["pr"]["acSha"], "abc123")


class UnlaunchedTest(unittest.TestCase):
    def test_a_fix_still_unlaunched_one_pass_later_is_an_alarm_once(self):
        row = ticket("F-1", status="fix", phase="fixing", pr={"url": "https://example.test/pull/5", "headSha": "h"})
        data = beam([row])
        pipeline.mark_shuttle(row, "fix", data)
        row["shuttle"]["launchLines"] = ["start F-1 step=fix"]
        lines = pipeline.advance(data, {}, now=plus(NOW, 6))
        self.assertTrue(any(line.startswith("shuttle: not launched F-1") for line in lines), lines)
        self.assertTrue(any(line.startswith("alarm: F-1 fix step was flagged") for line in lines), lines)
        self.assertTrue(any(line.startswith("herald: ALARM F-1 was flagged for a fix") for line in lines), lines)
        again = pipeline.advance(data, {}, now=plus(NOW, 9))
        self.assertTrue(any(line.startswith("shuttle: not launched F-1") for line in again))
        self.assertFalse(any(line.startswith("herald: ALARM F-1") for line in again), again)

    def test_a_fix_waiting_for_a_slot_is_said_and_alarmed_on_the_second_pass(self):
        row = ticket("F-2", status="review", phase="reviewing")
        data = beam([row])
        lines = []
        pipeline._fix_waits(data, row, lines)
        self.assertEqual(len(lines), 1)
        pipeline._fix_waits(data, row, lines)
        self.assertTrue(lines[-1].startswith("herald: ALARM F-2 needs a fix and no Shuttle has started"))


class StalledTest(unittest.TestCase):
    def working(self, tid="S-1", step="implement", started=NOW):
        return ticket(tid, status="coding", phase="implementing", lastSeenAt=plus(started, 1), workerStartedAt=started,
                      shuttle={"pending": True, "step": step, "startedAt": started})

    def test_no_new_commit_for_thirty_minutes_is_flagged_once_and_a_commit_clears_it(self):
        row = self.working()
        data = beam([row], staleMinutes=120)
        tip = {"warp/S-1": "c1"}
        self.assertEqual(progress.apply(data, lambda b: tip.get(b, ""), plus(NOW, 10), 30), [])
        lines = progress.apply(data, lambda b: tip.get(b, ""), plus(NOW, 31), 30)
        self.assertTrue(lines[0].startswith("stall: S-1 implement Shuttle has no new commit for 31m"), lines)
        self.assertTrue(any(line.startswith("herald: ALARM S-1: its implement Shuttle has made no new commit for 31m") for line in lines))
        later = progress.apply(data, lambda b: tip.get(b, ""), plus(NOW, 40), 30)
        self.assertTrue(later[0].startswith("stall: S-1"))
        self.assertFalse(any(line.startswith("herald:") for line in later))
        tip["warp/S-1"] = "c2"
        moved = progress.apply(data, lambda b: tip.get(b, ""), plus(NOW, 41), 30)
        self.assertEqual(moved, ["progress: S-1 committed again (c2); no longer stalled"])

    def test_review_and_verify_steps_are_not_judged(self):
        row = self.working(step="verify")
        data = beam([row], staleMinutes=120)
        self.assertEqual(progress.apply(data, lambda b: "", plus(NOW, 90), 30), [])

    def test_the_prompt_says_to_commit_as_you_go(self):
        self.assertIn(orchestrator.PROGRESS_LINE, orchestrator.step_lines(self.working()))
        self.assertNotIn(orchestrator.PROGRESS_LINE, orchestrator.step_lines(self.working(step="review")))


class PrOpenTest(unittest.TestCase):
    def test_a_refused_token_is_an_alarm_once_per_ticket_and_once_for_the_run(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / ".warp" / "beam.json"
        path.parent.mkdir()
        data = beam([ticket("P-1", status="coding"), ticket("P-2", status="coding")], baseBranch="main")
        opener = lambda *a: ("failed", "HTTP 403: Resource not accessible by integration")
        out = pipeline.run_pr_opens(data, path, ["pr: open P-1 warp/P-1", "pr: open P-2 warp/P-2"], opener=opener)
        self.assertIn("alarm: P-1 pull request not opened (permission)", out)
        self.assertEqual(sum(1 for line in out if line.startswith("herald: ALARM Warp's GitHub token")), 1)
        self.assertEqual(sum(1 for line in out if line.startswith("herald: ALARM P-")), 2)
        self.assertTrue(any(line.startswith("pr: MUST DO P-1") for line in out))
        again = pipeline.run_pr_opens(data, path, ["pr: open P-1 warp/P-1"], opener=opener)
        self.assertFalse(any(line.startswith("herald:") for line in again), again)
        self.assertIn("pr: open failed P-1 (permission, try 2): HTTP 403: Resource not accessible by integration", again)
        done = pipeline.run_pr_opens(data, path, ["pr: open P-1 warp/P-1"], opener=lambda *a: ("opened", "https://x/pull/9"))
        self.assertTrue(any(line.startswith("herald: P-1 pull request opened after 2 failed tries") for line in done), done)
        self.assertNotIn("openFailed", data["tickets"]["P-1"]["pr"])


class ListenerWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("messenger: slack\nslackChannel: warp-run\n")
        self.path = warp / "beam.json"
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "gates": [],
                                         "tickets": {"T-1": {"id": "T-1", "status": "review"}},
                                         "config": {"listenerSlowSeconds": 120}}) + "\n")
        self.assertEqual(orchestrator.supervise_listener(self.path, now=NOW), ["listener: poll"])

    def test_a_silent_listener_is_handed_back_between_passes_and_the_gap_is_named(self):
        self.assertIsNone(inbound.watch_listener(self.path, now=plus(NOW, 2)))
        line = inbound.watch_listener(self.path, now=plus(NOW, 6))
        self.assertTrue(line.startswith("listener: generation 1 silent for 6m; handed back (lost)"), line)
        raw = json.loads(self.path.read_text())["listener"]
        self.assertEqual((raw["holder"], raw["returnReason"], raw["downSince"]), ("", "lost", plus(NOW, 6)))
        got = orchestrator.supervise_listener(self.path, now=plus(NOW, 18))
        self.assertIn("listener: none was up for 12m (since 12:06:00Z, the last one ended lost); starting one now", got)
        self.assertTrue(any(line.startswith("herald: ALARM no Warp listener for 12m") for line in got))
        self.assertEqual(got[-1], "listener: poll")

    def test_a_shuttle_state_write_runs_the_watch(self):
        import ticket_state

        data = json.loads(self.path.read_text())
        data["listener"]["lastSeenAt"] = data["listener"]["generationStartedAt"] = data["listener"]["pollStartedAt"] = \
            data["listener"]["startedAt"] = "2026-01-01T00:00:00Z"
        self.path.write_text(json.dumps(data) + "\n")
        ticket_state.append_status(self.tmp, "T-1", "heartbeat", agent="a1")
        self.assertEqual(json.loads(self.path.read_text())["listener"]["returnReason"], "lost")

    def test_parent_alive_names_a_held_but_silent_listener(self):
        got = orchestrator.parent_alive(self.path, every=600, now=plus(NOW, 9))
        self.assertTrue(any("has not polled for 9m; it is gone" in line for line in got), got)


if __name__ == "__main__":
    unittest.main()
