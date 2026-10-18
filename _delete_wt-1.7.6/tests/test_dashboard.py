"""The run dashboard page and dashboard.py."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import beam  # noqa: E402
import dashboard  # noqa: E402
import scan  # noqa: E402

PAGE = ROOT / "assets" / "warp-dashboard.html"
SAMPLE = ROOT / "examples" / "beam.sample.json"


def page_text() -> str:
    return PAGE.read_text()


def js_object(name: str) -> dict:
    m = re.search(r"const %s = (\{.*?\});" % name, page_text())
    assert m, name
    return json.loads(m.group(1))


class PageTest(unittest.TestCase):
    def test_every_warp_status_has_one_of_six_colors(self):
        mapping = js_object("STATUS_CAT")
        self.assertEqual(sorted(mapping), sorted(beam.STATUSES))
        self.assertEqual(set(mapping.values()), {"done", "err", "prog", "rev", "qa", "todo"})
        self.assertEqual(mapping["awaiting_approval"], "qa")
        self.assertEqual(mapping["parked"], "err")
        self.assertEqual(mapping["queued"], "todo")
        for status in beam.TERMINAL:
            self.assertEqual(mapping[status], "done")

    def test_estimates_follow_the_scan(self):
        text = page_text()
        hours = re.search(r"const SIZE_HOURS = (\{[^}]*\});", text).group(1)
        self.assertEqual(json.loads(re.sub(r"(\w+):", r'"\1":', hours)), scan.HOURS)
        self.assertIn("const DEFAULT_APPROVAL = %s;" % scan.APPROVAL_HOURS, text)
        self.assertIn("const DEFAULT_CAP = %d;" % beam.default_config()["maxAgents"], text)

    def test_page_is_one_file(self):
        text = page_text()
        self.assertEqual(text.count(dashboard.SLOT), 1)
        self.assertNotRegex(text, r"<script[^>]+src=")
        self.assertNotRegex(text, r"<link[^>]+stylesheet")
        for label in ("Completed", "Errored / Parked", "In Progress", "In Review", "QA Ready", "To Do"):
            self.assertIn('"%s"' % label, text)
        for needle in ("showOpenFilePicker", "showDirectoryPicker", "webkitdirectory", "@media print", "prefers-color-scheme", 'data-act="reload"',
                       'data-view="table"', 'data-clock="actual"', 'id="util"', "PR opened", "Jira last comment", "assumptions"):
            self.assertIn(needle, text)

    def test_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        script = re.findall(r"<script>(.*?)</script>", page_text(), re.S)[-1]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
        result = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)
        Path(handle.name).unlink()
        self.assertEqual(result.returncode, 0, result.stderr)


class ScheduleV2Test(unittest.TestCase):
    """build-schedule/2 (the FORGEQA/HOS/WARPDEMO format): h, critical_chain and if_red."""

    def test_scan_reads_build_schedule_2(self):
        data = {
            "schema": "build-schedule/2",
            "critical_chain": ["A-01", "A-02"],
            "gates": [{"key": "G0", "name": "Start", "blocking": True, "members": ["A-01"], "checks": ["make ci"], "if_red": "stop"}],
            "tickets": [
                {"id": "A-01", "summary": "one", "size": "S", "h": 4, "after": [], "gate": "G0", "critical": True, "rank": 1, "locks": ["a/"]},
                {"id": "A-02", "summary": "two", "size": "L", "h": 11, "after": ["A-01", "other:B-01"], "gate": "", "critical": True, "rank": 2, "locks": ["b/"]},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(data))
            got = scan.from_schedule(path)
        self.assertEqual(got["format"], "build-schedule/2")
        self.assertEqual(got["criticalPath"], ["A-01", "A-02"])
        self.assertEqual(got["gates"][0]["ifRed"], "stop")
        two = got["tickets"][1]
        self.assertEqual((two["hours"], two["after"], two["gate"], two["size"]), (11, ["A-01", "other:B-01"], None, "L"))
        self.assertEqual(got["tickets"][0]["gate"], "G0")

    def test_legacy_schedule_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps({"criticalPath": ["X-1"], "tickets": [{"id": "X-1", "summary": "x", "hours": 6, "deps": [], "size": "M"}]}))
            got = scan.from_schedule(path)
        self.assertEqual((got["format"], got["criticalPath"], got["tickets"][0]["hours"]), ("schedule.json", ["X-1"], 6))


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.warp = self.tmp / ".warp"
        (self.warp / "tickets" / "C-01").mkdir(parents=True)
        data = json.loads(SAMPLE.read_text())
        data["tickets"]["C-01"]["summary"] = "closes </script><script>alert(1)</script>"
        (self.warp / "beam.json").write_text(json.dumps(data))
        (self.warp / "tickets" / "C-01" / "state.json").write_text(json.dumps({"id": "C-01", "state": "coding"}))
        for name in ("core", "admin"):
            folder = self.warp / "runs" / name
            folder.mkdir(parents=True)
            other = dict(data, run=name)
            (folder / "beam.json").write_text(json.dumps(other))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def embedded(self, path: Path) -> dict:
        text = path.read_text()
        m = re.search(r'<script id="warp-data" type="application/json">(.*?)</script>', text, re.S)
        self.assertIsNotNone(m)
        return json.loads(m.group(1))

    def test_write_embeds_beam_states_and_other_runs(self):
        info = dashboard.write(self.warp / "beam.json", self.warp / "dashboard.html")
        data = self.embedded(info["out"])
        paths = [f["path"] for f in data["files"]]
        self.assertEqual(paths, [".warp/beam.json", ".warp/tickets/C-01/state.json", ".warp/runs/admin/beam.json"])
        self.assertEqual(data["files"][0]["data"]["tickets"]["C-01"]["summary"], "closes </script><script>alert(1)</script>")
        self.assertEqual(data["jiraSite"], "acme.atlassian.net")
        text = info["out"].read_text()
        self.assertEqual(text.count('<script id="warp-data"'), 1)
        self.assertNotIn("</script><script>alert", text)

    def test_empty_page_has_no_data(self):
        out = self.tmp / "page.html"
        dashboard.write(self.warp / "beam.json", out, empty=True)
        self.assertIn(dashboard.SLOT, out.read_text())

    def test_refresh_only_when_the_page_exists(self):
        self.assertIsNone(dashboard.refresh(self.warp / "beam.json"))
        dashboard.write(self.warp / "beam.json", self.warp / "dashboard.html")
        data = json.loads((self.warp / "beam.json").read_text())
        data["tickets"]["C-01"]["status"] = "merged"
        (self.warp / "beam.json").write_text(json.dumps(data))
        self.assertEqual(dashboard.refresh(self.warp / "beam.json"), self.warp / "dashboard.html")
        self.assertEqual(self.embedded(self.warp / "dashboard.html")["files"][0]["data"]["tickets"]["C-01"]["status"], "merged")

    def test_status_write_refreshes_the_page(self):
        dashboard.write(self.warp / "beam.json", self.warp / "dashboard.html")
        data = json.loads((self.warp / "beam.json").read_text())
        data["tickets"]["C-02"]["status"] = "parked"
        (self.warp / "beam.json").write_text(json.dumps(data))
        with contextlib.redirect_stdout(io.StringIO()):
            scan.write_status(self.warp / "beam.json")
        self.assertEqual(self.embedded(self.warp / "dashboard.html")["files"][0]["data"]["tickets"]["C-02"]["status"], "parked")

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["write", "--beam", str(self.warp / "beam.json")])
        self.assertEqual(code, 0)
        self.assertIn("dashboard: %s tickets=%d files=3" % (self.warp / "dashboard.html", 68 * 2), out.getvalue())
        self.assertIn("open: file://", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["write", "--beam", str(self.tmp / "nope" / "beam.json")])
        self.assertEqual(code, 2)
        self.assertIn("refuse: no beam", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dashboard.main(["path"])
        self.assertIn("assets/warp-dashboard.html", out.getvalue())

    def test_status_json_alone(self):
        (self.warp / "beam.json").unlink()
        (self.warp / "status.json").write_text(json.dumps({"done": [], "working": [{"id": "T-1", "status": "coding"}], "left": []}))
        files = dashboard.gather(self.warp / "beam.json")
        self.assertIn(".warp/status.json", [f["path"] for f in files])


if __name__ == "__main__":
    unittest.main()
