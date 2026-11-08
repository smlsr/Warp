"""1.7.15 part 2: Warp as a self-driving supervisor. One test (or more) per acceptance item of the brief."""

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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

os.environ["WARP_NO_BACKGROUND"] = "1"

import asks  # noqa: E402
import audit  # noqa: E402
import beam  # noqa: E402
import blockgraph  # noqa: E402
import drive  # noqa: E402
import fleet  # noqa: E402
import gaps  # noqa: E402
import intake  # noqa: E402
import lane  # noqa: E402
import pipeline  # noqa: E402
import selfdrive  # noqa: E402
import waivers  # noqa: E402

NOW = "2026-11-08T14:00:00Z"
URL = "https://github.com/o/r/pull/%s"


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git(cwd, *args):
    done = run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class Beam(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text("jiraProject: HOS\nhumanAsk: [product-permission, data-deletion]\n"
                                                         "load: {high: 25, low: 8, spawnAt: 30}\n")
        self.path = self.tmp / ".warp" / "beam.json"
        self.write({})

    def write(self, tickets, **extra):
        doc = {"version": 1, "runState": "running", "config": {"maxAgents": 4}, "tickets": tickets}
        doc.update(extra)
        self.path.write_text(json.dumps(doc, indent=2) + "\n")

    def beam(self):
        return beam.load_json(self.path)


class ConfigTest(Beam):
    def test_flow_and_block_config(self):
        (self.tmp / ".warp" / "config.yaml").write_text("cadence: {tick: 120, sweep: 600}\nload:\n  high: 30\n"
                                                         "humanAsk: [budget]\n")
        cfg = selfdrive.config({}, self.path)
        self.assertEqual((cfg["cadence"]["tick"], cfg["cadence"]["sweep"], cfg["cadence"]["audit"]), (120, 600, 1800))
        self.assertEqual((cfg["load"]["high"], cfg["load"]["low"]), (30, 8))
        self.assertEqual(cfg["humanAsk"], ["budget"])
        self.assertEqual(selfdrive.DEFAULTS["agents"]["ceiling"], 120)

    def test_cadences_and_lease(self):
        cfg = selfdrive.config({}, self.path)
        start = 1_000_000.0
        self.assertEqual(selfdrive.due(self.path, cfg, start), ["tick", "sweep", "audit", "deep"])
        self.assertTrue(selfdrive.take_lease(self.path, "audit", cfg, start, "background"))
        self.assertFalse(selfdrive.take_lease(self.path, "audit", cfg, start + 1, "inline"))
        self.assertNotIn("audit", selfdrive.due(self.path, cfg, start + 10))
        selfdrive.finish(self.path, "audit", start + 20, ["audit: report x"], 20)
        self.assertNotIn("audit", selfdrive.due(self.path, cfg, start + 60))
        self.assertIn("audit", selfdrive.due(self.path, cfg, start + 1900))
        self.assertEqual(selfdrive.take_pending(self.path), ["audit: report x"])
        self.assertEqual(selfdrive.take_pending(self.path), [])


class MainRedTest(Beam):
    def test_waiver_gap_strict_gate_and_retire(self):
        self.write({"UI-1": {"id": "UI-1", "status": "review", "summary": "x", "pr": {"url": URL % 1}}})
        data = self.beam()
        base = {"sha": "c3", "state": "red", "failed": ["api/users::test_list"]}
        history = lambda: [("c1", "green"), ("c2", "red"), ("c3", "red")]
        lines = waivers.main_red(self.path, data, base, history, lambda sha: "dana", now=NOW)
        text = "\n".join(lines)
        self.assertIn("waiver: W-c2 registered", text)
        self.assertIn("heads containing c2", text)
        gid = gaps.gap_id("MAIN", "RED-c2")
        ticket = self.beam()["tickets"][gid]
        self.assertEqual((ticket["rank"], ticket["status"]), (0, "queued"))
        self.assertIn("label warp:%s" % gid, text)  # the Jira MUST DO with the label
        import jira_sync

        jira_sync.record_resolved(self.path, gid, "HOS-77", source="label")
        self.assertEqual(self.beam()["tickets"][gid]["jiraKey"], "HOS-77")
        # A second test broken by the same commit joins the same gap: one fix PR, one CI run.
        data = self.beam()
        more = waivers.main_red(self.path, data, dict(base, failed=["api/users::test_list", "web/app::test_nav"]),
                                history, lambda sha: "dana", now=NOW)
        self.assertTrue(any("now also covers web/app::test_nav passes on main" in line for line in more), more)
        self.assertEqual(sum(1 for t in self.beam()["tickets"] if t.startswith("G-MAIN")), 1)
        # Strict gate.
        data = self.beam()
        contains = lambda sha, head: head.startswith("child-of-c2")
        self.assertTrue(waivers.gate(data, "child-of-c2-head", ["api/users::test_list"], contains)[0])
        self.assertFalse(waivers.gate(data, "old-head", ["api/users::test_list"], contains)[0])
        ok, why = waivers.gate(data, "child-of-c2-head", ["api/users::test_list", "api/users::test_get"], contains)
        self.assertFalse(ok)
        self.assertIn("not the only failure in api/users", why)
        self.assertTrue(waivers.gate(data, "old-head", ["api/users::test_list"], contains, merge_ref="child-of-c2-merge")[0])
        # The fix merges: the waiver retires, once.
        data["tickets"][gid]["status"] = "merged"
        self.assertTrue(any("retired" in line for line in waivers.retire(self.path, data, now=NOW)))
        self.assertEqual(waivers.retire(self.path, data, now=NOW), [])
        self.assertEqual(waivers.active(data), [])

    def test_first_bad_bisects(self):
        results = {"a": "green", "b": "green", "c": "", "d": "red", "e": "red"}
        self.assertEqual(waivers.first_bad(list("abcde"), results.get), "d")


class GitCase(Beam):
    """A real origin and clone: the lane's git work is not mocked."""

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], self.tmp)
        self.repo = self.tmp / "repo"
        run(["git", "clone", "-q", str(self.origin), str(self.repo)], self.tmp)
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "GAP-REPORT.md").write_text("| id | gap |\n|---|---|\n| G-1 | first |\n")
        (self.repo / "app.py").write_text("print(1)\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "main")
        self.pushes = []

    def runner(self, cmd, cwd=None):
        if cmd[:2] == ["git", "push"]:
            self.pushes.append(cmd)
        return run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *cmd[1:]]
                   if cmd and cmd[0] == "git" else cmd, cwd)


class UnionTest(GitCase):
    def test_gap_report_only_conflict_is_merged_by_the_lane(self):
        git(self.repo, "checkout", "-q", "-b", "warp/x")
        (self.repo / "GAP-REPORT.md").write_text("| id | gap |\n|---|---|\n| G-1 | first |\n| G-2 | from the branch |\n")
        (self.repo / "x.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "branch")
        git(self.repo, "push", "-q", "origin", "warp/x")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "GAP-REPORT.md").write_text("| id | gap |\n|---|---|\n| G-1 | first |\n| G-3 | from main |\n")
        git(self.repo, "commit", "-qam", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        data = self.beam()
        prs = [{"number": "5", "branch": "warp/x", "base": "main", "dirty": True, "conflicts": ["GAP-REPORT.md"],
                "owner": "coord-ui", "agent": "", "head": "h"}]
        sent = []
        lines = lane.refresh_stale(self.path, data, prs, lambda b, onto: lane.merge_into(self.repo, b, onto, run=self.runner),
                                   lambda owner, text, agent="": sent.append(text) or [], selfdrive.config({}, self.path), now=NOW)
        self.assertTrue(any("no owner push" in line for line in lines), lines)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.pushes), 1)
        git(self.repo, "fetch", "-q", "origin")
        merged = git(self.repo, "show", "origin/warp/x:GAP-REPORT.md")
        self.assertIn("G-2 | from the branch", merged)
        self.assertIn("G-3 | from main", merged)

    def test_a_real_conflict_gets_one_refresh(self):
        data = self.beam()
        sent = []
        prs = [{"number": "6", "branch": "warp/y", "base": "main", "dirty": True, "conflicts": ["app.py"], "owner": "o", "head": "h1"}]
        send = lambda owner, text, agent="": sent.append(text) or ["relay: %s" % text]
        cfg = selfdrive.config({}, self.path)
        lane.refresh_stale(self.path, data, prs, lambda *a: ("conflict", ""), send, cfg, now=NOW)
        lane.refresh_stale(self.path, data, prs, lambda *a: ("conflict", ""), send, cfg, now=NOW)
        self.assertEqual(len(sent), 1)
        self.assertIn("push once", sent[0])
        later = lane.refresh_stale(self.path, data, prs, lambda *a: ("conflict", ""), send, cfg, now="2026-11-08T14:31:00Z")
        self.assertTrue(any(line.startswith("restaff: #6") for line in later), later)


class StackTest(GitCase):
    def test_child_rebuilt_retargeted_one_push_holds_lifted(self):
        git(self.repo, "checkout", "-q", "-b", "warp/parent")
        (self.repo / "parent.py").write_text("p = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "parent 1")
        (self.repo / "parent.py").write_text("p = 2\n")
        git(self.repo, "commit", "-qam", "parent 2")
        parent_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "warp/parent")
        git(self.repo, "checkout", "-q", "-b", "warp/child")
        (self.repo / "child.py").write_text("c = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "child")
        git(self.repo, "push", "-q", "origin", "warp/child")
        # GitHub squash-merges the parent into main.
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", "warp/parent")
        git(self.repo, "commit", "-qm", "parent (#1)")
        git(self.repo, "push", "-q", "origin", "main")
        self.write({"P": {"id": "P", "status": "review", "pr": {"url": URL % 1}, "branch": "warp/parent"},
                    "C": {"id": "C", "status": "review", "pr": {"url": URL % 2}, "branch": "warp/child", "holds": ["P"]}})
        data = self.beam()
        lane_doc = {"queue": {"1@abc": {"pr": "1", "url": URL % 1, "sha": parent_head, "ticket": "P", "branch": "warp/parent",
                                        "score": 5, "queuedAt": NOW}}}
        intake.save_lane(self.path, lane_doc)
        retargets = []
        lines = lane.run_lane(
            self.path, data, merge=lambda e: ("merged", "s" * 40),
            list_children=lambda branch: [{"number": "2", "url": URL % 2, "branch": "warp/child", "owner": "coord", "agent": ""}]
            if branch == "warp/parent" else [],
            retarget=lambda url, to: retargets.append((url, to)) or (True, ""),
            rebuild=lambda child, head: lane.rebuild_child(self.repo, child, head, run=self.runner),
            send=lambda owner, text, agent="": ["relay: %s" % text], now=NOW)
        text = "\n".join(lines)
        self.assertEqual(retargets, [(URL % 2, "main")])
        self.assertIn("child #2 rebuilt on main after #1 (only its own files; pushed once", text)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(data["tickets"]["C"]["holds"], [])
        self.assertIn("hold: lifted P on C", text)
        git(self.repo, "fetch", "-q", "origin")
        self.assertEqual(git(self.repo, "diff", "--name-only", "origin/main", "origin/warp/child"), "child.py")


class FleetTest(Beam):
    def rows(self, items, prs, data=None):
        data = data or {"tickets": {}, "prOwners": {}}
        reg = [{"id": i["id"], "parent": "coord-ui"} for i in items]
        rows = fleet.inventory(items, reg, data)
        return fleet.classify(rows, prs, data, selfdrive.config({}, self.path), now=NOW)

    def test_duplicates_finished_and_stale_wait(self):
        items = [
            {"id": "bc-old", "status": "RUNNING", "updatedAt": "2026-11-08T12:00:00Z", "target": {"prUrl": URL % 7}},
            {"id": "bc-new", "status": "RUNNING", "updatedAt": "2026-11-08T13:50:00Z", "target": {"prUrl": URL % 7}},
            {"id": "bc-done", "status": "RUNNING", "updatedAt": "2026-11-08T13:55:00Z", "target": {"prUrl": URL % 8}},
            {"id": "bc-wait", "status": "RUNNING", "updatedAt": "2026-11-08T13:55:00Z", "summary": "waiting on #12 to merge",
             "target": {"prUrl": URL % 9}},
        ]
        prs = {"7": {"state": "OPEN"}, "8": {"state": "MERGED"}, "9": {"state": "OPEN"},
               "12": {"state": "MERGED", "mergedAt": "2026-11-08T13:00:00Z"}}
        rows = self.rows(items, prs)
        cls = {r["id"]: r["class"] for r in rows}
        self.assertEqual(cls, {"bc-old": "duplicate", "bc-new": "active", "bc-done": "finished-running", "bc-wait": "stale-wait"})
        data = {"tickets": {}}
        stopped, told = [], []
        lines = fleet.act(self.path, data, rows, [], selfdrive.config({}, self.path),
                          stop=lambda r: stopped.append(r["id"]) or (True, "api"),
                          send=lambda owner, text, agent="": told.append(text) or ["relay: %s" % text],
                          restaff=lambda r: [], assign=lambda n: [], now=NOW)
        self.assertEqual(sorted(stopped), ["bc-done", "bc-old"])
        self.assertTrue(any("#12 merged at 2026-11-08T13:00:00Z" in t for t in told), told)
        again = fleet.act(self.path, data, rows, [], selfdrive.config({}, self.path),
                          stop=lambda r: stopped.append(r["id"]) or (True, "api"), send=lambda *a, **k: ["x"],
                          restaff=lambda r: [], assign=lambda n: [], now=NOW)
        self.assertEqual(again, [])

    def test_unstoppable_agent_is_one_line_not_an_alarm(self):
        rows = [{"id": "bc-x", "class": "finished-running", "why": "#8 is merged", "pr": "8"}]
        data = {}
        lines = fleet.act(self.path, data, rows, [], selfdrive.config({}, self.path), stop=lambda r: (False, ""),
                          send=lambda *a, **k: [], restaff=lambda r: [], assign=lambda n: [], now=NOW)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("needs-human-stop https://cursor.com/agents?id=bc-x"))
        self.assertNotIn("ALARM", lines[0])


class LoadTest(Beam):
    def data_with_chains(self):
        tickets = {}
        owners = {}
        n = 0
        for chain in range(10):
            prev = None
            for depth in range(4):
                n += 1
                tid = "T-%d" % n
                pr = {"url": URL % n, "headRef": "b%d" % n}
                if prev:
                    pr["base"] = "b%d" % prev
                tickets[tid] = {"id": tid, "status": "review", "pr": pr, "branch": "b%d" % n}
                owners[str(n)] = {"owner": "coord-a", "coordinator": "coord-a"}
                prev = n
        for m in (101, 102):
            tickets["U-%d" % m] = {"id": "U-%d" % m, "status": "review", "pr": {"url": URL % m}}
            owners[str(m)] = {"owner": "coord-b", "coordinator": "coord-b"}
        return {"tickets": tickets, "prOwners": owners, "config": {"maxAgents": 4}}

    def test_whole_chains_move_and_the_new_owner_waits_for_handed_off(self):
        data = self.data_with_chains()
        coords = {"coordinators": {"coord-a": {"area": "UI"}, "coord-b": {"area": "UI"}}}
        table = audit.load_table(data, coords, [])
        sizes = {r["name"]: len(r["prs"]) for r in table}
        self.assertEqual((sizes["coord-a"], sizes["coord-b"]), (40, 2))
        moves = audit.rebalance(data, table, selfdrive.config({}, self.path))
        self.assertTrue(moves)
        for chain, old, new in moves:
            self.assertEqual(len(chain), 4)  # a whole chain, never one pull request out of it
            self.assertEqual(old, "coord-a")
        self.assertFalse(audit.handoff_ready(self.path, "1", "coord-b", "abc1234", outbox_reader=lambda p: ""))
        self.assertTrue(audit.handoff_ready(self.path, "1", "coord-b", "abc1234def",
                                            outbox_reader=lambda p: "HANDED-OFF coord-b abc1234"))

    def test_a_coordinator_is_started_and_retired_after_two_empty_audits(self):
        data = {"tickets": {}, "prOwners": {str(n): {"coordinator": "warp"} for n in range(35)}, "config": {"maxAgents": 4}}
        coords = {}
        cfg = selfdrive.config({}, self.path)
        table = audit.load_table(data, coords, [])
        lines = audit.coordinators_step(self.path, data, coords, table, {"UI": 3}, cfg, now=NOW)
        self.assertTrue(any(line.startswith("coordinator: start coord-ui") for line in lines), lines)
        self.assertTrue(coords["coordinators"]["coord-ui"]["start"])
        empty = {"tickets": {}, "prOwners": {}, "config": {"maxAgents": 4}}
        for when in ("2026-11-08T14:30:00Z", "2026-11-08T15:00:00Z"):
            out = audit.coordinators_step(self.path, empty, coords, audit.load_table(empty, coords, []), {}, cfg, now=when)
        self.assertTrue(any(line.startswith("coordinator: retire coord-ui") for line in out), out)


class CapTest(Beam):
    def test_agent_cap_queues_and_retries(self):
        import checkout

        data = {"tickets": {"UI-1": {"id": "UI-1", "status": "coding", "phase": "implementing", "shuttle": {"pending": True}}}}
        outcome = checkout.record_result(data, "UI-1", "result: UI-1 failed Launch hit agent cap 20", False)
        row = data["tickets"]["UI-1"]
        self.assertEqual(outcome, "queued-cap")
        self.assertEqual((row["status"], row.get("phase"), row["capRetries"]), ("queued", None, 1))
        self.assertNotIn("recoveries", row)


class HungTest(Beam):
    def test_cancelled_once_and_never_rerun(self):
        calls = []

        def rest(cmd, cwd=None):
            calls.append(cmd)
            path = cmd[4]
            if "/actions/runs?" in path:
                body = {"workflow_runs": [{"id": 99, "status": "in_progress", "created_at": "2026-11-08T12:00:00Z"}]}
            elif path.endswith("/jobs?per_page=100"):
                body = {"jobs": [{"status": "in_progress", "started_at": "2026-11-08T12:50:00Z",
                                  "steps": [{"started_at": "2026-11-08T12:58:00Z"}]}]}
            else:
                body = {}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

        data = {}
        lines = waivers.hung(self.path, data, self.tmp, "o/r", [("h" * 40, "UI-1")], runner=rest, now=NOW, minutes=60)
        self.assertTrue(any("cancelled run 99" in line for line in lines), lines)
        self.assertEqual(sum(1 for c in calls if c[3] == "POST" and c[4].endswith("/cancel")), 1)
        again = waivers.hung(self.path, data, self.tmp, "o/r", [("h" * 40, "UI-1")], runner=rest, now=NOW, minutes=60)
        self.assertEqual(again, [])
        self.assertFalse(any("rerun" in c[4] for c in calls))

    def test_progress_within_the_hour_is_left_alone(self):
        def rest(cmd, cwd=None):
            path = cmd[4]
            body = ({"workflow_runs": [{"id": 5}]} if "/actions/runs?" in path else
                    {"jobs": [{"status": "in_progress", "steps": [{"started_at": "2026-11-08T13:30:00Z"}]}]})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

        self.assertEqual(waivers.hung(self.path, {}, self.tmp, "o/r", [("h", "UI-1")], runner=rest, now=NOW, minutes=60), [])


class AskTest(Beam):
    def test_one_question_with_a_default_and_nothing_else_stops(self):
        lines = asks.ask(self.path, "product-permission", "Remove atlas.manage from members?", ["keep", "remove"], "keep",
                         blocking=["AUTH-3"])
        self.assertEqual(len(lines), 2)
        self.assertIn("(recommended)", lines[0])
        self.assertIn("Only AUTH-3 waits", lines[0])
        self.assertEqual(asks.ask(self.path, "product-permission", "Remove atlas.manage from members?", ["keep", "remove"], "keep"), [])
        aid = lines[0].split()[1]
        self.assertIn("decided: remove", asks.decide(self.path, aid, "remove")[0])
        self.assertEqual(asks.decided(self.path, "Remove atlas.manage from members?"), "remove")
        # A category that is not a human ask takes its default and is recorded.
        auto = asks.ask(self.path, "budget-ish", "Rename an internal helper?", ["a", "b"], "a")
        self.assertIn("applied the default", auto[0])

    def test_the_ladder(self):
        cfg = selfdrive.config({}, self.path)
        data = {}
        steps = [asks.climb(data, "UI-1", False, cfg) for _ in range(10)]
        self.assertEqual([s for s in steps if s], ["nudge", "restaff", "coordinator", "ask"])
        self.assertEqual(asks.climb(data, "UI-1", True, cfg), "")


class BlockerTest(Beam):
    def test_score_is_dependents_times_age(self):
        data = {"tickets": {
            "A": {"id": "A", "status": "review", "addedAt": "2026-11-08T10:00:00Z", "pr": {"url": URL % 1}, "branch": "a"},
            "B": {"id": "B", "status": "review", "pr": {"url": URL % 2, "base": "a"}},
            "C": {"id": "C", "status": "queued", "deps": ["B"]},
            "D": {"id": "D", "status": "queued", "holds": ["#1"]},
            "M1": {"id": "M1", "status": "review", "locks": ["db/migrations/0041_a.sql"], "addedAt": "2026-11-08T13:00:00Z"},
            "M2": {"id": "M2", "status": "queued", "locks": ["db/migrations/0042_b.sql"]},
        }}
        ranked = blockgraph.ranked(data, now=NOW)
        top = {node: (score, deps) for node, score, deps in ranked}
        self.assertEqual(top["A"][1], ["B", "C", "D"])
        self.assertEqual(top["A"][0], 12.0)  # 3 dependents x 4 hours
        self.assertEqual(top["M1"][1], ["M2"])
        self.assertEqual(ranked[0][0], "A")


class BatchTest(unittest.TestCase):
    def test_a_fix_waits_for_every_check_then_takes_them_all_at_once(self):
        ticket = {"id": "UI-1", "status": "review", "phase": "reviewing",
                  "pr": {"url": URL % 1, "headSha": "h1", "ci": "red", "ciSha": "h1", "checksPending": 2,
                         "bugbot": "pass", "bugbotSha": "h1"}}
        item = pipeline._fix_class(ticket, {})
        self.assertEqual(item["kind"], "batch-wait")
        lines = []
        pipeline._launch_fix({"tickets": {"UI-1": ticket}}, ticket, item, lines)
        self.assertIn("waits for the whole batch (2 checks still running on h1)", lines[0])
        ticket["pr"]["checksPending"] = 0
        self.assertEqual(pipeline._fix_class(ticket, {})["kind"], "ci")
        out = pipeline.batched(["ci red", "bugbot: null deref in a.py"])
        self.assertTrue(out[0].startswith("ONE ROUND, ONE PUSH: fix all 2 items"))
        self.assertEqual(pipeline.batched(["ci red"]), ["ci red"])
        self.assertEqual(pipeline.batch_wait(dict(ticket, pr=dict(ticket["pr"], checksPending=3)), {"batch": {"fixes": False}}), "")


class IntakeTest(Beam):
    def test_ready_lines_are_verified_and_queued_by_score(self):
        (self.tmp / ".warp" / "outbox").mkdir()
        (self.tmp / ".warp" / "outbox" / "coord-ui.md").write_text(
            "- READY #11 abcdef1 waive=none bugbot=pass\n- GAPPR #12 1234567 gap=G-UI-X\n- READY #13 7654321\n")
        docs = {
            "11": {"state": "OPEN", "number": 11, "url": URL % 11, "headRefOid": "abcdef1" + "0" * 33, "mergeStateStatus": "CLEAN",
                   "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"}]},
            "12": {"state": "OPEN", "number": 12, "url": URL % 12, "headRefOid": "1234567" + "0" * 33, "mergeStateStatus": "DIRTY",
                   "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"}]},
            "13": {"state": "OPEN", "number": 13, "url": URL % 13, "headRefOid": "9999999" + "0" * 33, "mergeStateStatus": "CLEAN",
                   "statusCheckRollup": []},
        }
        data = self.beam()
        lines = intake.intake(self.path, data, lambda pr: docs[str(pr).lstrip("#")], now=NOW)
        text = "\n".join(lines)
        self.assertIn("intake: READY #11 abcdef1 verified", text)
        self.assertIn("GAPPR 12 1234567 not queued: it conflicts with its base (dirty)", text)
        self.assertIn("not queued: the head is 9999999", text)
        self.assertEqual([e["pr"] for _k, e in intake.ordered(self.path)], ["11"])
        self.assertEqual(intake.intake(self.path, data, lambda pr: docs[str(pr).lstrip("#")], now=NOW), [])


class TickTimingTest(Beam):
    def test_a_tick_with_150_open_pull_requests_finishes_under_90s(self):
        tickets = {}
        for n in range(1, 151):
            tickets["T-%d" % n] = {"id": "T-%d" % n, "status": "external" if n % 2 else "review", "summary": "t",
                                   "pr": {"url": URL % n, "headSha": "h%d" % n, "ci": "green", "mergeStateStatus": "CLEAN",
                                          "base": "main"}}
        self.write(tickets)

        def rest(cmd, cwd=None):
            time.sleep(0.001)
            path = cmd[4] if len(cmd) > 4 else ""
            body = [] if "/pulls?" in path else {}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

        def git_run(cmd, cwd=None):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        env = drive.Env(rest=rest, git=git_run, agents=lambda *a: (200, {"items": []}), env={})
        began = time.monotonic()
        lines = drive.tick(self.path, env=env, now=NOW, base_checks=lambda: {"sha": "m", "state": "green", "failed": []})
        took = time.monotonic() - began
        self.assertLess(took, 90)
        self.assertTrue(lines[-1].startswith("tick: done in"), lines)
        self.assertEqual(len(self.beam()["tickets"]), 150)


class GapTest(Beam):
    def test_dedupe_by_label_summary_and_row(self):
        gid, lines = gaps.create(self.path, self.beam, "AUTH", "rbac", "Members cannot list roles", blocks=[], source="audit")
        self.assertEqual(gid, "G-AUTH-RBAC")
        self.assertTrue(any("label warp:G-AUTH-RBAC" in line for line in lines))
        again, lines = gaps.create(self.path, self.beam, "AUTH", "other", "Members cannot list roles", source="audit")
        self.assertEqual(again, "G-AUTH-RBAC")
        self.assertIn("already covers it", lines[0])
        ledger = (self.tmp / ".warp" / "gap-owners.md").read_text()
        self.assertIn("| G-AUTH-RBAC | AUTH | warp |", ledger)


class AgentsApiTest(Beam):
    """1.7.16: Warp messages, starts and stops agents itself through the Cloud Agents API."""

    def transport(self, answers):
        self.calls = []

        def call(method, url, key, body):
            self.calls.append((method, url.replace("https://api.cursor.com", ""), body))
            for (m, suffix), reply in answers.items():
                if m == method and url.endswith(suffix):
                    return reply() if callable(reply) else reply
            return 404, {}
        return call

    def test_a_message_is_a_follow_up_run_and_a_busy_agent_gets_it_later(self):
        import relay

        busy = {"n": 0}

        def runs():
            busy["n"] += 1
            return (409, {}) if busy["n"] == 1 else (201, {"id": "run-2"})

        call = self.transport({("POST", "/v1/agents/bc-1/runs"): runs})
        data = {}
        lines = relay.send(self.path, data, "coord-ui", "REFRESH #5: merge main once, push once.", agent_id="bc-1",
                           transport=call, env={"CURSOR_API_KEY": "k"})
        self.assertIn("mid-run", lines[0])
        self.assertEqual(self.calls[0][:2], ("POST", "/v1/agents/bc-1/runs"))
        self.assertEqual(self.calls[0][2], {"prompt": {"text": "REFRESH #5: merge main once, push once."}})
        later = relay.retry_pending(self.path, transport=call, env={"CURSOR_API_KEY": "k"})
        self.assertTrue(later[0].startswith("relay: sent to coord-ui"), later)
        self.assertEqual(relay.retry_pending(self.path, transport=call, env={"CURSOR_API_KEY": "k"}), [])

    def test_a_named_coordinator_is_messaged_through_its_agent(self):
        import relay

        (self.tmp / ".warp" / "coordinators.json").write_text(json.dumps({"coordinators": {"coord-ui": {"agentId": "bc-9"}}}))
        call = self.transport({("POST", "/v1/agents/bc-9/runs"): (201, {"id": "r"})})
        lines = relay.send(self.path, {}, "coord-ui", "You take over #7.", transport=call, env={"CURSOR_API_KEY": "k"})
        self.assertTrue(lines[0].startswith("relay: sent to coord-ui (https://cursor.com/agents?id=bc-9)"), lines)

    def test_the_audit_starts_and_stops_coordinators_itself(self):
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:o/r.git"], cwd=self.tmp, check=True)
        call = self.transport({("POST", "/v1/agents"): (201, {"id": "bc-new"}),
                               ("GET", "/v1/agents/bc-new"): (200, {"latestRunId": "run-1"}),
                               ("POST", "/runs/run-1/cancel"): (200, {}), ("POST", "/v1/agents/bc-new/archive"): (200, {})})
        env = drive.Env(agents=call, env={"CURSOR_API_KEY": "k"})
        coords = {"coordinators": {"coord-ui": {"area": "UI", "start": True}}}
        lines = drive._coordinator_agents(self.path, {"config": {"maxAgents": 4}, "prOwners": {"5": {"coordinator": "coord-ui"}}},
                                          coords, self.tmp, env, now=NOW)
        self.assertTrue(lines[0].startswith("coordinator: started coord-ui (bc-new) for area UI"), lines)
        method, url, body = self.calls[0]
        self.assertEqual((method, url), ("POST", "/v1/agents"))
        self.assertEqual(body["repos"], [{"url": "https://github.com/o/r", "startingRef": "main"}])
        self.assertIn("You are **coord-ui**", body["prompt"]["text"])
        self.assertIn("#5", body["prompt"]["text"])
        self.assertTrue((self.tmp / ".warp" / "rules.md").is_file())
        self.assertEqual(coords["coordinators"]["coord-ui"]["agentId"], "bc-new")
        coords["coordinators"]["coord-ui"]["stop"] = True
        lines = drive._coordinator_agents(self.path, {}, coords, self.tmp, env, now=NOW)
        self.assertEqual(lines, ["coordinator: stopped coord-ui (bc-new)"])

    def test_a_restaff_worker_starts_on_the_pull_request(self):
        import coordinators

        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=self.tmp, check=True)
        call = self.transport({("POST", "/v1/agents"): (201, {"agent": {"id": "bc-w"}})})
        aid, lines = coordinators.worker(self.path, {}, URL % 9, "Take over #9.", "k", self.tmp, transport=call)
        self.assertEqual(aid, "bc-w")
        self.assertEqual(self.calls[0][2]["repos"], [{"url": "https://github.com/o/r", "prUrl": URL % 9}])
        self.assertIn("Fetch the remote head", self.calls[0][2]["prompt"]["text"])


class PushChainTest(Beam):
    """1.7.16: GITHUB_TOKEN, then the checkout's credentials, then the parent's Cursor GitHub tools, then the coordinator."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=self.tmp, check=True)

    def runner(self, token_ok=False, origin_ok=False):
        self.cmds = []

        def run(cmd, cwd=None):
            self.cmds.append(cmd)
            if cmd[:2] == ["git", "push"]:
                good = token_ok if "x-access-token" in cmd[3] else origin_ok
                return subprocess.CompletedProcess(cmd, 0 if good else 1, stdout="",
                                                   stderr="" if good else "fatal: Authentication failed for %s" % cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def test_the_token_is_tried_first_and_never_printed(self):
        import pusher

        ok, how, why = pusher.push(self.tmp, "a" * 40, "warp/x", run=self.runner(token_ok=True), env={"GITHUB_TOKEN": "ghp_secret"})
        self.assertEqual((ok, how), (True, "GITHUB_TOKEN"))
        self.assertEqual(self.cmds[0][3], "https://x-access-token:ghp_secret@github.com/o/r.git")
        ok, how, why = pusher.push(self.tmp, "a" * 40, "warp/x", run=self.runner(origin_ok=True), env={"GITHUB_TOKEN": "ghp_secret"})
        self.assertEqual((ok, how), (True, "origin credentials"))
        ok, how, why = pusher.push(self.tmp, "a" * 40, "warp/x", run=self.runner(), env={"GITHUB_TOKEN": "ghp_secret"})
        self.assertFalse(ok)
        self.assertNotIn("ghp_secret", why)
        self.assertIn("GITHUB_TOKEN:", why)
        self.assertIn("origin:", why)
        ok, how, _ = pusher.push(self.tmp, "a" * 40, "warp/x", run=self.runner(origin_ok=True), env={})
        self.assertEqual(how, "origin credentials")
        self.assertFalse(any("x-access-token" in " ".join(c) for c in self.cmds))

    def test_parent_tools_then_the_coordinator(self):
        import pusher

        run = self.runner()
        lines = pusher.fallback(self.path, self.tmp, "b" * 40, "warp/x", "origin: refused", pr="5", owner="UI-5",
                                coordinator="coord-ui", redo="merge origin/main into warp/x keeping both sides, push once",
                                files=["GAP-REPORT.md"], run=run)
        self.assertTrue(lines[0].startswith("push: MUST DO warp/x (#5): Warp could not push bbbbbbbbbbbb"), lines)
        self.assertIn("git push origin refs/warp/pending/warp/x:refs/heads/warp/x", lines[0])
        self.assertIn(["git", "update-ref", "refs/warp/pending/warp/x", "b" * 40], self.cmds)
        told = []
        rest_head = {"sha": "c" * 40}

        def rest(cmd, cwd=None):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"object": rest_head}), stderr="")

        send = lambda owner, text, agent="": told.append((owner, text)) or ["relay: sent to %s" % owner]
        out = pusher.check_pending(self.path, self.tmp, send, runner=rest, now=NOW)
        self.assertEqual(told[0][0], "coord-ui")
        self.assertIn("merge origin/main into warp/x keeping both sides", told[0][1])
        self.assertEqual(pusher.check_pending(self.path, self.tmp, send, runner=rest, now=NOW), [])
        self.assertEqual(len(told), 1)
        # The parent did push it before the tick: done, nobody is told.
        pusher.fallback(self.path, self.tmp, "d" * 40, "warp/y", "x", coordinator="coord-ui", run=run)
        rest_head["sha"] = "d" * 40
        done = pusher.check_pending(self.path, self.tmp, send, runner=rest, now=NOW)
        self.assertIn("push: warp/y is at dddddddddddd; pushed by the parent", done)


if __name__ == "__main__":
    unittest.main()
