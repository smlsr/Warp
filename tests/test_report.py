"""Completion report: waves, partial data, escaping, and when it is written."""

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
import install  # noqa: E402
import report  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def hours(n):
    return (T0 + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(repo, script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def status(at, to, frm="queued"):
    return {"at": at, "type": "status", "from": frm, "to": to}


def ticket(tid, **kw):
    return {
        "id": tid,
        "summary": kw.get("summary", tid),
        "module": kw.get("module", "app"),
        "size": kw.get("size", "S"),
        "autoMerge": kw.get("autoMerge", True),
        "status": kw.get("status", "merged"),
        "deps": kw.get("deps", []),
        "locks": kw.get("locks", []),
        "agent": kw.get("agent", "shuttle-" + tid),
        "branch": kw.get("branch", "warp/" + tid),
        "attempts": kw.get("attempts", 0),
        "tokens": kw.get("tokens", 0),
        "minutes": 0,
        "alarm": kw.get("alarm"),
        "events": kw.get("events", []),
        "usage": kw.get("usage", {}),
        "pr": kw.get("pr", {}),
        "jira": kw.get("jira", {}),
        "jiraKey": kw.get("jiraKey"),
    }


def beam_of(tickets, **kw):
    return {
        "version": 1,
        "generatedAt": "2026-01-01T00:00:00Z",
        "runStartedAt": kw.get("runStartedAt", "2026-01-01T00:00:00Z"),
        "runEndedAt": kw.get("runEndedAt", "2026-01-01T08:00:00Z"),
        "runState": kw.get("runState", "running"),
        "config": kw.get(
            "config",
            {
                "model": "claude-sonnet-5-5-high",
                "maxAgents": 4,
                "jiraSite": "https://acme.atlassian.net",
                "projectName": "ops",
            },
        ),
        "program": kw.get("program", {"agentHours": 28, "estimate": {"agentHours": 28, "humanHours": 1.0}}),
        "execution": kw.get("execution", {"mode": "local", "provider": None}),
        "tickets": {t["id"]: t for t in tickets},
    }


def span(tid, start, end, frm="queued", to="merged"):
    return ticket(
        tid,
        events=[status(hours(start), "claimed", frm), status(hours(end), to, "claimed")],
        pr={"url": "https://github.com/acme/app/pull/1", "sha": "abc", "mergedAt": hours(end), "bugbotFindings": 0, "bugbotFixed": 0, "ci": "green"},
        jira={"startedAt": hours(start), "doneAt": hours(end), "comments": {"merged": {"at": hours(end)}}},
    )


class WaveTests(unittest.TestCase):
    def test_parallel_then_one_then_two(self):
        spans = [(f"WV-0{i}", T0, T0 + timedelta(hours=4)) for i in range(1, 5)]
        spans.append(("WV-05", T0 + timedelta(hours=4), T0 + timedelta(hours=6)))
        spans.append(("WV-06", T0 + timedelta(hours=6), T0 + timedelta(hours=8)))
        spans.append(("WV-07", T0 + timedelta(hours=6), T0 + timedelta(hours=8)))
        found = report.waves(spans)
        self.assertEqual([w["count"] for w in found], [4, 1, 2])
        self.assertEqual(found[0]["ids"], ["WV-01", "WV-02", "WV-03", "WV-04"])
        self.assertEqual(found[1]["ids"], ["WV-05"])
        self.assertEqual(found[2]["ids"], ["WV-06", "WV-07"])

    def test_sequential_three(self):
        spans = [
            ("A", T0, T0 + timedelta(hours=1)),
            ("B", T0 + timedelta(hours=1), T0 + timedelta(hours=2)),
            ("C", T0 + timedelta(hours=2), T0 + timedelta(hours=3)),
        ]
        found = report.waves(spans)
        self.assertEqual([w["count"] for w in found], [1, 1, 1])
        self.assertEqual([w["ids"] for w in found], [["A"], ["B"], ["C"]])

    def test_same_instant_handoff_is_two_waves(self):
        spans = [
            ("A", T0, T0 + timedelta(hours=1)),
            ("B", T0, T0 + timedelta(hours=2)),
            ("C", T0 + timedelta(hours=1), T0 + timedelta(hours=2)),
        ]
        found = report.waves(spans)
        self.assertEqual([w["count"] for w in found], [2, 2])
        self.assertEqual(found[0]["ids"], ["A", "B"])
        self.assertEqual(found[1]["ids"], ["B", "C"])

    def test_gap_does_not_merge_the_same_set(self):
        spans = [
            ("A", T0, T0 + timedelta(hours=1)),
            ("A", T0 + timedelta(hours=2), T0 + timedelta(hours=3)),
        ]
        segs = report.segments(spans)
        self.assertIn(0, [s["count"] for s in segs])
        found = report.waves(spans)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["count"], 1)
        self.assertEqual(found[1]["count"], 1)


class RenderTests(unittest.TestCase):
    def test_parallel_waves_in_html(self):
        tickets = [span(f"WV-0{i}", 0, 4) for i in range(1, 5)]
        tickets.append(span("WV-05", 4, 6))
        tickets += [span("WV-06", 6, 8, to="merged"), span("WV-07", 6, 8)]
        tickets[5]["autoMerge"] = False
        tickets[5]["size"] = "L"
        tickets[6]["autoMerge"] = False
        tickets[6]["size"] = "XL"
        html = report.render_html(beam_of(tickets), {"mode": "connected", "provider": "github"}, False)
        self.assertIn("Wave 1: 4 tickets in parallel (WV-01, WV-02, WV-03, WV-04)", html)
        self.assertIn("Wave 2: 1 ticket (WV-05)", html)
        self.assertIn("Wave 3: 2 tickets in parallel (WV-06, WV-07)", html)
        self.assertIn("mode connected", html)
        self.assertIn("provider github", html)
        self.assertIn("<svg", html)
        self.assertIn('class="steps"', html)
        self.assertIn('class="gantt"', html)

    def test_blocked_ticket_without_events_is_not_a_wave(self):
        running = span("WV-01", 0, 2)
        other = span("WV-02", 0, 2)
        blocked = ticket("WV-03", status="blocked", summary="held", events=[])
        data = beam_of([running, other, blocked])
        html = report.render_html(data, {"mode": "local", "provider": None}, True)
        self.assertIn("Wave 1: 2 tickets in parallel (WV-01, WV-02)", html)
        self.assertNotIn("WV-03", html.split("Wave 1:")[1].split("</li>")[0])
        self.assertIn("WV-03: no status events; timeline is empty", html)
        self.assertIn("mode local", html)

    def test_alarm_interval_ends_when_it_leaves_the_working_set(self):
        failed = ticket(
            "WV-09",
            status="alarm",
            alarm="stuck",
            autoMerge=True,
            events=[status(hours(0), "claimed"), status(hours(2), "alarm", "claimed")],
        )
        found = report.waves([(failed["id"], a, b) for a, b in report.active_intervals(failed, T0 + timedelta(hours=8))])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["end"], T0 + timedelta(hours=2))

    def test_missing_usage_is_na_not_zero(self):
        data = beam_of([span("WV-01", 0, 1)])
        summary = report.summarize(data, {"mode": "local"}, T0 + timedelta(hours=1), False)
        text = report.totals_plain(summary)
        self.assertIn("Tokens in: n/a", text)
        self.assertIn("Tokens out: n/a", text)
        self.assertIn("Tokens cached: n/a", text)
        self.assertIn("Cost: n/a", text)
        self.assertNotIn("Cost: $0.00", text)

    def test_legacy_spend_is_not_tokens_in(self):
        row = span("WV-01", 0, 1)
        row["tokens"] = 50
        text = report.totals_plain(report.summarize(beam_of([row]), {"mode": "local"}, T0, False))
        self.assertIn("Spend tokens (legacy): 50", text)
        self.assertIn("Tokens in: n/a", text)

    def test_html_escapes_summary_and_has_no_external_resources(self):
        nasty = ticket(
            "WV-01",
            summary="<script>alert(1)</script>",
            status="merged",
            jiraKey="WAR-1",
            events=[status(hours(0), "claimed"), status(hours(1), "merged", "claimed")],
            pr={"url": "https://github.com/acme/app/pull/9"},
        )
        html = report.render_html(beam_of([nasty]), {"mode": "local"}, False)
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<link", html.lower())
        self.assertNotIn("url(http", html.lower())
        self.assertNotIn("fonts.google", html.lower())
        self.assertIn('href="https://acme.atlassian.net/browse/WAR-1"', html)
        self.assertIn('href="https://github.com/acme/app/pull/9"', html)
        self.assertIn("concurrency-run segments", html)

    def test_partial_banner(self):
        row = ticket("WV-01", status="coding", events=[status(hours(0), "claimed"), status(hours(1), "coding", "claimed")])
        html = report.render_html(beam_of([row], runEndedAt=None), {"mode": "local"}, True)
        self.assertIn("Partial snapshot", html)
        self.assertIn("in progress", html)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / ".warp").mkdir()
        self.beam = self.repo / ".warp" / "beam.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.beam.write_text(json.dumps(data))

    def test_refuses_until_partial(self):
        self.write(beam_of([ticket("WV-01", status="queued", events=[])], runEndedAt=None, runState="running"))
        refused = run(self.repo, "report.py", "--beam", ".warp/beam.json")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("not complete; pass --partial", refused.stdout)
        self.assertFalse((self.repo / ".warp" / "warp-complete.html").exists())
        snap = run(self.repo, "report.py", "--beam", ".warp/beam.json", "--partial")
        self.assertEqual(snap.returncode, 0, snap.stdout + snap.stderr)
        text = (self.repo / ".warp" / "warp-complete.html").read_text()
        self.assertIn("Partial snapshot", text)

    def test_report_on_complete_can_be_turned_off(self):
        self.write(
            beam_of(
                [ticket("WV-01", status="review", events=[], pr={}, autoMerge=True)],
                runEndedAt=None,
                runState="running",
            )
        )
        (self.repo / ".warp" / "config.yaml").write_text("reportOnComplete: false\n")
        proc = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--status",
            "merged",
            "--bugbot",
            "pass",
            "--ci",
            "green",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.repo / ".warp" / "warp-complete.html").exists())
        self.assertNotIn("reportGeneratedAt", self.beam.read_text())

    def test_set_writes_the_report_and_herald_once(self):
        self.write(
            beam_of(
                [ticket("WV-01", status="review", events=[], pr={}, autoMerge=True)],
                runEndedAt=None,
                runState="running",
            )
        )
        proc = run(
            self.repo,
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--status",
            "merged",
            "--bugbot",
            "pass",
            "--ci",
            "green",
            "--sha",
            "abc123",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("report:", proc.stdout)
        saved = json.loads(self.beam.read_text())
        self.assertTrue(saved.get("reportGeneratedAt"))
        self.assertTrue(saved.get("runComplete"))
        self.assertTrue((self.repo / ".warp" / "events.jsonl").is_file())
        posted = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertIn("gitignored", posted["text"].lower())
        self.assertIn("warp-complete.html", posted["text"])
        first = posted["text"]
        again = run(
            self.repo,
            "beam.py",
            "usage",
            "--beam",
            ".warp/beam.json",
            "--id",
            "WV-01",
            "--tokens-in",
            "10",
            "--tokens-out",
            "4",
            "--cost",
            "0.25",
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        saved = json.loads(self.beam.read_text())
        self.assertEqual(saved["tickets"]["WV-01"]["usage"]["tokensIn"], 10)
        self.assertEqual(saved["tickets"]["WV-01"]["usage"]["cost"], 0.25)
        self.assertIn("usage", (self.repo / ".warp" / "events.jsonl").read_text())
        self.assertIn("$0.25", (self.repo / ".warp" / "warp-complete.html").read_text())
        posted = json.loads((self.repo / ".warp" / "notify-post.json").read_text())
        self.assertEqual(posted["text"], first)

    def test_stop_writes_a_partial_report_and_start_clears_the_flag(self):
        self.write(beam_of([ticket("WV-01", status="queued", events=[])], runEndedAt=None, runState="running"))
        stopped = run(self.repo, "scan.py", "stop", "--beam", ".warp/beam.json")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertTrue((self.repo / ".warp" / "warp-complete.html").is_file())
        self.assertIn("Partial snapshot", (self.repo / ".warp" / "warp-complete.html").read_text())
        self.assertTrue(json.loads(self.beam.read_text()).get("runComplete"))
        started = run(self.repo, "scan.py", "start", "--beam", ".warp/beam.json")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertFalse(json.loads(self.beam.read_text()).get("runComplete"))

    def test_status_footer_names_the_report(self):
        self.write(beam_of([span("WV-01", 0, 1)], runState="running"))
        proc = run(self.repo, "report.py", "--beam", ".warp/beam.json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status = run(self.repo, "scan.py", "status", "--beam", ".warp/beam.json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        text = (self.repo / ".warp" / "STATUS.md").read_text()
        self.assertIn("warp-complete.html", text)

    def test_open_prints_a_file_url(self):
        self.write(beam_of([span("WV-01", 0, 1)]))
        proc = run(self.repo, "report.py", "--beam", ".warp/beam.json", "--open")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("file://", proc.stdout)

    def test_init_backfills_the_report_keys(self):
        example = (ROOT / "assets" / "config.example.yaml").read_text()
        text, added = install.add_missing_keys("someExisting: 1\n", example)
        self.assertIn("reportOnComplete", added)
        self.assertIn("reportPath", added)
        self.assertIn("write .warp/warp-complete.html", text)


if __name__ == "__main__":
    unittest.main()
