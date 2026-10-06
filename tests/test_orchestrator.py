"""Orchestrator rules: ready, slots, park, merge queue, conflicts, restart."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import beam  # noqa: E402
import orchestrator  # noqa: E402
import report  # noqa: E402


def ticket(tid, **kw):
    row = {
        "id": tid,
        "status": kw.get("status", "queued"),
        "summary": tid,
        "size": kw.get("size", "S"),
        "autoMerge": kw.get("autoMerge", True),
        "deps": kw.get("deps", []),
        "after": kw.get("after", []),
        "locks": kw.get("locks", ["src/%s" % tid]),
        "critical": kw.get("critical", False),
        "starred": kw.get("starred", False),
        "rank": kw.get("rank"),
        "rankDays": kw.get("rankDays", 0),
        "priority": kw.get("priority"),
        "baseFix": kw.get("baseFix", False),
        "agent": kw.get("agent"),
        "branch": kw.get("branch", "warp/%s" % tid),
        "attempts": kw.get("attempts", 0),
        "alarm": kw.get("alarm"),
        "plan": kw.get("plan"),
        "result": kw.get("result"),
        "pr": kw.get("pr", {}),
        "jira": {},
    }
    return row


def beam_of(tickets, **kw):
    return {
        "version": 1,
        "runState": kw.get("runState", "running"),
        "paused": False,
        "baseGreen": kw.get("baseGreen", True),
        "config": kw.get("config", {"maxAgents": 4, "bugbotRequired": True, "bugbotManual": True, "maxFixAttempts": 3}),
        "gates": kw.get("gates", []),
        "program": {"criticalPath": []},
        "tickets": {t["id"]: t for t in tickets},
    }


def green(extra=None):
    pr = {"bugbot": "pass", "ci": "green", "url": "https://example.test/pull/1"}
    if extra:
        pr.update(extra)
    return pr


class ReadyTests(unittest.TestCase):
    def test_parent_path_lock_overlap_is_not_ready(self):
        holder = ticket("A", status="coding", locks=["pkg/api"])
        # The overlapping parent is past a two-path display truncation.
        waiter = ticket("B", locks=["docs/readme", "other/file", "pkg/api/handlers"])
        self.assertGreater(len(waiter["locks"]), 2)
        data = beam_of([holder, waiter])
        self.assertEqual(beam.ready(data), [])
        self.assertTrue(beam.lock_overlap(orchestrator.lock_paths(holder), orchestrator.lock_paths(waiter)))

        child = ticket("C", status="review", locks=["pkg/api/handlers"], pr=green(), plan="p", result="r")
        parent = ticket("D", locks=["pkg/api"])
        self.assertEqual(beam.ready(beam_of([child, parent])), [])

    def test_open_pr_does_not_satisfy_a_blocker(self):
        upstream = ticket("A", status="review", pr=green(), plan="plan", result="done")
        downstream = ticket("B", deps=["A"], locks=["src/b"])
        data = beam_of([upstream, downstream])
        self.assertEqual([t["id"] for t in beam.ready(data)], [])
        upstream["status"] = "merged"
        self.assertEqual([t["id"] for t in beam.ready(data)], ["B"])

    def test_after_list_is_a_blocker_too(self):
        upstream = ticket("A", status="review", pr=green())
        downstream = ticket("B", deps=[], after=["A"], locks=["src/b"])
        self.assertEqual(beam.ready(beam_of([upstream, downstream])), [])

    def test_slot_frees_on_green_pr_while_locks_remain_until_merge(self):
        done = ticket(
            "A",
            status="review",
            locks=["src/a"],
            pr=green(),
            plan="plan",
            result="ok",
        )
        other = ticket("B", locks=["src/b"])
        same = ticket("C", locks=["src/a"])
        data = beam_of([done, other, same], config={"maxAgents": 1, "bugbotRequired": True})
        self.assertFalse(orchestrator.holds_slot(done, data["config"]))
        self.assertTrue(orchestrator.holds_locks(done))
        self.assertIn("A", [i for i, _ in beam.held_locks(data)])
        ready_ids = [t["id"] for t in beam.ready(data)]
        self.assertEqual(ready_ids, ["B"])
        done["status"] = "merged"
        self.assertNotIn("A", [i for i, _ in beam.held_locks(data)])
        del data["tickets"]["B"]
        self.assertEqual([t["id"] for t in beam.ready(data)], ["C"])

    def test_does_not_wait_for_a_whole_level(self):
        inflight = ticket("A", status="coding", locks=["src/a"])
        blocked = ticket("B", deps=["A"], locks=["src/b"])
        later = ticket("C", rank=5, locks=["src/c"])
        sooner = ticket("D", rank=1, locks=["src/d"])
        starred = ticket("E", starred=True, rank=9, locks=["src/e"])
        data = beam_of([inflight, blocked, later, sooner, starred], config={"maxAgents": 4})
        self.assertEqual([t["id"] for t in beam.ready(data)], ["E", "D", "C"])

    def test_cap_comes_from_config(self):
        rows = [ticket("T%s" % n, locks=["src/t%s" % n]) for n in range(3)]
        data = beam_of(rows, config={"maxAgents": 2})
        self.assertEqual(len(beam.ready(data)), 2)
        self.assertIsNone(orchestrator.agent_cap({}))
        self.assertEqual(orchestrator.agent_cap({"maxAgents": 2}), 2)


class ParkTests(unittest.TestCase):
    def test_park_on_the_third_red(self):
        row = ticket("A", status="review", branch="warp/A", pr={"url": "https://example.test/pull/9", "ci": "red"}, locks=["src/a"])
        child = ticket("B", deps=["A"], locks=["src/b"])
        other = ticket("C", locks=["src/c"])
        data = beam_of([row, child, other], config={"maxAgents": 4, "maxFixAttempts": 3})
        self.assertEqual(orchestrator.note_failure(row, "ci red 1", data["config"]), "fix")
        self.assertEqual(orchestrator.note_failure(row, "ci red 2", data["config"]), "fix")
        self.assertTrue(orchestrator.holds_slot(row, data["config"]))
        self.assertTrue(orchestrator.holds_locks(row))
        self.assertEqual(orchestrator.note_failure(row, "ci red 3", data["config"]), "parked")
        self.assertEqual(row["status"], "parked")
        self.assertEqual(row["branch"], "warp/A")
        self.assertEqual(row["pr"]["url"], "https://example.test/pull/9")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["plan"]["blockers"][-1]["output"], "ci red 3")
        self.assertFalse(orchestrator.holds_locks(row))
        self.assertNotIn("A", [i for i, _ in beam.held_locks(data)])
        ready_ids = [t["id"] for t in beam.ready(data)]
        self.assertIn("C", ready_ids)
        self.assertNotIn("B", ready_ids)
        blocks = orchestrator.parked_blocks(data)
        self.assertEqual(blocks[0]["id"], "A")
        self.assertEqual(blocks[0]["blocks"], ["B"])
        html = report.render_html(data, {"mode": "local"}, True)
        self.assertIn("Parked", html)
        self.assertIn("A", html)
        self.assertIn("B", html)


class MergeQueueTests(unittest.TestCase):
    def test_manual_sizes_do_not_enter_the_merge_queue(self):
        manual = ticket(
            "L",
            size="L",
            autoMerge=False,
            status="awaiting_approval",
            pr=green(),
            plan="plan",
            result="result",
        )
        auto = ticket("S", status="review", pr=green(), plan="plan", result="result", rank=2)
        data = beam_of([manual, auto])
        self.assertEqual([t["id"] for t in orchestrator.merge_queue(data)], ["S"])
        manual["status"] = "merging"
        manual["pr"]["proceededBy"] = "alex"
        queued = [t["id"] for t in orchestrator.merge_queue(data)]
        self.assertEqual(queued, ["S", "L"])
        data["baseGreen"] = False
        self.assertEqual(orchestrator.merge_queue(data), [])
        self.assertIsNone(orchestrator.next_merge(data))

    def test_stack_falls_back_one_by_one_on_red(self):
        first = ticket("A", status="review", pr=green(), plan="p", result="r", rank=1)
        second = ticket("B", status="review", pr=green(), plan="p", result="r", rank=2)
        data = beam_of([second, first])
        queue = orchestrator.merge_queue(data)
        self.assertEqual([t["id"] for t in queue], ["A", "B"])
        stacked = orchestrator.stack_groups(queue, red=False)
        self.assertEqual([[t["id"] for t in group] for group in stacked], [["A", "B"]])
        isolated = orchestrator.stack_groups(queue, red=True)
        self.assertEqual([[t["id"] for t in group] for group in isolated], [["A"], ["B"]])
        nxt = orchestrator.next_merge(data)
        self.assertEqual(nxt["steps"], ["rebase", "check", "merge", "delete-branch", "dispatch"])
        self.assertEqual(nxt["ids"], ["A", "B"])

    def test_unset_check_command_uses_bugbot_and_ci(self):
        spec = orchestrator.required_checks({})
        self.assertEqual(spec["mode"], "bugbot-ci")
        self.assertIsNone(spec["command"])
        self.assertNotIn("make check", spec["detail"])
        self.assertIn("Bugbot and CI", spec["detail"])
        named = orchestrator.required_checks({"checkCommand": "scripts/check_version.py"})
        self.assertEqual(named["command"], "scripts/check_version.py")


class ConflictTests(unittest.TestCase):
    def test_append_only_conflict_keeps_both_sides(self):
        base = "header\nkeep\n"
        ours = "header\nkeep\nours-line\n"
        theirs = "header\nkeep\ntheirs-line\n"
        result = orchestrator.resolve_file("shared/notes.md", base, ours, theirs, ["shared/notes.md"])
        self.assertEqual(result["action"], "resolved")
        text = result["text"]
        self.assertIn("header\nkeep\n", text)
        self.assertIn("ours-line", text)
        self.assertIn("theirs-line", text)
        self.assertLess(text.index("keep"), text.index("ours-line"))
        self.assertLess(text.index("ours-line"), text.index("theirs-line"))

    def test_any_other_conflict_is_sent_back(self):
        result = orchestrator.resolve_file("src/app.py", "a\n", "a\nours\n", "a\ntheirs\n", [])
        self.assertEqual(result["action"], "send-back")
        self.assertNotIn("text", result)
        mixed = orchestrator.resolve_conflicts(
            [
                {"path": "shared/notes.md", "base": "a\n", "ours": "a\nours\n", "theirs": "a\ntheirs\n"},
                {"path": "src/app.py", "base": "a\n", "ours": "a\nours\n", "theirs": "a\ntheirs\n"},
            ],
            ["shared/notes.md"],
        )
        self.assertEqual(mixed["action"], "send-back")
        self.assertEqual(mixed["paths"], ["src/app.py"])
        self.assertNotIn("files", mixed)


class RestartTests(unittest.TestCase):
    def test_restart_classifies_merged_in_flight_and_pending(self):
        merged = ticket("A", status="coding")
        flying = ticket("B", status="queued", branch=None)
        pending = ticket("C", status="claimed", agent="shuttle-C")
        parked = ticket("P", status="parked", branch="warp/P", pr={"url": "https://example.test/pull/3"})
        data = beam_of([merged, flying, pending, parked])
        classes = orchestrator.rebuild(
            data,
            {
                "A": {"on_base": True, "open_branch": False, "open_pr": False},
                "B": {"on_base": False, "open_branch": True, "open_pr": True},
                "C": {"on_base": False, "open_branch": False, "open_pr": False},
                "P": {"on_base": False, "open_branch": True, "open_pr": True},
            },
        )
        self.assertEqual(classes["A"], "merged")
        self.assertEqual(classes["B"], "in-flight")
        self.assertEqual(classes["C"], "pending")
        self.assertEqual(data["tickets"]["A"]["status"], "merged")
        self.assertEqual(data["tickets"]["B"]["status"], "review")
        self.assertEqual(data["tickets"]["C"]["status"], "queued")
        self.assertIsNone(data["tickets"]["C"]["agent"])
        self.assertEqual(data["tickets"]["P"]["status"], "parked")

    def test_checkout_is_isolated_and_branch_is_not_feature(self):
        row = ticket("A", branch=None)
        row["jiraKey"] = "WAR-1"
        self.assertEqual(orchestrator.branch_name(row), "warp/A-WAR-1")
        self.assertFalse(orchestrator.branch_name(row).startswith("feature/"))
        self.assertEqual(orchestrator.checkout_kind({"runner": "cloud"}), "cloud-vm")
        self.assertEqual(orchestrator.checkout_kind({"runner": "local"}), "worktree")
        data = beam_of(
            [
                ticket("A", status="coding", branch="warp/same"),
                ticket("B", status="coding", branch="warp/same"),
            ]
        )
        self.assertEqual(len(orchestrator.shared_branches(data)), 1)

    def test_base_fix_is_ahead_of_every_rank(self):
        normal = ticket("N", starred=True, rank=1, locks=["src/n"])
        data = beam_of([normal], baseGreen=False, config={"maxAgents": 2, "runner": "local"})
        actions = orchestrator.dispatch_actions(data, beam.ready(data), cap=2)
        self.assertEqual(actions[0]["action"], "dispatch-base-fix")
        self.assertEqual(actions[0]["checkout"], "worktree")
        self.assertEqual(actions[1]["id"], "N")
        parked = beam_of([ticket("Z", status="parked")], baseGreen=True)
        self.assertTrue(orchestrator.program_done(parked))
        parked["baseGreen"] = False
        self.assertFalse(orchestrator.program_done(parked))


if __name__ == "__main__":
    unittest.main()
