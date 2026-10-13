"""A pass that starts a ticket asks for the In Progress move; a launch right after another Shuttle starts;
the parent opens the pull request a Shuttle pushed but could not open."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import agents  # noqa: E402
import jira_sync  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
from test_bugbot_heal import SHA, Repo, row  # noqa: E402
from test_real_merge import Live  # noqa: E402


def queued(tid, key):
    ticket = row(tid, status="queued", pr={}, jiraKey=key, jiraMapping="mapped", locks=["app/%s" % tid])
    ticket.pop("phase", None)
    return ticket


class ClaimMoveTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("jiraTransition: true\njiraProject: WAR\njiraKeyPrefixes: WAR\njiraInProgressStatus: In Progress\n")

    def test_a_pass_that_starts_tickets_asks_for_each_move_and_keeps_them_all(self):
        self.write([queued("WV-02", "WAR-48"), queued("WV-03", "WAR-49")], jiraTransition=True, jiraProject="WAR")
        lines = orchestrator.supervise(self.beam_path, now="2026-10-13T23:00:00Z", provider={})
        starts = [line for line in lines if line.startswith("start WV-0")]
        self.assertEqual(len(starts), 2, lines)
        text = "\n".join(lines)
        self.assertIn("WAR-48", text)
        self.assertIn("WAR-49", text)
        self.assertIn("In Progress", text)
        todo = json.loads((self.warp / "jira-todo.json").read_text())
        ids = sorted(str(t.get("id") or t.get("ticket")) for t in todo["tickets"])
        self.assertEqual(ids, ["WV-02", "WV-03"], todo)

    def test_no_second_ask_once_the_move_is_recorded(self):
        ticket = queued("WV-02", "WAR-48")
        ticket["jira"] = {"startedAt": "2026-10-13T22:00:00Z"}
        data = self.write([ticket], jiraTransition=True, jiraProject="WAR")
        self.assertEqual(jira_sync.queue_claim(self.beam_path, data, data["tickets"]["WV-02"]), "")


class LaunchAfterAShuttleTest(Repo):
    def test_a_fix_right_after_the_implement_shuttle_prints_its_start_line(self):
        # The implement Shuttle came back without its own pull request; its heartbeat is still on the ticket.
        ticket = row("WV-03", status="coding", phase="implementing", pr={}, agent="shuttle-WV-03",
                     workerStartedAt="2026-10-13T22:00:00Z", lastSeenAt="2026-10-13T22:30:00Z",
                     shuttle={"pending": False, "step": "implement"})
        data = self.write([ticket])
        lines = []
        pipeline.begin_fix(data, data["tickets"]["WV-03"], ["no pull request"], lines)
        self.assertTrue(any(line.startswith("start WV-03 ") and "step=fix" in line for line in lines), lines)
        self.assertNotIn("shuttle: hold WV-03", lines)

    def test_a_review_shuttle_after_a_shuttle_prints_its_start_line(self):
        ticket = row(status="review", phase="reviewing", agent="shuttle-WV-01-r2",
                     workerStartedAt="2026-10-13T22:00:00Z", lastSeenAt="2026-10-13T22:30:00Z",
                     shuttle={"pending": False, "step": "restart"})
        data = self.write([ticket])
        lines = []
        pipeline.start_review(data, data["tickets"]["WV-01"], lines)
        self.assertTrue(any(line.startswith("start WV-01 ") and "step=review" in line for line in lines), lines)

    def test_a_launched_shuttle_is_busy(self):
        ticket = row(shuttle={"pending": True, "step": "fix", "startedAt": "2026-10-13T22:40:00Z"},
                     workerStartedAt="2026-10-13T22:40:00Z", lastSeenAt="2026-10-13T22:41:00Z")
        self.write([ticket])
        self.assertTrue(agents.shuttle_busy(self.load(), ticket, now="2026-10-13T22:42:00Z", beam_path=self.beam_path))


class OpenPullRequestTest(Repo):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/smlsr/WarpDemo.git"], cwd=self.tmp, check=True)
        ticket = row("WV-03", status="coding", phase="implementing", pr={}, branch="warp/view/WV-03-WAR-49",
                     agent="shuttle-WV-03", acs=["parses"], workerStartedAt="2026-10-13T22:00:00Z",
                     shuttle={"pending": True, "step": "implement", "hadPr": False, "startedAt": "2026-10-13T22:00:00Z"})
        self.write([ticket])
        import ticket_state
        from contextlib import redirect_stdout
        from io import StringIO

        with redirect_stdout(StringIO()):
            ticket_state.append_status(self.tmp, "WV-03", "done", agent="shuttle-WV-03", when="2026-10-13T22:30:00Z")
        data = self.load()
        data["tickets"]["WV-03"]["checkout"] = "worktree"
        data["tickets"]["WV-03"]["worktree"] = ".warp/worktrees/WV-03"
        self.beam_path.write_text(json.dumps(data))

    def run_pass(self, outcome, now="2026-10-13T22:31:00Z"):
        calls = []
        saved = provider.open_pr
        provider.open_pr = lambda root, branch, base, title, body, runner=None: (calls.append((branch, title)), outcome)[1]
        try:
            with Live({}):
                lines = orchestrator.supervise(self.beam_path, now=now)
        finally:
            provider.open_pr = saved
        return lines, calls

    def test_a_pushed_branch_without_a_pull_request_gets_one_from_the_parent(self):
        lines, calls = self.run_pass(("opened", "https://github.com/smlsr/WarpDemo/pull/5"))
        self.assertEqual([c[0] for c in calls], ["warp/view/WV-03-WAR-49"])
        self.assertTrue(calls[0][1].startswith("[WV-03] "))
        self.assertIn("pr: open WV-03 warp/view/WV-03-WAR-49", lines)
        self.assertIn("pr: opened WV-03 https://github.com/smlsr/WarpDemo/pull/5 (the Shuttle pushed but could not open it)", lines)
        self.assertFalse(any(line.startswith("start WV-03 ") for line in lines), lines)
        saved = self.load()["tickets"]["WV-03"]
        self.assertEqual(saved["pr"]["url"], "https://github.com/smlsr/WarpDemo/pull/5")
        self.assertEqual(saved["status"], "review")
        # The next pass asks Bugbot on it.
        lines, _calls = self.run_pass(("found", "unused"), now="2026-10-13T22:35:00Z")
        self.assertIn("bugbot: request WV-03 https://github.com/smlsr/WarpDemo/pull/5", lines)

    def test_a_branch_not_on_origin_goes_back_to_the_shuttle(self):
        lines, _calls = self.run_pass(("missing", "branch warp/view/WV-03-WAR-49 is not on origin"))
        self.assertTrue(any(line.split(" ")[0] in {"start", "resume"} and " WV-03 " in line and "step=fix" in line for line in lines), lines)

    def test_when_gh_cannot_open_it_says_how_and_waits(self):
        lines, _calls = self.run_pass(("failed", "HTTP 403"))
        self.assertTrue(any(line.startswith("pr: MUST DO WV-03: open a pull request from warp/view/WV-03-WAR-49") for line in lines), lines)
        self.assertTrue(any("orchestrator.py opened --beam" in line for line in lines))
        self.assertFalse(any(line.startswith("start WV-03 ") for line in lines), lines)
        # The next pass asks again, and does not relaunch the implement Shuttle.
        lines, calls = self.run_pass(("found", "https://github.com/smlsr/WarpDemo/pull/5"), now="2026-10-13T22:40:00Z")
        self.assertIn("pr: found WV-03 https://github.com/smlsr/WarpDemo/pull/5 (already open)", lines)
        self.assertFalse(any(line.startswith("start WV-03 ") for line in lines), lines)

    def test_open_pr_checks_for_one_first_then_the_branch(self):
        seen = []

        def runner(cmd):
            seen.append(cmd[:3])
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if cmd[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="abc\trefs/heads/x\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/o/r/pull/9\n", stderr="")

        self.assertEqual(provider.open_pr(self.tmp, "x", "main", "t", "b", runner=runner), ("opened", "https://github.com/o/r/pull/9"))
        self.assertEqual(seen, [["gh", "pr", "list"], ["git", "ls-remote", "--heads"], ["gh", "pr", "create"]])


if __name__ == "__main__":
    import unittest

    unittest.main()


class RecordOpenedTest(Repo):
    def test_recording_a_pr_clears_a_fix_step_that_was_never_launched(self):
        ticket = row("WV-04", status="fix", phase="fixing", pr={}, branch="warp/view/WV-04-WAR-50",
                     shuttle={"pending": True, "step": "fix", "launching": True})
        self.write([ticket])
        out = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "orchestrator.py"), "opened", "--beam",
                              str(self.beam_path), "--id", "WV-04", "--pr", "https://github.com/smlsr/WarpDemo/pull/6"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("WV-04: the fix step that was never launched is cleared", out.stdout)
        saved = self.load()["tickets"]["WV-04"]
        self.assertEqual((saved["status"], saved["phase"], saved["shuttle"]["pending"]), ("review", "pr-open", False))

    def test_a_launched_fix_shuttle_is_left_running(self):
        ticket = row("WV-04", status="fix", phase="fixing", pr={},
                     shuttle={"pending": True, "step": "fix", "startedAt": "2026-10-13T23:00:00Z"})
        self.write([ticket])
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "orchestrator.py"), "opened", "--beam",
                        str(self.beam_path), "--id", "WV-04", "--pr", "https://github.com/smlsr/WarpDemo/pull/6"],
                       capture_output=True, text=True, check=True)
        self.assertTrue(self.load()["tickets"]["WV-04"]["shuttle"]["pending"])
