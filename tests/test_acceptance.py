"""Acceptance criteria: imported from the pack, listed in the prompt, recorded in the ticket folder, gating the merge."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import acceptance  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import scan  # noqa: E402
import ticket_state  # noqa: E402
from test_bugbot_heal import SHA, Repo, row  # noqa: E402
from test_resume_fixes import LiveProvider  # noqa: E402

PACK = {
    "projectKey": "WAR",
    "tickets": [
        {"tempId": "WV-01", "summary": "scaffold", "acceptanceCriteria": [
            {"id": "AC-1", "kind": "happy", "text": "build writes dist/viewer.html"},
            {"id": "AC-2", "kind": "edge", "text": "npm test passes"},
        ]},
        {"tempId": "WV-02", "summary": "no criteria"},
    ],
}


def clean_row(**extra):
    ticket = row(agent="shuttle-WV-01-r3", checkout="worktree", worktree=".warp/worktrees/WV-01", **extra)
    ticket["pr"].update({"bugbot": "pass", "bugbotSha": SHA, "check": "green", "checkSha": SHA, "ciNone": True})
    return ticket


class ItemsTest(Repo):
    def test_every_format_reads_the_same(self):
        self.assertEqual(acceptance.items({"acceptanceCriteria": [{"id": "AC-1", "text": "a"}, {"text": "b"}]}),
                         [("AC-1", "a"), ("AC-2", "b")])
        self.assertEqual(acceptance.items({"acs": ["builds", "AC-7: docs"]}), [("AC-1", "builds"), ("AC-7", "docs")])
        self.assertEqual(acceptance.items({"acceptance": "one | two"}), [("AC-1", "one"), ("AC-2", "two")])
        self.assertEqual(acceptance.items({}), [])
        self.assertEqual(orchestrator.acceptance_text({"acs": ["builds"]}), "AC-1: builds")

    def test_results_match_by_id_or_text(self):
        ticket = {"acs": ["builds", "docs"], "pr": {"acResults": {"builds": "pass", "AC2": "PASS"}}}
        self.assertTrue(acceptance.all_pass(ticket))
        self.assertEqual(acceptance.missing(ticket), [])
        ticket["pr"]["acResults"] = {"AC-1": "pass"}
        self.assertEqual(acceptance.missing(ticket), ["AC-2"])
        self.assertFalse(pipeline.acs_pass(ticket))
        self.assertEqual(acceptance.parse_args(["AC-1=pass", "AC-2=FAILED", "bad", "AC-3=flaky"]),
                         {"AC-1": "pass", "AC-2": "fail", "AC-3": "flaky"})


class ImportTest(Repo):
    def write_pack(self):
        folder = self.tmp / "WARPDEMO" / "WarpView" / "tickets"
        folder.mkdir(parents=True)
        (folder / "tickets.json").write_text(json.dumps(PACK))
        return "WARPDEMO/WarpView/tickets/tickets.json"

    def test_pack_criteria_fill_tickets_that_have_none(self):
        rel = self.write_pack()
        criteria, used = acceptance.pack_criteria(self.tmp, [rel])
        self.assertEqual(used, [rel])
        tickets = [{"id": "WV-01", "acs": None}, {"id": "WV-02"}, {"id": "WV-03", "acs": ["kept"]}]
        self.assertEqual(acceptance.fill(tickets, criteria), ["WV-01"])
        self.assertEqual(acceptance.items(tickets[0])[1], ("AC-2", "npm test passes"))
        self.assertEqual(tickets[2]["acs"], ["kept"])
        lines = acceptance.summary_lines(tickets)
        self.assertEqual(lines[0], "acceptance: 2 of 3 tickets have criteria")
        self.assertIn("no criteria for WV-02", lines[1])

    def test_resume_backfills_a_beam_scanned_before(self):
        rel = self.write_pack()
        data = self.write([row(acs=None)])
        data["source"] = {"scan": {"jira": [rel]}}
        self.beam_path.write_text(json.dumps(data))
        buf = io.StringIO()
        with redirect_stdout(buf):
            scan.set_run(self.beam_path, "running", "resume")
        self.assertIn("acceptance: criteria added to 1 tickets from %s (WV-01)" % rel, buf.getvalue())
        self.assertEqual(acceptance.ids(self.load()["tickets"]["WV-01"]), ["AC-1", "AC-2"])


class PromptTest(Repo):
    def test_the_prompt_lists_each_criterion_and_the_done_line_asks_for_each(self):
        ticket = clean_row(acs=[{"id": "AC-1", "text": "build"}, {"id": "AC-2", "text": "test"}],
                           shuttle={"step": "restart", "pending": True})
        text = orchestrator.subagent_prompt(ticket, {}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("acceptance: AC-1: build | AC-2: test", text)
        self.assertIn("  AC-1: build", text.splitlines())
        self.assertIn("--state done --agent shuttle-WV-01-r3 --root /repo --ac AC-1=pass|fail --ac AC-2=pass|fail", text)
        vm = orchestrator.subagent_vm_prompt(ticket, {}, "warp/WV-01")
        self.assertIn("--state done --agent shuttle-WV-01-r3 --push --ac AC-1=pass|fail --ac AC-2=pass|fail", vm)
        ticket["shuttle"]["step"] = "verify"
        text = orchestrator.subagent_prompt(ticket, {}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("VERIFY ONLY. Do not change code, commit, or push.", text)

    def test_no_criteria_keeps_the_plain_done_line(self):
        ticket = clean_row(acs=None, shuttle={"step": "fix", "pending": True})
        text = orchestrator.subagent_prompt(ticket, {}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("--state done --agent shuttle-WV-01-r3 --root /repo", text)
        self.assertNotIn("--ac ", text)


class RecordTest(Repo):
    def append(self, state, **kw):
        with redirect_stdout(io.StringIO()) as buf:
            ticket_state.append_status(self.tmp, "WV-01", state, **kw)
        return buf.getvalue()

    def test_ac_results_in_the_folder_reach_the_beam_and_open_the_gate(self):
        ticket = clean_row(acs=["build", "test"], shuttle={"step": "restart", "pending": True, "startedAt": "2026-10-13T21:00:00Z"})
        self.write([ticket])
        self.append("done", agent="shuttle-WV-01-r3", acs={"AC-1": "pass", "AC-2": "pass"}, when="2026-10-13T21:05:00Z")
        state = json.loads((ticket_state.ticket_dir(self.tmp, "WV-01") / "state.json").read_text())
        self.assertEqual(state["acResults"], {"AC-1": "pass", "AC-2": "pass"})
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T21:06:00Z")
        self.assertIn("herald: WV-01 acceptance: 2 recorded, all pass.", lines)
        self.assertIn("phase: WV-01 ready", lines)
        self.assertEqual(self.load()["tickets"]["WV-01"]["pr"]["acSha"], SHA)

    def test_a_fail_goes_to_fix(self):
        ticket = clean_row(acs=["build", "test"])
        self.write([ticket])
        lines = []
        ticket_state._apply_acs(ticket, {"acs": {"AC-1": "pass", "AC-2": "fail"}}, lines, set())
        self.assertIn("herald: WV-01 acceptance: AC-2 failed.", lines)
        self.assertIn("ac AC-2 fail", pipeline.red_reasons(ticket, {}))

    def test_results_belong_to_the_head(self):
        pr = {"headSha": SHA, "acResults": {"AC-1": "pass"}, "acSha": SHA}
        pipeline._drop_stale_results(pr, "e" * 40)
        self.assertEqual(pr["acResults"], {})

    def test_a_line_without_agent_is_checked_as_the_current_agent(self):
        ticket = clean_row(acs=["build"])
        ticket["agent"] = "shuttle-WV-01-r3"
        self.write([ticket])
        self.append("coding", agent="shuttle-WV-01-r2")  # an older Shuttle wrote the folder last
        self.assertEqual(ticket_state._current_agent(self.tmp, "WV-01"), "shuttle-WV-01-r3")
        out = self.append("acceptance", acs={"AC-1": "pass"})
        self.assertNotIn("shuttle-WV-01-r2", out)


class VerifyShuttleTest(Repo):
    def test_done_work_with_missing_results_gets_a_verify_shuttle_twice_then_a_person(self):
        self.write([clean_row(acs=["build", "test"])])
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T21:30:00Z")
        self.assertTrue(any(line.startswith("start WV-01 ") and "step=verify" in line for line in lines), lines)
        self.assertIn("acceptance: verify-shuttle WV-01 1/2 missing AC-1, AC-2", lines)
        for n, now, back in ((2, "2026-10-13T21:40:00Z", "2026-10-13T21:35:00Z"), (3, "2026-10-13T21:50:00Z", "2026-10-13T21:45:00Z")):
            # The verify Shuttle comes back (done) without recording any result.
            agent = self.load()["tickets"]["WV-01"]["agent"]
            with redirect_stdout(io.StringIO()):
                ticket_state.append_status(self.tmp, "WV-01", "done", agent=agent, when=back)
            with LiveProvider():
                lines = orchestrator.supervise(self.beam_path, now=now)
            if n == 2:
                self.assertIn("acceptance: verify-shuttle WV-01 2/2 missing AC-1, AC-2", lines)
            else:
                self.assertTrue(any(line.startswith("acceptance: MUST DO WV-01") for line in lines), lines)
                self.assertFalse(any(line.startswith("start WV-01 ") for line in lines), lines)

    def test_no_verify_shuttle_while_bugbot_or_checks_are_not_done(self):
        ticket = clean_row(acs=["build"])
        ticket["pr"]["bugbot"] = "pending"
        self.write([ticket])
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T21:30:00Z")
        self.assertFalse(any("step=verify" in line for line in lines), lines)


if __name__ == "__main__":
    import unittest

    unittest.main()
