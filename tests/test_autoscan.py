"""1.7.2: the auto-scan, config.yaml synced into the beam, the hourly spawn cap as a wait, the parent repo that
already has state, and status posts that list tickets in review and Bugbot review."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from test_isolation import GitRepo, git, run  # noqa: E402

import autoscan  # noqa: E402
import beam as beam_mod  # noqa: E402
import config_sync  # noqa: E402
import multirepo  # noqa: E402
import news  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-16T11:00:00Z"
LATER = "2026-10-16T11:11:00Z"


def row(tid, status="queued", **kw):
    base = {"id": tid, "status": status, "alarm": None, "locks": ["src/%s" % tid], "deps": [], "after": [], "size": "S",
            "summary": tid, "pr": {}, "branch": "warp/%s" % tid, "module": "core", "tokens": 0, "minutes": 0}
    base.update(kw)
    return base


class Beam(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".warp" / "beam.json"
        self.path.parent.mkdir(parents=True)

    def write(self, tickets, **cfg):
        config = {"maxAgents": 4, "runner": "local", "maxRecoveries": 5, "jiraTransition": False}
        config.update(cfg)
        data = {"runState": "running", "paused": False, "config": config, "gates": [],
                "tickets": {t["id"]: t for t in tickets}}
        self.path.write_text(json.dumps(data))
        return data

    def load(self):
        return json.loads(self.path.read_text())


class IntervalTest(Beam):
    def test_default_off_and_due(self):
        data = self.write([])
        self.assertEqual(autoscan.interval(data, self.path), 600)
        self.assertTrue(autoscan.due(data, self.path, NOW))
        data["autoScan"] = {"at": NOW}
        self.assertFalse(autoscan.due(data, self.path, "2026-10-16T11:09:00Z"))
        self.assertTrue(autoscan.due(data, self.path, LATER))
        (self.path.parent / "config.yaml").write_text("autoScan: 0\n")
        self.assertEqual(autoscan.interval(data, self.path), 0)
        self.assertFalse(autoscan.due(data, self.path, LATER))
        (self.path.parent / "config.yaml").write_text("autoScan: 20\n")
        self.assertEqual(autoscan.interval(data, self.path), 60)


class ScanTest(Beam):
    def test_capped_alarms_get_a_fresh_budget_and_the_run_hears_once(self):
        url = "https://github.com/acme/app/pull/3"
        self.write([
            row("W-1", "alarm", alarm="worker-died", recoveries=5),
            row("S-1", "alarm", alarm="stuck", stuckRestarts=5, pr={"url": url}),
            row("P-1", "parked", alarm="parked", attempts=5, pr={"url": url}),
            row("G-1", "alarm", alarm="gate-red"),
            row("F-1", "alarm", alarm="worker-died", recoveries=1),  # under the cap: the pipeline restarts it
            row("A-1", "alarm", alarm="worker-died", recoveries=5, acceptance=["AC-1: ok"], pr={"acResults": {"AC-1": "pass"}}),
        ])
        lines = autoscan.run_on_disk(self.path, now=NOW)
        self.assertTrue(lines[0].startswith("autoscan: scan 1: 3 reset"), lines)
        data = self.load()
        t = data["tickets"]
        self.assertEqual((t["W-1"]["status"], t["W-1"]["recoveries"], t["W-1"]["alarm"]), ("queued", 0, None))
        self.assertEqual((t["S-1"]["status"], t["S-1"]["stuckRestarts"]), ("fix", 0))
        self.assertEqual((t["P-1"]["status"], t["P-1"]["attempts"]), ("fix", 0))
        self.assertEqual(t["G-1"]["status"], "alarm")
        self.assertEqual(t["F-1"]["status"], "alarm")
        self.assertEqual(t["A-1"]["status"], "alarm")  # its criteria pass: the pass opens its pull request instead
        self.assertEqual(t["W-1"]["autoScanResets"], 1)
        self.assertIsNone(data["repairSweep"]["at"])  # the sweep runs in this pass
        herald = [l for l in lines if l.startswith("herald: Auto-scan:")]
        self.assertEqual(len(herald), 1)
        self.assertIn("reset P-1, S-1, W-1", herald[0])
        # Not due again for 10 minutes; one scan later F-1 is still in alarm, so it gets the same.
        self.assertEqual(autoscan.run_on_disk(self.path, now="2026-10-16T11:05:00Z"), [])
        again = autoscan.run_on_disk(self.path, now=LATER)
        self.assertIn("F-1", "\n".join(again))
        self.assertEqual(self.load()["tickets"]["F-1"]["status"], "queued")

    def test_resets_run_out_and_a_person_is_told_once(self):
        self.write([row("W-1", "alarm", alarm="worker-died", recoveries=5, autoScanResets=3)])
        lines = autoscan.run_on_disk(self.path, now=NOW)
        self.assertIn("left for a person (warp:retry W-1)", "\n".join(lines))
        self.assertTrue(any(l.startswith("herald: Auto-scan: needs a person") for l in lines), lines)
        later = autoscan.run_on_disk(self.path, now=LATER)
        self.assertFalse(any(l.startswith("herald:") for l in later), later)
        self.assertEqual(self.load()["tickets"]["W-1"]["status"], "alarm")

    def test_spawn_cap_lock_escape_merge_and_pull_request(self):
        url = "https://github.com/acme/app/pull/4"
        self.write([
            row("C-1", "alarm", alarm="spawn-cap", spawnCapped=True),
            row("L-1", "alarm", alarm="lock-escape", escaped=["src/L-1/x.ts"], alarmRepairs=5,
                alarmRepair={"state": "gave-up", "gaveUp": True}),
            row("M-1", "merging", phase="merging", pr={"url": url, "headSha": "a" * 40, "mergeRequestedSha": "a" * 40,
                                                         "mergeRequestedAt": "2026-10-16T10:30:00Z"}),
            row("O-1", "coding", phase="pr-open", pr={}),
        ])
        (self.path.parent / "agents.json").write_text(json.dumps({"agents": [
            {"id": "", "ticket": "C-1", "role": "shuttle", "capped": True, "state": "ended"}], "spawns": []}))
        lines = autoscan.run_on_disk(self.path, now=NOW)
        text = "\n".join(lines)
        t = self.load()["tickets"]
        self.assertEqual(t["C-1"]["status"], "queued")
        self.assertTrue(t["C-1"].get("spawnResetAt"))
        self.assertFalse(json.loads((self.path.parent / "agents.json").read_text())["agents"][0]["capped"])
        self.assertIn("L-1 lock-escape repairs given up", text)
        self.assertIsNone(t["L-1"]["alarm"])  # its escaped path is already inside its locks: cleared
        self.assertEqual((t["M-1"]["status"], t["M-1"]["phase"]), ("review", "ready"))
        self.assertNotIn("mergeRequestedSha", t["M-1"]["pr"])
        self.assertTrue(t["O-1"]["pr"]["openRequested"])

    def test_the_wait_wakes_for_a_due_scan(self):
        data = self.write([row("Q-1", "review", phase="reviewing", pr={"url": "u"})], pollSeconds=600, reviewPollSeconds=0)
        data["listener"] = {}
        data["autoScan"] = {"at": "2026-10-16T10:00:00Z"}
        data["pass"] = {"work": ["x"], "startedAt": "2026-10-16T10:00:00Z"}
        self.path.write_text(json.dumps(data))
        clock = iter(range(1792148400, 1792150000, 2))  # 2026-10-16T11:00:00Z onward
        got = orchestrator.parent_wait(self.path, sleep=lambda s: None, clock=lambda: next(clock))
        self.assertEqual(got, "work autoscan")


class SyncTest(Beam):
    def test_config_yaml_reaches_the_beam_typed_and_old_defaults_move_on(self):
        data = self.write([], maxSpawnsPerHour=40, maxInProgress=20, pollSeconds=300, mergeWindows=["08:30"])
        (self.path.parent / "config.yaml").write_text(
            "maxAgents: 18  # agents\nmaxSpawnsPerHour: 200\nrespectMergeWindows: true\nmergeWindows: [\"09:00\"]\n"
            "siblingRoot: /tmp/x\nslackToken: xoxb-1\nunknownKey: 4\n")
        lines = config_sync.sync(data, self.tmp)
        cfg = data["config"]
        self.assertEqual((cfg["maxAgents"], cfg["maxSpawnsPerHour"], cfg["respectMergeWindows"]), (18, 200, True))
        self.assertIn("config: maxSpawnsPerHour 40 -> 200 (.warp/config.yaml)", lines)
        self.assertEqual(cfg["maxInProgress"], "auto")  # 20 was an old default and the file does not set it
        self.assertEqual(cfg["pollSeconds"], 120)
        self.assertEqual(cfg["mergeWindows"], ["08:30"])  # a list is left to the code that parses it
        for key in ("siblingRoot", "slackToken", "unknownKey"):
            self.assertNotIn(key, cfg)
        self.assertFalse(config_sync.due(data, self.tmp))
        self.assertEqual(config_sync.sync(data, self.tmp), [])

    def test_a_kept_old_value_in_the_file_is_not_replaced(self):
        data = self.write([], maxSpawnsPerHour=40)
        (self.path.parent / "config.yaml").write_text("maxSpawnsPerHour: 40\n")
        self.assertEqual(config_sync.sync(data, self.tmp), [])
        self.assertEqual(data["config"]["maxSpawnsPerHour"], 40)


class StartSyncTest(GitRepo):
    def test_resume_puts_the_new_spawn_cap_in_the_beam_first(self):
        self.beam([self.ticket("T-1")], runner="local", launch="worktree", subagentVm=False, maxSpawnsPerHour=40)
        (self.repo / ".warp" / "config.yaml").write_text("runner: local\nmaxSpawnsPerHour: 250\n")
        out = run("scan.py", "resume", "--beam", ".warp/beam.json", "--force", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("config: maxSpawnsPerHour 40 -> 250 (.warp/config.yaml)", out.stdout)
        self.assertLess(out.stdout.index("config: maxSpawnsPerHour"), out.stdout.index("start T-1") if "start T-1" in out.stdout else len(out.stdout))
        self.assertEqual(json.loads((self.repo / ".warp" / "beam.json").read_text())["config"]["maxSpawnsPerHour"], 250)


class ParentTest(unittest.TestCase):
    def test_the_repo_that_has_state_stays_the_parent(self):
        self.assertEqual(multirepo.choose_parent({"Core": 380, "UI": 20}, [], {"Core": "", "UI": ".warp/beam.json"}), "UI")
        self.assertEqual(multirepo.choose_parent({"Core": 380, "UI": 20}, [], {"Core": "x", "UI": "y"}), "Core")
        self.assertEqual(multirepo.choose_parent({"Core": 380, "UI": 20}, [], {}), "Core")

    def test_state_on_disk_or_published_on_the_base_branch(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
        work = tmp / "work"
        subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True, check=True)
        git(work, "config", "user.email", "w@example.com")
        git(work, "config", "user.name", "W")
        (work / "README.md").write_text("x\n")
        git(work, "add", "README.md")
        git(work, "commit", "-m", "init")
        git(work, "push", "origin", "main")
        clone = tmp / "UI"
        subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True, check=True)
        self.assertEqual(multirepo.state_in(clone), "")
        (work / ".warp" / "runs" / "ui").mkdir(parents=True)
        (work / ".warp" / "runs" / "ui" / "beam.json").write_text("{\"tickets\": {}}\n")
        git(work, "add", ".warp")
        git(work, "commit", "-m", "state")
        git(work, "push", "origin", "main")
        git(clone, "fetch", "origin")
        self.assertIn(".warp/runs/ui/beam.json on origin/", multirepo.state_in(clone))
        (work / ".warp" / "beam.json").write_text("{\"tickets\": {}}\n")
        self.assertEqual(multirepo.state_in(work), ".warp/beam.json")
        got = multirepo.state_repos(work, {"siblingRoot": ".."}, "work", ["work", "UI", "Nope"])
        self.assertEqual(got["work"], ".warp/beam.json")
        self.assertTrue(got["UI"])
        self.assertEqual(got["Nope"], "")


class StatusListTest(unittest.TestCase):
    def test_review_and_bugbot_review_are_listed_by_id(self):
        url = "https://github.com/acme/app/pull/%d"
        data = {"config": {"bugbotRequired": True}, "runState": "running", "tickets": {
            "A": row("A", "coding", phase="implementing", shuttle={"pending": True, "step": "implement", "startedAt": NOW}, lastSeenAt=NOW),
            "B": row("B", "review", phase="reviewing", pr={"url": url % 1, "headSha": "b" * 40,
                                                           "bugbotRun": {"sha": "b" * 40, "startedAt": "2026-10-16T10:56:00Z"}}),
            "C": row("C", "review", phase="reviewing", pr={"url": url % 2, "headSha": "c" * 40, "bugbotRequested": True,
                                                           "bugbotRequestedAt": "2026-10-16T10:58:00Z"}),
            "D": row("D", "review", phase="reviewing", pr={"url": url % 3, "headSha": "d" * 40, "ci": "pending", "bugbot": "pass",
                                                           "bugbotSha": "d" * 40}),
            "E": row("E", "awaiting_approval", phase="ready", pr={"url": url % 4}),
            "F": row("F", "alarm", alarm="stuck"),
            "Q": row("Q"),
        }}
        text = news.pipeline_text(data, NOW)
        self.assertIn("Shuttles working (1): A (implement).", text)
        self.assertIn("Bugbot reviewing (2): B running 4m, C not started 2m.", text)
        self.assertIn("Waiting for approval (1): E.", text)
        self.assertIn("Alarms (1): F (stuck).", text)
        self.assertNotIn("Q", text)


if __name__ == "__main__":
    unittest.main()
