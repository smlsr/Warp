"""A red `make ci` goes back to that ticket. A stale gate still clears."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import alarm_repair  # noqa: E402
import beam  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-06T12:00:00Z"
MEMBERS = ["P-002", "P-003", "P-004", "P-018"]


def plus(stamp, minutes):
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(tid, status="merged", **extra):
    row = {
        "id": tid,
        "status": status,
        "summary": "ship %s" % tid,
        "size": "S",
        "complexity": "LOW",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "autoMerge": True,
        "deps": extra.pop("deps", []),
        "locks": ["src/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "agent": extra.pop("agent", None),
        "branch": "warp/%s" % tid,
        "attempts": extra.pop("attempts", 0),
        "alarm": extra.pop("alarm", None),
        "pr": extra.pop("pr", {}),
        "jira": {},
    }
    row.update(extra)
    return row


class CommandTests(unittest.TestCase):
    def test_make_ci_stays_two_words(self):
        self.assertEqual(orchestrator.norm_check_name("  Make   CI  "), "make ci")
        self.assertTrue(orchestrator.is_make_ci("make ci"))
        self.assertFalse(orchestrator.is_make_ci("make"))
        self.assertFalse(orchestrator.is_make_ci("make check"))
        self.assertEqual(orchestrator.conclusion_kind("make ci"), "")
        self.assertEqual(orchestrator.conclusion_kind("FAILURE"), "red")
        self.assertEqual(orchestrator.conclusion_kind("success"), "green")
        spec = orchestrator.required_checks({"checkCommand": "make ci"})
        self.assertEqual(spec["command"], "make ci")
        self.assertNotIn("make check", spec["detail"])
        unset = orchestrator.required_checks({})
        self.assertNotIn("make check", unset["detail"])

    def test_yaml_keeps_the_space(self):
        samples = [
            "checkCommand: make ci\n",
            'checkCommand: "make ci"\n',
            "checkCommand: make ci # project target\n",
            "model: claude\ncheckCommand: make ci\n",
        ]
        for text in samples:
            self.assertEqual(orchestrator.check_command_text(text), "make ci", text)
        self.assertEqual(orchestrator.check_command_text("checkCommand:\n"), "")
        self.assertEqual(
            orchestrator.resolve_check_command({"checkCommand": ""}, "checkCommand: make ci\n"),
            "make ci",
        )
        self.assertEqual(
            orchestrator.resolve_check_command({"checkCommand": "make ci"}, "checkCommand: make check\n"),
            "make ci",
        )


class MakeCiBeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        warp = self.repo / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"

    def write(self, tickets, gates=None, **extra):
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": gates if gates is not None else [],
            "runState": "running",
            "paused": False,
            "baseGreen": True,
            "config": {"maxAgents": 4, "alarmRepairMinutes": 15, "runner": "cloud", "checkCommand": "make ci"},
            "program": {"criticalPath": []},
        }
        body.update(extra)
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def load(self):
        return json.loads(self.beam_path.read_text())

    def gate(self, key="G1"):
        return next(g for g in self.load()["gates"] if g["key"] == key)

    def shawn(self, member_status=None, evidence=None):
        statuses = {tid: "merged" for tid in MEMBERS}
        if member_status:
            statuses.update(member_status)
        tickets = {tid: ticket(tid, status=statuses[tid]) for tid in MEMBERS}
        tickets["P-019"] = ticket("P-019", status="queued", deps=["P-002"])
        gates = [
            {
                "key": "G1",
                "name": "wave-1",
                "blocking": True,
                "members": list(MEMBERS),
                "checks": ["review"],
                "status": "pending",
                "evidence": evidence,
                "greenAt": None,
            }
        ]
        self.write(tickets, gates)

    def refresh(self):
        return beam.refresh_pending_gates(self.beam_path, now=NOW, run_dispatch=True)

    def starts(self, lines):
        return [line for line in lines if line.startswith("start ")]

    def test_red_named_check_on_review_is_sent_back_and_the_gate_stays(self):
        self.shawn({"P-004": "review"})
        data = self.load()
        data["tickets"]["P-004"]["agent"] = "agent-1"
        data["tickets"]["P-004"]["pr"] = {
            "checks": [{"name": "make ci", "conclusion": "failure", "log": "src/app.py: boom"}],
            "rollup": "red",
        }
        self.beam_path.write_text(json.dumps(data) + "\n")
        lines, _data = self.refresh()
        saved = self.load()["tickets"]["P-004"]
        self.assertEqual(saved["status"], "fix")
        self.assertIsNone(saved["agent"])
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(saved["pr"]["check"], "red")
        self.assertEqual(saved["pr"]["checkName"], "make ci")
        self.assertEqual(saved["pr"]["checkLog"], "src/app.py: boom")
        self.assertEqual(saved["plan"]["blockers"][0]["output"], "src/app.py: boom")
        self.assertIn("send-back P-004 fix", lines)
        self.assertEqual(len(self.starts(lines)), 1)
        self.assertIn("start P-004 checkout=cloud-vm branch=warp/P-004", self.starts(lines)[0])
        self.assertIn("refuse-in-process", self.starts(lines)[0])
        self.assertEqual(self.gate()["status"], "pending")
        journal = (self.beam_path.parent / "journal.jsonl").read_text()
        self.assertIn('"type":"send-back"', journal)
        self.assertIn("make ci", journal)
        again, _data = self.refresh()
        self.assertEqual(self.starts(again), [])
        self.assertNotIn("send-back P-004 fix", again)
        self.assertEqual(self.load()["tickets"]["P-004"]["attempts"], 1)

    def test_yaml_command_sends_each_red_ticket(self):
        self.shawn({"P-004": "review"})
        data = self.load()
        data["config"]["checkCommand"] = ""
        data["tickets"]["P-004"]["pr"] = {"ci": "red", "ciLog": "make: *** [ci] Error 2"}
        data["tickets"]["P-018"]["status"] = "review"
        data["tickets"]["P-018"]["pr"] = {"ci": "red"}
        self.beam_path.write_text(json.dumps(data) + "\n")
        (self.beam_path.parent / "config.yaml").write_text('checkCommand: "make ci"\n')
        lines, _data = self.refresh()
        self.assertEqual(self.load()["tickets"]["P-004"]["status"], "fix")
        self.assertEqual(self.load()["tickets"]["P-004"]["pr"]["check"], "red")
        self.assertIn("make: *** [ci] Error 2", self.load()["tickets"]["P-004"]["plan"]["blockers"][0]["output"])
        self.assertEqual(self.load()["tickets"]["P-018"]["status"], "fix")
        self.assertIn("send-back P-004 fix", lines)
        self.assertIn("send-back P-018 fix", lines)
        self.assertEqual(self.gate()["status"], "pending")

    def test_set_sends_back_one_id(self):
        self.write(
            {
                "P-004": ticket("P-004", status="review", agent="agent-1"),
                "P-009": ticket("P-009", status="review", pr={"ci": "red"}),
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "beam.py"),
                "set",
                "--beam",
                str(self.beam_path),
                "--id",
                "P-004",
                "--ci",
                "red",
                "--check-log",
                "linker failed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("send-back P-004 fix", proc.stdout)
        self.assertIn("start P-004 ", proc.stdout)
        self.assertNotIn("P-009", proc.stdout)
        saved = self.load()
        self.assertEqual(saved["tickets"]["P-004"]["status"], "fix")
        self.assertEqual(saved["tickets"]["P-004"]["pr"]["checkLog"], "linker failed")
        self.assertEqual(saved["tickets"]["P-009"]["status"], "review")
        self.assertEqual(saved["tickets"]["P-009"]["attempts"], 0)

    def test_rollup_stores_the_named_check_and_sends_it_back(self):
        self.write({"P-004": ticket("P-004", status="review", agent="agent-1")})
        checks = self.tmp / "checks.json"
        checks.write_text(
            json.dumps(
                [
                    {"name": "bump", "conclusion": "success"},
                    {"name": "make ci", "conclusion": "failure", "output": "recipe failed"},
                ]
            )
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "provider.py"),
                "rollup",
                "--root",
                str(self.repo),
                "--beam",
                str(self.beam_path),
                "--id",
                "P-004",
                "--checks",
                str(checks),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("red", proc.stdout.splitlines()[0])
        self.assertIn("send-back P-004 fix", proc.stdout)
        saved = self.load()["tickets"]["P-004"]
        self.assertEqual(saved["status"], "fix")
        self.assertEqual(saved["pr"]["rollup"], "red")
        self.assertEqual(saved["pr"]["checks"][1]["name"], "make ci")
        self.assertEqual(saved["pr"]["check"], "red")
        self.assertEqual(saved["plan"]["blockers"][0]["output"], "recipe failed")

    def test_green_named_check_records_green_and_clears_a_stale_gate(self):
        self.shawn()
        data = self.load()
        data["tickets"]["P-002"]["pr"] = {
            "ci": "red",
            "checks": [{"name": "make ci", "conclusion": "success"}],
        }
        self.beam_path.write_text(json.dumps(data) + "\n")
        lines, _data = self.refresh()
        saved = self.load()
        self.assertEqual(saved["tickets"]["P-002"]["pr"]["check"], "green")
        self.assertEqual(saved["tickets"]["P-002"]["pr"]["ci"], "green")
        self.assertEqual(saved["tickets"]["P-002"]["status"], "merged")
        self.assertNotIn("send-back", "\n".join(lines))
        self.assertEqual(self.gate()["status"], "green")
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", lines)
        self.assertTrue(self.starts(lines)[0].startswith("start P-019 "))

    def test_evidence_that_is_only_the_name_does_not_block(self):
        self.shawn(evidence="make ci")
        lines, _data = self.refresh()
        self.assertEqual(self.gate()["status"], "green")
        self.assertIn("herald: G1 pending cleared. Members merged. Tick ran.", lines)

    def test_red_named_check_on_a_merged_member_is_not_cleared(self):
        self.shawn()
        data = self.load()
        data["tickets"]["P-002"]["pr"] = {"checks": [{"name": "make ci", "conclusion": "failure"}]}
        self.beam_path.write_text(json.dumps(data) + "\n")
        lines, _data = self.refresh()
        self.assertEqual(self.gate()["status"], "pending")
        self.assertEqual(self.starts(lines), [])
        self.assertNotIn("send-back", "\n".join(lines))
        self.assertEqual(self.load()["tickets"]["P-002"]["status"], "merged")
        self.assertEqual(self.load()["tickets"]["P-002"]["pr"]["check"], "red")

    def test_green_make_ci_does_not_clear_a_red_bugbot(self):
        self.shawn()
        data = self.load()
        data["tickets"]["P-002"]["pr"] = {
            "ci": "red",
            "bugbot": "fail",
            "checks": [{"name": "make ci", "conclusion": "success"}],
        }
        self.beam_path.write_text(json.dumps(data) + "\n")
        lines, _data = self.refresh()
        saved = self.load()["tickets"]["P-002"]["pr"]
        self.assertEqual(saved["ci"], "green")
        self.assertEqual(saved["check"], "green")
        self.assertEqual(saved["bugbot"], "fail")
        self.assertEqual(self.gate()["status"], "pending")
        self.assertEqual(self.starts(lines), [])

    def test_third_red_parks_without_a_start(self):
        self.write({"P-004": ticket("P-004", status="review", attempts=2, pr={"ci": "red", "checkLog": "still red"})})
        lines, _data = self.refresh()
        saved = self.load()["tickets"]["P-004"]
        self.assertEqual(saved["status"], "parked")
        self.assertEqual(saved["attempts"], 3)
        self.assertIn("send-back P-004 parked", lines)
        self.assertEqual(self.starts(lines), [])

    def test_worker_died_and_an_in_progress_shuttle_stay(self):
        self.write(
            {
                "P-004": ticket(
                    "P-004",
                    status="alarm",
                    alarm="worker-died",
                    pr={"checks": [{"name": "make ci", "conclusion": "failure"}]},
                ),
                "P-005": ticket("P-005", status="coding", agent="busy", pr={"ci": "red"}),
            }
        )
        lines, _data = self.refresh()
        saved = self.load()["tickets"]
        self.assertEqual(saved["P-004"]["status"], "alarm")
        self.assertEqual(saved["P-004"]["alarm"], "worker-died")
        self.assertEqual(saved["P-004"]["attempts"], 0)
        self.assertEqual(saved["P-005"]["status"], "coding")
        self.assertEqual(saved["P-005"]["agent"], "busy")
        self.assertEqual(saved["P-005"]["attempts"], 0)
        self.assertEqual(self.starts(lines), [])

    def test_make_check_is_not_make_ci(self):
        self.write({"P-004": ticket("P-004", status="review")})
        data = self.load()
        data["config"]["checkCommand"] = ""
        data["tickets"]["P-004"]["pr"] = {"checks": [{"name": "make check", "conclusion": "failure"}]}
        self.beam_path.write_text(json.dumps(data) + "\n")
        lines, _data = self.refresh()
        self.assertEqual(self.load()["tickets"]["P-004"]["status"], "review")
        self.assertNotIn("send-back", "\n".join(lines))

    def test_paused_does_not_send_back(self):
        self.write(
            {"P-004": ticket("P-004", status="review", pr={"ci": "red"})},
            runState="paused",
            paused=True,
        )
        lines, _data = self.refresh()
        self.assertEqual(lines, [])
        self.assertEqual(self.load()["tickets"]["P-004"]["status"], "review")


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / "repo" / ".warp"
        warp.mkdir(parents=True)
        self.beam_path = warp / "beam.json"
        tickets = {tid: ticket(tid, status="merged") for tid in MEMBERS}
        tickets["P-004"]["status"] = "review"
        tickets["P-004"]["pr"] = {"checks": [{"name": "make ci", "conclusion": "failure", "log": "no rule to make ci"}]}
        tickets["P-019"] = ticket("P-019", status="queued", deps=["P-002"])
        body = {
            "version": 1,
            "tickets": tickets,
            "gates": [
                {
                    "key": "G1",
                    "name": "wave-1",
                    "blocking": True,
                    "members": list(MEMBERS),
                    "checks": ["review"],
                    "status": "pending",
                    "evidence": None,
                    "greenAt": None,
                }
            ],
            "runState": "running",
            "paused": False,
            "baseGreen": True,
            "config": {"maxAgents": 4, "alarmRepairMinutes": 15, "runner": "cloud", "checkCommand": "make ci"},
            "program": {"criticalPath": []},
        }
        self.beam_path.write_text(json.dumps(body, indent=2) + "\n")

    def test_empty_ready_pass_sends_the_red_check_back_once(self):
        self.assertEqual(beam.ready(json.loads(self.beam_path.read_text())), [])
        first = alarm_repair.next_repair(self.beam_path, now=NOW)
        self.assertIn("send-back P-004 fix", first)
        starts = [line for line in first if line.startswith("start ")]
        self.assertEqual(len(starts), 1)
        self.assertTrue(starts[0].startswith("start P-004 "))
        data = json.loads(self.beam_path.read_text())
        self.assertEqual(data["tickets"]["P-004"]["status"], "fix")
        self.assertEqual(data["gates"][0]["status"], "pending")
        self.assertNotIn("herald: G1 pending cleared. Members merged. Tick ran.", first)
        immediate = alarm_repair.next_repair(self.beam_path, now=NOW)
        self.assertIn("alarm-repair: wait", immediate)
        self.assertEqual([line for line in immediate if line.startswith("start ")], [])
        later = alarm_repair.next_repair(self.beam_path, now=plus(NOW, 15))
        self.assertNotIn("send-back P-004 fix", later)
        self.assertEqual([line for line in later if line.startswith("start ")], [])
        self.assertEqual(json.loads(self.beam_path.read_text())["tickets"]["P-004"]["attempts"], 1)
