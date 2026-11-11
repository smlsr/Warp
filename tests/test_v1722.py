"""1.7.22: AI working time only (no idle), every AI use by step, code written, and bugs vs CI send-backs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import agents  # noqa: E402
import beam  # noqa: E402
import effort  # noqa: E402
import pipeline  # noqa: E402

NOW = "2026-11-10T12:00:00Z"


def shuttle(tid, started, ended=None, state="ended", reason="returned", **extra):
    row = {"id": "shuttle-%s-%s" % (tid, started[11:16]), "ticket": tid, "role": "shuttle", "started": started,
           "ended": ended, "state": state, "endReason": reason}
    row.update(extra)
    return row


class WorkTimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.path = self.tmp / ".warp" / "beam.json"

    def run_rows(self, rows, tickets=None, spawns=None):
        data = {"version": 1, "config": {}, "tickets": tickets or {}}
        self.path.write_text(json.dumps(data))
        (self.tmp / ".warp" / "agents.json").write_text(json.dumps({"agents": rows, "spawns": spawns or []}))
        return effort.compute(data, self.path, now=NOW), data

    def test_forge_qa_replacements_count_their_work_not_39_hours_each(self):
        # G-EXPLORATION-USER-TRIGGER: 8 Shuttles 20 minutes apart, all closed together 39 hours later.
        starts = ["2026-11-08T22:%02d:00Z" % m for m in (0, 20, 40)] + ["2026-11-08T23:%02d:00Z" % m for m in (0, 20, 40)] + \
                 ["2026-11-09T00:00:00Z", "2026-11-09T00:20:00Z"]
        rows = [shuttle("G-X", s, "2026-11-10T14:04:40Z", reason="stopped") for s in starts]
        stamps = ["2026-11-08T22:15:00Z", "2026-11-08T23:05:00Z", "2026-11-09T00:35:00Z"]
        work, _ = self.run_rows(rows, {"G-X": {"id": "G-X", "status": "merged", "aiSeen": stamps}})
        hours = work["G-X"]["minutes"] / 60
        self.assertLess(hours, 4, work["G-X"])  # was 310 h
        self.assertGreater(work["G-X"]["dropped"] / 60, 300)

    def test_phantoms_and_rows_stuck_in_starting_count_zero(self):
        rows = [shuttle("P-1", "2026-11-09T10:00:00Z", "2026-11-09T22:00:00Z", reason="phantom-start never launched"),
                shuttle("P-1", "2026-11-09T23:00:00Z", None, state="starting", reason="")]
        work, _ = self.run_rows(rows, {"P-1": {"id": "P-1", "status": "queued"}})
        self.assertEqual(work["P-1"]["minutes"], 0)

    def test_a_turn_ends_at_its_last_sign_of_life_plus_grace(self):
        rows = [shuttle("L-1", "2026-11-10T08:00:00Z", "2026-11-10T11:00:00Z")]
        work, _ = self.run_rows(rows, {"L-1": {"id": "L-1", "status": "review",
                                               "aiSeen": ["2026-11-10T08:10:00Z", "2026-11-10T08:42:00Z"]}})
        self.assertAlmostEqual(work["L-1"]["minutes"], 47, delta=0.5)  # 42 + 5 grace, not 180

    def test_a_silent_turn_is_capped_and_marked_estimated(self):
        rows = [shuttle("S-1", "2026-11-10T08:00:00Z", "2026-11-10T11:00:00Z")]
        work, _ = self.run_rows(rows, {"S-1": {"id": "S-1", "status": "review"}})
        self.assertAlmostEqual(work["S-1"]["minutes"], 30, delta=0.5)
        self.assertAlmostEqual(work["S-1"]["estimated"], 30, delta=0.5)

    def test_cursor_run_time_wins_and_a_resume_skips_the_gap(self):
        rows = [shuttle("R-1", "2026-11-10T06:00:00Z", "2026-11-10T09:00:00Z", runMs=25 * 60000),
                shuttle("R-2", "2026-11-10T06:00:00Z", "2026-11-10T10:20:00Z",
                        turns=[["2026-11-10T06:00:00Z", "2026-11-10T06:20:00Z"]], turnStart="2026-11-10T10:00:00Z")]
        tickets = {"R-1": {"id": "R-1"}, "R-2": {"id": "R-2", "aiSeen": ["2026-11-10T06:15:00Z", "2026-11-10T10:15:00Z"]}}
        work, _ = self.run_rows(rows, tickets)
        self.assertEqual(round(work["R-1"]["minutes"]), 25)
        self.assertEqual(round(work["R-2"]["minutes"]), 40)  # 06:00-06:20 and 10:00-10:20; the 3h40 between is not work

    def test_runs_and_minutes_by_step(self):
        rows = [shuttle("K-1", "2026-11-10T06:00:00Z", "2026-11-10T06:30:00Z", step="implement"),
                shuttle("K-1", "2026-11-10T07:00:00Z", "2026-11-10T07:10:00Z", step="fix", fixKinds=["bug", "ci"])]
        work, _ = self.run_rows(rows, {"K-1": {"id": "K-1", "aiSeen": ["2026-11-10T06:30:00Z", "2026-11-10T07:10:00Z"]}})
        self.assertEqual(work["K-1"]["runs"], {"implement": 1, "fix": 1})
        self.assertEqual(work["K-1"]["byFix"], {"bug": 10, "ci": 10})

    def test_update_recomputes_and_never_ratchets(self):
        rows = [shuttle("U-1", "2026-11-10T06:00:00Z", "2026-11-10T06:20:00Z")]
        self.run_rows(rows, {"U-1": {"id": "U-1", "minutes": 9999, "aiSeen": ["2026-11-10T06:20:00Z"]}})
        data = json.loads(self.path.read_text())
        effort.update(data, self.path, now=NOW)
        self.assertEqual(data["tickets"]["U-1"]["minutes"], 20)
        self.assertEqual(data["tickets"]["U-1"]["ai"]["runs"], {"work": 1})

    def test_orphan_rows_are_closed(self):
        rows = [shuttle("O-1", "2026-11-08T21:18:16Z", None, state="starting", reason=""),
                shuttle("O-2", "2026-11-10T11:30:00Z", None, state="starting", reason="")]
        rows[1]["id"] = "subagent:O-2"
        self.run_rows(rows, {"O-1": {"id": "O-1", "status": "merged"}, "O-2": {"id": "O-2", "status": "coding", "agent": "subagent:O-2"}})
        data = json.loads(self.path.read_text())
        self.assertEqual(agents.close_orphans(data, self.path, now=NOW), 1)
        doc = json.loads((self.tmp / ".warp" / "agents.json").read_text())
        self.assertEqual([r["endReason"] for r in doc["agents"]], ["orphan never launched", ""])

    def test_the_hook_stores_cursors_run_time(self):
        rows = [shuttle("H-1", "2026-11-10T06:00:00Z", None, state="running", reason="")]
        self.run_rows(rows, {"H-1": {"id": "H-1"}})
        self.assertTrue(agents.note_run_ms(self.tmp, "H-1", 1_500_000))
        doc = json.loads((self.tmp / ".warp" / "agents.json").read_text())
        self.assertEqual(doc["agents"][0]["runMs"], 1_500_000)


class SendBackTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(effort.classify(["bugbot: a.py:3 null deref"]), ["bug"])
        self.assertEqual(effort.classify(["ci red", "rollup red"]), ["ci"])
        both = pipeline.batched(["bugbot fail", "bugbot: x", "check red"])
        self.assertEqual(effort.classify(both), ["bug", "ci"])
        self.assertEqual(effort.classify(["shuttle review fail", "review: y"]), ["bug"])
        self.assertEqual(effort.classify(["merge check red: go test"]), ["ci"])
        self.assertEqual(effort.classify(["ac AC-2 fail"]), ["acceptance"])
        self.assertEqual(effort.classify(["rebase: the branch conflicts with the base"]), ["conflict"])

    def test_begin_fix_counts_bug_and_ci_rounds(self):
        ticket = {"id": "F-1", "status": "review", "locks": ["src"], "size": "S", "pr": {"url": "https://github.com/o/r/pull/1", "headSha": "h1"}}
        data = {"tickets": {"F-1": ticket}, "config": {}}
        pipeline.begin_fix(data, ticket, pipeline.batched(["bugbot: a", "ci red"]), [])
        ticket.pop("shuttle", None)
        pipeline.begin_fix(data, ticket, ["ci red"], [])
        self.assertEqual(ticket["fixCounts"], {"rounds": 2, "bug": 1, "ci": 2})
        self.assertEqual(ticket["fixLog"][0]["kinds"], ["bug", "ci"])
        totals = effort.totals(data)
        self.assertEqual((totals["fixCounts"]["bug"], totals["fixCounts"]["ci"]), (1, 2))
        self.assertEqual(totals["ticketsBack"], {"bug": 1, "ci": 1})


class CodeCountTest(unittest.TestCase):
    def test_lines_and_approximate_tokens_outside_warp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)

        def git(*a):
            subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                           cwd=tmp, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        (tmp / "a.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        (tmp / "a.py").write_text("x = 1\ndef add(a, b):\n    return a + b\n")
        (tmp / ".warp").mkdir()
        (tmp / ".warp" / "state.json").write_text("{" + "x" * 400 + "}\n")
        git("add", "-A")
        git("commit", "-qm", "work")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True).stdout.strip()
        ticket = {"id": "C-1", "status": "merged", "pr": {"url": "u", "mergeSha": sha}}
        got = effort.code_for(ticket, tmp, "main")
        chars = len("def add(a, b):") + len("    return a + b")
        self.assertEqual((got["added"], got["deleted"], got["files"]), (2, 0, 1))
        self.assertEqual(got["tokens"], -(-chars // 4))
        data = {"tickets": {"C-1": ticket}, "config": {}, "_repoRoot": str(tmp)}
        self.assertEqual(effort.code_pass(data, tmp / ".warp" / "beam.json"), 1)
        self.assertEqual(effort.totals(data)["code"]["added"], 2)
        self.assertEqual(effort.code_pass(data, tmp / ".warp" / "beam.json"), 0, "counted once per sha")


class SpendTest(unittest.TestCase):
    def test_spend_minutes_replace_and_stay_apart(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".warp").mkdir()
        path = tmp / ".warp" / "beam.json"
        path.write_text(json.dumps({"version": 1, "config": {}, "tickets": {"Z-1": {"id": "Z-1", "status": "coding", "minutes": 12}}}))
        import argparse
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            for total in (10, 20, 30):
                beam.cmd_spend(path, argparse.Namespace(id="Z-1", tokens=0, minutes=total))
        t = json.loads(path.read_text())["tickets"]["Z-1"]
        self.assertEqual((t["reportedMinutes"], t["minutes"]), (30, 12))


class ModelTest(unittest.TestCase):
    """1.7.22: the model of every run (chosen and, when seen, really used), and of every fix round."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.path = self.tmp / ".warp" / "beam.json"

    def test_the_launch_row_records_the_chosen_model_and_the_hook_the_real_one(self):
        cfg = {"model": "sonnet-5-5", "modelL": "opus-5-5", "fixModel": "opus-5-5-max", "fixModelAfter": 2, "runner": "local"}
        ticket = {"id": "M-1", "status": "queued", "size": "L", "locks": ["src"], "attempts": 0}
        data = {"version": 1, "runState": "running", "config": cfg, "tickets": {"M-1": ticket}}
        self.path.write_text(json.dumps(data))
        lines = agents.launch_lines(data, ticket, "implement", beam_path=self.path)
        self.assertTrue(any("model=opus-5-5" in l for l in lines), lines)
        agents.flush(data, self.path)
        row = [r for r in json.loads((self.tmp / ".warp" / "agents.json").read_text())["agents"] if r.get("ticket") == "M-1"][-1]
        self.assertEqual((row["model"], row["modelWhy"], row["step"]), ("opus-5-5", "modelL for size L", "implement"))
        self.assertTrue(agents.note_turn(self.tmp, "M-1", 600000, "claude-opus-5-5"))
        row = [r for r in json.loads((self.tmp / ".warp" / "agents.json").read_text())["agents"] if r.get("ticket") == "M-1"][-1]
        self.assertEqual(row["modelUsed"], "claude-opus-5-5")
        self.assertEqual(agents.hook_model({"model": {"name": "gpt-6"}}), "gpt-6")

    def test_fix_rounds_name_their_model_and_totals_split_by_model(self):
        cfg = {"model": "sonnet-5-5", "fixModel": "opus-5-5", "fixModelAfter": 2}
        ticket = {"id": "F-2", "status": "review", "locks": ["src"], "size": "S", "attempts": 0,
                  "pr": {"url": "https://github.com/o/r/pull/2", "headSha": "h1"}}
        data = {"tickets": {"F-2": ticket}, "config": cfg}
        pipeline.begin_fix(data, ticket, ["ci red"], [])
        ticket.pop("shuttle", None)
        ticket["attempts"] = 2
        pipeline.begin_fix(data, ticket, ["bugbot: a.py:1 x"], [])
        self.assertEqual([e["model"] for e in ticket["fixLog"]], ["sonnet-5-5", "opus-5-5"])
        ticket["ai"] = {"models": {"sonnet-5-5": {"runs": 2, "minutes": 50, "steps": {"implement": 1, "fix": 1}},
                                   "opus-5-5": {"runs": 1, "minutes": 20, "steps": {"fix": 1}}}}
        tot = effort.totals(data)
        self.assertEqual(tot["aiByModel"]["sonnet-5-5"]["minutes"], 50)
        self.assertEqual(tot["fixByModel"], {"sonnet-5-5": {"rounds": 1, "ci": 1}, "opus-5-5": {"rounds": 1, "bug": 1}})

    def test_working_time_by_model_prefers_what_really_ran(self):
        rows = [shuttle("W-1", "2026-11-10T06:00:00Z", "2026-11-10T06:30:00Z", step="implement", model="sonnet-5-5"),
                shuttle("W-1", "2026-11-10T07:00:00Z", "2026-11-10T07:20:00Z", step="fix", model="sonnet-5-5"),
                shuttle("W-1", "2026-11-10T08:00:00Z", "2026-11-10T08:10:00Z", step="fix", model="opus-5-5",
                        modelUsed="claude-opus-5-5-max")]
        ticket = {"id": "W-1", "aiSeen": ["2026-11-10T06:30:00Z", "2026-11-10T07:20:00Z", "2026-11-10T08:10:00Z"],
                  "aiModels": [["2026-11-10T07:01:00Z", "gpt-6"]]}
        data = {"version": 1, "config": {}, "tickets": {"W-1": ticket}}
        self.path.write_text(json.dumps(data))
        (self.tmp / ".warp" / "agents.json").write_text(json.dumps({"agents": rows}))
        effort.update(data, self.path, now=NOW)
        models = data["tickets"]["W-1"]["ai"]["models"]
        self.assertEqual(sorted(models), ["claude-opus-5-5-max", "gpt-6", "sonnet-5-5"])
        self.assertEqual(models["gpt-6"], {"runs": 1, "minutes": 20, "steps": {"fix": 1}})
        self.assertEqual(models["sonnet-5-5"]["steps"], {"implement": 1})

    def test_the_shuttle_reports_its_model_in_its_folder(self):
        import contextlib
        import io
        import os

        import ticket_state

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        cwd = Path.cwd()
        os.chdir(self.tmp)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ticket_state.append_status(self.tmp, "R-9", "started", step="review", model="claude-sonnet-5-5")
        finally:
            os.chdir(cwd)
        rows = [json.loads(l) for l in (self.tmp / ".warp" / "tickets" / "R-9" / "log.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["model"], "claude-sonnet-5-5")
        ticket = {"id": "R-9"}
        effort.seen_model(ticket, rows[-1]["at"], rows[-1]["model"])
        self.assertEqual(ticket["aiModels"][-1][1], "claude-sonnet-5-5")


class BackupModelTest(unittest.TestCase):
    """1.7.22: a model failure starts the ticket again on backupModel (default grok-4.7-high)."""

    def test_what_counts_as_a_model_failure(self):
        import modelfail

        for text in ("result: A-1 failed: Model claude-opus-5-5 is not available on your plan",
                     "Error: anthropic API overloaded_error, try again later",
                     "your free trial has ended; upgrade your plan to keep using this model",
                     "429 Too Many Requests from the model provider",
                     "invalid model: gpt-7-ultra", "provider returned 503 Service Unavailable"):
            self.assertTrue(modelfail.is_model_error(text), text)
        for text in ("result: A-1 failed: go test ./... FAIL TestModelLoad", "ci red: lint",
                     "Launch hit agent cap 20", "result: A-1 ok", ""):
            self.assertFalse(modelfail.is_model_error(text), text)

    def test_result_line_switches_the_ticket_and_the_run_to_the_backup(self):
        import checkout
        import modelfail
        import orchestrator

        cfg = {"model": "claude-opus-5-5"}
        a = {"id": "A-1", "status": "coding", "size": "M", "locks": ["src/a"], "shuttle": {"step": "implement", "model": "claude-opus-5-5"}}
        b = {"id": "B-1", "status": "queued", "size": "M", "locks": ["src/b"]}
        data = {"version": 1, "config": cfg, "tickets": {"A-1": a, "B-1": b}}
        out = checkout.record_result(data, "A-1", "result: A-1 failed: model claude-opus-5-5 is unavailable (vendor outage)", True)
        self.assertEqual(out, "model-retry")
        self.assertEqual(a["status"], "queued")
        self.assertNotIn("recoveries", a)
        self.assertTrue(data["_modelLines"][0].startswith("model: claude-opus-5-5 failed for A-1"))
        self.assertEqual(orchestrator.model_for(a, cfg, "implement")[0], modelfail.DEFAULT_BACKUP)
        slug, why = orchestrator.model_for(b, cfg, "implement", down=data["modelDown"])
        self.assertEqual(slug, "grok-4.7-high")
        self.assertTrue(why.startswith("backupModel: claude-opus-5-5 is down for this run"))
        # The window ends: the primary is tried again.
        self.assertEqual(orchestrator.model_for(b, cfg, "implement", down=data["modelDown"], now="2099-01-01T00:00:00Z")[0], "claude-opus-5-5")

    def test_the_backup_failing_too_is_an_alarm_and_none_turns_it_off(self):
        import checkout
        import modelfail

        a = {"id": "A-2", "status": "coding", "size": "M", "locks": ["src/a"], "shuttle": {"step": "fix", "model": "grok-4.7-high"}}
        data = {"version": 1, "config": {"model": "claude-opus-5-5"}, "tickets": {"A-2": a}}
        self.assertEqual(checkout.record_result(data, "A-2", "failed: grok model rate limit exceeded", True), "model-alarm")
        self.assertEqual((a["status"], a["alarm"]), ("alarm", "model-failure"))
        self.assertEqual(modelfail.backup({"backupModel": "none"}), "")
        c = {"id": "C-2", "status": "coding", "size": "M", "locks": ["src/c"], "shuttle": {"step": "implement"}}
        off = {"version": 1, "config": {"backupModel": "none"}, "tickets": {"C-2": c}}
        self.assertEqual(checkout.record_result(off, "C-2", "failed: model is unavailable", True), "worker-died")

    def test_a_shuttle_failed_state_with_a_model_error_retries_on_the_backup(self):
        import ticket_state

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".warp").mkdir()
        path = tmp / ".warp" / "beam.json"
        ticket = {"id": "S-3", "status": "coding", "size": "S", "locks": ["src"], "shuttle": {"step": "implement", "startedAt": "2026-11-10T09:00:00Z", "model": "gpt-6"}}
        data = {"version": 1, "config": {}, "tickets": {"S-3": ticket}}
        path.write_text(json.dumps(data))
        lines = []
        changed = ticket_state._apply_event(path, data, ticket, {"state": "failed", "at": "2026-11-10T09:05:00Z",
                                                                 "error": "OpenAI: insufficient_quota for this model", "agent": "subagent:S-3"},
                                           NOW, set(), lines)
        self.assertTrue(changed)
        self.assertEqual(ticket["status"], "queued")
        self.assertEqual(ticket["modelFallback"]["to"], "grok-4.7-high")
        self.assertTrue(any(l.startswith("result: retry S-3 on the backup model") for l in lines), lines)


if __name__ == "__main__":
    unittest.main()
