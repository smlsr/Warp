"""Stall detection, fixer passes, escalation, and open-work status."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import orchestrator  # noqa: E402
import pipeline  # noqa: E402
import scan  # noqa: E402
import watch  # noqa: E402

NOW = "2026-10-07T12:00:00Z"
OLD = "2026-10-07T10:00:00Z"
RECENT = "2026-10-07T11:30:00Z"
LATER = "2026-10-07T13:01:00Z"


def ticket(tid, **extra):
    row = {
        "id": tid,
        "status": "queued",
        "locks": ["src/%s" % tid.lower()],
        "size": "S",
        "module": "app",
        "autoMerge": True,
        "jiraKey": "WAR-1",
        "branch": "warp/%s" % tid,
        "summary": "ship %s" % tid,
        "agent": None,
        "pr": {},
    }
    row.update(extra)
    return row


def beam(tickets, **config):
    cfg = {
        "maxAgents": 4,
        "maxLocalSubagents": 4,
        "bugbotRequired": True,
        "maxFixAttempts": 3,
        "maxRecoveries": 3,
        "maxStallFixes": 3,
        "autoMergeSizes": ["S", "M"],
        "checkCommand": "make ci",
        "runner": "cloud",
        "launch": "worktree",
        "subagentVm": False,
    }
    cfg.update(config)
    return {
        "version": 1,
        "runState": "running",
        "baseGreen": True,
        "tickets": {row["id"]: row for row in tickets},
        "gates": [],
        "config": cfg,
    }


def text(lines):
    return "\n".join(lines)


def stalled(row, state):
    row["watch"] = {
        "state": state,
        "since": OLD,
        "progressAt": OLD,
        "lastActive": OLD,
        "stalled": False,
    }
    row["lastSeenAt"] = OLD
    row["updatedAt"] = OLD
    return row


class StallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        self.path = warp / "beam.json"

    def test_no_progress_marks_a_stall_and_a_heartbeat_clears_it(self):
        row = stalled(
            ticket(
                "T-1",
                status="review",
                phase="reviewing",
                pr={"url": "https://example.test/pull/1", "bugbot": "pass", "ci": "green", "rollup": "green"},
            ),
            "reviewing",
        )
        data = beam([row])
        self.path.write_text(json.dumps(data) + "\n")
        lines = watch.apply(data, beam_path=self.path, now=NOW)
        saved = data["tickets"]["T-1"]
        self.assertTrue(saved["stalled"])
        self.assertIn("stall: T-1", text(lines))
        log = self.path.parent / "tickets" / "T-1" / "log.jsonl"
        entry = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(entry["type"], "stall")
        self.assertEqual(entry["id"], "T-1")
        self.assertIn("reviewing", entry["reason"])

        again = watch.apply(data, beam_path=self.path, now=NOW)
        self.assertNotIn("stall: T-1", text(again))
        self.assertEqual(log.read_text().count('"type":"stall"'), 1)

        saved["lastSeenAt"] = NOW
        cleared = watch.apply(data, beam_path=self.path, now=NOW)
        self.assertFalse(saved["stalled"])
        self.assertNotIn("stall: T-1", text(cleared))

    def test_inside_the_limit_is_not_stalled_and_a_shorter_limit_is(self):
        row = stalled(
            ticket("T-1", status="review", phase="reviewing", pr={"bugbot": "pass", "ci": "green", "rollup": "green"}),
            "reviewing",
        )
        row["watch"]["progressAt"] = RECENT
        row["lastSeenAt"] = RECENT
        quiet = beam([row])
        lines = watch.apply(quiet, now=NOW)
        self.assertFalse(quiet["tickets"]["T-1"]["stalled"])
        self.assertNotIn("stall: T-1", text(lines))

        tight = beam([json.loads(json.dumps(row))], stallReviewingMinutes=10)
        watch.apply(tight, now=NOW)
        self.assertTrue(tight["tickets"]["T-1"]["stalled"])


class FixerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        self.path = warp / "beam.json"

    def test_slow_bugbot_is_asked_again(self):
        row = stalled(
            ticket(
                "T-1",
                status="review",
                phase="reviewing",
                pr={
                    "url": "https://example.test/pull/2",
                    "bugbot": "running",
                    "bugbotRequested": True,
                    "reviewCycle": 0,
                    "ci": "green",
                    "rollup": "green",
                },
            ),
            "reviewing",
        )
        data = beam([row])
        lines = watch.apply(data, now=NOW)
        self.assertIn("bugbot: request T-1 https://example.test/pull/2", text(lines))
        self.assertIn("fixer: T-1 bugbot", text(lines))
        self.assertEqual(data["tickets"]["T-1"]["pr"]["reviewCycle"], 1)
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 1)

    def test_bugbot_that_never_started_is_requested(self):
        row = ticket(
            "T-1",
            status="review",
            phase="pr-open",
            pr={"url": "https://example.test/pull/3", "bugbot": "", "ci": "pending"},
        )
        data = beam([row])
        lines = watch.apply(data, now=NOW)
        self.assertIn("bugbot: request T-1 https://example.test/pull/3", text(lines))
        self.assertIn("fixer: T-1 bugbot", text(lines))

    def test_pending_ci_and_a_flake_are_rerun(self):
        pending = stalled(
            ticket(
                "C-1",
                status="review",
                phase="reviewing",
                pr={"url": "https://example.test/pull/4", "bugbot": "pass", "ci": "pending", "rollup": "pending"},
            ),
            "reviewing",
        )
        data = beam([pending])
        lines = watch.apply(data, now=NOW)
        self.assertIn("ci: rerun C-1", text(lines))
        self.assertIn("fixer: C-1 ci", text(lines))

        flaked = stalled(
            ticket(
                "C-2",
                status="review",
                phase="reviewing",
                pr={"url": "https://example.test/pull/5", "bugbot": "pass", "ci": "red", "flake": True},
            ),
            "reviewing",
        )
        again = beam([flaked])
        flake_lines = watch.apply(again, now=NOW)
        self.assertIn("ci: rerun C-2", text(flake_lines))
        self.assertIn("fixer: C-2 ci", text(flake_lines))

    def test_conflict_and_behind_start_a_rebase(self):
        conflict = ticket(
            "C-1",
            status="review",
            phase="reviewing",
            pr={"url": "https://example.test/pull/6", "bugbot": "pass", "ci": "green", "conflict": True},
        )
        behind = ticket(
            "B-1",
            status="review",
            phase="reviewing",
            pr={"url": "https://example.test/pull/7", "bugbot": "pass", "ci": "green", "behind": True},
        )
        data = beam([conflict, behind])
        lines = watch.apply(data, now=NOW)
        blob = text(lines)
        self.assertIn("fixer: C-1 rebase", blob)
        self.assertIn("fixer: B-1 rebase", blob)
        self.assertIn("rebase onto main", blob)
        self.assertIn("step=fix", blob)
        self.assertEqual(data["tickets"]["C-1"]["phase"], "fixing")
        self.assertEqual(data["tickets"]["B-1"]["status"], "fix")

    def test_a_silent_shuttle_restarts_the_step(self):
        row = stalled(
            ticket("S-1", status="coding", phase="implementing", shuttle={"pending": True, "step": "implement"}),
            "implementing",
        )
        data = beam([row])
        lines = watch.apply(data, now=NOW)
        blob = text(lines)
        self.assertIn("fixer: S-1 shuttle", blob)
        self.assertIn("step=restart", blob)
        self.assertIn("herald: S-1 shuttle restarted.", blob)
        self.assertEqual(data["tickets"]["S-1"]["status"], "recovering")

    def test_lock_escape_widens_and_reruns(self):
        row = ticket(
            "L-1",
            status="alarm",
            alarm="lock-escape",
            phase="implementing",
            escaped=["src/extra.py"],
            locks=["src/a"],
        )
        data = beam([row])
        self.path.write_text(json.dumps(data) + "\n")
        lines = watch.apply(data, beam_path=self.path, now=NOW)
        blob = text(lines)
        self.assertIn("fixer: L-1 lock-escape", blob)
        self.assertIn("alarm-repair: start L-1", blob)
        saved = json.loads(self.path.read_text())
        locks = saved["tickets"]["L-1"]["locks"]
        self.assertIn("src/a", locks)
        self.assertIn("src/extra.py", locks)

    def test_a_stale_gate_clears_when_members_merged(self):
        members = ["P-002", "P-003", "P-004", "P-018"]
        rows = [ticket(tid, status="merged", phase="merged") for tid in members]
        rows.append(ticket("Q-1"))
        data = beam(rows)
        data["gates"] = [{"key": "G1", "status": "red", "members": members, "evidence": "stale"}]
        lines = watch.apply(data, now=NOW)
        self.assertIn("herald: G1 red cleared. Members merged. Tick ran.", text(lines))
        self.assertIn("fixer: gates", text(lines))
        self.assertEqual(data["gates"][0]["status"], "green")
        self.assertEqual(data["gates"][0]["evidence"], "members merged: P-002, P-003, P-004, P-018")

    def test_a_red_base_starts_a_fix_and_alarms_once_at_the_cap(self):
        data = beam([ticket("Q-1")], maxStallFixes=1)
        data["baseGreen"] = False
        first = watch.apply(data, now=NOW)
        self.assertIn("fixer: base red", text(first))
        self.assertIn("dispatch-base-fix checkout=worktree", text(first))
        self.assertNotIn("slack: alarm base", text(first))
        second = watch.apply(data, now=NOW)
        self.assertEqual(text(second).count("slack: alarm base base-red"), 1)
        third = watch.apply(data, now=NOW)
        self.assertNotIn("slack: alarm base", text(third))

    def test_an_approved_pull_request_returns_to_the_merge_queue(self):
        row = ticket(
            "A-1",
            status="awaiting_approval",
            phase="ready",
            autoMerge=False,
            pr={
                "url": "https://example.test/pull/8",
                "bugbot": "pass",
                "rollup": "green",
                "ci": "green",
                "approvedAt": NOW,
                "acResults": {"builds": "pass"},
            },
        )
        data = beam([row])
        self.assertFalse(orchestrator.in_merge_queue(data["tickets"]["A-1"], data["config"]))
        lines = watch.apply(data, now=NOW)
        self.assertIn("merge: queue A-1", text(lines))
        self.assertIn("fixer: A-1 queue", text(lines))
        self.assertEqual(data["tickets"]["A-1"]["status"], "review")
        self.assertTrue(orchestrator.in_merge_queue(data["tickets"]["A-1"], data["config"]))


class EscalationTests(unittest.TestCase):
    def test_the_cap_parks_once_and_does_not_repost(self):
        row = stalled(
            ticket(
                "T-1",
                status="review",
                phase="reviewing",
                pr={
                    "url": "https://example.test/pull/9",
                    "bugbot": "running",
                    "bugbotRequested": True,
                    "ci": "green",
                    "rollup": "green",
                },
            ),
            "reviewing",
        )
        row["stalled"] = True
        row["watch"]["stalled"] = True
        data = beam([row], maxStallFixes=2)
        first = watch.apply(data, now=NOW)
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 1)
        self.assertNotEqual(data["tickets"]["T-1"]["status"], "parked")
        self.assertIn("fixer: T-1 bugbot", text(first))
        second = watch.apply(data, now=NOW)
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 2)
        self.assertIn("fixer: T-1 bugbot", text(second))
        parked = watch.apply(data, now=NOW)
        blob = text(parked)
        self.assertEqual(data["tickets"]["T-1"]["status"], "parked")
        self.assertEqual(data["tickets"]["T-1"]["parkReason"], "fixer cap: bugbot")
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 2)
        self.assertEqual(blob.count("slack: alarm T-1 fixer cap: bugbot"), 1)
        self.assertIn("park: T-1 fixer cap: bugbot", blob)
        later = watch.apply(data, now=NOW)
        self.assertNotIn("slack: alarm", text(later))
        self.assertEqual(data["tickets"]["T-1"]["watch"]["fixerAttempts"], 2)


class StatusTests(unittest.TestCase):
    def test_status_leads_with_alarms_and_stalls_and_stores_open_work(self):
        alarmed = ticket(
            "A-1",
            status="alarm",
            alarm="ci-red",
            phase="reviewing",
            attempts=1,
            pr={"url": "https://example.test/pull/11", "bugbot": "fail", "ci": "red", "acResults": {"builds": "fail"}},
        )
        stalled_row = stalled(
            ticket(
                "S-1",
                status="review",
                phase="reviewing",
                attempts=2,
                pr={"url": "https://example.test/pull/12", "bugbot": "pass", "ci": "green", "acResults": {"builds": "pass"}},
            ),
            "reviewing",
        )
        stalled_row["stalled"] = True
        stalled_row["watch"]["stalled"] = True
        stalled_row["watch"]["fixerAttempts"] = 1
        queued = ticket("Q-1")
        merged = ticket("M-1", status="merged", phase="merged")
        data = beam([queued, stalled_row, alarmed, merged])
        lines = watch.snapshot(data, now=NOW)
        blob = text(lines)
        self.assertLess(blob.find("alarms:"), blob.find("stalls:"))
        self.assertLess(blob.find("stalls:"), blob.find("counts:"))
        self.assertLess(blob.find("counts:"), blob.find("open-work:"))
        self.assertLess(blob.find("A-1 state="), blob.find("stalls:"))
        self.assertLess(blob.find("stalls:"), blob.find("S-1 state="))
        self.assertLess(blob.find("S-1 state="), blob.find("counts:"))
        self.assertIn("open: 3", blob)
        self.assertIn("alarms: 1", blob)
        self.assertIn("stalls: 1", blob)
        self.assertNotIn("M-1", blob)
        row = next(line for line in lines if line.startswith("S-1 state="))
        for field in ("state=reviewing", "step=", "age=", "last=", "pr=https://example.test/pull/12", "bugbot=pass", "ci=green", "ac=pass", "stalled=yes", "retries=3", "alarm=none", "next="):
            self.assertIn(field, row)
        stored = data["openWork"]
        self.assertEqual(stored["alarms"], ["A-1"])
        self.assertIn("S-1", stored["stalls"])
        self.assertEqual([item["id"] for item in stored["tickets"]], ["A-1", "Q-1", "S-1"])
        self.assertEqual(stored["counts"].get("reviewing"), 2)

    def test_digest_posts_on_the_interval_and_not_twice(self):
        data = beam([ticket("Q-1")])
        first = watch.apply(data, now=NOW)
        self.assertEqual(text(first).count("slack: digest"), 1)
        self.assertIn("digest: ", text(first))
        second = watch.apply(data, now=NOW)
        self.assertNotIn("slack: digest", text(second))
        third = watch.apply(data, now=LATER)
        self.assertEqual(text(third).count("slack: digest"), 1)

        paused = beam([ticket("Q-1")])
        paused["runState"] = "paused"
        self.assertEqual(watch.apply(paused, now=NOW), ["watch: idle"])

    def test_supervise_logs_the_stall_and_status_prints_open_work(self):
        row = stalled(
            ticket(
                "T-1",
                status="review",
                phase="reviewing",
                pr={"url": "https://example.test/pull/13", "bugbot": "pass", "ci": "green", "rollup": "green"},
            ),
            "reviewing",
        )
        data = beam([row])
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        warp = self.tmp / ".warp"
        warp.mkdir()
        path = warp / "beam.json"
        path.write_text(json.dumps(data) + "\n")
        pipeline.advance(data, beam_path=path, now=NOW)
        log = warp / "tickets" / "T-1" / "log.jsonl"
        self.assertIn('"type":"stall"', log.read_text())
        self.assertIn("T-1", data["openWork"]["stalls"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scan.print_open_work(path)
        printed = buf.getvalue()
        self.assertIn("alarms:", printed)
        self.assertIn("stalls:", printed)
        self.assertIn("open-work:", printed)
        self.assertIn("counts:", printed)
        saved = json.loads(path.read_text())
        self.assertIn("openWork", saved)

        status_buf = io.StringIO()
        with contextlib.redirect_stdout(status_buf):
            scan.write_status(path)
        status = (warp / "STATUS.md").read_text()
        self.assertIn("## Working now", status)
        self.assertIn("## Open work", status)
        self.assertIn("alarms:", status)
        self.assertIn("T-1 state=", status_buf.getvalue())
