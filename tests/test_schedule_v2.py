"""build-schedule/2: parsing, repo selection, milestone gates, priority gates, named ranges, other repos."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import beam  # noqa: E402
import blocks  # noqa: E402
import buildsched  # noqa: E402
import orchestrator  # noqa: E402
import runs  # noqa: E402
import scan  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "build_schedule_v2.json"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "remote", "add", "origin", origin)
    return path


class ParseTest(unittest.TestCase):
    def test_every_field_is_read(self):
        g = scan.from_schedule(FIX, Path(tempfile.mkdtemp()))
        self.assertEqual(g["format"], "build-schedule/2")
        t = {x["id"]: x for x in g["tickets"]}
        ar = t["AR-00"]
        self.assertEqual(ar["hours"], 11)
        self.assertEqual(ar["locks"], ["scripts/ready.sh", "apps/admin/tools/ready"])  # repo prefix dropped
        self.assertEqual(ar["priorityGate"], "G-RBAC")
        self.assertEqual(ar["checks"], [{"name": "ready (G0)", "check": "make ready"}])
        self.assertEqual(ar["planned"]["slot"], 1)
        self.assertEqual((ar["repo"], ar["lane"]), ("Core", "1 ADMIN backend"))
        self.assertIsNone(t["AD-01"]["gate"])
        self.assertEqual(g["criticalPath"], ["AR-00", "AD-01", "AU-01", "AD-04"])
        gates = {x["key"]: x for x in g["gates"]}
        self.assertEqual(gates["G-RBAC"]["starts"], ["Demo-UI"])
        self.assertEqual(gates["G-RBAC"]["siblings"], ["Demo-UI"])
        self.assertTrue(gates["G-RBAC"]["priority"])
        self.assertEqual(gates["G0"]["onGreen"], "make gate-close GATE=G0")
        self.assertEqual(gates["G0"]["ifRed"], "Nobody starts.")
        self.assertEqual({x["hold"] for x in g["gates"]}, {"red"})
        self.assertEqual(g["schedule"]["gateOrder"], "deps")

    def test_workspace_keeps_prefix(self):
        root = Path(tempfile.mkdtemp())
        (root / "Core").mkdir()
        g = scan.from_schedule(FIX, root)
        self.assertEqual(g["tickets"][0]["locks"][0], "Core/scripts/ready.sh")

    def test_strict_model_keeps_warp_rule(self):
        data = json.loads(FIX.read_text())
        data["model"]["gate_order"] = "strict"
        g = buildsched.parse(data, FIX)
        self.assertEqual(g["schedule"]["gateOrder"], "strict")
        self.assertEqual({x["hold"] for x in g["gates"]}, {"green"})

    def test_parse_map(self):
        for raw in ("{Core-UI: admin-ui, X: y}", "Core-UI=admin-ui, X=y", '{"Core-UI": "admin-ui", "X": "y"}', {"Core-UI": "admin-ui", "X": "y"}):
            self.assertEqual(buildsched.parse_map(raw), {"Core-UI": "admin-ui", "X": "y"})
        self.assertEqual(buildsched.parse_map("{admin-ui: https://github.com/o/Core-UI.git#main}"), {"admin-ui": "https://github.com/o/Core-UI.git#main"})


class RepoTest(unittest.TestCase):
    def test_pick_repo_keeps_one_and_rewires(self):
        g = scan.from_schedule(FIX, Path(tempfile.mkdtemp()))
        got, lines = buildsched.pick_repo(g, Path(tempfile.mkdtemp()), {"repo": "Core", "repoRuns": "{Core-UI: admin-ui}"})
        self.assertEqual(got, "Core")
        ids = [t["id"] for t in g["tickets"]]
        self.assertNotIn("AU-01", ids)
        ad04 = next(t for t in g["tickets"] if t["id"] == "AD-04")
        self.assertEqual(ad04["after"], ["admin-ui:AU-01"])
        g1 = next(x for x in g["gates"] if x["key"] == "G1")
        self.assertEqual((g1["members"], g1["otherMembers"]), (["AD-03"], ["AU-01"]))
        self.assertNotIn("AU-01", g["criticalPath"])
        self.assertTrue(any("externalRepos" in l for l in lines))

    def test_default_run_name_and_unknown_checkout(self):
        g = scan.from_schedule(FIX, Path(tempfile.mkdtemp()))
        buildsched.pick_repo(g, Path(tempfile.mkdtemp()), {"repo": "core"})
        self.assertEqual(next(t for t in g["tickets"] if t["id"] == "AD-04")["after"], ["core-ui:AU-01"])
        g = scan.from_schedule(FIX, Path(tempfile.mkdtemp()))
        got, lines = buildsched.pick_repo(g, Path(tempfile.mkdtemp()), {"repo": "Elsewhere"})
        self.assertIsNone(got)
        self.assertEqual(len(g["tickets"]), 6)
        self.assertIn("set `repo: <name>`", lines[1])

    def test_scan_end_to_end(self):
        tmp = Path(tempfile.mkdtemp())
        root = repo(tmp / "Core", "https://github.com/example/Core.git")
        (root / "PLAN/plan").mkdir(parents=True)
        shutil.copy(FIX, root / "PLAN/plan/schedule.json")
        (root / ".warp").mkdir()
        (root / ".warp/config.yaml").write_text("run: admin\nrepoRuns: {Core-UI: admin-ui}\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        out = subprocess.run([sys.executable, str(ROOT / "scripts/scan.py"), "scan", "--folder", "PLAN", "--out", ".warp/beam.json"], cwd=root, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("repo: Core — 5 of 6 tickets", out.stdout)
        self.assertIn("gate order deps", out.stdout)
        b = json.loads((root / ".warp/beam.json").read_text())
        self.assertEqual(len(b["tickets"]), 5)
        self.assertEqual(b["tickets"]["AD-04"]["after"], ["admin-ui:AU-01"])
        self.assertEqual(b["tickets"]["AR-00"]["locks"], ["scripts/ready.sh", "apps/admin/tools/ready"])
        self.assertEqual(b["tickets"]["AR-00"]["priorityGate"], "G-RBAC")
        self.assertEqual(b["tickets"]["AR-00"]["checks"][0]["check"], "make ready")
        self.assertEqual(b["tickets"]["AR-00"]["planned"]["merge"], 11.0)
        self.assertEqual(b["program"]["gateOrder"], "deps")
        self.assertEqual(b["program"]["schedule"]["repo"], "Core")
        gates = {g["key"]: g for g in b["gates"]}
        self.assertEqual((gates["G0"]["hold"], gates["G0"]["onGreen"], gates["G-RBAC"]["starts"]), ("red", "make gate-close GATE=G0", ["Demo-UI"]))
        self.assertEqual(blocks.keys(b), ["G0", "G-RBAC", "G1", "G2"])
        self.assertEqual(blocks.order_mode(b), "deps")


def mini_beam():
    g = scan.from_schedule(FIX, Path(tempfile.mkdtemp()))
    tickets = {}
    for t in g["tickets"]:
        tickets[t["id"]] = dict(t, status="queued", autoMerge=True)
    gates = [dict(x, status="pending") for x in g["gates"]]
    return {"runState": "running", "paused": False, "config": {"maxAgents": 18}, "tickets": tickets, "gates": gates,
            "program": {"gateOrder": "deps"}, "agents": {}}


class DispatchTest(unittest.TestCase):
    def test_pending_milestone_gate_holds_nothing(self):
        b = mini_beam()
        b["tickets"]["AR-00"]["status"] = "merged"
        b["tickets"]["AD-01"]["status"] = "merged"
        b["tickets"]["AU-01"]["status"] = "merged"
        # G1 (blocking) is pending: AD-03 is unmerged, yet AD-04 (needs AU-01, a G1 member) is free to start
        self.assertIsNone(beam.gate_blocks(b, "AD-04", {}))

    def test_red_gates(self):
        b = mini_beam()
        gates = {g["key"]: g for g in b["gates"]}
        gates["G1"]["status"] = "red"
        self.assertEqual(beam.gate_blocks(b, "AD-02", {}), "G1")  # blocking red: everyone else stops
        self.assertIsNone(beam.gate_blocks(b, "AD-03", {}))       # a member keeps going (it is the fix)
        gates["G1"]["status"] = "green"
        gates["G-RBAC"]["status"] = "red"
        self.assertIsNone(beam.gate_blocks(b, "AD-03", {}))       # non-blocking red: unrelated tickets go on
        b["tickets"]["AD-03"]["after"] = ["AD-02"]
        self.assertEqual(beam.gate_blocks(b, "AD-03", {}), "G-RBAC")  # its dependents wait

    def test_legacy_gate_still_holds_until_green(self):
        b = mini_beam()
        for g in b["gates"]:
            g.pop("hold")
        b["tickets"]["AR-00"]["status"] = "merged"
        # G0 is still pending, so under the old rule it holds AR-00's dependents.
        self.assertEqual(beam.gate_blocks(b, "AD-04", {}), "G0")
        for g in b["gates"]:
            if g["key"] in ("G0", "G-RBAC"):
                g["status"] = "green"
        self.assertEqual(beam.gate_blocks(b, "AD-04", {}), "G1")

    def test_priority_gate_sorts_after_critical_before_rank(self):
        a = {"id": "A", "rank": 1}
        p = {"id": "P", "rank": 9, "priorityGate": "G-RBAC"}
        c = {"id": "C", "rank": 20, "critical": True}
        got = sorted([a, p, c], key=orchestrator.sort_key)
        self.assertEqual([t["id"] for t in got], ["C", "P", "A"])

    def test_named_ranges(self):
        b = mini_beam()
        found, err = blocks.resolve(b, "G-RBAC")
        self.assertIsNone(err)
        self.assertEqual((found["from"], found["to"]), ("G-RBAC", "G-RBAC"))
        found, err = blocks.resolve(b, "G0..g-rbac")
        self.assertEqual((found["from"], found["to"]), ("G0", "G-RBAC"))
        found, err = blocks.resolve(b, "G0-G1")
        self.assertEqual(blocks.keys(b)[blocks.keys(b).index(found["from"]): blocks.keys(b).index(found["to"]) + 1], ["G0", "G-RBAC", "G1"])

    def test_on_green_and_starts_lines(self):
        b = mini_beam()
        g = next(x for x in b["gates"] if x["key"] == "G-RBAC")
        g["_clearedCondition"] = "pending"
        lines = beam.gate_flip_lines([g], b, False)
        self.assertIn("gate: G-RBAC on_green: make gate-close GATE=G-RBAC — the parent runs it now and reports the result", lines)
        self.assertTrue(any(l.startswith("gate: G-RBAC starts Demo-UI") for l in lines))


class ExternalRepoTest(unittest.TestCase):
    def test_reads_other_repos_published_beam(self):
        tmp = Path(tempfile.mkdtemp())
        ui = repo(tmp / "Core-UI", "https://github.com/example/Core-UI.git")
        (ui / ".warp/runs/admin-ui").mkdir(parents=True)
        (ui / ".warp/runs/admin-ui/beam.json").write_text(json.dumps({"run": "admin-ui", "tickets": {"AU-01": {"status": "merged"}}}))
        git(ui, "add", "-A")
        git(ui, "commit", "-qm", "published")
        core = repo(tmp / "Core", str(tmp / "nowhere.git"))
        (core / "x").write_text("x")
        git(core, "add", "-A")
        git(core, "commit", "-qm", "init")
        b = {"run": "admin", "externalRepos": {"admin-ui": str(ui)}, "tickets": {"AD-04": {"status": "queued", "after": ["admin-ui:AU-01"]}}}
        with contextlib.redirect_stderr(io.StringIO()):
            changed = runs.refresh_external(core, b, force=True, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        self.assertTrue(changed)
        self.assertEqual(b["external"]["status"], {"admin-ui:AU-01": "merged"})
        self.assertEqual(b["external"]["repos"], {"admin-ui": "refs/warp/external/admin-ui"})
        self.assertEqual(runs.external_status(b, "admin-ui:AU-01"), "merged")

    def test_unreachable_repo_waits(self):
        tmp = Path(tempfile.mkdtemp())
        core = repo(tmp / "Core", str(tmp / "nowhere.git"))
        (core / "x").write_text("x")
        git(core, "add", "-A")
        git(core, "commit", "-qm", "init")
        b = {"run": "admin", "externalRepos": "{admin-ui: " + str(tmp / "missing") + "}", "tickets": {"AD-04": {"status": "queued", "after": ["admin-ui:AU-01"]}}}
        runs.refresh_external(core, b, force=True)
        self.assertEqual(b["external"]["repos"], {"admin-ui": "unreachable"})
        self.assertIsNone(runs.external_status(b, "admin-ui:AU-01"))


class DashboardTest(unittest.TestCase):
    def test_page_reads_build_schedule_2(self):
        text = (ROOT / "assets/warp-dashboard.html").read_text()
        for needle in ("function isScheduleV2", "function runFromSchedule", 'value="lane"', 'value="repo"', "Checks when it merges", "Priority gate", "as written in schedule.json", "on green: "):
            self.assertIn(needle, text)

    def test_show_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            buildsched.main(["show", str(FIX), "--repo", "Core"])
        text = out.getvalue()
        self.assertIn("schedule: Demo-Admin build-schedule/2 tickets=6", text)
        self.assertIn("AD-04 ← AU-01", text)
        self.assertIn("gate: G-RBAC at T+15.0h scope: UI program start priority starts Demo-UI", text)


if __name__ == "__main__":
    unittest.main()
