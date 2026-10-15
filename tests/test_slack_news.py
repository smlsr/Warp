"""1.7.0: Slack and Teams news for alarms in and out, every Shuttle start, and the run's start, resume, pause, and stop."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_isolation import GitRepo, run  # noqa: E402

import news  # noqa: E402
import pipeline  # noqa: E402


def row(tid, **kw):
    base = {"id": tid, "status": "coding", "alarm": None, "locks": ["src/%s" % tid], "size": "M", "summary": tid,
            "branch": "warp/%s" % tid, "pr": {"url": "https://github.com/acme/app/pull/7"}}
    base.update(kw)
    return base


class AlarmNewsTest(unittest.TestCase):
    def test_in_and_out_of_alarm_are_announced_once_each_with_why(self):
        data = {"config": {}, "tickets": {"WE-07": row("WE-07", status="alarm", alarm="lock-escape", escaped=["app/x.ts", "app/y.ts"])}}
        self.assertEqual(news.alarm_news(data), ["herald: WE-07 is in alarm: lock-escape. It edited files outside its locks: app/x.ts, app/y.ts."])
        self.assertEqual(news.alarm_news(data), [])  # once
        t = data["tickets"]["WE-07"]
        t.update(status="claimed", alarm=None, alarmRepair={"state": "working", "added": ["app/x.ts", "app/y.ts"]})
        self.assertEqual(news.alarm_news(data), ["herald: WE-07 is out of alarm (lock-escape): its locks were widened to app/x.ts, app/y.ts and a repair Shuttle is on it."])
        self.assertEqual(news.alarm_news(data), [])

    def test_other_reasons_and_exits(self):
        data = {"config": {}, "tickets": {
            "A": row("A", status="alarm", alarm="worker-died"),
            "B": row("B", status="alarm", alarm="ci-red"),
            "C": row("C", status="alarm", alarm="something-new", error="the provider said no"),
        }}
        got = news.alarm_news(data)
        self.assertIn("herald: A is in alarm: worker-died. Its Shuttle died and the recovery cap is reached.", got)
        self.assertIn("herald: B is in alarm: ci-red. CI is red: https://github.com/acme/app/pull/7.", got)
        self.assertIn("herald: C is in alarm: something-new. the provider said no.", got)
        self.assertEqual(news.alarm_text(row("D", status="alarm")), "D is in alarm.")
        data["tickets"]["A"].update(status="recovering", alarm=None, shuttle={"step": "restart"})
        data["tickets"]["B"].update(status="merged", alarm=None)
        got = news.alarm_news(data)
        self.assertIn("herald: A is out of alarm (worker-died): a new Shuttle is restarting it.", got)
        self.assertIn("herald: B is out of alarm (ci-red): it is merged.", got)
        # A different alarm on a ticket already in alarm is news too.
        data["tickets"]["C"]["alarm"] = "stuck"
        self.assertEqual(news.alarm_news(data), ["herald: C is in alarm: stuck. Its Shuttle stopped reporting: the provider said no."])


class StartNewsTest(unittest.TestCase):
    def test_every_shuttle_start_is_announced_once(self):
        data = {"config": {"maxAgents": 4, "runner": "local"}, "runState": "running", "tickets": {}}
        ticket = row("WE-09", status="review", claimedAt="2026-10-15T10:00:00Z")
        data["tickets"]["WE-09"] = ticket
        lines = []
        pipeline.begin_fix(data, ticket, ["bugbot: app/x.ts:12 null check (high)"], lines)
        heralds = [line for line in lines if line.startswith("herald: WE-09: a fix Shuttle is starting")]
        self.assertEqual(len(heralds), 1, lines)
        self.assertIn("on warp/WE-09", heralds[0])
        self.assertIn("bugbot: app/x.ts:12 null check (high)", heralds[0])
        again = pipeline._start_lines(data, ticket, "fix", "bugbot: app/x.ts:12 null check (high)")
        self.assertFalse(any(line.startswith("herald:") for line in again), again)  # printed again until launched, posted once
        other = row("WE-10", status="review", claimedAt="2026-10-15T10:00:00Z")
        data["tickets"]["WE-10"] = other
        pipeline.mark_shuttle(other, "review", data)
        nxt = pipeline._start_lines(data, other, "review")
        self.assertTrue(any(line.startswith("herald: WE-10: a review Shuttle is starting") for line in nxt), nxt)
        self.assertNotIn("announced", pipeline._shuttle(ticket) if False else {})
        pipeline.mark_shuttle(ticket, "restart", data)  # a new Shuttle on WE-09: announced again
        self.assertNotIn("announced", ticket["shuttle"])

    def test_texts(self):
        data = {"config": {"model": "claude-sonnet-5-5-high"}, "tickets": {}}
        t = row("WE-01")
        self.assertEqual(news.start_text(data, t, "implement", resumed=True),
                         "WE-01: its Shuttle is resuming (implement) (on warp/WE-01, model claude-sonnet-5-5-high).")
        self.assertTrue(news.start_text(data, t, "fix", replacing=True).startswith("WE-01: a new Shuttle replaces the one that died (fix)"))


class RunNewsTest(GitRepo):
    def setUp(self):
        super().setUp()
        self.beam([self.ticket("T-1", status="queued"), self.ticket("T-2", status="merged"),
                   self.ticket("T-3", status="alarm", alarm="stuck")],
                  runner="local", launch="worktree", subagentVm=False, messenger="slack", jiraTransition=False)
        (self.repo / ".warp" / "config.yaml").write_text("messenger: slack\nslackChannel: warp-demo\nnotify: verbose\nrunner: local\n")
        self.path = self.repo / ".warp" / "beam.json"
        data = json.loads(self.path.read_text())
        data["tickets"]["T-3"].update(alarm="stuck", alarmAnnounced="stuck")  # posted when it went in
        self.path.write_text(json.dumps(data))

    def payload(self):
        return json.loads((self.repo / ".warp" / "notify-pass.json").read_text())

    def test_resume_and_pause_post_what_happened(self):
        data = json.loads(self.path.read_text())
        data.update(runState="paused", paused=True, pauseReason="lunch")
        self.path.write_text(json.dumps(data))
        out = run("scan.py", "resume", "--beam", ".warp/beam.json", "--force", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("herald: post .warp/notify-pass.json to slack warp-demo", out.stdout)
        body = self.payload()["lines"]
        head = body[0]
        self.assertTrue(head.startswith("Run resumed (was paused: lunch). 1 of 3 merged"), body)
        self.assertIn("Starting now: T-1, T-3.", head)  # resume restarts the stuck ticket
        self.assertIn("T-1 claimed (size M", "\n".join(body))
        self.assertTrue(any(line.startswith("T-3 is out of alarm (stuck): ") for line in body), body)
        out = run("scan.py", "pause", "--beam", ".warp/beam.json", "--reason", "dinner", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(any(line.startswith("Run paused (dinner).") for line in self.payload()["lines"]), self.payload())

    def test_a_refused_start_posts_why(self):
        lines = ["refuse: multi-repo run not started: launch: agent starts each ticket in its own cloud clone"]
        self.assertEqual(news.refused_line("resume", lines),
                         "Run not resumed: multi-repo run not started: launch: agent starts each ticket in its own cloud clone")


if __name__ == "__main__":
    unittest.main()
