"""/warp-from-jira: Jira issues with given labels become a plan folder /warp-scan reads."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jira_pull  # noqa: E402
import plansim  # noqa: E402
import scan  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "jira_pull"


def run(argv, root: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = jira_pull.main(["--root", str(root)] + argv)
    return code, out.getvalue()


class Repo:
    def __init__(self, config: str = 'jiraProject: "WAR"\nmaxAgents: 4\n'):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / ".warp").mkdir()
        (self.dir / ".warp/config.yaml").write_text(config)

    def results(self, name: str = "search.json") -> Path:
        dest = self.dir / ".warp/jira-pull/issues.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / name, dest)
        return dest

    def schedule(self, folder: str = "warp-plans/version-1.5.5") -> dict:
        return json.loads((self.dir / folder / "plan/schedule.json").read_text())

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def test_any_label_in_the_configured_project(self):
        code, out = run(["plan", "version:1.5.5,ui"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn('jql: project = "WAR" AND labels in ("version:1.5.5", "ui") ORDER BY key ASC', out)
        self.assertIn("folder: warp-plans/version-1.5.5-ui (new)", out)
        req = json.loads((self.repo.dir / ".warp/jira-pull.json").read_text())
        self.assertEqual(req["labels"], ["version:1.5.5", "ui"])
        self.assertIn("issuelinks", req["fields"])
        self.assertIn("searchJiraIssuesUsingJql", out)

    def test_every_label_any_project_extra_clause_and_folder(self):
        code, out = run(["plan", "a", "b", "--match", "all", "--any-project", "--jql", "type != Bug", "--folder", "plans/x"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn('jql: labels = "a" AND labels = "b" AND (type != Bug) ORDER BY key ASC', out)
        self.assertIn("folder: plans/x", out)

    def test_external_id_field_by_id_is_requested(self):
        self.repo.close()
        self.repo = Repo('jiraExternalIdField: "customfield_10050"\n')
        code, out = run(["plan", "v1"], self.repo.dir)
        self.assertIn("customfield_10050", out)
        self.assertNotIn("project =", out)

    def test_refuses_a_folder_that_has_a_plan_unless_forced(self):
        plan = self.repo.dir / "warp-plans/v1/plan"
        plan.mkdir(parents=True)
        (plan / "schedule.json").write_text('{"tickets": []}')
        code, out = run(["plan", "v1"], self.repo.dir)
        self.assertEqual(code, 3)
        self.assertIn("refuse: warp-plans/v1 already has a plan", out)
        code, out = run(["plan", "v1", "--force"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("(rebuilt)", out)

    def test_no_label_is_refused(self):
        code, out = run(["plan", ","], self.repo.dir)
        self.assertEqual(code, 2)


class ReadTest(unittest.TestCase):
    def test_adf_headings_lists_and_code(self):
        doc = {"type": "doc", "content": [
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Locks"}]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "src/a/", "marks": [{"type": "code"}]}]}]}]}]}
        text = jira_pull.text_of(doc)
        self.assertEqual(jira_pull.sections(text)["locks"], "- `src/a/`")
        self.assertEqual(jira_pull.clean_path("`src/a/`"), "src/a/")
        self.assertEqual(jira_pull.clean_path("{{src/b/}}"), "src/b/")

    def test_sizes(self):
        self.assertEqual(jira_pull.size_of("size:XL"), "XL")
        self.assertEqual(jira_pull.size_of("M — auto-merge"), "M")
        self.assertEqual(jira_pull.size_of("Large"), "L")
        self.assertIsNone(jira_pull.size_of("tbd"))
        self.assertEqual(jira_pull.size_from_hours(11), "L")

    def test_issue_fields(self):
        issues, names = jira_pull.load_results(FIX / "search.json")
        self.assertEqual(len(issues), 7)
        rows = {r["key"]: r for r in (jira_pull.read_issue(i, names, {}) for i in issues)}
        a = rows["WAR-1"]
        self.assertEqual((a["id"], a["idFrom"], a["size"], a["sizeFrom"], a["locks"], a["gate"]), ("APP-01", "field External ID", "S", "label", ["app/build/"], "G0"))
        self.assertEqual(a["summary"], "App scaffold")
        self.assertEqual(a["acs"], ["build runs", "test runs"])
        c = rows["WAR-3"]
        self.assertEqual((c["id"], c["size"], c["sizeFrom"], c["hours"], c["lane"], c["module"]), ("WAR-3", "L", "original estimate", 11.0, "ui", "UI"))
        self.assertEqual([r["ref"] for r in c["refs"]], ["WAR-1", "WAR-9"])
        self.assertTrue(c["refs"][1]["done"])
        e = rows["WAR-5"]
        self.assertEqual([(r["run"], r["ref"]) for r in e["refs"]], [(None, "WAR-3"), ("view", "WV-15")])
        self.assertTrue(rows["WAR-6"]["done"])

    def test_label_and_summary_ids_without_a_field(self):
        issue = {"key": "WAR-8", "fields": {"summary": "[WV-08] Totals", "labels": ["v"], "status": {"name": "To Do"}}}
        self.assertEqual(jira_pull.read_issue(issue, {}, {})["id"], "WV-08")
        issue["fields"]["labels"] = ["ext:WV-09"]
        self.assertEqual(jira_pull.read_issue(issue, {}, {})["id"], "WV-09")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        run(["plan", "version:1.5.5"], self.repo.dir)

    def tearDown(self):
        self.repo.close()

    def test_writes_the_folder_and_reports(self):
        self.repo.results()
        code, out = run(["build"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("note: 1 done in Jira, left out: WAR-6", out)
        self.assertIn("note: 1 epic(s) left out: WAR-7", out)
        self.assertIn("plan: 5 tickets · 3 gates (G0, G1, G2)", out)
        self.assertIn("WAR-4 ← WAR-77 (In Progress)", out)
        self.assertIn("planned as M: WAR-4", out)
        self.assertIn("next: /warp-scan warp-plans/version-1.5.5", out)
        d = self.repo.schedule()
        self.assertEqual(d["schema"], "build-schedule/2")
        self.assertEqual(d["model"]["gate_order"], "strict")
        self.assertEqual(d["model"]["slots"], 4)
        T = {t["id"]: t for t in d["tickets"]}
        self.assertEqual(set(T), {"APP-01", "APP-02", "WAR-3", "WAR-4", "WAR-5"})
        self.assertEqual(T["WAR-3"]["after"], ["APP-01"])          # WAR-9 is Done: met
        self.assertEqual(T["WAR-4"]["after"], ["APP-02"])          # WAR-77 is outside: left out
        self.assertEqual(T["WAR-5"]["after"], ["WAR-3", "view:WV-15"])
        self.assertEqual(T["APP-02"]["jira"], "WAR-2")
        # the planned picture keeps Warp's rules
        for t in d["tickets"]:
            for dep in t["after"]:
                if dep in T:
                    self.assertLessEqual(T[dep]["merge"], t["start"] + 1e-9, (t["id"], dep))
        for a in d["tickets"]:
            for b in d["tickets"]:
                if a is not b and a["slot"] == b["slot"]:
                    self.assertTrue(a["start"] + a["h"] <= b["start"] + 1e-9 or b["start"] + b["h"] <= a["start"] + 1e-9, (a["id"], b["id"]))
        g = {x["key"]: x for x in d["gates"]}
        self.assertEqual(g["G1"]["members"], ["APP-02", "WAR-3"])
        self.assertTrue(all(T[m]["start"] >= g["G0"]["at"] for m in g["G1"]["members"]))   # strict blocks
        jmap = json.loads((self.repo.dir / ".warp/jira-map.json").read_text())
        self.assertEqual(jmap["APP-01"]["key"], "WAR-1")
        self.assertTrue((self.repo.dir / "warp-plans/version-1.5.5/plan/FROM_JIRA.md").is_file())
        issues = json.loads((self.repo.dir / "warp-plans/version-1.5.5/jira/issues.json").read_text())
        self.assertEqual(len(issues["issues"]), 5)

    def test_warp_scan_reads_the_folder(self):
        self.repo.results()
        run(["build"], self.repo.dir)
        found = scan.scan(self.repo.dir, self.repo.dir / "warp-plans/version-1.5.5")
        got = scan.pick(self.repo.dir, found)
        self.assertEqual(got["format"], "build-schedule/2")
        keys = {t["id"]: t["jiraKey"] for t in got["tickets"]}
        self.assertEqual(keys["APP-01"], "WAR-1")
        self.assertEqual(keys["WAR-3"], "WAR-3")
        acs = {t["id"]: t["acs"] for t in got["tickets"]}
        self.assertEqual(acs["APP-01"], ["build runs", "test runs"])

    def test_end_to_end_scan_stores_every_key(self):
        self.repo.results()
        run(["build"], self.repo.dir)
        subprocess.run(["git", "init", "-q"], cwd=self.repo.dir, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/App.git"], cwd=self.repo.dir, check=True)
        out = subprocess.run([sys.executable, str(ROOT / "scripts/scan.py"), "scan", "--folder", "warp-plans/version-1.5.5"],
                             cwd=self.repo.dir, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("jira: 5 tickets: 5 keyed, 0 need mapping", out.stdout)
        beam = json.loads((self.repo.dir / ".warp/beam.json").read_text())
        self.assertEqual(beam["tickets"]["APP-02"]["jiraKey"], "WAR-2")

    def test_dry_run_writes_nothing(self):
        self.repo.results()
        code, out = run(["build", "--dry-run"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: would write", out)
        self.assertFalse((self.repo.dir / "warp-plans").exists())

    def test_second_build_refuses_without_force(self):
        self.repo.results()
        self.assertEqual(run(["build"], self.repo.dir)[0], 0)
        code, out = run(["build"], self.repo.dir)
        self.assertEqual(code, 3)
        self.assertEqual(run(["build", "--force"], self.repo.dir)[0], 0)

    def test_folder_of_pages(self):
        pages = self.repo.dir / "pages"
        pages.mkdir()
        doc = json.loads((FIX / "search.json").read_text())
        for n, page in enumerate(doc["pages"]):
            if n == 0:
                page["names"] = doc["names"]
            (pages / f"page-{n}.json").write_text(json.dumps(page))
        code, out = run(["build", "--results", str(pages), "--dry-run"], self.repo.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("plan: 5 tickets", out)

    def test_cycle_is_refused(self):
        self.repo.results("cycle.json")
        code, out = run(["build", "--labels", "v"], self.repo.dir)
        self.assertEqual(code, 4, out)
        self.assertIn("dependency cycle", out)

    def test_missing_results_and_empty_search(self):
        code, out = run(["build"], self.repo.dir)
        self.assertEqual(code, 2)
        self.assertIn("no saved search", out)
        dest = self.repo.dir / ".warp/jira-pull/issues.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"issues": []}')
        code, out = run(["build"], self.repo.dir)
        self.assertEqual(code, 2)
        self.assertIn("holds no issues", out)

    def test_deps_gate_order_and_auto_merge_sizes_from_config(self):
        self.repo.close()
        self.repo = Repo("gateOrder: deps\nmaxAgents: 18\nautoMergeSizes: S, M, L, XL\n")
        run(["plan", "version:1.5.5"], self.repo.dir)
        self.repo.results()
        run(["build"], self.repo.dir)
        d = self.repo.schedule()
        self.assertEqual(d["model"]["gate_order"], "deps")
        T = {t["id"]: t for t in d["tickets"]}
        self.assertEqual(T["WAR-3"]["merge"], T["WAR-3"]["start"] + 11)   # L auto-merges: no approval wait


class SimTest(unittest.TestCase):
    def test_locks_keep_two_tickets_apart_and_priority_gates_go_first(self):
        tickets = [{"id": "A", "hours": 4, "size": "S", "after": [], "locks": ["src/x/"], "gate": None},
                   {"id": "B", "hours": 4, "size": "S", "after": [], "locks": ["src/x/y.py"], "gate": None},
                   {"id": "C", "hours": 4, "size": "S", "after": [], "locks": ["src/z/"], "gate": None}]
        got = plansim.plan(tickets, [], slots=4)
        rows = got["tickets"]
        self.assertTrue(rows["B"]["start"] >= rows["A"]["merge"] or rows["A"]["start"] >= rows["B"]["merge"])
        got = plansim.plan(tickets, [{"key": "G-X", "members": ["C"], "priority": True}], slots=1)
        self.assertEqual(got["tickets"]["C"]["priority_gate"], "G-X")

    def test_gate_keys_order(self):
        self.assertEqual(plansim.gate_keys(["G10", "G-RBAC", "G2", "G0"]), ["G0", "G2", "G10", "G-RBAC"])


if __name__ == "__main__":
    unittest.main()
