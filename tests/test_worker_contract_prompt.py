"""1.7.23: LOCAL CI IS MANDATORY and the HEARTBEAT RULE, verbatim, in every Shuttle prompt."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrator  # noqa: E402

PINNED = ("LOCAL CI IS MANDATORY", "FIX → RUN → FIX → RUN", "HEARTBEAT RULE", "heartbeatAt", "ticket_state.py append --id")


def ticket(step, **extra):
    row = {"id": "C-101", "jiraKey": "ONSPEK-101", "locks": ["web/"], "size": "M", "acceptance": ["AC-1: the test passes"],
           "shuttle": {"step": step}, "pr": {}}
    row.update(extra)
    return row


def prompts(t, config):
    return {
        "worktree": orchestrator.subagent_prompt(t, config, "warp/web/C-101", "/repo/.warp/worktrees/C-101", "/repo"),
        "vm": orchestrator.subagent_vm_prompt(t, config, "warp/web/C-101"),
        "agent": orchestrator.implement_prompt(t, config),
    }


class WorkerContractPromptTest(unittest.TestCase):
    def check(self, t, config=None):
        for kind, text in prompts(t, config or {}).items():
            for part in PINNED:
                self.assertIn(part, text, "%s prompt, step %s: %s missing" % (kind, t["shuttle"]["step"], part))
            self.assertIn(orchestrator.LOCAL_CI_CLAUSE, text)
            self.assertIn(orchestrator.HEARTBEAT_CLAUSE, text)
            # Above the ticket-specific contract and reap rule.
            self.assertLess(text.index("LOCAL CI IS MANDATORY"), text.index("You are one worker for one ticket."))
            self.assertLess(text.index("HEARTBEAT RULE"), text.index("Run the reap check first"))

    def test_implement(self):
        self.check(ticket("implement"))

    def test_fix(self):
        self.check(ticket("fix", pr={"url": "https://github.com/o/r/pull/234"},
                          shuttle={"step": "fix", "detail": "ci red | check red: web-tests"}))

    def test_rebase(self):
        self.check(ticket("rebase", pr={"url": "https://github.com/o/r/pull/234"}))

    def test_resume(self):
        # A resume gets the prompt of the step it resumes, with the same contract.
        self.check(ticket("fix", pr={"url": "https://github.com/o/r/pull/234"},
                          shuttle={"step": "fix", "resume": "shuttle-C-101-r1", "detail": "bugbot: web/a.ts:3 x"}))

    def test_the_clauses_are_verbatim(self):
        self.assertTrue(orchestrator.LOCAL_CI_CLAUSE.startswith("LOCAL CI IS MANDATORY BEFORE OPENING THE PULL REQUEST. "
                                                                "Before you open the PR, run `./scripts/ci/local.sh`"))
        self.assertTrue(orchestrator.LOCAL_CI_CLAUSE.endswith("A push on red local CI is a bug in the work."))
        self.assertTrue(orchestrator.HEARTBEAT_CLAUSE.startswith("HEARTBEAT RULE — write a `heartbeat` state every ~90 seconds"))
        self.assertTrue(orchestrator.HEARTBEAT_CLAUSE.endswith("Keep the same agent id you were started with."))

    def test_the_prompt_names_this_shuttles_commands(self):
        texts = prompts(ticket("implement"), {"checkCommand": "make ci"})
        self.assertIn("Your local CI command: `make ci`.", texts["worktree"])
        self.assertIn("ticket_state.py append --root /repo --id C-101 --state heartbeat --agent", texts["worktree"])
        self.assertIn("--state heartbeat --agent subagent:C-101 --push`", texts["vm"])
        self.assertIn("refs/warp/beat/C-101", texts["vm"])
        self.assertIn("Your local CI command: `./scripts/ci/local.sh`.", prompts(ticket("implement"), {})["worktree"])

    def test_require_local_ci_off_leaves_clause_a_out(self):
        text = prompts(ticket("implement"), {"requireLocalCiGreenBeforePush": False})["worktree"]
        self.assertNotIn("LOCAL CI IS MANDATORY", text)
        self.assertIn("HEARTBEAT RULE", text)


if __name__ == "__main__":
    unittest.main()
