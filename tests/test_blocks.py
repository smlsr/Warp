"""Run in blocks: strict gate order, ranges, and the pause at the end of a range."""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import beam  # noqa: E402
import blocks  # noqa: E402
import inbound  # noqa: E402
import scan  # noqa: E402


def ticket(tid, status="queued", **extra):
    row = {
        "id": tid,
        "status": status,
        "summary": "ship %s" % tid,
        "size": "S",
        "complexity": "LOW",
        "module": "app",
        "hours": 1,
        "tokens": 100,
        "minutes": 5,
        "autoMerge": True,
        "deps": extra.pop("deps", []),
        "locks": ["src/%s" % tid],
        "critical": False,
        "rankDays": 0,
        "agent": None,
        "branch": "warp/%s" % tid,
        "attempts": 0,
        "alarm": None,
        "pr": {},
        "jira": {},
        "usage": {"cost": 0.25},
    }
    row.update(extra)
    return row


def gate(key, members, status="pending", **extra):
    row = {"key": key, "name": key, "blocking": False, "members": members, "checks": [], "status": status}
    row.update(extra)
    return row


def plan_beam(**extra):
    tickets = {
        "A-1": ticket("A-1", "merged"),
        "A-2": ticket("A-2", "coding"),
        "B-1": ticket("B-1"),
        "B-2": ticket("B-2", deps=["A-1"]),
        "C-1": ticket("C-1"),
        "D-1": ticket("D-1", gate="G2"),
        "E-1": ticket("E-1", deps=["C-1"]),
    }
    body = {
        "version": 1,
        "tickets": tickets,
        "gates": [gate("G0", ["A-1", "A-2"]), gate("G1", ["B-1", "B-2"]), gate("G2", ["C-1"]), gate("G10", [])],
        "runState": "running",
        "paused": False,
        "baseGreen": True,
        "config": {"maxAgents": 8, "runner": "cloud"},
        "program": {"criticalPath": []},
    }
    body.update(extra)
    return body


def ready_ids(data):
    return sorted(t["id"] for t in beam.ready(data))


class RangeParseTests(unittest.TestCase):
    def test_is_range(self):
        for text in ("G3", "G0-", "-G2", "G0-G3", "3-5", "g1..g2", "G1:G2"):
            self.assertTrue(blocks.is_range(text), text)
        for text in ("", "-", "overnight", "G", "meeting at 3pm"):
            self.assertFalse(blocks.is_range(text), text)

    def test_resolve_forms(self):
        data = plan_beam()
        got, err = blocks.resolve(data, "G0-G1")
        self.assertEqual((got["from"], got["to"], got["spec"]), ("G0", "G1", "G0-G1"))
        got, err = blocks.resolve(data, "G1-")
        self.assertEqual((got["from"], got["to"], got["spec"]), ("G1", None, "G1-"))
        got, err = blocks.resolve(data, "G2")
        self.assertEqual((got["from"], got["to"], got["spec"]), ("G2", "G2", "G2"))
        got, err = blocks.resolve(data, "-G2")
        self.assertEqual((got["from"], got["to"], got["spec"]), ("G0", "G2", "-G2"))
        self.assertEqual(blocks.resolve(data, ""), (None, None))

    def test_minus_g2_starts_where_the_last_run_left_off(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        got, err = blocks.resolve(data, "-G2")
        self.assertEqual(got["from"], "G1")

    def test_errors(self):
        data = plan_beam()
        self.assertIn("gate G7 is not in this plan. Gates: G0, G1, G2, G10.", blocks.resolve(data, "G7")[1])
        self.assertIn("comes after", blocks.resolve(data, "G2-G1")[1])
        self.assertIn("not a gate range", blocks.resolve(data, "banana")[1])
        data["gates"] = []
        self.assertIn("no gates", blocks.resolve(data, "G1")[1])

    def test_a_cleared_range_is_refused(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        self.assertIn("already cleared", blocks.resolve(data, "G0")[1])

    def test_apply_sets_and_clears(self):
        data = plan_beam()
        ok, lines = blocks.apply(data, "G0-G1")
        self.assertTrue(ok)
        self.assertEqual(data["range"]["spec"], "G0-G1")
        self.assertIn("range: G0 through G1 (G0-G1).", lines[0])
        ok, lines = blocks.apply(data, "")
        self.assertTrue(ok)
        self.assertNotIn("range", data)
        self.assertIn("range: none (was G0-G1)", lines[0])
        self.assertEqual(blocks.apply(data, "")[1], [])


class AssignTests(unittest.TestCase):
    def test_gates_run_in_number_order(self):
        data = plan_beam()
        data["gates"] = [gate("G10", []), gate("G2", []), gate("G0", []), gate("X", [])]
        self.assertEqual(blocks.keys(data), ["G0", "G2", "G10", "X"])

    def test_explicit_member_derived_and_default(self):
        data = plan_beam()
        data["tickets"]["F-1"] = ticket("F-1")
        got = blocks.assign(data)
        self.assertEqual(got["D-1"], "G2")  # explicit gate field
        self.assertEqual(got["A-2"], "G0")  # member
        self.assertEqual(got["E-1"], "G2")  # derived from its dependency C-1
        self.assertEqual(got["F-1"], "G0")  # nothing: first block

    def test_a_member_of_two_gates_takes_the_lower(self):
        data = plan_beam()
        data["gates"][2]["members"].append("A-2")
        self.assertEqual(blocks.assign(data)["A-2"], "G0")

    def test_a_dependency_cycle_does_not_loop(self):
        data = plan_beam()
        data["tickets"]["X-1"] = ticket("X-1", deps=["X-2"])
        data["tickets"]["X-2"] = ticket("X-2", deps=["X-1"])
        self.assertIn("X-1", blocks.assign(data))

    def test_no_gates_means_no_blocks(self):
        data = plan_beam()
        data["gates"] = []
        self.assertIsNone(blocks.plan(data))
        self.assertEqual(blocks.open_line(data), "blocks: none. The plan has no gates.")


class StrictOrderTests(unittest.TestCase):
    def test_a_block_waits_for_the_one_before_it(self):
        data = plan_beam()
        # G0 still has A-2 open, so G1 and G2 tickets wait
        self.assertEqual(ready_ids(data), [])
        data["tickets"]["A-2"]["status"] = "merged"
        self.assertEqual(ready_ids(data), ["B-1", "B-2"])
        data["tickets"]["B-1"]["status"] = "merged"
        self.assertEqual(ready_ids(data), ["B-2"])
        data["tickets"]["B-2"]["status"] = "merged"
        self.assertEqual(ready_ids(data), ["C-1", "D-1"])

    def test_deps_mode_goes_back_to_dependencies_only(self):
        data = plan_beam(gateOrder="deps")
        self.assertEqual(ready_ids(data), ["B-1", "B-2", "C-1", "D-1"])

    def test_the_config_value_works_too(self):
        data = plan_beam()
        data["config"]["gateOrder"] = "deps"
        self.assertIn("C-1", ready_ids(data))

    def test_a_parked_ticket_holds_the_blocks_after_it(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "parked"
        self.assertEqual(ready_ids(data), [])
        self.assertEqual(blocks.holds(data), ["hold: 2 tickets, G1 waits on G0", "hold: 3 tickets, G2 waits on G0"])

    def test_a_red_gate_holds_the_next_block(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        data["gates"][0]["status"] = "red"
        self.assertEqual(ready_ids(data), [])

    def test_a_skipped_ticket_counts_as_cleared(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "skipped"
        self.assertEqual(ready_ids(data), ["B-1", "B-2"])

    def test_a_block_with_no_tickets_is_skipped(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        data["tickets"] = {k: v for k, v in data["tickets"].items() if k not in {"B-1", "B-2"}}
        data["gates"][1]["members"] = []
        self.assertEqual(ready_ids(data), ["C-1", "D-1"])


class RangeOrderTests(unittest.TestCase):
    def test_a_range_keeps_other_blocks_from_starting(self):
        data = plan_beam(gateOrder="deps")
        blocks.apply(data, "G1")
        self.assertEqual(ready_ids(data), ["B-1", "B-2"])
        blocks.apply(data, "G2")
        self.assertEqual(ready_ids(data), ["C-1", "D-1"])

    def test_a_range_that_starts_late_ignores_the_blocks_before_it(self):
        data = plan_beam()
        blocks.apply(data, "G2")
        # G0 and G1 are not cleared, and G2 still starts: the range asked for G2 alone
        self.assertEqual(ready_ids(data), ["C-1", "D-1"])

    def test_inside_a_range_the_order_is_still_strict(self):
        data = plan_beam()
        blocks.apply(data, "G1-G2")
        self.assertEqual(ready_ids(data), ["B-1", "B-2"])
        data["tickets"]["B-1"]["status"] = "merged"
        data["tickets"]["B-2"]["status"] = "merged"
        self.assertEqual(ready_ids(data), ["C-1", "D-1"])

    def test_an_open_ended_range_runs_to_the_last_block(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        blocks.apply(data, "G1-")
        self.assertEqual(ready_ids(data), ["B-1", "B-2"])

    def test_open_tickets_outside_the_range_are_not_touched(self):
        data = plan_beam()
        blocks.apply(data, "G1")
        self.assertEqual(data["tickets"]["A-2"]["status"], "coding")
        self.assertEqual(blocks.hold_reason(blocks.plan(data), "A-2"), "outside the range: G0 is before G1")


class RangeEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        self.path = self.tmp / ".warp" / "beam.json"

    def write(self, data):
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def test_nothing_happens_before_the_range_is_cleared(self):
        data = plan_beam()
        blocks.apply(data, "G0")
        self.write(data)
        self.assertEqual(blocks.check(self.path), [])
        self.assertEqual(json.loads(self.path.read_text())["runState"], "running")

    def test_a_cleared_range_pauses_the_run_with_a_summary(self):
        data = plan_beam()
        data["tickets"]["A-2"]["status"] = "merged"
        data["tickets"]["A-2"]["minutes"] = 30
        data["range"] = {"spec": "G0", "from": "G0", "to": "G0"}
        self.write(data)
        with redirect_stdout(io.StringIO()), mock.patch("notify.report", lambda *a, **k: None):
            lines = blocks.check(self.path)
        text = "\n".join(lines)
        self.assertIn("range: G0 complete. G0 through G0 are cleared. Pausing.", text)
        self.assertIn("block G0: 2 of 2 merged, gate pending", text)
        self.assertIn("next: G1 has 2 tickets. /warp-start G1- continues, or /warp-start G1 runs it alone.", text)
        self.assertIn("herald: Range G0 complete.", text)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["runState"], "paused")
        self.assertEqual(saved["pauseReason"], "range G0 complete")

    def test_the_last_block_says_so(self):
        data = plan_beam()
        for row in data["tickets"].values():
            row["status"] = "merged"
        data["range"] = {"spec": "G0-", "from": "G0", "to": None}
        self.write(data)
        with redirect_stdout(io.StringIO()), mock.patch("notify.report", lambda *a, **k: None):
            lines = blocks.check(self.path)
        self.assertIn("that was the last block", "\n".join(lines))

    def test_a_paused_run_is_left_alone(self):
        data = plan_beam(paused=True, runState="paused")
        for row in data["tickets"].values():
            row["status"] = "merged"
        data["range"] = {"spec": "G0", "from": "G0", "to": "G0"}
        self.write(data)
        self.assertEqual(blocks.check(self.path), [])

    def test_a_run_with_no_range_is_left_alone(self):
        data = plan_beam()
        for row in data["tickets"].values():
            row["status"] = "merged"
        self.write(data)
        self.assertEqual(blocks.check(self.path), [])


class StartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text("runner: local\ngateOrder: deps\n")
        self.path = self.tmp / ".warp" / "beam.json"
        data = plan_beam(runState="stopped", paused=True)
        self.path.write_text(json.dumps(data))

    def go(self, gates=""):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            code = scan.go(self.path, None, force=True, gates=gates)
        return code, buf.getvalue()

    def test_start_with_a_range_stores_it_and_prints_the_blocks(self):
        code, out = self.go("G1-G2")
        self.assertEqual(code, 0, out)
        self.assertIn("range: G1 through G2 (G1-G2).", out)
        self.assertIn("blocks: G0 1/2, G1 0/2, G2 0/3. range G1-G2.", out)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["range"]["from"], "G1")
        self.assertEqual(saved["gateOrder"], "deps")
        self.assertEqual(saved["runState"], "running")
        self.assertTrue(saved["promptForced"])

    def test_a_bad_range_starts_nothing(self):
        code, out = self.go("G9")
        self.assertEqual(code, 2)
        self.assertIn("gate G9 is not in this plan", out)
        self.assertEqual(json.loads(self.path.read_text())["runState"], "stopped")

    def test_a_start_with_no_range_clears_the_old_one(self):
        self.go("G1")
        code, out = self.go("")
        self.assertEqual(code, 0, out)
        self.assertIn("range: none (was G1)", out)
        self.assertNotIn("range", json.loads(self.path.read_text()))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".warp").mkdir()
        (self.tmp / ".warp" / "config.yaml").write_text("runner: local\n")
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.write_text(json.dumps(plan_beam(runState="stopped", paused=True)))

    def start(self, *extra):
        argv = ["scan.py", "start", "--force", "--beam", str(self.path), *extra]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            try:
                scan.main()
            except SystemExit as e:
                return e.code, buf.getvalue()
        return 0, buf.getvalue()

    def test_the_range_can_be_a_word_after_the_options_or_an_option(self):
        for extra in (("G1-G2",), ("--gates=G1-G2",), ("--gates", "G1-G2")):
            code, out = self.start(*extra)
            self.assertEqual(code, 0, (extra, out))
            self.assertEqual(json.loads(self.path.read_text())["range"]["spec"], "G1-G2", extra)

    def test_a_range_that_starts_with_a_dash_is_not_read_as_an_option(self):
        code, out = self.start("-G1")
        self.assertEqual(code, 0, out)
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["range"]["spec"], saved["range"]["from"], saved["range"]["to"]), ("-G1", "G0", "G1"))


class SlackTests(unittest.TestCase):
    def test_start_and_resume_take_a_range(self):
        data = plan_beam()
        parsed = inbound.parse("warp:start G2-G3 after standup")
        plan = inbound.plan_command(data, parsed, {})
        self.assertEqual(plan["range"], "G2-G3")
        self.assertEqual(plan["reason"], "after standup")
        self.assertEqual(plan["ack"], "Received warp:start. Starting the run blocks G2-G3.")
        plan = inbound.plan_command(data, inbound.parse("warp:resume -G2"), {})
        self.assertEqual(plan["range"], "-G2")
        plan = inbound.plan_command(data, inbound.parse("warp:start"), {})
        self.assertEqual(plan["range"], "")
        self.assertEqual(plan["ack"], "Received warp:start. Starting the run.")

    def test_a_reason_that_is_not_a_range_stays_a_reason(self):
        plan = inbound.plan_command(plan_beam(), inbound.parse("warp:start after lunch"), {})
        self.assertEqual(plan["range"], "")
        self.assertEqual(plan["reason"], "after lunch")


if __name__ == "__main__":
    unittest.main()
