"""1.7.21: Bugbot reviews whole fix rounds; no done without a product commit; the hourly full audit; Slack `message`."""

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

import bugbot  # noqa: E402
import fullaudit  # noqa: E402
import herald_fmt  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import productdiff  # noqa: E402
import ticket_state  # noqa: E402

NOW = "2026-11-10T09:30:00Z"
URL = "https://github.com/o/r/pull/%d"


def git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


class BugbotRoundTest(unittest.TestCase):
    def ticket(self):
        return {"id": "UI-7", "status": "review", "phase": "reviewing", "locks": ["src/x"], "size": "S",
                "pr": {"url": URL % 7, "headSha": "h1", "bugbot": "fail", "bugbotSha": "h1", "ci": "green", "ciSha": "h1",
                       "bugbotFindings": ["a.py:3 null deref", "b.py:9 race", "c.py:1 leak"], "bugbotFindingsSha": "h1"}}

    def tearDown(self):
        bugbot.THREADS = None

    def test_all_findings_go_to_one_round_and_the_next_review_waits_for_all_of_them(self):
        data = {"tickets": {}, "config": {"bugbotTrigger": "comment"}, "_live": True}
        ticket = self.ticket()
        data["tickets"]["UI-7"] = ticket
        lines = []
        reasons = pipeline.red_reasons(ticket, data["config"])
        pipeline.begin_fix(data, ticket, pipeline.batched(reasons), lines)
        rnd = ticket["pr"]["bugbotRound"]
        self.assertEqual(len(rnd["findings"]), 3)
        self.assertTrue(any("BUGBOT: fix every Bugbot finding below before you push" in str(r)
                            for r in (ticket.get("shuttle") or {}).get("detail", "").split(" | ")) or
                        "never push one finding at a time" in json.dumps(ticket), json.dumps(ticket)[:400])
        # The fix pushed h2 but addressed only one finding: no review; back to fix with the two left.
        threads = [{"id": 1, "commit": "h1", "line": "a.py:3 null deref", "resolved": True},
                   {"id": 2, "commit": "h1", "line": "b.py:9 race", "resolved": False},
                   {"id": 3, "commit": "h1", "line": "c.py:1 leak", "resolved": False}]
        bugbot.THREADS = lambda root, url: threads
        ticket["pr"]["headSha"] = "h2"
        ticket.pop("shuttle", None)
        ticket["status"], ticket["phase"] = "review", "reviewing"
        lines = []
        pipeline._request_bugbot(ticket, lines, data, now=NOW)
        text = "\n".join(lines)
        self.assertIn("bugbot: hold UI-7: 2 of 3 findings from h1 are still open on h2", text)
        self.assertNotIn("bugbot: request UI-7", text)
        self.assertNotIn("bugbot: comment UI-7", text)
        self.assertEqual(ticket["status"], "fix")
        # All addressed (resolved, outdated, or "Fixed in"): the next review is asked for.
        threads[1]["outdated"] = True
        threads[2]["replied"] = True
        ticket["pr"]["headSha"] = "h3"
        ticket.pop("shuttle", None)
        ticket["status"], ticket["phase"] = "review", "reviewing"
        lines = []
        pipeline._request_bugbot(ticket, lines, data, now=NOW)
        text = "\n".join(lines)
        self.assertIn("bugbot: all 3 findings from h1 are addressed on h3; asking for the next review", text)
        self.assertIn("bugbot: request UI-7", text)

    def test_the_manual_command_is_review_bugbot(self):
        self.assertEqual(bugbot.manual_command({}, URL % 1537), "/review-bugbot 1537")
        self.assertEqual(bugbot.manual_command({"config": {"bugbotManualCommand": "/bb {url}"}}, URL % 1), "/bb " + URL % 1)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = self.tmp / "vm"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "app.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "-b", "warp/core/G-INSTALL")

    def state(self, tid, state, step="", product=False):
        if product:
            (self.repo / "app.py").write_text("x = %d\n" % (len((self.repo / "app.py").read_text()) + 1))
            git(self.repo, "commit", "-qam", "product change")
        old = Path.cwd()
        import os

        os.chdir(self.repo)
        try:
            code = ticket_state.append_status(self.repo, tid, state, step=step, push=False)
        finally:
            os.chdir(old)
        return code, json.loads((self.repo / ".warp" / "tickets" / tid / "state.json").read_text())


class NoProductTest(RepoCase):
    def test_done_without_a_product_commit_is_refused_and_recorded_failed(self):
        import contextlib
        import io
        import os

        # A local Shuttle in the clone itself counts as its own checkout when run with --push; here, a worktree of root.
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt), "-b", "warp/core/T-1")
        cwd = Path.cwd()
        os.chdir(wt)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ticket_state.append_status(self.repo, "T-1", "started", step="restart")
                ticket_state.append_status(self.repo, "T-1", "coding")
                code = ticket_state.append_status(self.repo, "T-1", "done", step="restart")
        finally:
            os.chdir(cwd)
        body = json.loads((self.repo / ".warp" / "tickets" / "T-1" / "state.json").read_text())
        self.assertEqual(code, 3)
        self.assertEqual((body["state"], body["alarm"]), ("failed", "no-product-commit"))
        self.assertIn("no product commit", body["error"])
        self.assertIn("refuse: done T-1", buf.getvalue())

    def test_done_with_a_product_commit_this_turn_stands_and_review_is_exempt(self):
        import contextlib
        import io
        import os

        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt), "-b", "warp/core/T-2")
        cwd = Path.cwd()
        os.chdir(wt)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ticket_state.append_status(self.repo, "T-2", "started", step="fix")
                (wt / "app.py").write_text("x = 2\n")
                git(wt, "commit", "-qam", "fix")
                self.assertEqual(ticket_state.append_status(self.repo, "T-2", "done", step="fix"), 0)
                ticket_state.append_status(self.repo, "T-3", "started", step="review")
                self.assertEqual(ticket_state.append_status(self.repo, "T-3", "done", step="review"), 0)
        finally:
            os.chdir(cwd)

    def test_the_parent_sees_the_one_second_sprint_and_retries(self):
        # The #1537 shape: the branch has earlier product work, the turn wrote four states in one second.
        (self.repo / "app.py").write_text("x = 9\n")
        git(self.repo, "commit", "-qam", "earlier work")
        git(self.repo, "push", "-q", "origin", "warp/core/G-INSTALL")
        rows = [{"state": s, "at": "2026-11-10T08:43:00Z", "seq": i} for i, s in enumerate(["started", "coding", "pr-opened", "done"], 1)]
        why = productdiff.branch_verdict(self.repo, "warp/core/G-INSTALL", "main", state={}, rows=rows, step="restart")
        self.assertIn("wrote started, coding, pr-opened and done", why)
        # Only .warp/ on the branch: refused whatever the timing.
        git(self.repo, "checkout", "-q", "-b", "warp/core/EMPTY", "main")
        (self.repo / ".warp").mkdir(exist_ok=True)
        (self.repo / ".warp" / "x.json").write_text("{}")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "state only")
        git(self.repo, "push", "-q", "origin", "warp/core/EMPTY")
        self.assertIn("changes nothing outside .warp/", productdiff.branch_verdict(self.repo, "warp/core/EMPTY", "main"))
        self.assertEqual(productdiff.branch_verdict(self.repo, "warp/core/EMPTY", "main", step="verify"), "")

    def test_retry_goes_back_with_the_restart_rule(self):
        data = {"tickets": {}, "config": {}}
        ticket = {"id": "G-1", "status": "review", "locks": ["src"], "size": "S", "pr": {"url": URL % 1537, "headSha": "h"}}
        data["tickets"]["G-1"] = ticket
        lines = []
        pipeline.no_product_retry(data, ticket, "its 0s turn wrote started, coding, pr-opened and done", lines)
        self.assertEqual(ticket["status"], "fix")
        self.assertIn("never write done without at least one commit", ticket["shuttle"]["detail"])
        self.assertTrue(lines[0].startswith("shuttle: retry G-1"))
        for _ in range(3):
            pipeline.no_product_retry(data, ticket, "again", [])
        self.assertEqual((ticket["status"], ticket["alarm"]), ("alarm", "no-product-commit"))

    def test_the_restart_prompt_spells_out_the_rules(self):
        ticket = {"id": "G-1", "locks": ["src"], "pr": {"url": URL % 1537}, "shuttle": {"step": "restart"}}
        text = "\n".join(orchestrator.step_lines(ticket))
        for part in ("IS NOT DONE", "(a) fetch origin", "(b) run the check command", "(c) fix every red check",
                     "(d) NEVER write done without at least one commit"):
            self.assertIn(part, text)
        done = orchestrator.done_lines(ticket, "python3 x/ticket_state.py append --id G-1 --state done --agent a --push", push=True)
        self.assertTrue(any("--state done --agent a --push --step restart" in l for l in done), done)


class FullAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.path = self.tmp / ".warp" / "beam.json"

    def test_every_ticket_is_checked_fixed_and_listed(self):
        tickets = {
            "M-1": {"id": "M-1", "status": "review", "pr": {"url": URL % 1, "headSha": "a"}},
            "S-1": {"id": "S-1", "status": "coding", "locks": ["src/s"], "size": "S", "pr": {"url": URL % 2, "headSha": "b"},
                    "updatedAt": "2026-11-10T07:00:00Z"},
            "B-1": {"id": "B-1", "status": "review", "phase": "reviewing", "locks": ["src/b"], "size": "S",
                    "pr": {"url": URL % 3, "headSha": "c", "ci": "green", "ciSha": "c"}},
            "Q-1": {"id": "Q-1", "status": "queued", "deps": ["Q-0"]},
            "Q-0": {"id": "Q-0", "status": "queued"},
            "P-1": {"id": "P-1", "status": "parked", "parkReason": "waits on M-1", "pr": {"url": URL % 4}},
            "X-1": {"id": "X-1", "status": "coding", "locks": ["src/x"], "pr": {"url": URL % 5, "headSha": "e"},
                    "updatedAt": "2026-11-10T07:00:00Z", "externalOwner": "coord-e49"},
            "D-1": {"id": "D-1", "status": "merged"},
        }
        self.path.write_text(json.dumps({"version": 1, "runState": "running", "config": {"bugbotTrigger": "comment"},
                                         "tickets": tickets}))
        snaps = {"M-1": {"prState": "MERGED", "mergeSha": "m" * 40}, "S-1": {"prState": "OPEN"},
                 "B-1": {"prState": "OPEN", "headSha": "c", "ci": "green", "bugbot": "absent"},
                 "P-1": {"prState": "OPEN"}, "X-1": {"prState": "OPEN"}}
        lines = fullaudit.run(self.path, now=NOW, snaps=snaps, live=lambda t: False)
        text = "\n".join(lines)
        after = json.loads(self.path.read_text())["tickets"]
        self.assertEqual(after["M-1"]["status"], "merged")
        self.assertEqual(after["S-1"]["status"], "fix")  # restarted from the remote head
        self.assertIn("bugbot: MUST DO B-1: Bugbot has not run on #3 (head c). Run `/review-bugbot 3` now.", text)
        self.assertEqual(after["X-1"]["status"], "coding")  # owned elsewhere: told, not worked
        self.assertIn("relay:", text)
        self.assertTrue(any(l.startswith("fullaudit: Full audit: 7 tickets not finished") for l in lines), lines[-3:])
        report = sorted((self.tmp / ".warp" / "fullaudit").glob("*.md"))[-1].read_text()
        for tid in ("M-1", "S-1", "B-1", "Q-1", "Q-0", "P-1", "X-1"):
            self.assertIn("| %s |" % tid, report)
        self.assertIn("waits on Q-0", report)
        self.assertNotIn("| D-1 |", report)

    def test_cadence_is_hourly(self):
        import selfdrive

        self.assertEqual(selfdrive.DEFAULTS["cadence"]["fullAudit"], 3600)
        self.assertIn("fullAudit", selfdrive.CADENCES)


class SlackArgumentTest(unittest.TestCase):
    def test_slack_send_message_takes_message_not_text(self):
        view = herald_fmt.render("Warp | r", "Claim API-01", facts=[("size", "M")])["slack"]
        args = herald_fmt.slack_arguments("C123", view)
        self.assertEqual(sorted(args), ["channel_id", "message"])
        self.assertIn("Claim API-01", args["message"])
        self.assertNotIn("text", args)


class BugbotLadderTest(unittest.TestCase):
    """1.7.21: when Warp asks for Bugbot: `bugbot run` x2 -> `/review-bugbot` -> Warp's AI review, 20 minutes a rung; a
    clean AI review posts its findings, approves and merges."""

    def setUp(self):
        import aireview

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        (self.tmp / ".warp").mkdir()
        self.calls = []

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def run(cmd):
            self.calls.append(cmd)
            return Done()

        aireview.RUN = run
        self.addCleanup(setattr, aireview, "RUN", None)
        self.head = "c" * 40

    def data(self, trigger, **pr):
        (self.tmp / ".warp" / "config.yaml").write_text("bugbotRequired: true\nbugbotTrigger: %s\n" % trigger)
        base = {"url": URL % 9, "headSha": self.head, "rollup": "green", "ci": "green", "ciSha": self.head,
                "bugbotRequested": True, "bugbotRequestedSha": self.head, "bugbotRequestedAt": "2026-11-10T10:00:00Z",
                "acResults": {"AC-1": "pass"}}
        base.update(pr)
        ticket = {"id": "AI-1", "status": "review", "phase": "reviewing", "size": "L", "autoMerge": False, "locks": ["src"],
                  "branch": "warp/x/AI-1", "pr": base}
        data = {"version": 1, "runState": "running", "config": {"bugbotTrigger": trigger, "runner": "local"},
                "tickets": {"AI-1": ticket}, "_repoRoot": str(self.tmp)}
        (self.tmp / ".warp" / "beam.json").write_text(json.dumps(data))
        return data, ticket

    def test_manual_mode_asks_review_bugbot_at_once_then_hands_over_to_ai(self):
        data, ticket = self.data("manual")
        lines = []
        bugbot.ask_manual(data, ticket, lines)
        self.assertTrue(any("Run `/review-bugbot 9`" in l for l in lines), lines)
        ticket["pr"]["bugbotCommandAt"] = "2026-11-10T10:00:00Z"
        lines = []
        self.assertFalse(bugbot.check_silence(data, ticket, lines, now="2026-11-10T10:15:00Z"))
        self.assertTrue(bugbot.check_silence(data, ticket, lines, now="2026-11-10T10:21:00Z"))
        self.assertTrue(lines[0].startswith("bugbot: ai-review AI-1"), lines)
        self.assertTrue(ticket["pr"]["aiReview"])

    def test_ai_review_starts_its_shuttle_and_a_clean_head_is_posted_approved_and_merged(self):
        import aireview

        data, ticket = self.data("comment", aiReview=True, aiReviewWhy="no Bugbot after 2 `bugbot run` comments and `/review-bugbot 9`")
        lines = []
        pipeline._advance_one(data, ticket, None, lines, [], now="2026-11-10T12:00:00Z")
        self.assertTrue(any(l.startswith("start AI-1") and "step=aireview" in l for l in lines), lines)
        self.assertIn("bugbot: ai-review-shuttle AI-1 1/2", lines)
        self.assertFalse(any(l.startswith("bugbot: comment") for l in lines), "no `bugbot run` once on AI review")
        prompt = "\n".join(orchestrator.review_lines(ticket, data["config"], str(self.tmp)))
        self.assertIn("--source ai", prompt)
        self.assertIn("repeat steps 2-3 until a pass finds nothing", prompt)
        # The AI review fixed two findings and passed the final head.
        ticket.pop("shuttle", None)
        bugbot.record_result(ticket, "pass", ["src/a.py:3 null deref (fixed in c1)", "src/b.py:9 race (fixed in c1)"],
                             now="2026-11-10T12:30:00Z", source="ai", sha=self.head)
        lines = []
        pipeline._advance_one(data, ticket, None, lines, [], now="2026-11-10T12:31:00Z")
        text = "\n".join(lines)
        self.assertIn("bugbot: ai-review clean AI-1 on %s; approving and merging" % self.head[:12], text)
        comment = [c for c in self.calls if c[:3] == ["gh", "pr", "comment"]]
        approve = [c for c in self.calls if c[:3] == ["gh", "pr", "review"] and "--approve" in c]
        self.assertEqual(len(comment), 1)
        self.assertIn("src/a.py:3 null deref (fixed in c1)", comment[0][-1])
        self.assertEqual(len(approve), 1)
        self.assertEqual(ticket["pr"]["approvedBy"], aireview.APPROVER)
        self.assertEqual((ticket["status"], ticket["phase"]), ("review", "ready"), "approved: queued to merge, not waiting")
        self.assertNotIn("approve: wait AI-1", text)
        # Once per head.
        self.calls.clear()
        pipeline._advance_one(data, ticket, None, [], [], now="2026-11-10T12:32:00Z")
        self.assertEqual(self.calls, [])

    def test_a_failed_ai_review_goes_to_one_fix_round(self):
        data, ticket = self.data("comment", aiReview=True)
        bugbot.record_result(ticket, "fail", ["src/a.py:3 still wrong"], now="2026-11-10T12:30:00Z", source="ai", sha=self.head)
        reasons = pipeline.red_reasons(ticket, data["config"])
        self.assertIn("bugbot: src/a.py:3 still wrong", reasons)
        self.assertEqual(ticket["pr"]["aiReviewLog"][-1]["result"], "fail")

    def test_the_ai_step_is_exempt_from_the_product_commit_rule(self):
        self.assertIn("aireview", productdiff.NO_CODE_STEPS)


class GateOrderTest(unittest.TestCase):
    """1.7.21: work is pulled gate by gate, lowest first, in plan order; `/warp-start all` pulls all available work."""

    def beam(self, **extra):
        import test_blocks

        data = test_blocks.plan_beam(**extra)
        data["tickets"]["A-2"]["status"] = "merged"  # G0 cleared: G1 is the open gate
        data["tickets"]["B-1"].update(rank=5, planned={"start": 3.0})
        data["tickets"]["B-2"].update(rank=2, planned={"start": 9.0})
        data["tickets"]["B-3"] = dict(data["tickets"]["B-1"], id="B-3", rank=5, planned={"start": 1.0}, locks=["src/b3"])
        data["gates"][1]["members"].append("B-3")
        return data

    def ids(self, data):
        import beam as beam_mod

        return [row["id"] for row in beam_mod.ready(data)]

    def test_a_build_schedule_no_longer_starts_every_gate_at_once(self):
        import blocks

        data = self.beam(program={"criticalPath": [], "gateOrder": "deps", "schedule": {"gateOrder": "deps"}})
        self.assertEqual(blocks.order_mode(data), "strict")
        # Only G1 (the lowest gate not cleared), in plan order: rank, then the planned start.
        self.assertEqual(self.ids(data), ["B-2", "B-3", "B-1"])
        self.assertIn("order: gates in order (default). G1 is open", blocks.order_line(data))

    def test_a_run_started_before_carries_no_deps_over(self):
        import blocks

        data = self.beam(gateOrder="deps", program={"criticalPath": [], "gateOrder": "deps"})
        self.assertEqual(self.ids(data), ["B-2", "B-3", "B-1"])
        data["config"]["gateOrder"] = "deps"
        self.assertEqual(blocks.order_mode(data), "deps", "config still decides")

    def test_pull_all_takes_every_gate_lowest_first_and_ordered_goes_back(self):
        import blocks

        data = self.beam()
        lines = blocks.set_order(data, "all")
        self.assertTrue(lines[0].startswith("order: all available work."), lines)
        self.assertTrue(data["pullAll"])
        self.assertEqual(self.ids(data), ["B-2", "B-3", "B-1", "C-1", "D-1"])
        blocks.set_order(data, "ordered")
        self.assertNotIn("pullAll", data)
        self.assertEqual(self.ids(data), ["B-2", "B-3", "B-1"])
        self.assertTrue(blocks.set_order(data, "sideways")[0].startswith("refuse: "))

    def test_start_all_and_resume_keep_it_and_a_plain_start_goes_back(self):
        import contextlib
        import io
        from unittest import mock

        import scan

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "config.yaml").write_text("runner: local\n")
        path = tmp / ".warp" / "beam.json"
        path.write_text(json.dumps(self.beam(runState="stopped", paused=True)))

        def go(gates, verb="start"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
                code = scan.go(path, None, force=True, gates=gates, verb=verb)
            return code, buf.getvalue(), json.loads(path.read_text())

        code, out, saved = go("all")
        self.assertEqual(code, 0, out)
        self.assertEqual((saved["gateOrder"], saved.get("pullAll")), ("deps", True))
        self.assertIn("order: all available work.", out)
        code, out, saved = go("", verb="resume")
        self.assertEqual(saved["gateOrder"], "deps", "a resume keeps this run's `all`")
        code, out, saved = go("")
        self.assertEqual((saved["gateOrder"], saved.get("pullAll")), ("strict", None))
        self.assertIn("order: gates in order (default).", out)


if __name__ == "__main__":
    unittest.main()
