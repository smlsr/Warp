"""Findings from the WarpDemo view run: allowlist, review count, recorded reviews, liveness, gates, notes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import agents  # noqa: E402
import beam as beam_mod  # noqa: E402
import bugbot  # noqa: E402
import checkout  # noqa: E402
import mcp_allow  # noqa: E402
import mcp_tools  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
import sweep  # noqa: E402
import ticket_state  # noqa: E402
from test_bugbot_heal import SHA, Repo, row  # noqa: E402


def utc(minutes_ago: float = 0.0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class AllowlistTest(unittest.TestCase):
    """1. A list written before create_pull_request was added still allows it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hooks = self.tmp / ".cursor" / "hooks"
        self.hooks.mkdir(parents=True)
        (self.hooks / "warp-allow.json").write_text(json.dumps({"pairs": [
            {"server": "*github*", "tool": "add_issue_comment"},
            {"server": "*slack*", "tool": "slack_send_message"},
        ]}))
        self.home = self.tmp / "home"
        self.home.mkdir()

    def test_an_old_list_with_add_issue_comment_allows_the_other_github_tools(self):
        for tool in ("create_pull_request", "merge_pull_request", "add_issue_comment"):
            self.assertEqual(mcp_allow.decide("user-github", tool, self.tmp, self.home), "allow", tool)
        self.assertEqual(mcp_allow.decide("user-github", "delete_repository", self.tmp, self.home), "ask")
        self.assertEqual(mcp_allow.decide("plugin-slack-slack", "create_pull_request", self.tmp, self.home), "ask")

    def test_the_ide_hook_says_the_same(self):
        hook = self.hooks / "warp-mcp-allow.py"
        shutil.copy(ROOT / "hooks" / "warp-mcp-allow.py", hook)

        def ask(server, tool):
            out = subprocess.run([sys.executable, str(hook)], input=json.dumps({"mcp_server_name": server, "tool_name": tool}),
                                 capture_output=True, text=True, check=True)
            return json.loads(out.stdout)["permission"]

        self.assertEqual(ask("user-github", "create_pull_request"), "allow")
        self.assertEqual(ask("user-github", "delete_repository"), "ask")
        self.assertEqual(ask("plugin-slack-slack", "create_pull_request"), "ask")

    def test_the_hook_names_the_same_github_tools(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("warp_mcp_allow_hook", ROOT / "hooks" / "warp-mcp-allow.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(tuple(module.GITHUB_TOOLS), tuple(mcp_tools.GITHUB_TOOLS))


class ReviewCountTest(Repo):
    """2. A review Shuttle is counted once, not on every pass while it is out."""

    def ticket(self, **shuttle):
        ticket = row(status="review", phase="reviewing", agent="shuttle-WV-02-r1", workerStartedAt=utc(3))
        ticket["pr"].update({"bugbotReviewSha": SHA, "bugbotReviews": 1})
        ticket["shuttle"] = dict({"pending": True, "step": "review"}, **shuttle)
        return ticket

    def test_a_review_shuttle_still_out_is_held_and_not_counted_again(self):
        data = self.data([self.ticket(startedAt=utc(3))], bugbotTrigger="shuttle")
        for _ in range(3):
            lines = []
            pipeline.start_review(data, data["tickets"]["WV-01"], lines)
            self.assertEqual(lines, ["shuttle: hold WV-01"])
        self.assertEqual(data["tickets"]["WV-01"]["pr"]["bugbotReviews"], 1)

    def test_a_review_marked_and_never_launched_is_printed_again_at_the_same_count(self):
        data = self.data([self.ticket(launching=True)], bugbotTrigger="shuttle")
        lines = []
        pipeline.start_review(data, data["tickets"]["WV-01"], lines)
        self.assertIn("bugbot: review-shuttle WV-01 1/2", lines)
        self.assertEqual(data["tickets"]["WV-01"]["pr"]["bugbotReviews"], 1)

    def test_a_dead_review_shuttle_is_counted(self):
        ticket = self.ticket(startedAt=utc(40))
        ticket["workerStartedAt"] = utc(40)
        data = self.data([ticket], bugbotTrigger="shuttle")
        lines = []
        pipeline.start_review(data, data["tickets"]["WV-01"], lines)
        self.assertIn("bugbot: review-shuttle WV-01 2/2", lines)


class RecordedReviewTest(unittest.TestCase):
    """3. A Shuttle's review is kept, but Cursor Bugbot's own result decides the merge (1.6.14)."""

    def ticket(self):
        ticket = row(status="review", phase="reviewing")
        bugbot.record_result(ticket, "pass", source="shuttle", sha=SHA)
        ticket["pr"].update({"ciNone": True})
        return ticket

    def test_a_pending_bugbot_check_makes_it_wait_and_keeps_the_shuttle_review(self):
        ticket = self.ticket()
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "pending", "ci": "absent", "bugbot": "pending"})
        self.assertEqual(ticket["pr"]["bugbot"], "pending")
        self.assertEqual((ticket["pr"]["shuttleReview"], ticket["pr"]["shuttleReviewSha"]), ("pass", SHA))

    def test_a_finished_bugbot_fail_still_counts(self):
        ticket = self.ticket()
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "red", "ci": "absent", "bugbot": "fail"})
        self.assertEqual(ticket["pr"]["bugbot"], "fail")

    def test_the_merge_waits_for_the_bugbot_check(self):
        ticket = self.ticket()
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "pending", "ci": "absent", "bugbot": "pending"})
        self.assertFalse(orchestrator.checks_green(ticket, {"bugbotRequired": True}))
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "green", "ci": "absent", "bugbot": "pass"})
        self.assertTrue(orchestrator.checks_green(ticket, {"bugbotRequired": True}))

    def test_other_checks_still_running_hold_it(self):
        ticket = self.ticket()
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "pending", "ci": "pending", "bugbot": "pending"})
        self.assertFalse(orchestrator.checks_green(ticket, {"bugbotRequired": True}))


class LivenessTest(Repo):
    """4. A Shuttle working in its worktree is not dead, and a new step never reuses a finished id."""

    def test_worktree_activity_is_a_heartbeat(self):
        tree = self.tmp / ".warp" / "worktrees" / "WV-02"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "theme.css").write_text("body{}")
        ticket = row("WV-02", status="coding", phase="implementing", pr={}, worktree=".warp/worktrees/WV-02",
                     checkout="worktree", workerStartedAt=utc(16), shuttle={"pending": True, "step": "implement", "startedAt": utc(16)})
        data = self.data([ticket], staleMinutes=15)
        self.assertTrue(agents.confirmed_dead(ticket, data))
        self.assertTrue(ticket_state._apply_activity(self.tmp, ticket, utc(0)))
        self.assertFalse(agents.confirmed_dead(ticket, data))

    def test_changes_from_before_the_shuttle_started_do_not_count(self):
        tree = self.tmp / ".warp" / "worktrees" / "WV-02"
        tree.mkdir(parents=True)
        path = tree / "old.txt"
        path.write_text("x")
        old = time.time() - 30 * 60
        os.utime(path, (old, old))
        os.utime(tree, (old, old))
        ticket = row("WV-02", status="coding", worktree=".warp/worktrees/WV-02", checkout="worktree",
                     workerStartedAt=utc(16), shuttle={"pending": True, "step": "implement", "startedAt": utc(16)})
        self.assertFalse(ticket_state._apply_activity(self.tmp, ticket, utc(0)))

    def test_a_launch_starts_its_own_clock(self):
        ticket = row("WV-02", status="review", workerStartedAt=utc(40), lastSeenAt=utc(30),
                     shuttle={"pending": True, "step": "review", "launching": True})
        data = self.data([ticket], staleMinutes=15)
        checkout._note_live(data, ticket)
        self.assertNotIn("lastSeenAt", ticket)
        self.assertFalse(agents.confirmed_dead(ticket, data))

    def test_a_launch_by_hand_does_not_reuse_a_finished_id(self):
        ticket = row("WV-02", status="review", agent="shuttle-WV-02-r1")
        data = self.data([ticket])
        (self.warp / "agents.json").write_text(json.dumps({"agents": [
            {"id": "shuttle-WV-02-r1", "ticket": "WV-02", "role": "shuttle", "state": "ended", "endReason": "returned"}]}))
        agents.note_checkout(data, ticket, beam_path=self.beam_path)
        self.assertNotEqual(ticket["agent"], "shuttle-WV-02-r1")
        ids = [r["id"] for r in json.loads((self.warp / "agents.json").read_text())["agents"]]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_any_line_from_the_shuttle_is_a_heartbeat(self):
        ticket = row("WV-02", status="coding", phase="implementing", pr={}, workerStartedAt=utc(20))
        data = self.data([ticket], staleMinutes=15)
        ticket_state._apply_event_state(self.beam_path, data, ticket, {"at": utc(1), "state": "coding"}, utc(0), set(), [], "coding")
        self.assertEqual(ticket["lastSeenAt"], utc(1))


class GateTest(unittest.TestCase):
    """5. A pending gate is not stuck while the run is moving."""

    def data(self, statuses, at=None):
        tickets = {}
        for tid, status in statuses.items():
            tickets[tid] = {"id": tid, "status": status, "watch": {}}
        data = {"tickets": tickets, "gates": [
            {"key": "G1", "status": "pending", "members": ["WV-01", "WV-02"], "blocking": True},
            {"key": "G2", "status": "pending", "members": ["WV-03"], "blocking": True},
        ], "config": {}}
        if at:
            data["repairSweep"] = {"at": at, "stuckGates": ["G1", "G2"]}
        return data

    def run_gates(self, data, now):
        findings, actions, escalated, lines = [], [], [], []
        sweep._gates(data, now, findings, actions, escalated, lines)
        return findings, escalated, lines

    def test_gates_are_not_stuck_while_a_member_is_worked(self):
        data = self.data({"WV-01": "coding", "WV-02": "merged", "WV-03": "queued"}, at="2026-10-13T23:00:00Z")
        findings, escalated, lines = self.run_gates(data, "2026-10-13T23:15:00Z")
        self.assertEqual((findings, escalated, lines), ([], [], []))

    def test_gates_are_stuck_when_nothing_moves(self):
        data = self.data({"WV-01": "queued", "WV-02": "merged", "WV-03": "queued"}, at="2026-10-13T23:00:00Z")
        findings, escalated, lines = self.run_gates(data, "2026-10-13T23:15:00Z")
        self.assertEqual(sorted(f["id"] for f in findings), ["G1", "G2"])
        self.assertIn("sweep: escalate G1 gate stuck", lines)

    def test_a_member_in_alarm_makes_its_gate_stuck(self):
        data = self.data({"WV-01": "alarm", "WV-02": "coding", "WV-03": "queued"})
        findings, _escalated, lines = self.run_gates(data, "2026-10-13T23:15:00Z")
        self.assertEqual([f["id"] for f in findings], ["G1"])
        self.assertIn("sweep: finding G1 gate stuck", lines)


class NoteTest(unittest.TestCase):
    """7. The note says a pushed branch was pushed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.beam_path = self.tmp / ".warp" / "beam.json"
        self.beam_path.write_text(json.dumps({"tickets": {"WV-02": {"id": "WV-02", "branch": "warp/view/WV-02-WAR-48"}}}))

    def runner(self, stdout):
        return lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def test_a_pushed_branch_is_not_called_local_only(self):
        text = provider.note(self.tmp, self.beam_path, "WV-02", "create_pull_request needs approval (mcp_allow: ask)",
                             runner=self.runner("abc\trefs/heads/warp/view/WV-02-WAR-48\n"))
        self.assertIn("pushed warp/view/WV-02-WAR-48 but could not open its pull request", text)
        self.assertNotIn("Nothing was pushed", text)
        post = json.loads((self.tmp / ".warp" / "notify-post.json").read_text())
        self.assertEqual(post["kind"], "pr-not-opened")
        self.assertNotIn("Nothing was pushed", json.dumps(post))
        journal = [json.loads(line) for line in (self.tmp / ".warp" / "journal.jsonl").read_text().splitlines()]
        self.assertEqual((journal[-1]["type"], journal[-1]["pushed"]), ("pr-not-opened", True))

    def test_a_branch_not_on_origin_is_local_only(self):
        text = provider.note(self.tmp, self.beam_path, "WV-02", "no GitHub connector", runner=self.runner(""))
        self.assertIn("Nothing was pushed", text)


if __name__ == "__main__":
    unittest.main()
