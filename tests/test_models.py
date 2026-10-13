"""Shuttle model by ticket size, and a stronger model from the second fix round on."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrator  # noqa: E402
from test_isolation import GitRepo, run  # noqa: E402

CFG = {"model": "base-model", "modelS": "small-model", "modelL": "large-model", "modelXL": "", "fixModel": "fix-model"}


def ticket(size="M", attempts=0, step=None, **extra):
    row = {"id": "T-1", "size": size, "attempts": attempts, "shuttle": {"step": step} if step else {}}
    row.update(extra)
    return row


class ModelForTest(unittest.TestCase):
    def test_size_keys_then_model_then_default(self):
        self.assertEqual(orchestrator.model_for(ticket("S"), CFG)[0], "small-model")
        self.assertEqual(orchestrator.model_for(ticket("L"), CFG)[0], "large-model")
        self.assertEqual(orchestrator.model_for(ticket("M"), CFG)[0], "base-model")      # no modelM
        self.assertEqual(orchestrator.model_for(ticket("XL"), CFG)[0], "base-model")     # modelXL blank
        self.assertEqual(orchestrator.model_for(ticket("L"), {})[0], orchestrator.DEFAULT_MODEL)
        self.assertEqual(orchestrator.model_for(ticket("L"), {"model": ""})[0], orchestrator.DEFAULT_MODEL)
        self.assertEqual(orchestrator.model_for(ticket(None), CFG)[0], "base-model")
        self.assertEqual(orchestrator.model_for(ticket("S"), {"modelS": "default", "model": "m"})[0], "m")

    def test_fix_model_from_the_second_fix_round(self):
        # attempts is the fix round: the first red round makes it 1
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=1), CFG, "fix")[0], "large-model")
        slug, why = orchestrator.model_for(ticket("L", attempts=2), CFG, "fix")
        self.assertEqual(slug, "fix-model")
        self.assertIn("fix round 2", why)
        self.assertEqual(orchestrator.model_for(ticket("S", attempts=3), CFG, "fix")[0], "fix-model")
        # not a fix step: the size model, whatever the attempts
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=4), CFG, "implement")[0], "large-model")
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=4), CFG, "restart")[0], "large-model")
        # the step comes from the ticket's shuttle when not given
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=2, step="fix"), CFG)[0], "fix-model")

    def test_fix_model_after_and_blank_fix_model(self):
        cfg = dict(CFG, fixModelAfter="3")
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=2), cfg, "fix")[0], "large-model")
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=3), cfg, "fix")[0], "fix-model")
        cfg = dict(CFG, fixModelAfter="1")
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=1), cfg, "fix")[0], "fix-model")
        cfg = dict(CFG, fixModel="")
        self.assertEqual(orchestrator.model_for(ticket("L", attempts=5), cfg, "fix")[0], "large-model")

    def test_retry_counts_earlier_rounds(self):
        # warp:retry resets attempts and keeps the old count on retryLog
        row = ticket("M", attempts=0, retryLog=[{"attempts": 5}])
        self.assertEqual(orchestrator.fix_round(row), 5)
        self.assertEqual(orchestrator.model_for(row, CFG, "fix")[0], "fix-model")

    def test_prompts_carry_the_model(self):
        row = ticket("L", attempts=2, step="fix", locks=["src/a"], jiraKey="WAR-1")
        text = orchestrator.subagent_prompt(row, CFG, "warp/T-1-WAR-1", "/w/T-1", "/repo")
        self.assertIn("model: fix-model", text.splitlines())
        text = orchestrator.subagent_vm_prompt(row, CFG, "warp/T-1-WAR-1")
        self.assertIn("model: fix-model", text.splitlines())
        text = orchestrator.implement_prompt(ticket("S"), CFG)
        self.assertIn("model: small-model", text.splitlines())
        self.assertTrue(orchestrator.model_line(ticket("S"), CFG).startswith("model: small-model (modelS for size S)."))


class LaunchTest(GitRepo):
    def test_launch_prints_the_model_line_above_the_mark(self):
        row = self.ticket(status="claimed", agent="shuttle-T-1")
        row["size"] = "L"
        row["jiraKey"] = "WAR-1"
        self.beam([row], runner="cloud", baseBranch="main", launch="worktree", subagentVm=False, model="beam-model")
        # the yaml wins over the beam's copy, so a key set after the scan applies
        (self.repo / ".warp" / "config.yaml").write_text('modelL: "yaml-large"\n')
        out = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        above, _mark, below = out.stdout.partition(orchestrator.PROMPT_MARK)
        self.assertIn("model: yaml-large (modelL for size L).", above)
        self.assertIn("model: yaml-large", below.splitlines())

    def test_start_line_names_the_model(self):
        import agents

        row = self.ticket(status="fix", agent=None)
        row["size"] = "M"
        row["attempts"] = 2
        path = self.beam([row], model="m-model", fixModel="fixer")
        data = json.loads(path.read_text())
        t = data["tickets"]["T-1"]
        lines = agents.launch_lines(data, t, "fix", "bugbot finding", beam_path=path)
        self.assertTrue(lines[0].startswith("start T-1"), lines)
        self.assertIn(" model=fixer ", lines[0] + " ")
        row["attempts"] = 1
        (self.repo / ".warp" / "agents.json").unlink(missing_ok=True)
        path = self.beam([row], model="m-model", fixModel="fixer")
        data = json.loads(path.read_text())
        lines = agents.launch_lines(data, data["tickets"]["T-1"], "fix", beam_path=path)
        self.assertIn(" model=m-model", lines[0])


if __name__ == "__main__":
    unittest.main()
