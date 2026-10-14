"""1.6.17: a fix that pushed nothing, a red rollup that is only Bugbot, a Shuttle pass over a Bugbot finding, numbered passes."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bugbot  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import sweep  # noqa: E402
import ticket_state  # noqa: E402
import watch  # noqa: E402
from test_bugbot_heal import Repo  # noqa: E402

SHA = "c6ec7848b6213a3566c865e0529be3e00f3d1201"
FINDING = "WARPDEMO/WarpView/app/src/view/table/table.mjs:142 Filter misses planned block (Medium)"
SNAP = {"headSha": SHA, "rollup": "red", "ci": "absent", "bugbot": "fail", "findings": [FINDING]}


def follow_up(**shuttle):
    return {
        "id": "WV-11-BB1", "status": "fix", "phase": "fixing", "module": "WarpView", "hours": 1, "size": "S",
        "agent": "shuttle-WV-11-BB1-r1", "followUpOf": "WV-11", "acs": [{"id": "AC-1", "kind": "fix", "text": "x"}],
        "locks": ["app/"], "branch": "warp/view/WV-11-BB1", "autoMerge": True, "checkout": "worktree",
        "worktree": ".warp/worktrees/WV-11-BB1",
        "shuttle": dict({"pending": True, "step": "fix", "startedAt": "2026-10-14T01:47:00Z", "hadPr": True, "headAtMark": SHA}, **shuttle),
        "pr": {"url": "https://github.com/smlsr/WarpDemo/pull/22", "headSha": SHA, "bugbot": "fail", "bugbotSha": SHA,
               "bugbotSource": "provider", "bugbotProviderSha": SHA, "bugbotFindings": [FINDING], "bugbotFindingsSha": SHA,
               "ciNone": True, "ci": "absent", "rollup": "red", "acResults": {"AC-1": "pass"}, "acSha": SHA, "reviewCycle": 1},
    }


class FixReturnTest(Repo):
    def setUp(self):
        super().setUp()
        self.config.write_text("bugbotRequired: true\nbugbotTrigger: shuttle\n")
        data = self.write([follow_up()], bugbotRequired=True, bugbotTrigger="shuttle")
        data["bugbotSeen"] = True
        self.beam_path.write_text(json.dumps(data))
        self.saved = ticket_state._default_checks
        ticket_state._default_checks = lambda root, ticket: None

    def tearDown(self):
        ticket_state._default_checks = self.saved
        super().tearDown()

    def come_back(self, *states):
        with redirect_stdout(StringIO()):
            for i, state in enumerate(states):
                ticket_state.append_status(self.tmp, "WV-11-BB1", state, agent="shuttle-WV-11-BB1-r1", sha=SHA if state.startswith("review") else None,
                                           when="2026-10-14T01:5%d:00Z" % (4 + i))

    def one_pass(self, now):
        observed = ticket_state.observe(self.beam_path, now=now)
        data = self.load()
        lines = pipeline.advance(data, provider={"WV-11-BB1": SNAP}, beam_path=self.beam_path, now=now)
        self.beam_path.write_text(json.dumps(data))
        return observed, lines, data["tickets"]["WV-11-BB1"]

    def test_a_fix_that_pushed_nothing_is_fixed_again_with_the_finding(self):
        self.come_back("review-pass", "done")
        observed, lines, ticket = self.one_pass("2026-10-14T01:56:00Z")
        self.assertIn("herald: WV-11-BB1 Shuttle review: pass, but Cursor Bugbot's finding on c6ec7848b621 stands.", observed)
        self.assertIn("fix: WV-11-BB1 came back with no new commit; Cursor Bugbot's finding on c6ec7848b621 still stands: %s" % FINDING, lines)
        self.assertTrue(any(line.split(" ")[0] in {"start", "resume"} and " WV-11-BB1 " in line and "step=fix" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("bugbot: request WV-11-BB1") for line in lines), lines)
        self.assertFalse(any(line.startswith("ci: rerun WV-11-BB1") for line in lines), lines)
        self.assertEqual((ticket["pr"]["bugbot"], ticket["phase"]), ("fail", "fixing"))
        self.assertEqual(ticket["attempts"], 1)

    def test_a_fix_with_a_new_commit_goes_back_to_review(self):
        data = self.load()
        data["tickets"]["WV-11-BB1"]["shuttle"]["headAtMark"] = "b" * 40  # it pushed since it started
        self.beam_path.write_text(json.dumps(data))
        self.come_back("done")
        _observed, lines, _ticket = self.one_pass("2026-10-14T01:56:00Z")
        self.assertFalse(any(line.startswith("fix: WV-11-BB1 came back with no new commit") for line in lines), lines)


class BugbotOnlyRedTest(unittest.TestCase):
    def test_a_red_rollup_from_bugbot_alone_is_not_ci(self):
        ticket = follow_up(pending=False)
        ticket["status"] = "review"
        ticket["phase"] = "reviewing"
        self.assertEqual(watch._ci_label(ticket), "none")
        self.assertEqual(sweep._ticket_action({"config": {}}, ticket, {}), "fix")
        ticket["pr"]["ci"] = "red"
        self.assertEqual(watch._ci_label(ticket), "red")


class NumberedPassTest(Repo):
    def test_each_pass_says_its_number(self):
        self.config.write_text("messenger: slack\nslackChannel: warp\n")
        self.write([follow_up(pending=False)])
        first = orchestrator.supervise(self.beam_path, now="2026-10-14T02:00:00Z", provider={})
        second = orchestrator.supervise(self.beam_path, now="2026-10-14T02:01:00Z", provider={})
        self.assertEqual(first[0], "run: pass 1 at 02:00:00Z")
        self.assertEqual(second[0], "run: pass 2 at 02:01:00Z")


class PromptTest(unittest.TestCase):
    def test_the_fix_prompt_lists_the_findings_and_forbids_git_stash(self):
        ticket = follow_up(detail="bugbot fail | bugbot: %s" % FINDING)
        prompt = orchestrator.subagent_prompt(ticket, {}, "warp/view/WV-11-BB1", "/w/WV-11-BB1", "/repo")
        self.assertIn("step: fix", prompt)
        self.assertIn("FIX WV-11-BB1. The pull request https://github.com/smlsr/WarpDemo/pull/22 is open", prompt)
        self.assertIn("- bugbot: %s" % FINDING, prompt)
        self.assertIn("never record review-pass on the commit the findings were made on", prompt)
        self.assertIn("Never run `git stash`", prompt)
        vm = orchestrator.subagent_vm_prompt(ticket, {}, "warp/view/WV-11-BB1")
        self.assertIn("- bugbot: %s" % FINDING, vm)
        implement = orchestrator.implement_prompt(dict(ticket, shuttle={"step": "implement"}), {})
        self.assertIn("step: implement", implement)
        self.assertNotIn("FIX ", implement)

    def test_the_detail_is_kept_for_the_prompt(self):
        data = {"config": {"maxAgents": 4}, "tickets": {}}
        ticket = follow_up(pending=False)
        data["tickets"][ticket["id"]] = ticket
        pipeline.mark_shuttle(ticket, "fix", data)
        pipeline._start_lines(data, ticket, "fix", "bugbot: %s" % FINDING)
        self.assertEqual(ticket["shuttle"]["detail"], "bugbot: %s" % FINDING)


class FollowUpLocksTest(unittest.TestCase):
    def test_a_follow_up_locks_its_module_and_sits_in_its_parents_block(self):
        data = {"config": {}, "gates": [{"key": "G0", "members": []}, {"key": "G2", "members": ["WV-11"]}], "tickets": {
            "WV-11": {"id": "WV-11", "module": "WarpView", "status": "merged", "gate": "G2",
                      "locks": ["WARPDEMO/WarpView/app/src/view/table/table.mjs"],
                      "pr": {"url": "https://github.com/smlsr/WarpDemo/pull/12"}},
            "WV-16": {"id": "WV-16", "module": "WarpView", "status": "merged", "locks": ["WARPDEMO/WarpView/viewer.html"]},
        }}
        new_id = pipeline._follow_up(data, data["tickets"]["WV-11"], [FINDING], "2026-10-14T01:30:00Z")
        follow = data["tickets"][new_id]
        self.assertIn("WARPDEMO/WarpView/", follow["locks"])
        self.assertEqual(follow["gate"], "G2")


class ShuttleFailStandsTest(unittest.TestCase):
    def test_a_shuttle_fail_is_not_undone_by_a_bugbot_pass(self):
        ticket = follow_up(pending=False)
        ticket["pr"].update({"bugbot": "", "bugbotSource": ""})
        bugbot.record_result(ticket, "fail", ["test/app.test.mjs fails: parity"], source="shuttle", sha=SHA)
        pipeline._apply_snapshot(ticket, {"headSha": SHA, "rollup": "green", "ci": "absent", "bugbot": "pass", "findings": []})
        self.assertEqual(ticket["pr"]["bugbot"], "pass")
        reasons = pipeline.red_reasons(ticket, {})
        self.assertIn("shuttle review fail", reasons)
        self.assertIn("review: test/app.test.mjs fails: parity", reasons)
        # A new commit clears it.
        pipeline._apply_snapshot(ticket, {"headSha": "e" * 40, "rollup": "green", "ci": "absent", "bugbot": "pass", "findings": []})
        self.assertNotIn("shuttle review fail", pipeline.red_reasons(ticket, {}))


class MergeCheckTest(Repo):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/smlsr/WarpDemo.git"], cwd=self.tmp, check=True)
        self.config.write_text("bugbotRequired: true\ngitProvider: github\n")
        app = self.tmp / "WARPDEMO" / "WarpView" / "app"
        app.mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
        (self.tmp / "other").mkdir()
        (self.tmp / "other" / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))

    def test_the_command_is_the_test_script_next_to_the_tickets_files(self):
        ticket = {"locks": ["WARPDEMO/WarpView/app/src/view/table/table.mjs"]}
        import provider

        self.assertEqual(provider.merge_check_command(self.tmp, ticket, {}), "(cd WARPDEMO/WarpView/app && npm test --silent)")
        self.assertEqual(provider.merge_check_command(self.tmp, ticket, {"checkCommand": "make ci"}), "make ci")
        self.assertEqual(provider.merge_check_command(self.tmp, ticket, {"mergeCheck": "off"}), "")

    def test_a_red_check_is_not_merged_and_goes_to_fix(self):
        ticket = follow_up(pending=False)
        ticket.update({"status": "merging", "phase": "merging"})
        data = self.write([ticket])
        merged = []
        lines = ["merge: request WV-11-BB1 https://github.com/smlsr/WarpDemo/pull/22"]
        out = pipeline.run_merges(data, self.beam_path, lines, merger=lambda root, number, cfg: (merged.append(number), ("merged", "a" * 40))[1],
                                  checker=lambda root, t, cfg: ("red", "(cd app && npm test --silent)\nnot ok 3 - parity"))
        self.assertEqual(merged, [])
        self.assertIn("merge: check red WV-11-BB1 ((cd app && npm test --silent)). Not merged; back to fix.", out)
        self.assertTrue(any(line.split(" ")[0] in {"start", "resume"} and "step=fix" in line for line in out), out)
        self.assertEqual(data["tickets"]["WV-11-BB1"]["phase"], "fixing")
        self.assertIn("not ok 3 - parity", data["tickets"]["WV-11-BB1"]["shuttle"]["detail"])

    def test_a_green_check_merges(self):
        ticket = follow_up(pending=False)
        ticket.update({"status": "merging", "phase": "merging"})
        data = self.write([ticket])
        out = pipeline.run_merges(data, self.beam_path, ["merge: request WV-11-BB1 https://github.com/smlsr/WarpDemo/pull/22"],
                                  merger=lambda root, number, cfg: ("merged", "a" * 40), checker=lambda root, t, cfg: ("green", "npm test"))
        self.assertIn("merge: check green WV-11-BB1 (npm test)", out)
        self.assertIn("merge: WV-11-BB1 merged", out)


class BugbotRunsHereTest(unittest.TestCase):
    def test_a_merged_tickets_bugbot_result_counts(self):
        data = {"tickets": {"WV-15-BB1": {"status": "merged", "pr": {"bugbotSource": "provider"}}}}
        self.assertTrue(pipeline.bugbot_runs_here(data))
        self.assertTrue(data["bugbotSeen"])
        self.assertFalse(pipeline.bugbot_runs_here({"tickets": {"A": {"pr": {"bugbotSource": "shuttle"}}}}))


class CloudKeyTest(Repo):
    def test_a_cloud_session_without_a_key_is_refused(self):
        import agents

        cloud = {"CURSOR_CLOUD_AGENT": "1"}
        self.assertIn("CURSOR_API_KEY is not set", agents.cloud_key_missing({}, env=cloud))
        self.assertEqual(agents.cloud_key_missing({}, env=dict(cloud, CURSOR_API_KEY="k")), "")
        self.assertEqual(agents.cloud_key_missing({"requireCursorApiKey": "false"}, env=cloud), "")
        self.assertEqual(agents.cloud_key_missing({"runner": "local"}, env=cloud), "")
        self.assertEqual(agents.cloud_key_missing({}, env={}), "")  # a local session with worktree Shuttles

    def test_a_run_with_cloud_agents_recorded_is_refused(self):
        import agents

        self.write([follow_up(pending=False)])
        (self.warp / "agents.json").write_text(json.dumps({"agents": [{"id": "shuttle-WV-11-BB1-r1", "cloudId": "bc-123", "role": "shuttle"}]}))
        self.assertIn("CURSOR_API_KEY is not set", agents.cloud_key_missing({}, env={}, beam_path=self.beam_path))


if __name__ == "__main__":
    unittest.main()
