"""A Shuttle on an existing pull request (restart, fix, repair) is seen to return from its ticket folder."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import beam  # noqa: E402
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import ticket_state  # noqa: E402
from test_bugbot_heal import SHA, Repo, row  # noqa: E402
from test_resume_fixes import LiveProvider  # noqa: E402

AGENT = "shuttle-WV-01-r1"


def restart_row(**extra):
    ticket = row(status="recovering", phase="reviewing", agent=AGENT, checkout="worktree",
                 worktree=".warp/worktrees/WV-01", workerStartedAt="2026-10-13T19:00:00Z",
                 lastSeenAt="2026-10-13T19:44:30Z",
                 shuttle={"pending": True, "step": "restart", "hadPr": True, "startedAt": "2026-10-13T19:44:17Z"})
    ticket["pr"].update({"agentFinished": True, "opened": True})
    ticket.update(extra)
    return ticket


class ShuttleReturnTest(Repo):
    def append(self, state, **kw):
        with redirect_stdout(io.StringIO()):
            ticket_state.append_status(self.tmp, "WV-01", state, **kw)

    def test_the_wv01_restart_returns_from_its_check_line_and_is_not_replaced(self):
        self.write([restart_row()])
        self.append("pr-opened", pr="https://github.com/smlsr/WarpDemo/pull/4", agent=AGENT, when="2026-10-13T19:45:20Z")
        self.append("check-green", name="npm test", agent=AGENT, when="2026-10-13T19:45:21Z")
        # 45 minutes later: the heartbeat is long stale. Before the fix this replaced it as dead.
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T20:30:00Z")
        self.assertIn("shuttle: returned WV-01 step=restart (check-green)", lines)
        self.assertFalse(any(line.startswith("shuttle: replace WV-01") for line in lines), lines)
        self.assertFalse(any("worker died" in line for line in lines), lines)
        saved = self.load()["tickets"]["WV-01"]
        self.assertFalse(saved["shuttle"]["pending"])

    def test_done_is_the_explicit_return(self):
        self.write([restart_row()])
        self.append("done", agent=AGENT, when="2026-10-13T19:50:00Z")
        with LiveProvider():
            lines = orchestrator.supervise(self.beam_path, now="2026-10-13T19:51:00Z")
        self.assertIn("shuttle: returned WV-01 step=restart (done)", lines)
        self.assertFalse(self.load()["tickets"]["WV-01"]["shuttle"]["pending"])

    def test_lines_from_before_this_shuttle_or_another_agent_do_not_count(self):
        ticket = restart_row()
        old = {"state": "check-green", "agent": AGENT, "at": "2026-10-13T19:30:00Z"}
        self.assertFalse(ticket_state.note_shuttle_return(ticket, old, old["at"]))
        other = {"state": "check-green", "agent": "shuttle-WV-01", "at": "2026-10-13T19:50:00Z"}
        self.assertFalse(ticket_state.note_shuttle_return(ticket, other, other["at"]))
        mine = {"state": "check-red", "agent": AGENT, "at": "2026-10-13T19:50:00Z"}
        self.assertTrue(ticket_state.note_shuttle_return(ticket, mine, mine["at"]))
        self.assertTrue(orchestrator.shuttle_returned(ticket))
        # The next Shuttle starts out.
        pipeline.mark_shuttle(ticket, "fix", {})
        self.assertFalse(orchestrator.shuttle_returned(ticket))
        self.assertNotIn("shuttleReturned", ticket["pr"])

    def test_a_working_shuttles_review_line_is_not_its_return(self):
        ticket = restart_row()
        review = {"state": "review-pass", "agent": AGENT, "at": "2026-10-13T19:50:00Z"}
        self.assertFalse(ticket_state.note_shuttle_return(ticket, review, review["at"]))
        ticket["shuttle"]["step"] = "review"
        self.assertTrue(ticket_state.note_shuttle_return(ticket, review, review["at"]))

    def test_the_watchdog_does_not_replace_a_returned_shuttle(self):
        ticket = restart_row()
        ticket["shuttle"]["returned"] = True
        ticket["pr"]["shuttleReturned"] = True
        self.write([ticket])
        lines = beam.watchdog(self.beam_path, now="2026-10-13T20:30:00Z")
        self.assertIn("shuttle: returned WV-01", lines)
        self.assertFalse(any(line.startswith("shuttle: replace") for line in lines), lines)

    def test_prompts_end_with_done(self):
        ticket = restart_row()
        text = orchestrator.subagent_prompt(ticket, {}, "warp/WV-01", "/w/WV-01", "/repo")
        self.assertIn("ticket_state.py append --id WV-01 --state done --agent %s --root /repo" % AGENT, text)
        vm = orchestrator.subagent_vm_prompt(ticket, {}, "warp/WV-01")
        self.assertIn("ticket_state.py append --id WV-01 --state done --agent %s --push" % AGENT, vm)


if __name__ == "__main__":
    import unittest

    unittest.main()
