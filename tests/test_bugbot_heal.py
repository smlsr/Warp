"""Bugbot that never starts, a repo with no CI, a leftover lock-escape, and resume healing alarms."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import bugbot  # noqa: E402
import heal  # noqa: E402
import inbound  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import ticket_state  # noqa: E402

SHA = "579fb1873f14055f8397c9ffac21b6c8109b2f4b"
URL = "https://github.com/smlsr/WarpDemo/pull/4"
LOCKS = ["app/package.json", "app/test/", "app/package-lock.json", "app/src/core/util.test.mjs"]


def row(tid="WV-01", **extra):
    ticket = {
        "id": tid,
        "status": "review",
        "phase": "reviewing",
        "size": "S",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "deps": [],
        "autoMerge": True,
        "locks": list(LOCKS),
        "branch": "warp/view/%s-WAR-47" % tid,
        "attempts": 0,
        "alarm": None,
        "pr": {"url": URL, "headSha": SHA, "bugbotRequested": True, "bugbotRequestedSha": SHA,
               "bugbotRequestedAt": "2026-10-13T19:01:17Z", "rollup": "absent", "ci": "absent"},
        "plan": {},
    }
    ticket.update(extra)
    return ticket


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        self.warp = self.tmp / ".warp"
        self.warp.mkdir()
        self.config = self.warp / "config.yaml"
        self.config.write_text("bugbotRequired: true\nmaxAgents: 4\n")
        self.beam_path = self.warp / "beam.json"

    def write(self, tickets, **config):
        cfg = {"runner": "local", "maxAgents": 4, "jiraTransition": False, "messenger": "teams"}
        cfg.update(config)
        data = {"version": 1, "runState": "running", "paused": False, "config": cfg,
                "tickets": {t["id"]: t for t in tickets}, "gates": [], "program": {"criticalPath": []}}
        self.beam_path.write_text(json.dumps(data, indent=2) + "\n")
        return data

    def load(self):
        return json.loads(self.beam_path.read_text())

    def data(self, tickets, **config):
        data = self.write(tickets, **config)
        data["_repoRoot"] = str(self.tmp)
        return data


class BugbotTriggerTest(Repo):
    def test_auto_switches_to_comment_when_bugbot_never_starts(self):
        data = self.data([row()])
        ticket = data["tickets"]["WV-01"]
        lines = []
        # Inside the start window: nothing yet.
        self.assertFalse(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:05:00Z"))
        self.assertEqual(lines, [])
        self.assertTrue(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:20:00Z"))
        self.assertTrue(lines[0].startswith("config: bugbotTrigger comment. Bugbot did not start on its own for WV-01"))
        self.assertTrue(lines[1].startswith("herald: Bugbot did not start on its own on WV-01."), lines)
        self.assertEqual(lines[-1], "bugbot: comment WV-01 %s" % URL)
        self.assertIn("bugbotTrigger: comment", self.config.read_text())
        self.assertEqual(data["config"]["bugbotTrigger"], "comment")
        self.assertEqual(bugbot.trigger(data), "comment")

    def test_answered_bugbot_is_left_alone(self):
        for state in ("pending", "pass", "fail"):
            ticket = row()
            ticket["pr"].update({"bugbot": state, "bugbotSha": SHA})
            data = self.data([ticket])
            lines = []
            self.assertFalse(bugbot.check_silence(data, data["tickets"]["WV-01"], lines, now="2026-10-13T21:00:00Z"))
            self.assertEqual(lines, [])

    def test_comment_mode_asks_on_request_then_twice_then_switches_to_manual(self):
        self.config.write_text("bugbotRequired: true\nbugbotTrigger: comment\n")
        data = self.data([row()])
        ticket = data["tickets"]["WV-01"]
        lines = []
        bugbot.on_request(data, ticket, lines)
        self.assertEqual(lines, ["bugbot: comment WV-01 %s" % URL])
        bugbot.record(ticket, now="2026-10-13T19:02:00Z")
        self.assertEqual(bugbot.comments_on_head(ticket["pr"]), 1)
        lines = []
        bugbot.on_request(data, ticket, lines)
        self.assertEqual(lines, [], "one comment per head on request")
        self.assertFalse(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:05:00Z"))
        self.assertTrue(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:13:00Z"))
        self.assertEqual(lines, ["bugbot: comment WV-01 %s" % URL])
        bugbot.record(ticket, now="2026-10-13T19:13:00Z")
        lines = []
        self.assertTrue(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:30:00Z"))
        self.assertTrue(lines[0].startswith("config: bugbotTrigger manual. Bugbot did not answer 2 `bugbot run` comments"), lines)
        self.assertIn("not listening on smlsr/WarpDemo", lines[0])
        self.assertIn("bugbot: silent WV-01", lines)
        self.assertIn("bugbot: manual WV-01 %s branch=warp/view/WV-01-WAR-47" % URL, lines)
        self.assertTrue(any(line.startswith("bugbot: MUST DO WV-01: Bugbot cannot be started from here") for line in lines))
        self.assertIn("bugbotTrigger: manual", self.config.read_text())
        self.assertEqual(bugbot.trigger(data), "manual")
        lines = []
        self.assertFalse(bugbot.check_silence(data, ticket, lines, now="2026-10-13T19:45:00Z"))
        self.assertEqual(lines, [], "manual asks once per head")

    def test_manual_mode_asks_at_once_and_once_per_head(self):
        self.config.write_text("bugbotRequired: true\nbugbotTrigger: manual\nbugbotManual: true\n")
        data = self.data([row()])
        ticket = data["tickets"]["WV-01"]
        lines = []
        bugbot.on_request(data, ticket, lines)
        self.assertEqual(lines[0], "bugbot: manual WV-01 %s branch=warp/view/WV-01-WAR-47" % URL)
        self.assertIn("bugbot.py record --beam", lines[1])
        self.assertTrue(lines[2].startswith("herald: WV-01 needs a Bugbot review by hand"))
        self.assertFalse(any(line.startswith("bugbot: comment") for line in lines))
        again = []
        bugbot.on_request(data, ticket, again)
        self.assertFalse(bugbot.check_silence(data, ticket, again, now="2026-10-13T23:00:00Z"))
        self.assertEqual(again, [])
        # A new head asks again, with no wait.
        ticket["pr"]["headSha"] = "b" * 40
        ticket["pr"]["bugbotRequestedSha"] = "b" * 40
        later = []
        self.assertTrue(bugbot.check_silence(data, ticket, later, now="2026-10-13T19:01:30Z"))
        self.assertTrue(later[0].startswith("bugbot: manual WV-01"))

    def test_bugbot_manual_does_not_choose_the_trigger(self):
        for manual in ("true", "false"):
            self.config.write_text("bugbotManual: %s\n" % manual)
            self.assertEqual(bugbot.trigger(self.data([row()])), "auto")
        self.assertTrue(orchestrator.bugbot_applies({"autoMerge": False}, {"bugbotManual": True}))
        self.assertFalse(orchestrator.bugbot_applies({"autoMerge": False}, {"bugbotManual": False}))
        self.assertTrue(orchestrator.bugbot_applies({"autoMerge": True}, {"bugbotManual": False}))

    def test_record_by_hand_survives_a_provider_read_with_no_bugbot_check(self):
        self.config.write_text("bugbotTrigger: manual\n")
        self.write([row()])
        out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "bugbot.py"), "record", "--beam", str(self.beam_path),
                              "--id", "WV-01", "--result", "pass", "--evidence", "reviewed in Cursor"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(out.stdout.startswith("bugbot: recorded WV-01 pass on 579fb1873f14"), out.stdout)
        ticket = self.load()["tickets"]["WV-01"]
        self.assertEqual((ticket["pr"]["bugbot"], ticket["pr"]["bugbotSource"]), ("pass", "hand"))
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "bugbot": "", "findings": [], "rollup": "absent"})
        self.assertEqual(pipeline.bugbot_state(ticket), "pass")
        self.assertTrue(bugbot.answered(ticket))
        # A new head is a new review.
        pipeline._apply_snapshot(ticket, {"headSha": "c" * 40, "bugbot": ""})
        self.assertEqual(pipeline.bugbot_state(ticket), "")

    def test_record_fail_sends_findings_to_fix(self):
        self.write([row()])
        out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "bugbot.py"), "record", "--beam", str(self.beam_path),
                              "--id", "WV-01", "--result", "fail", "--finding", "null check in util.mjs"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        ticket = self.load()["tickets"]["WV-01"]
        self.assertIn("bugbot fail", pipeline.red_reasons(ticket, {}))
        self.assertIn("bugbot: null check in util.mjs", pipeline.red_reasons(ticket, {}))

    def test_a_new_head_starts_the_count_again(self):
        ticket = row()
        ticket["pr"].update({"bugbotCommentSha": "old", "bugbotComments": 2})
        self.assertEqual(bugbot.comments_on_head(ticket["pr"]), 0)

    def test_post_pending_posts_and_records(self):
        data = self.data([row()])
        calls = []

        def ok(cmd):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/x#issuecomment-1\n", stderr="")

        got = bugbot.post_pending(data, self.beam_path, ["bugbot: comment WV-01 %s" % URL, "bugbot: comment WV-01 %s" % URL],
                                  runner=ok, now="2026-10-13T19:20:00Z")
        self.assertEqual(got, ["bugbot: commented WV-01 %s" % URL])
        self.assertEqual(calls, [["gh", "pr", "comment", URL, "--body", "bugbot run"]])
        pr = data["tickets"]["WV-01"]["pr"]
        self.assertEqual((pr["bugbotComments"], pr["bugbotCommentSha"], pr["bugbotCommentAt"]), (1, SHA, "2026-10-13T19:20:00Z"))

        def fail(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: not logged in\n")

        got = bugbot.post_pending(data, self.beam_path, ["bugbot: comment WV-01 %s" % URL], runner=fail)
        self.assertTrue(got[0].startswith("bugbot: MUST DO post a pull request comment with the body `bugbot run` on %s (gh: not logged in)" % URL))
        self.assertIn("bugbot.py run --beam", got[0])
        self.assertIn("--id WV-01 --posted", got[0])

    def test_a_posted_comment_is_on_disk_even_if_the_pass_is_dropped(self):
        data = self.data([row()])

        def ok(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        bugbot.post_pending(data, self.beam_path, ["bugbot: comment WV-01 %s" % URL], runner=ok, now="2026-10-13T19:20:00Z")
        # The in-memory beam is thrown away (an aborted pass). The disk still has the comment.
        pr = self.load()["tickets"]["WV-01"]["pr"]
        self.assertEqual((pr["bugbotComments"], pr["bugbotCommentSha"]), (1, SHA))
        self.assertNotIn("bugbotCommentPending", pr)

    def test_supervise_does_not_post_when_a_halt_lands_during_the_pass(self):
        import agents

        self.write([row()])
        posted = []
        snap = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}}
        saved = (pipeline.live_provider, bugbot.post, agents.halted_meanwhile)

        def halted(data, path):
            disk = json.loads(Path(path).read_text())
            disk["runState"] = "paused"
            disk["paused"] = True
            return disk

        try:
            pipeline.live_provider = lambda data, path: snap
            bugbot.post = lambda root, url, runner=None: (posted.append(url), (True, ""))[1]
            agents.halted_meanwhile = halted
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T19:30:00Z")
        finally:
            pipeline.live_provider, bugbot.post, agents.halted_meanwhile = saved
        self.assertIn("pass: aborted. The run was paused during this pass.", lines)
        self.assertEqual(posted, [])
        # Not posted, so nothing is recorded: the next pass asks again.
        self.assertNotIn("bugbotComments", self.load()["tickets"]["WV-01"]["pr"])

    def test_supervise_posts_and_saves_on_a_normal_pass(self):
        self.write([row()])
        posted = []
        snap = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}}
        saved = (pipeline.live_provider, bugbot.post)
        try:
            pipeline.live_provider = lambda data, path: snap
            bugbot.post = lambda root, url, runner=None: (posted.append(url), (True, ""))[1]
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T19:30:00Z")
        finally:
            pipeline.live_provider, bugbot.post = saved
        self.assertEqual(posted, [URL])
        self.assertIn("bugbot: commented WV-01 %s" % URL, lines)
        pr = self.load()["tickets"]["WV-01"]["pr"]
        self.assertEqual(pr["bugbotComments"], 1)
        again = []
        try:
            pipeline.live_provider = lambda data, path: snap
            bugbot.post = lambda root, url, runner=None: (again.append(url), (True, ""))[1]
            orchestrator.supervise(self.beam_path, now="2026-10-13T19:32:00Z")
        finally:
            pipeline.live_provider, bugbot.post = saved
        self.assertEqual(again, [], "no second comment inside the start window")

    def test_cli_records_a_comment_posted_another_way(self):
        self.write([row()])
        out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "bugbot.py"), "run", "--beam", str(self.beam_path),
                              "--id", "WV-01", "--posted"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "bugbot: commented WV-01 %s" % URL)
        self.assertEqual(self.load()["tickets"]["WV-01"]["pr"]["bugbotComments"], 1)

    def test_supervise_pass_asks_by_comment(self):
        self.write([row()])
        provider = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}}
        lines = orchestrator.supervise(self.beam_path, now="2026-10-13T19:30:00Z", provider=provider)
        self.assertIn("bugbot: comment WV-01 %s" % URL, lines)
        self.assertFalse(any(line.startswith("ci: start") for line in lines), lines)
        saved = self.load()
        self.assertEqual(saved["config"]["bugbotTrigger"], "comment")
        self.assertEqual(saved["tickets"]["WV-01"]["pr"]["bugbotCommentPending"], SHA)


SNAP = {"WV-01": {"headSha": SHA, "rollup": "absent", "checkCount": 0, "ciConfigured": False,
                  "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}}


class ShuttleReviewTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("bugbotRequired: true\nbugbotTrigger: shuttle\n")

    def test_trigger_reads_shuttle(self):
        self.assertEqual(bugbot.trigger(self.data([row()])), "shuttle")

    def test_the_working_shuttle_is_told_to_review_its_own_diff(self):
        ticket = row(shuttle={"step": "implement", "pending": True})
        text = orchestrator.subagent_prompt(ticket, {"bugbotTrigger": "shuttle"}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("bugbotTrigger: shuttle. You run the Bugbot review.", text)
        self.assertIn("ticket_state.py append --root /repo --id WV-01 --state review-pass|review-fail --sha <head sha>", text)
        self.assertNotIn("REVIEW ONLY", text)
        plain = orchestrator.subagent_prompt(ticket, {"bugbotTrigger": "auto"}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertNotIn("review-pass", plain)
        vm = orchestrator.subagent_vm_prompt(ticket, {"bugbotTrigger": "shuttle"}, "warp/WV-01")
        self.assertIn("ticket_state.py append --push --id WV-01 --state review-pass|review-fail", vm)

    def test_a_review_step_reviews_only(self):
        ticket = row(shuttle={"step": "review", "pending": True})
        text = orchestrator.subagent_prompt(ticket, {"bugbotTrigger": "shuttle"}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("REVIEW ONLY. bugbotTrigger: shuttle. Do not change code, commit, or push.", text)
        self.assertIn("result: WV-01 ok review=pass|fail findings=<n>", text)

    def test_a_review_line_in_the_ticket_folder_records_the_result(self):
        ticket = row()
        self.write([ticket])
        lines, heralded = [], set()
        event = {"state": "review-fail", "sha": SHA[:12], "findings": ["util.mjs:12 null deref"], "source": "shuttle",
                 "at": "2026-10-13T20:00:00Z"}
        self.assertTrue(ticket_state._apply_event(self.beam_path, {}, ticket, event, "2026-10-13T20:00:00Z", heralded, lines))
        self.assertEqual((ticket["pr"]["bugbot"], ticket["pr"]["bugbotSha"]), ("fail", SHA))
        self.assertEqual(ticket["pr"]["bugbotFindings"], ["util.mjs:12 null deref"])
        self.assertIn("herald: WV-01 Bugbot review by the Shuttle: 1 finding(s). Back to fix.", lines)
        self.assertIn("bugbot: util.mjs:12 null deref", pipeline.red_reasons(ticket, {}))
        # Same line again changes nothing. A review of another commit (one the Shuttle just pushed) is kept
        # without touching this head's result, and applies when the head reaches that commit (1.6.17).
        self.assertFalse(ticket_state._apply_event(self.beam_path, {}, ticket, event, "2026-10-13T20:01:00Z", heralded, []))
        newer = dict(event, state="review-pass", sha="d" * 40, findings=[])
        out = []
        self.assertTrue(ticket_state._apply_event(self.beam_path, {}, ticket, newer, "2026-10-13T20:02:00Z", heralded, out))
        self.assertTrue(out[0].startswith("bugbot: review for WV-01 kept for dddddddddddd; the head Warp has is"), out)
        self.assertEqual(ticket["pr"]["bugbot"], "fail")
        pipeline._apply_snapshot(ticket, {"headSha": "d" * 40})
        self.assertEqual((ticket["pr"]["bugbot"], ticket["pr"]["bugbotSha"]), ("pass", "d" * 40))
        self.assertNotIn("reviewForSha", ticket["pr"])

    def test_append_writes_the_review_into_the_folder(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with redirect_stdout(StringIO()):
            ticket_state.append_status(self.tmp, "WV-01", "review-pass", sha=SHA, source="shuttle")
        folder = ticket_state.ticket_dir(self.tmp, "WV-01")
        state = json.loads((folder / "state.json").read_text())
        self.assertEqual((state["review"]["result"], state["review"]["sha"]), ("pass", SHA))
        event = ticket_state._event_from_state(state)
        self.assertEqual((event["state"], event["sha"]), ("review-pass", SHA))
        line = json.loads((folder / "log.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["state"], line["sha"], line["source"]), ("review-pass", SHA, "shuttle"))

    def run_pass(self, now):
        saved = pipeline.live_provider
        try:
            pipeline.live_provider = lambda data, path: SNAP
            return orchestrator.supervise(self.beam_path, now=now)
        finally:
            pipeline.live_provider = saved

    def test_work_done_with_no_review_starts_a_review_shuttle_twice_then_asks_by_hand(self):
        self.write([row()])
        lines = self.run_pass("2026-10-13T19:30:00Z")
        start = [line for line in lines if line.startswith("start WV-01 ")]
        self.assertEqual(len(start), 1, lines)
        self.assertIn("step=review", start[0])
        self.assertIn("bugbot: review-shuttle WV-01 1/2", lines)
        self.assertFalse(any(line.startswith("bugbot: comment") for line in lines))
        saved = self.load()["tickets"]["WV-01"]
        self.assertEqual((saved["shuttle"]["step"], saved["shuttle"]["pending"]), ("review", True))
        # It returned with nothing recorded: one more, then a person.
        for n, now in ((2, "2026-10-13T19:40:00Z"), (3, "2026-10-13T19:50:00Z")):
            data = self.load()
            data["tickets"]["WV-01"]["shuttle"]["pending"] = False
            data["tickets"]["WV-01"].setdefault("report", {})["returned"] = True
            self.beam_path.write_text(json.dumps(data))
            lines = self.run_pass(now)
            if n == 2:
                self.assertIn("bugbot: review-shuttle WV-01 2/2", lines)
            else:
                self.assertFalse(any(line.startswith("start WV-01 ") for line in lines), lines)
                self.assertTrue(any(line.startswith("bugbot: manual WV-01") for line in lines), lines)

    def test_a_recorded_pass_ends_the_review(self):
        ticket = row()
        ticket["pr"].update({"bugbot": "pass", "bugbotSha": SHA, "bugbotRecordedSha": SHA, "bugbotSource": "shuttle"})
        self.write([ticket])
        lines = self.run_pass("2026-10-13T19:30:00Z")
        self.assertFalse(any(line.startswith("start WV-01 ") for line in lines), lines)
        self.assertEqual(self.load()["tickets"]["WV-01"]["pr"]["bugbot"], "pass")

    def test_a_stuck_ticket_whose_work_is_done_gets_a_review_shuttle(self):
        ticket = row(status="alarm", alarm="stuck")
        ticket["pr"].update({"agentFinished": True, "opened": True})
        self.write([ticket])
        lines = self.run_pass("2026-10-13T19:30:00Z")
        self.assertIn("alarm: WV-01 stuck cleared; the work is done and only the review is missing", lines)
        self.assertTrue(any(line.startswith("start WV-01 ") and "step=review" in line for line in lines), lines)
        self.assertIsNone(self.load()["tickets"]["WV-01"]["alarm"])


class NoCiTest(unittest.TestCase):
    def test_a_repo_with_no_workflows_is_not_a_ci_that_never_started(self):
        pr = {"headAt": "2026-10-13T18:00:00Z"}
        pipeline._note_ci_absent(pr, {"rollup": "absent", "checkCount": 0, "ciConfigured": False}, "2026-10-13T19:00:00Z", {})
        self.assertFalse(pr["ciAbsent"])
        self.assertTrue(pr["ciNone"])
        pr = {"headAt": "2026-10-13T18:00:00Z"}
        pipeline._note_ci_absent(pr, {"rollup": "absent", "checkCount": 0, "ciConfigured": True}, "2026-10-13T19:00:00Z", {})
        self.assertTrue(pr["ciAbsent"])

    def test_checks_green_with_no_ci_needs_bugbot(self):
        ticket = row()
        cfg = {"bugbotRequired": True}
        self.assertFalse(orchestrator.checks_green(ticket, cfg))
        ticket["pr"]["ciNone"] = True
        self.assertFalse(orchestrator.checks_green(ticket, cfg))
        ticket["pr"].update({"bugbot": "pass", "bugbotSha": SHA})
        self.assertTrue(orchestrator.checks_green(ticket, cfg))
        ticket["pr"]["ciNone"] = False
        self.assertFalse(orchestrator.checks_green(ticket, cfg))

    def test_repo_has_ci(self):
        import provider

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertFalse(provider.repo_has_ci(tmp))
        (tmp / ".github" / "workflows").mkdir(parents=True)
        self.assertFalse(provider.repo_has_ci(tmp))
        (tmp / ".github" / "workflows" / "ci.yml").write_text("on: pull_request\n")
        self.assertTrue(provider.repo_has_ci(tmp))


class LeftoverLockEscapeTest(Repo):
    def test_a_later_state_clears_the_alarm_fields(self):
        root = self.tmp
        with open("/dev/null", "w") as quiet:
            sys_stdout = sys.stdout
            sys.stdout = quiet
            try:
                ticket_state.append_status(root, "WV-01", "lock-escape", escaped=["app/extra"])
                ticket_state.append_status(root, "WV-01", "review")
            finally:
                sys.stdout = sys_stdout
        state = json.loads((ticket_state.ticket_dir(root, "WV-01") / "state.json").read_text())
        self.assertEqual((state["state"], state["alarm"], state["escaped"]), ("review", "", []))

    def test_a_leftover_alarm_field_is_not_a_new_lock_escape(self):
        old = {"id": "WV-01", "state": "check-green", "alarm": "lock-escape", "escaped": ["app/extra"],
               "check": {"result": "green", "name": "npm test"}, "updatedAt": "2026-10-13T19:45:21Z"}
        self.assertEqual(ticket_state._event_from_state(old)["state"], "check-green")
        fresh = dict(old, state="lock-escape")
        self.assertEqual(ticket_state._event_from_state(fresh)["state"], "lock-escape")

    def test_paths_inside_the_locks_do_not_raise_it(self):
        ticket = row()
        self.write([ticket])
        self.assertFalse(ticket_state._apply_lock_escape(self.beam_path, ticket, ["app/package-lock.json", "app/test/a.mjs"], "2026-10-13T19:00:00Z"))
        self.assertEqual(ticket["status"], "review")
        self.assertTrue(ticket_state._apply_lock_escape(self.beam_path, ticket, ["app/package-lock.json", "app/other.mjs"], "2026-10-13T19:00:00Z"))
        self.assertEqual((ticket["status"], ticket["alarm"]), ("alarm", "lock-escape"))


class HealTest(Repo):
    def test_covered_lock_escape_goes_back_to_review(self):
        ticket = row(status="recovering", alarm="lock-escape", escaped=["app/package-lock.json"], alarmRepairs=2,
                     alarmRepair={"state": "working", "attempts": 2}, stalled=True,
                     shuttle={"pending": True, "step": "restart", "hadPr": True},
                     workerStartedAt="2026-10-13T19:44:17Z")
        self.write([ticket])
        data = self.load()
        data["alarmRepair"] = {"active": "WV-01", "tried": ["WV-01"]}
        self.beam_path.write_text(json.dumps(data))
        lines = heal.heal(self.beam_path, now="2026-10-13T20:00:00Z")
        self.assertEqual(lines, ["heal: WV-01 lock-escape cleared, back to review on %s." % URL])
        saved = self.load()
        got = saved["tickets"]["WV-01"]
        self.assertEqual((got["status"], got["phase"], got["alarm"], got["alarmRepairs"], got["stalled"]),
                         ("review", "reviewing", None, 0, False))
        self.assertFalse(got["shuttle"]["pending"])
        self.assertIsNone(saved["alarmRepair"]["active"])

    def test_a_live_shuttle_keeps_its_step_and_only_the_alarm_goes(self):
        self.write([row(status="recovering", alarm="lock-escape", escaped=["app/package-lock.json"],
                        shuttle={"pending": True, "step": "restart", "hadPr": True},
                        workerStartedAt="2026-10-13T19:58:00Z")])
        lines = heal.heal(self.beam_path, now="2026-10-13T20:00:00Z")
        self.assertEqual(lines, ["heal: WV-01 lock-escape cleared; its restart Shuttle is still out."])
        got = self.load()["tickets"]["WV-01"]
        self.assertEqual((got["status"], got["alarm"], got["shuttle"]["pending"]), ("recovering", None, True))

    def test_uncovered_paths_are_added_when_no_one_holds_them(self):
        self.write([row(status="alarm", alarm="lock-escape", escaped=["app/new.mjs"])])
        lines = heal.heal(self.beam_path)
        self.assertIn("Added app/new.mjs to its locks.", lines[0])
        self.assertIn("app/new.mjs", self.load()["tickets"]["WV-01"]["locks"])

    def test_held_paths_keep_the_alarm_and_reset_the_repair_count(self):
        holder = row("WV-02", status="coding", phase="implementing", locks=["app/new.mjs"], pr={})
        self.write([row(status="alarm", alarm="lock-escape", escaped=["app/new.mjs"], alarmRepairs=5,
                        alarmRepair={"gaveUp": True}), holder])
        lines = heal.heal(self.beam_path)
        self.assertIn("held by WV-02", lines[0])
        got = self.load()["tickets"]["WV-01"]
        self.assertEqual((got["status"], got["alarmRepairs"], got["alarmRepair"]["gaveUp"]), ("alarm", 0, False))

    def test_other_alarms_retry_and_gate_red_stays(self):
        self.write([
            row("WV-01", status="alarm", alarm="worker-died", attempts=4),
            row("WV-02", status="alarm", alarm="stuck", pr={}),
            row("WV-03", status="alarm", alarm="gate-red"),
            row("WV-04", status="parked", alarm="stalled"),
        ])
        lines = heal.heal(self.beam_path)
        text = "\n".join(lines)
        self.assertIn("heal: WV-01 retried: was alarm (worker-died)", text)
        self.assertIn("heal: WV-02 retried: was alarm (stuck)", text)
        self.assertIn("heal: WV-03 left in alarm (gate-red)", text)
        self.assertNotIn("WV-04", text)
        saved = self.load()["tickets"]
        self.assertEqual((saved["WV-01"]["status"], saved["WV-01"]["attempts"]), ("fix", 0))
        self.assertEqual(saved["WV-02"]["status"], "queued")
        self.assertEqual(saved["WV-03"]["status"], "alarm")
        self.assertEqual(saved["WV-04"]["status"], "parked")

    def test_resume_runs_heal(self):
        import scan

        self.write([row(status="alarm", alarm="lock-escape", escaped=["app/package-lock.json"])])
        data = self.load()
        data["runState"] = "stopped"
        data["paused"] = True
        self.beam_path.write_text(json.dumps(data))
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            scan.set_run(self.beam_path, "running", "resume")
        self.assertIn("heal: WV-01 lock-escape cleared, back to review on %s." % URL, buf.getvalue())
        self.assertEqual(self.load()["tickets"]["WV-01"]["status"], "review")


class ListenerSelfStampTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("messenger: slack\nslackChannel: warp\nlistenerMaxReads: 3\n")
        self.write([row()], messenger="slack", slackChannel="warp")
        self.assertEqual(orchestrator.supervise_listener(self.beam_path, now="2026-10-13T19:00:00Z"), ["listener: poll"])

    def test_rotated_is_refused_until_the_script_rotates(self):
        got = inbound.mark_returned(self.beam_path, "rotated", lease=1, holder="listener", now="2026-10-13T19:01:00Z")
        self.assertTrue(got.startswith("listener: not returned."), got)
        got = inbound.mark_returned(self.beam_path, "rotated", now="2026-10-13T19:01:00Z")
        self.assertTrue(got.startswith("listener: not returned."), got)
        self.assertEqual(self.load()["listener"]["holder"], "listener")
        data = self.load()
        data["listener"]["reads"] = 3
        self.beam_path.write_text(json.dumps(data))
        self.assertEqual(inbound.mark_returned(self.beam_path, "rotated", lease=1, holder="listener", now="2026-10-13T19:02:00Z"),
                         "listener: returned rotated")


if __name__ == "__main__":
    unittest.main()
