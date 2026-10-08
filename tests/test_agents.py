"""One Shuttle per ticket, spawn caps, and cleanup. No second agent while one is alive."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import agents  # noqa: E402
import beam  # noqa: E402
import orchestrator  # noqa: E402

NOW = "2026-10-06T12:00:00Z"
FRESH = "2026-10-06T11:50:00Z"
STALE = "2026-10-06T10:00:00Z"
ORIGIN = "https://github.com/smlsr/Warp.git"
OTHER = "https://github.com/acme/Other.git"
UPDATED = "2026-10-06T11:00:00Z"


def ticket(tid="T-1", **extra):
    row = {
        "id": tid,
        "status": "coding",
        "summary": "ship %s" % tid,
        "size": "M",
        "module": "app",
        "hours": 1,
        "tokens": 0,
        "minutes": 0,
        "deps": [],
        "locks": ["src/%s" % tid],
        "agent": "",
        "branch": "warp/%s" % tid,
        "attempts": 0,
        "pr": {"url": "https://example.test/pull/1"},
        "shuttle": {},
    }
    row.update(extra)
    return row


def cloud_item(ident, name, status="IDLE", repo=ORIGIN, prompt="", updated=UPDATED):
    item = {
        "id": ident,
        "name": name,
        "status": status,
        "updatedAt": updated,
        "url": "https://cursor.com/agents/%s" % ident,
    }
    if repo:
        item["repos"] = [{"url": repo}]
    if prompt:
        item["prompt"] = {"text": prompt}
    return item


def recording_transport(items, details=None):
    calls = []
    details = details or {}

    def transport(method, url, key, body=None):
        calls.append((method, url, key))
        if method == "POST":
            return 200, {}
        if "/v1/agents?" in url:
            return 200, {"items": items}
        ident = url.rstrip("/").rsplit("/", 1)[-1]
        return 200, details.get(ident, {})

    return calls, transport


def posted_urls(calls):
    return [url for method, url, _key in calls if method == "POST"]


def beam_of(*rows, **config):
    cfg = {"staleMinutes": 15, "maxAgents": 18, "baseBranch": "main"}
    cfg.update(config)
    tickets = {}
    for row in rows:
        tickets[row["id"]] = row
    return {
        "version": 1,
        "runState": "running",
        "paused": False,
        "tickets": tickets,
        "gates": [],
        "config": cfg,
        "program": {"criticalPath": []},
    }


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "beam.json"

    def save(self, data):
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def test_a_ticket_never_has_two_live_shuttles(self):
        data = beam_of(ticket())
        self.save(data)
        first = agents.launch_lines(data, data["tickets"]["T-1"], "implement", beam_path=self.path, now=NOW)
        second = agents.launch_lines(data, data["tickets"]["T-1"], "implement", beam_path=self.path, now=NOW)
        doc = json.loads((self.tmp / "agents.json").read_text())
        live = [row for row in doc["agents"] if row.get("state") in {"starting", "running"} and not row.get("ended")]
        self.assertEqual(len(live), 1)
        self.assertTrue(first[0].startswith("start T-1"))
        self.assertEqual(second, ["shuttle: hold T-1"])
        self.assertEqual(doc["agents"][0]["id"], data["tickets"]["T-1"]["agent"])
        self.assertEqual(doc["agents"][0]["role"], "shuttle")
        self.assertEqual(doc["agents"][0]["ticket"], "T-1")

    def test_a_returned_fix_resumes_the_same_shuttle(self):
        data = beam_of(ticket())
        self.save(data)
        row = data["tickets"]["T-1"]
        agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
        ident = row["agent"]
        agents.note_returned(data, row, now=NOW, beam_path=self.path)
        again = agents.launch_lines(data, row, "fix", "red", beam_path=self.path, now=NOW)
        self.assertTrue(again[0].startswith("resume T-1"))
        self.assertIn("resume: T-1 agent=%s" % ident, again)
        self.assertEqual(row["agent"], ident)
        doc = json.loads((self.tmp / "agents.json").read_text())
        self.assertEqual(len(doc["spawns"]), 1)
        self.assertEqual(sum(1 for item in doc["agents"] if item["id"] == ident), 1)

    def test_registry_records_the_exit(self):
        data = beam_of(ticket())
        self.save(data)
        row = data["tickets"]["T-1"]
        agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
        agents.note_returned(data, row, now=NOW, beam_path=self.path)
        doc = json.loads((self.tmp / "agents.json").read_text())
        saved = doc["agents"][0]
        self.assertEqual(saved["state"], "ended")
        self.assertEqual(saved["endReason"], "returned")
        self.assertEqual(saved["started"], NOW)
        self.assertEqual(saved["ended"], NOW)
        self.assertIn("session", saved)

    def test_a_fresh_heartbeat_is_not_replaced_across_sessions(self):
        row = ticket(
            agent="shuttle-T-1",
            lastSeenAt=FRESH,
            workerStartedAt=STALE,
            shuttle={"pending": True, "step": "implement", "session": "old-session"},
        )
        data = beam_of(row)
        data["parentSession"] = "new-session"
        self.save(data)
        self.assertTrue(orchestrator.shuttle_is_live(row, data, now=NOW))
        self.assertFalse(agents.confirmed_dead(row, data, now=NOW))
        lines = beam.watchdog(self.path, now=NOW)
        self.assertNotIn("shuttle: replace T-1 agent=shuttle-T-1-r1 branch=warp/T-1", "\n".join(lines))
        self.assertFalse(any(line.startswith("shuttle: replace") for line in lines))
        held = agents.launch_lines(data, data["tickets"]["T-1"], "restart", beam_path=self.path, now=NOW)
        self.assertEqual(held, ["shuttle: hold T-1"])

    def test_a_stale_shuttle_is_ended_before_the_replacement_starts(self):
        row = ticket(
            agent="shuttle-T-1",
            lastSeenAt=STALE,
            workerStartedAt=STALE,
            status="coding",
            size="M",
        )
        data = beam_of(row, maxRecoveries=5)
        self.save(data)
        self.assertTrue(agents.confirmed_dead(row, data, now=NOW))
        lines = beam.watchdog(self.path, now=NOW)
        self.assertTrue(any(line.startswith("shuttle: replace T-1 agent=") for line in lines))
        doc = json.loads((self.tmp / "agents.json").read_text())
        rows = [item for item in doc["agents"] if item.get("id")]
        self.assertGreaterEqual(len(rows), 2)
        old = rows[0]
        new = rows[1]
        self.assertEqual(old["id"], "shuttle-T-1")
        self.assertEqual(old["state"], "ended")
        self.assertEqual(old["endReason"], "dead")
        self.assertEqual(old["ended"], NOW)
        self.assertEqual(new["state"], "starting")
        self.assertNotEqual(new["id"], old["id"])
        self.assertLessEqual(old["ended"], new["started"])
        live = [item for item in rows if item.get("state") in {"starting", "running"} and not item.get("ended")]
        self.assertEqual(len(live), 1)

    def test_spawn_caps_stop_and_do_not_loop(self):
        data = beam_of(ticket(), maxSpawnsPerTicket=2, maxSpawnsPerHour=10)
        self.save(data)
        row = data["tickets"]["T-1"]
        for _step in range(2):
            agents.note_returned(data, row, now=NOW, beam_path=self.path)
            started = agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
            self.assertTrue(started[0].startswith("start "))
            agents.note_returned(data, row, now=NOW, beam_path=self.path)
        capped = agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
        self.assertIn("spawn: cap T-1", capped)
        self.assertEqual(row["alarm"], "spawn-cap")
        again = agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
        self.assertEqual(again[0], "spawn: cap T-1")
        self.assertEqual(sum(1 for line in again if line.startswith("start ")), 0)

        registry = self.tmp / "agents.json"
        if registry.exists():
            registry.unlink()
        other = beam_of(ticket("A-1"), ticket("B-1"), ticket("C-1"), maxSpawnsPerTicket=8, maxSpawnsPerHour=2)
        self.save(other)
        for tid in ("A-1", "B-1"):
            lines = agents.launch_lines(other, other["tickets"][tid], "implement", beam_path=self.path, now=NOW)
            self.assertTrue(lines[0].startswith("start "))
        blocked = agents.launch_lines(other, other["tickets"]["C-1"], "implement", beam_path=self.path, now=NOW)
        self.assertIn("spawn: cap C-1", blocked)
        self.assertEqual(blocked[0], "spawn: cap C-1")

    def test_listener_restarts_back_off_and_then_cap(self):
        data = beam_of(ticket(), maxListenerRestartsPerHour=2)
        data["listener"] = {"restarts": 0}
        self.save(data)
        self.assertEqual(agents.listener_decision(data, now=NOW, beam_path=self.path), "start")
        data["listener"]["restarts"] = 1
        agents.note_listener_restart(data, now=NOW, beam_path=self.path)
        self.assertEqual(agents.listener_backoff_minutes(1), 0)
        second = "2026-10-06T12:10:00Z"
        self.assertEqual(agents.listener_decision(data, now=second, beam_path=self.path), "start")
        data["listener"]["restarts"] = 2
        agents.note_listener_restart(data, now=second, beam_path=self.path)
        self.assertEqual(agents.listener_decision(data, now=second, beam_path=self.path), "backoff")
        after = "2026-10-06T12:13:00Z"
        self.assertEqual(agents.listener_decision(data, now=after, beam_path=self.path), "cap")
        agents.cap_listener(data, now=after, beam_path=self.path)
        self.assertEqual(agents.listener_decision(data, now="2026-10-06T12:40:00Z", beam_path=self.path), "cap")

    def test_bugbot_is_requested_once_per_commit(self):
        data = beam_of(ticket())
        row = data["tickets"]["T-1"]
        row["pr"]["headSha"] = "aaa"
        self.save(data)
        self.assertTrue(agents.allow_bugbot(row))
        agents.note_bugbot(data, row, now=NOW, beam_path=self.path)
        self.assertFalse(agents.allow_bugbot(row))
        self.assertTrue(agents.same_commit_bugbot(row))
        row["pr"]["headSha"] = "bbb"
        row["pr"]["bugbotRequested"] = False
        row["pr"]["bugbotRequestedSha"] = ""
        row["pr"].pop("bugbotRequestedAt", None)
        self.assertTrue(agents.allow_bugbot(row))
        agents.note_bugbot(data, row, now=NOW, beam_path=self.path)
        self.assertFalse(agents.allow_bugbot(row))
        agents.note_bugbot(data, row, now=NOW, beam_path=self.path)
        doc = json.loads((self.tmp / "agents.json").read_text())
        bugbots = [item for item in doc["agents"] if item.get("role") == "bugbot"]
        self.assertEqual(sorted(item["id"] for item in bugbots), ["bugbot:T-1:aaa", "bugbot:T-1:bbb"])

    def test_nothing_spawns_after_stop_or_when_the_run_is_finished(self):
        data = beam_of(ticket())
        self.save(data)
        data["runState"] = "stopped"
        data["paused"] = True
        lines = agents.launch_lines(data, data["tickets"]["T-1"], "implement", beam_path=self.path, now=NOW)
        self.assertEqual(lines, ["spawn: closed T-1"])
        self.assertEqual(agents.listener_decision(data, now=NOW, beam_path=self.path), "closed")
        self.assertEqual(agents.check_line(data), "spawn: closed paused")
        stopped = agents.stop_all(data, now=NOW, beam_path=self.path)
        self.assertEqual(stopped, [])
        data["runState"] = "running"
        data["paused"] = False
        agents.launch_lines(data, data["tickets"]["T-1"], "implement", beam_path=self.path, now=NOW)
        data["runState"] = "stopped"
        data["paused"] = True
        stopped = agents.stop_all(data, now=NOW, beam_path=self.path)
        self.assertTrue(stopped)
        self.assertTrue(all(line.startswith("stop: ") for line in stopped))
        done = beam_of(ticket(status="merged"), ticket("T-2", status="parked"))
        self.assertFalse(agents.spawns_open(done))
        self.assertEqual(
            agents.launch_lines(done, done["tickets"]["T-1"], "implement", now=NOW),
            ["spawn: closed T-1"],
        )
        self.assertEqual(agents.check_line(done), "spawn: closed done")

    def write_registry(self, rows):
        (self.tmp / "agents.json").write_text(json.dumps({"agents": rows, "spawns": []}) + "\n")

    def sweep(self, items, apply=False, all_idle=False, any_repo=False, details=None, self_id="", rows=None):
        data = beam_of(ticket())
        self.save(data)
        if rows:
            self.write_registry(rows)
        calls, transport = recording_transport(items, details)
        lines = agents.cleanup(
            data,
            beam_path=self.path,
            cloud=True,
            apply=apply,
            key="test-key",
            transport=transport,
            now=NOW,
            all_idle=all_idle,
            any_repo=any_repo,
            origin=ORIGIN,
            self_id=self_id,
        )
        return lines, posted_urls(calls)

    def test_cleanup_prints_links_without_a_key(self):
        data = beam_of(ticket(agent="bc-old"))
        self.save(data)
        row = data["tickets"]["T-1"]
        agents.launch_lines(data, row, "implement", beam_path=self.path, now=NOW)
        agents.note_closed(data, row, "merged", now=NOW, beam_path=self.path)
        listed = agents.cleanup(data, beam_path=self.path, now=NOW)
        self.assertTrue(any("cleanup: no CURSOR_API_KEY" in line for line in listed))
        self.assertTrue(any("https://cursor.com/agents/bc-old" in line for line in listed))
        self.assertFalse(any(line.startswith("cleanup: archived") for line in listed))

    def test_cleanup_repo_filter(self):
        self.assertEqual(agents.repo_key(ORIGIN), agents.repo_key("git@github.com:smlsr/Warp.git"))
        self.assertNotEqual(agents.repo_key(ORIGIN), agents.repo_key(OTHER))
        here = cloud_item("bc-here", "Shuttle T-1", repo="git@github.com:smlsr/Warp.git")
        other = cloud_item("bc-other", "Shuttle elsewhere", repo=OTHER)
        stranger = cloud_item("bc-stranger", "garden notes")
        bare = {"id": "bc-detail", "name": "", "status": "IDLE", "updatedAt": UPDATED, "url": "https://cursor.com/agents/bc-detail"}
        detail = cloud_item("bc-detail", "Shuttle T-9")
        items = [here, other, stranger, bare]
        lines, posted = self.sweep(items, apply=True, details={"bc-detail": detail})
        self.assertEqual(
            posted,
            [
                "https://api.cursor.com/v1/agents/bc-here/archive",
                "https://api.cursor.com/v1/agents/bc-detail/archive",
            ],
        )
        self.assertTrue(any(line == "cleanup: count 2" for line in lines))
        wider, wider_posted = self.sweep(items, apply=True, all_idle=True, details={"bc-detail": detail})
        self.assertIn("https://api.cursor.com/v1/agents/bc-stranger/archive", wider_posted)
        self.assertNotIn("https://api.cursor.com/v1/agents/bc-other/archive", wider_posted)
        self.assertTrue(any("repo=github.com/smlsr/warp" in line for line in wider))
        both, both_posted = self.sweep(items, apply=True, all_idle=True, any_repo=True, details={"bc-detail": detail})
        self.assertIn("https://api.cursor.com/v1/agents/bc-other/archive", both_posted)

    def test_cleanup_role_match(self):
        rows = [{"id": "bc-reg", "ticket": "T-1", "role": "shuttle", "state": "ended", "ended": NOW}]
        prompt_only = cloud_item("bc-implement", "worker")
        prompt_only.pop("prompt", None)
        items = [
            cloud_item("bc-shuttle", "Shuttle T-1"),
            prompt_only,
            cloud_item("bc-fix", "fix CI"),
            cloud_item("bc-rebase", "rebase onto main"),
            cloud_item("bc-listener", "Warp listener"),
            cloud_item("bc-bugbot", "Warp-triggered Bugbot"),
            cloud_item("bc-prefix", "prefix tool", prompt="leave the prefix alone"),
            cloud_item("bc-reg", "garden notes"),
        ]
        detail = cloud_item("bc-implement", "worker", prompt="IMPLEMENT T-2 acceptance")
        lines, posted = self.sweep(items, apply=True, details={"bc-implement": detail}, rows=rows)
        archived = {url.rsplit("/", 2)[-2] for url in posted}
        self.assertEqual(
            archived,
            {"bc-shuttle", "bc-implement", "bc-fix", "bc-rebase", "bc-listener", "bc-bugbot", "bc-reg"},
        )
        self.assertNotIn("bc-prefix", archived)
        self.assertTrue(any(line == "cleanup: count 7" for line in lines))
        self.assertFalse(agents.warp_role("prefix tool"))
        self.assertTrue(agents.warp_role("IMPLEMENT T-1"))
        self.assertTrue(agents.warp_role("Warp-triggered Bugbot"))

    def test_cleanup_skips_self_and_running_agents(self):
        items = [
            cloud_item("bc-self", "Shuttle self"),
            cloud_item("bc-run", "Shuttle running", status="RUNNING"),
            cloud_item("bc-active", "Shuttle active", status="ACTIVE"),
            cloud_item("bc-idle", "Shuttle idle"),
            cloud_item("bc-foreign", "notes", status="RUNNING", repo=OTHER),
        ]
        previous = os.environ.get("CURSOR_AGENT_ID")
        os.environ["CURSOR_AGENT_ID"] = "bc-self"
        try:
            data = beam_of(ticket())
            self.save(data)
            calls, transport = recording_transport(items)
            lines = agents.cleanup(
                data,
                beam_path=self.path,
                cloud=True,
                apply=True,
                key="test-key",
                transport=transport,
                now=NOW,
                all_idle=True,
                any_repo=True,
                origin=ORIGIN,
            )
        finally:
            if previous is None:
                os.environ.pop("CURSOR_AGENT_ID", None)
            else:
                os.environ["CURSOR_AGENT_ID"] = previous
        posted = posted_urls(calls)
        self.assertEqual(posted, ["https://api.cursor.com/v1/agents/bc-idle/archive"])
        self.assertTrue(any(line == "cleanup: skip bc-self reason=self" for line in lines))
        self.assertTrue(any(line == "cleanup: skip bc-run reason=running" for line in lines))
        self.assertTrue(any(line == "cleanup: skip bc-active reason=running" for line in lines))
        self.assertTrue(any(line == "cleanup: skip bc-foreign reason=running" for line in lines))
        self.assertTrue(any(line == "cleanup: count 1" for line in lines))

    def test_cleanup_dry_run_is_the_default(self):
        items = [cloud_item("bc-shuttle", "Shuttle T-1")]
        lines, posted = self.sweep(items, apply=False)
        self.assertEqual(posted, [])
        self.assertTrue(any(line == "cleanup: dry-run" for line in lines))
        self.assertTrue(any(line == "cleanup: count 1" for line in lines))
        match = next(line for line in lines if line.startswith("cloud:"))
        self.assertIn("id=bc-shuttle", match)
        self.assertIn("name=Shuttle T-1", match)
        self.assertIn("repo=github.com/smlsr/warp", match)
        self.assertIn("status=IDLE", match)
        self.assertIn("updated=%s" % UPDATED, match)
        self.assertIn("https://cursor.com/agents/bc-shuttle", match)
        self.assertFalse(any(line.startswith("cleanup: archived") for line in lines))
        data = beam_of(ticket())
        self.save(data)
        calls, transport = recording_transport(items)
        held = agents.cleanup(
            data,
            beam_path=self.path,
            cloud=False,
            apply=True,
            key="test-key",
            transport=transport,
            now=NOW,
            origin=ORIGIN,
            self_id="",
        )
        self.assertTrue(any(line == "cleanup: cloud required to archive" for line in held))
        self.assertEqual(posted_urls(calls), [])
