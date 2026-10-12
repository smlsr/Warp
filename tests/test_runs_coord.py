"""Runs that wait on each other: startAfter, cross-run dependencies, paths, locks, base-fix owner."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_blocks import gate, ticket as btk  # noqa: E402
from test_runs import CONFIG, RepoCase, git, ticket  # noqa: E402

import beam  # noqa: E402
import orchestrator  # noqa: E402
import runs  # noqa: E402
import scan  # noqa: E402

CORE_GATES = [gate("G0", ["C-1"]), gate("G1", ["C-2"]), gate("G2", ["C-3"])]


def core_tickets(*merged):
    return {tid: ticket(tid, "merged" if tid in merged else "queued") for tid in ("C-1", "C-2", "C-3")}


class StartAfterTests(RepoCase):
    def publish_core(self, *merged):
        self.run_published(self.repo, "core", core_tickets(*merged), gates=CORE_GATES)

    def test_parse(self):
        self.assertEqual(runs.parse_start_after("core:G2"), [("core", "G2")])

    def test_not_met_until_the_gate_and_every_block_before_it_are_cleared(self):
        self.publish_core("C-1")
        status = runs.start_after_status(self.repo, "HEAD", [("core", "G1")])
        self.assertFalse(status[0][2])
        self.assertIn("core is at G1 (0 of 1 merged). It must clear G1 first", status[0][3])
        self.publish_core("C-1", "C-2")
        self.assertTrue(runs.start_after_status(self.repo, "HEAD", [("core", "G1")])[0][2])
        self.publish_core("C-2", "C-3")  # G0 is not cleared, so G2 is not reached
        self.assertFalse(runs.start_after_status(self.repo, "HEAD", [("core", "G2")])[0][2])

    def test_an_unpublished_run_and_an_unknown_gate(self):
        self.publish_core("C-1")
        self.assertIn("no run named ui is published", runs.start_after_status(self.repo, "HEAD", [("ui", "G1")])[0][3])
        self.assertIn("core has no gate G7. It has: G0, G1, G2", runs.start_after_status(self.repo, "HEAD", [("core", "G7")])[0][3])

    def test_blocker_text(self):
        self.publish_core()
        refusal, lines = runs.start_after_blocker(self.repo, "admin", "core:G1")
        self.assertEqual(refusal, "refuse: admin starts after core:G1. /warp-start --now starts it anyway.")
        self.assertIn("startAfter: core:G1 not met (", lines[0])
        self.assertEqual(runs.start_after_blocker(self.repo, "admin", ""), (None, []))
        self.publish_core("C-1", "C-2")
        refusal, lines = runs.start_after_blocker(self.repo, "admin", "core:G1")
        self.assertIsNone(refusal)
        self.assertIn("met (core cleared G1)", lines[0])

    def test_start_refuses_then_goes_with_now(self):
        self.publish_core()
        two = self.second_checkout()
        self.write_config(two, CONFIG.replace('scanFolder: "HOS/UI/Admin"\n', "") + "startAfter: core:G1\n", run="admin")
        self.write_beam(two, {"A-1": ticket("A-1")}, run="admin", runState="stopped", paused=True)
        path = two / ".warp" / "beam.json"
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            code = scan.go(path, None, force=True)
        self.assertEqual(code, 2, buf.getvalue())
        self.assertIn("refuse: admin starts after core:G1", buf.getvalue())
        self.assertEqual(json.loads(path.read_text())["runState"], "stopped")
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("notify.report", lambda *a, **k: None):
            code = scan.go(path, None, force=True, now=True)
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(json.loads(path.read_text())["runState"], "running")

    def test_list_shows_whether_the_wait_is_met(self):
        self.publish_core()
        two = self.second_checkout()
        self.write_config(two, CONFIG.replace('scanFolder: "HOS/UI/Admin"\n', "") + "startAfter: core:G1\n", run="admin")
        self.write_beam(two, {"A-1": ticket("A-1")}, run="admin", runState="stopped")
        (two / ".warp" / "scan.json").write_text(json.dumps({"scannedAt": "2026-10-12T12:00:00Z"}))
        self.assertEqual(__import__("state_commit").publish_base(two, force=True), 0)
        code, out = run_cli("list", "--root", str(self.repo))
        self.assertEqual(code, 0)
        self.assertIn("startAfter=core:G1(waits)", out)
        self.publish_core("C-1", "C-2")
        code, out = run_cli("list", "--root", str(self.repo))
        self.assertIn("startAfter=core:G1(met)", out)


class CrossRunDepTests(RepoCase):
    def test_dep_ids_keep_the_run(self):
        self.assertEqual(scan.dep_ids("P-001, core:P-014 and ui:WV-02"), ["P-001", "core:P-014", "ui:WV-02"])
        self.assertEqual(scan.dep_ids(""), [])

    def test_split_dep(self):
        self.assertEqual(runs.split_dep("core:P-014"), ("core", "P-014"))
        self.assertEqual(runs.split_dep("P-014"), ("", "P-014"))
        self.assertEqual(runs.split_dep("Core:P-014"), ("core", "P-014"))

    def admin_beam(self, **extra):
        data = {
            "run": "admin",
            "tickets": {"A-1": btk("A-1", deps=["core:C-1"]), "A-2": btk("A-2")},
            "gates": [],
            "runState": "running",
            "paused": False,
            "baseGreen": True,
            "config": {"maxAgents": 8, "runner": "cloud"},
            "program": {"criticalPath": []},
        }
        data.update(extra)
        return data

    def ready(self, data):
        return sorted(t["id"] for t in beam.ready(data))

    def test_a_dependency_on_another_run_waits_for_its_status(self):
        data = self.admin_beam()
        self.assertEqual(self.ready(data), ["A-2"])
        data["external"] = {"status": {"core:C-1": "coding"}, "locks": []}
        self.assertEqual(self.ready(data), ["A-2"])
        data["external"]["status"]["core:C-1"] = "merged"
        self.assertEqual(self.ready(data), ["A-1", "A-2"])

    def test_refresh_reads_the_other_runs_published_state(self):
        self.run_published(
            self.repo,
            "core",
            {"C-1": ticket("C-1", "merged"), "C-2": ticket("C-2", "coding", locks=["src/api"])},
        )
        two = self.second_checkout()
        data = self.admin_beam()
        self.assertTrue(runs.refresh_external(two, data, force=True))
        self.assertEqual(data["external"]["status"], {"core:C-1": "merged"})
        self.assertEqual(data["external"]["locks"], [{"run": "core", "id": "C-2", "locks": ["src/api"]}])
        self.assertEqual(self.ready(data), ["A-1", "A-2"])
        self.assertFalse(runs.refresh_external(two, data))  # throttled
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        self.assertTrue(runs.refresh_external(two, data, now=later))

    def test_an_unnamed_run_reads_nothing(self):
        data = self.admin_beam()
        data.pop("run")
        self.assertFalse(runs.refresh_external(self.repo, data, force=True))
        self.assertNotIn("external", data)

    def test_the_lock_another_run_holds_keeps_a_ticket_out(self):
        data = self.admin_beam()
        data["tickets"]["A-2"]["locks"] = ["src/api/users"]
        data["external"] = {"status": {"core:C-1": "merged"}, "locks": [{"run": "core", "id": "C-2", "locks": ["src/api"]}]}
        self.assertEqual(self.ready(data), ["A-1"])
        data["external"]["locks"] = []
        self.assertEqual(self.ready(data), ["A-1", "A-2"])

    def test_check_accepts_a_dependency_on_another_run(self):
        data = self.admin_beam()
        data["tickets"]["A-1"]["status"] = "queued"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(beam.cmd_check(data), 0)

    def test_skip_owned_rewrites_dependents_to_the_owner(self):
        self.run_published(self.repo, "core", {"C-1": ticket("C-1")})
        two = self.second_checkout()
        graph = {
            "tickets": [{"id": "C-1", "deps": []}, {"id": "A-1", "deps": ["C-1"], "after": ["C-1", "A-2"]}, {"id": "A-2", "deps": []}],
            "gates": [{"key": "G0", "members": ["C-1", "A-1"]}],
            "criticalPath": ["C-1"],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scan.apply_ownership(two, "admin", graph, True)
        self.assertEqual(code, 0)
        by_id = {t["id"]: t for t in graph["tickets"]}
        self.assertEqual(sorted(by_id), ["A-1", "A-2"])
        self.assertEqual(by_id["A-1"]["deps"], ["core:C-1"])
        self.assertEqual(by_id["A-1"]["after"], ["core:C-1", "A-2"])
        self.assertIn("They now wait for it as <run>:<id>: A-1", buf.getvalue())

    def test_scan_warns_about_a_run_that_is_not_published(self):
        data = self.admin_beam()
        self.write_config(self.repo, run="admin")
        lines = runs.scope_warnings(self.repo, "admin", data)
        self.assertEqual(len(lines), 1)
        self.assertIn("warn: core is not ready for admin: A-1 needs core:C-1", lines[0])
        self.run_published(self.repo, "core", {"C-9": ticket("C-9")})
        self.write_config(self.repo, run="admin")
        lines = runs.scope_warnings(self.repo, "admin", data)
        self.assertIn("A-1 needs core:C-1, which core does not have", lines[0])


class PathsTests(RepoCase):
    def test_parse_and_match(self):
        self.assertEqual(runs.parse_paths("HOS/UI/Admin, packages/admin/"), ["HOS/UI/Admin", "packages/admin"])
        self.assertEqual(runs.parse_paths(["a/", " b "]), ["a", "b"])
        self.assertEqual(runs.parse_paths(""), [])
        self.assertEqual(runs.parse_paths("*"), [])
        paths = ["HOS/UI"]
        self.assertEqual(runs.outside_paths(paths, ["HOS/UI/a/**", "HOS/UI", "HOS/UI/*"]), [])
        self.assertEqual(runs.outside_paths(paths, ["HOS/UIx", "HOS", "other"]), ["HOS/UIx", "HOS", "other"])
        self.assertEqual(runs.outside_paths([], ["anything"]), [])

    def beam_with_paths(self):
        return {
            "run": "admin",
            "paths": ["HOS/UI"],
            "tickets": {"A-1": btk("A-1", locks=["HOS/UI/admin/**"]), "A-2": btk("A-2", locks=["HOS/core/api"]), "A-3": btk("A-3", locks=[])},
            "gates": [],
            "runState": "running",
            "paused": False,
            "baseGreen": True,
            "config": {"maxAgents": 8},
            "program": {"criticalPath": []},
        }

    def test_a_ticket_outside_the_paths_does_not_start(self):
        data = self.beam_with_paths()
        self.assertEqual(sorted(t["id"] for t in beam.ready(data)), ["A-1", "A-3"])
        data["paths"] = []
        self.assertEqual(sorted(t["id"] for t in beam.ready(data)), ["A-1", "A-2", "A-3"])

    def test_start_prints_the_hold(self):
        lines = scan.path_holds(self.beam_with_paths())
        self.assertEqual(lines, ["hold: 1 tickets, locks outside this run's paths (HOS/UI): A-2"])
        self.assertEqual(scan.path_holds({"tickets": {}}), [])

    def test_a_scan_warns_about_tickets_outside_the_paths(self):
        self.write_config(self.repo, CONFIG.replace('scanFolder: "HOS/UI/Admin"', 'scanFolder: ""') + "paths: HOS/UI\n", run="admin")
        lines = runs.scope_warnings(self.repo, "admin", self.beam_with_paths())
        self.assertTrue(lines[0].startswith("paths: 1 tickets have locks outside this run's paths (HOS/UI) and will not start: A-2 (HOS/core/api)"), lines)


class BaseFixOwnerTests(unittest.TestCase):
    def red(self, run, owner):
        return {"run": run, "baseFixOwner": owner, "baseGreen": False, "tickets": {"T-1": btk("T-1")}, "config": {}}

    def test_no_owner_means_every_run_fixes_its_own(self):
        self.assertTrue(orchestrator.needs_base_fix(self.red("admin", "")))

    def test_only_the_owner_dispatches_the_fix(self):
        self.assertTrue(orchestrator.needs_base_fix(self.red("core", "core")))
        self.assertFalse(orchestrator.needs_base_fix(self.red("admin", "core")))

    def test_a_green_base_needs_none(self):
        data = self.red("core", "core")
        data["baseGreen"] = True
        self.assertFalse(orchestrator.needs_base_fix(data))

    def test_the_other_run_does_not_reserve_a_slot(self):
        data = self.red("admin", "core")
        data["runState"] = "running"
        data["paused"] = False
        data["gates"] = []
        data["program"] = {"criticalPath": []}
        data["config"] = {"maxAgents": 1}
        # base red holds nothing back by itself; the point is that no slot is kept for a fix
        self.assertEqual([t["id"] for t in beam.ready(data)], ["T-1"])


def run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = runs.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code or 0, buf.getvalue()


if __name__ == "__main__":
    unittest.main()
