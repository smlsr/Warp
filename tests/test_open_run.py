"""1.7.15 part 1: a run with live work never reads as finished.

Seen on Forge-QA run core (8 Nov 2026): 1202 merged, 163 parked, 147 skipped, 7 done. Parked and skipped counted as
settled, so spawns closed, halt_state said done, parent_may_exit was true, and the listener started and exited in a
loop while about 160 parked pull requests were being driven by coordinators outside Warp.
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

import agents  # noqa: E402
import beam  # noqa: E402
import ghrest  # noqa: E402
import inbound  # noqa: E402
import jira_autokey  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import provider  # noqa: E402
import terminal  # noqa: E402
import unpark  # noqa: E402
from test_flow_starts import LiveRepo, done_ticket, queued  # noqa: E402

NOW = "2026-11-08T13:30:00Z"
URL = "https://github.com/o/r/pull/%d"


def parked(tid, n, **extra):
    row = {"id": tid, "status": "parked", "phase": "parked", "locks": ["src/%s" % tid.lower()], "size": "S",
           "module": "UI", "summary": tid, "deps": [], "jiraKey": "HOS-%d" % n, "rank": n,
           "pr": {"url": URL % n, "headSha": "p%d" % n}}
    row.update(extra)
    return row


class TerminalTest(unittest.TestCase):
    def test_terminal_and_inactive(self):
        self.assertTrue(beam.is_terminal({"status": "merged"}))
        self.assertTrue(beam.is_terminal({"status": "done"}))
        self.assertTrue(beam.is_terminal({"status": "skipped", "skipReason": "Won't Do"}))
        self.assertTrue(beam.is_terminal({"status": "skipped", "reason": "duplicate of UI-3"}))
        self.assertFalse(beam.is_terminal({"status": "skipped", "skipReason": "later"}))
        self.assertFalse(beam.is_terminal({"status": "parked"}))
        self.assertFalse(beam.is_terminal({"status": "external"}))
        self.assertTrue(beam.is_inactive({"status": "external"}))
        self.assertIn("external", beam.STATUSES)

    def test_all_parked_or_external_keeps_the_run_open_with_nothing_dispatchable(self):
        data = {"runState": "running", "tickets": {
            "A": parked("A", 1), "B": parked("B", 2, status="external", phase="external"),
            "C": {"id": "C", "status": "merged"}}}
        self.assertTrue(terminal.run_open(data))
        self.assertEqual(terminal.dispatchable(data), [])
        self.assertFalse(orchestrator.parent_may_exit(data))
        self.assertTrue(orchestrator.listener_should_run(data))
        self.assertEqual(agents.halt_state(data), "")
        self.assertEqual(terminal.open_breakdown(data), {"parked": 1, "external": 1})

    def test_all_merged_or_done_is_done(self):
        data = {"runState": "running", "tickets": {"A": {"id": "A", "status": "merged"},
                                                     "B": {"id": "B", "status": "done"}}}
        self.assertFalse(terminal.run_open(data))
        self.assertEqual(agents.halt_state(data), "done")
        self.assertTrue(orchestrator.parent_may_exit(data))


class ListenerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text("messenger: slack\nslackChannel: '#warp'\n")
        self.path = self.tmp / ".warp" / "beam.json"

    def write(self, tickets):
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "instance": {"id": "abc"},
                                         "tickets": tickets}) + "\n")

    def test_parked_run_polls_and_the_listener_is_not_reaped(self):
        from unittest import mock

        self.write({"A": parked("A", 1), "B": parked("B", 2)})
        with mock.patch.object(inbound, "channels", lambda path: [("slack", "#warp")]):
            lines = orchestrator.supervise_listener(self.path, now=NOW)
        self.assertIn("listener: poll", lines)
        reap = agents.reap_from(self.tmp, inbound.LISTENER_ID, "", "listener", beam_path=self.path)
        self.assertFalse(reap and reap[0].startswith("reap: exit"), reap)

    def test_a_closed_run_says_idle_done_once_and_no_churn(self):
        self.write({"A": {"id": "A", "status": "merged"}, "B": {"id": "B", "status": "done"}})
        first = orchestrator.supervise_listener(self.path, now=NOW)
        second = orchestrator.supervise_listener(self.path, now=NOW)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].startswith("listener: idle reason=done"), first)
        self.assertEqual(second, ["listener: idle"])
        self.assertNotIn("listener: poll", first + second)
        self.assertFalse(any("churn" in line for line in first + second))
        data = beam.load_json(self.path)
        raw = data.setdefault("listener", {})
        raw["history"] = [{"g": n, "reason": "done", "lived": 30} for n in range(6)]
        self.assertIsNone(inbound.churn(data))


class ReconsiderTest(unittest.TestCase):
    def data(self, *tickets):
        return {"runState": "running", "config": {}, "tickets": {t["id"]: t for t in tickets}}

    def test_merged_closed_external_and_blocker(self):
        lines = []
        data = self.data(parked("A", 1), parked("B", 2), parked("C", 3, parkReason="owner outside Warp (e49-restart coordinator)"),
                         parked("D", 4, parkReason="waits on E"), {"id": "E", "status": "merged"},
                         parked("F", 6, parkReason="waits on G"), {"id": "G", "status": "coding"})
        t = data["tickets"]
        self.assertEqual(unpark.reconsider(data, t["A"], {"prState": "MERGED"}, lines, NOW), "merged")
        self.assertEqual(unpark.reconsider(data, t["B"], {"prState": "CLOSED"}, lines, NOW), "queued")
        self.assertEqual(t["B"]["status"], "queued")
        self.assertEqual(unpark.reconsider(data, t["C"], {"prState": "OPEN"}, lines, NOW), "external")
        self.assertEqual(t["C"]["externalOwner"], "e49-restart coordinator")
        self.assertEqual(unpark.reconsider(data, t["D"], {"prState": "OPEN"}, lines, NOW), "queued")
        self.assertEqual(unpark.reconsider(data, t["F"], {"prState": "OPEN"}, lines, NOW), "")
        self.assertEqual(t["F"]["status"], "parked")
        # External is polled: its merge records it.
        self.assertEqual(unpark.reconsider(data, t["C"], {"prState": "MERGED"}, lines, NOW), "merged")

    def test_closed_as_wont_do_is_skipped(self):
        data = self.data(parked("A", 1, skipReason="won't do: superseded"))
        lines = []
        self.assertEqual(unpark.reconsider(data, data["tickets"]["A"], {"prState": "CLOSED"}, lines, NOW), "skipped")

    def test_unpark_command(self):
        data = self.data(parked("A", 1), parked("B", 2, parkReason="owner outside Warp"), parked("C", 3, parkReason="waits on UI-99"))
        lines = unpark.unpark(data, "all-runnable", NOW)
        self.assertEqual(data["tickets"]["A"]["status"], "queued")
        self.assertEqual(data["tickets"]["B"]["status"], "parked")
        self.assertEqual(data["tickets"]["C"]["status"], "parked")
        self.assertIn("reopened 1 (A)", lines[0])
        self.assertEqual(unpark.unpark(data, "B", NOW)[0].split()[1], "B")
        self.assertEqual(data["tickets"]["B"]["status"], "queued")
        parsed = inbound.parse("warp: unpark all-runnable")
        self.assertEqual((parsed["verb"], parsed["token"]), ("unpark", "all-runnable"))


class ReconsiderPassTest(LiveRepo):
    def test_parked_merged_becomes_merged_and_blocker_merge_prints_start(self):
        self.write(done_ticket("UI-1"), parked("UI-2", 2, parkReason="waits on UI-1"),
                   parked("UI-3", 3))
        self.patch(pipeline, "live_provider", lambda data, path: {"UI-3": {"prState": "MERGED", "mergeSha": "m" * 40}})
        first = orchestrator.supervise(self.path, now=NOW)
        self.assertIn("merge: UI-1 merged", first)
        self.assertEqual(self.beam()["tickets"]["UI-3"]["status"], "merged")
        # UI-1 merged in that pass; the next pass reopens UI-2 and prints its start line.
        lines = orchestrator.supervise(self.path, now=NOW)
        text = "\n".join(first + lines)
        self.assertIn("unpark: UI-2 waited on UI-1, now merged", text)
        self.assertTrue(any(line.startswith("start UI-2 ") for line in first + lines), text)


class JiraKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text("jiraProject: HOS\njiraKeyPrefixes: HOS\n")
        self.path = self.tmp / ".warp" / "beam.json"
        merged = lambda tid: {"id": tid, "status": "merged", "summary": "Ship %s" % tid, "jiraKey": None,
                              "jiraKeySource": "manual", "pr": {"url": URL % 1, "mergeAnnounced": True}}
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "config": {},
                                         "tickets": {"UI-1": merged("UI-1"), "UI-2": merged("UI-2")}}) + "\n")

    def test_label_match_maps_and_moves_to_done_in_one_pass(self):
        data = beam.load_json(self.path)
        lines = pipeline.jira_done_lines(data, self.path, [])
        self.assertTrue(any("labels in (\"warp:UI-1\", \"warp:UI-2\")" in line for line in lines), lines)
        self.assertFalse(any("ALARM" in line for line in lines))
        out = jira_autokey.apply(self.path, [{"key": "HOS-7", "fields": {"summary": "Ship UI-1", "labels": ["warp:UI-1"]}}])
        text = "\n".join(out)
        self.assertEqual(beam.load_json(self.path)["tickets"]["UI-1"]["jiraKey"], "HOS-7")
        self.assertIn("HOS-7", text)
        self.assertIn("Done", text)
        self.assertIn("UI-2 no-issue", text)
        # No match: one note, then nothing more, ever.
        data = beam.load_json(self.path)
        for _ in range(5):
            again = pipeline.jira_done_lines(data, self.path, [])
            self.assertFalse(any("UI-2" in line for line in again), again)

    def test_resolve_treats_a_manual_none_as_unset(self):
        import jira_sync

        text = jira_sync.record_resolved(self.path, "UI-1", "HOS-9", source="label")
        self.assertNotIn("manual key", text)
        self.assertEqual(beam.load_json(self.path)["tickets"]["UI-1"]["jiraKey"], "HOS-9")

    def test_transition_arguments_are_a_string_id(self):
        import jira_sync

        self.assertEqual(jira_sync.transition_arguments({"id": 61}, "HOS-1", "c1"),
                         {"cloudId": "c1", "issueIdOrKey": "HOS-1", "transitionId": "61"})
        self.assertIn('"transitionId"', jira_sync.TRANSITION_ARGS)


class RestTest(unittest.TestCase):
    """A mocked GraphQL 403: the provider read and the merge succeed through REST."""

    def runner(self, calls):
        pull = {"number": 4, "html_url": URL % 4, "state": "open", "merged": False, "mergeable": True,
                "mergeable_state": "clean", "head": {"sha": "a" * 40, "ref": "warp/x"}, "base": {"ref": "main"},
                "updated_at": "2026-11-08T13:00:00Z", "created_at": "2026-11-08T12:00:00Z"}

        def run(cmd, root=None):
            calls.append(cmd)
            if cmd[:2] == ["gh", "pr"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="GraphQL: API rate limit exceeded for user (HTTP 403)")
            path = cmd[4]
            if cmd[3] == "PUT" and path.endswith("/merge"):
                fields = dict(cmd[i + 1].split("=", 1) for i, part in enumerate(cmd) if part == "-f")
                if fields.get("sha") != "a" * 40:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 409: Head branch was modified")
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"merged": True, "sha": "m" * 40}), stderr="")
            if "check-runs" in path:
                body = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}
            elif path.endswith("/status"):
                body = {"statuses": []}
            elif "/pulls?" in path:
                body = [pull]
            else:
                body = pull
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

        return run

    def test_read_and_merge_through_rest(self):
        calls = []
        run = self.runner(calls)
        old = provider._run
        provider._run = run
        old_which = provider.shutil.which
        provider.shutil.which = lambda name: "/usr/bin/gh"
        try:
            snap = provider.fetch_pr_state(Path("."), URL % 4)
            self.assertEqual((snap["headSha"], snap["ci"], snap["prState"]), ("a" * 40, "green", "OPEN"))
            self.assertEqual(ghrest.merge(Path("."), URL % 4, sha="a" * 40, runner=run), ("merged", "m" * 40))
            self.assertEqual(ghrest.merge(Path("."), URL % 4, sha="b" * 40, runner=run)[0], "rejected")
            self.assertTrue(ghrest.rate_limited("GraphQL: API rate limit exceeded"))
            # GraphQL first (opt-in) falls back to REST on the rate limit.
            import os

            os.environ["WARP_GITHUB_GRAPHQL_FIRST"] = "1"
            try:
                snap = provider.fetch_pr_state(Path("."), URL % 4)
            finally:
                os.environ.pop("WARP_GITHUB_GRAPHQL_FIRST", None)
            self.assertEqual(snap["headSha"], "a" * 40)
        finally:
            provider._run = old
            provider.shutil.which = old_which

    def test_the_read_is_incremental(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "beam.json"
        calls, reads = [], []
        run = self.runner(calls)

        def fetch(url):
            reads.append(url)
            return {"headSha": "a", "ci": "green", "bugbot": "pass", "mergeable": "MERGEABLE", "prState": "OPEN"}

        urls = [URL % n for n in range(1, 151)]
        snaps, stats = ghrest.incremental(path, Path("."), urls, fetch, runner=run)
        self.assertEqual((len(snaps), stats["read"]), (150, 150))
        reads.clear()
        # Second pass: only #4 changed (its updated_at is newer than the cursor? no: same) -> nothing is read again.
        snaps, stats = ghrest.incremental(path, Path("."), urls, fetch, runner=run)
        self.assertEqual(len(snaps), 150)
        self.assertLessEqual(stats["read"], 1)


if __name__ == "__main__":
    unittest.main()
