"""Checkout isolation, rollup slots, committed state, the start gate, and upgrade."""

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import checkout  # noqa: E402
import install  # noqa: E402
import orchestrator  # noqa: E402
import prompt_gate  # noqa: E402
import provider  # noqa: E402
import state_commit  # noqa: E402


def run(script, *args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


class GitRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "warp@example.com")
        git(self.repo, "config", "user.name", "Warp")
        (self.repo / "README.md").write_text("hello\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "init")

    def beam(self, tickets, **config):
        warp = self.repo / ".warp"
        warp.mkdir(exist_ok=True)
        cfg = {"runner": "local", "maxAgents": 4, "jiraTransition": False, "messenger": "teams"}
        cfg.update(config)
        data = {
            "version": 1,
            "runState": "running",
            "config": cfg,
            "tickets": {t["id"]: t for t in tickets},
            "gates": [],
        }
        path = warp / "beam.json"
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def ticket(self, tid="T-1", **kw):
        row = {
            "id": tid,
            "status": kw.get("status", "queued"),
            "agent": kw.get("agent"),
            "branch": kw.get("branch"),
            "worktree": kw.get("worktree"),
            "locks": kw.get("locks", ["src/%s" % tid]),
            "size": "M",
            "module": "core",
            "tokens": 0,
            "minutes": 0,
            "pr": kw.get("pr", {"url": None, "openedAt": None, "bugbot": None, "ci": None}),
            "plan": {},
            "result": {},
        }
        return row


class WorktreeTests(GitRepo):
    def test_add_and_remove_a_real_worktree(self):
        self.beam([self.ticket()])
        added = run("checkout.py", "add", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        path = self.repo / ".warp" / "worktrees" / "T-1"
        self.assertTrue(path.is_dir(), added.stdout)
        listed = git(self.repo, "worktree", "list")
        self.assertIn(str(path), listed.stdout)
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertEqual(data["tickets"]["T-1"]["worktree"], ".warp/worktrees/T-1")
        self.assertTrue(data["tickets"]["T-1"]["branch"].startswith("warp/T-1"))
        data["tickets"]["T-1"]["status"] = "coding"
        other = self.ticket("T-2", status="coding")
        other["worktree"] = ".warp/worktrees/T-1"
        other["status"] = "coding"
        data["tickets"]["T-2"] = other
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        blocked = run("checkout.py", "add", "--root", str(self.repo), "--id", "T-2", cwd=self.repo)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("refuse", blocked.stderr + blocked.stdout)
        removed = run("checkout.py", "remove", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(path.exists())

    def test_two_agents_cannot_share_a_ticket_or_a_vm(self):
        row = self.ticket(status="coding", agent="agent-a")
        self.beam([row], runner="cloud")
        second = run(
            "checkout.py",
            "bind",
            "--root",
            str(self.repo),
            "--id",
            "T-1",
            "--agent",
            "agent-b",
            cwd=self.repo,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already has agent", second.stderr + second.stdout)
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        data["tickets"]["T-2"] = self.ticket("T-2", status="coding")
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        shared = run(
            "checkout.py",
            "bind",
            "--root",
            str(self.repo),
            "--id",
            "T-2",
            "--agent",
            "agent-a",
            cwd=self.repo,
        )
        self.assertNotEqual(shared.returncode, 0)
        self.assertIn("already on ticket", shared.stderr + shared.stdout)

    def test_cloud_launch_refuses_in_process_and_names_the_task_tool(self):
        self.beam([self.ticket()], runner="cloud")
        refused = run("checkout.py", "implement", "--id", "T-1", cwd=self.repo)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("refuse: in-process", refused.stdout)
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn("tool: Task", launched.stdout)
        self.assertIn("environment: cloud", launched.stdout)
        self.assertIn("subagent_type: shuttle", launched.stdout)
        self.assertIn("no cloud-agent API", launched.stdout)
        self.assertIn("IMPLEMENT T-1", launched.stdout)


class SlotTests(unittest.TestCase):
    def test_pr_opened_keeps_the_slot_until_rollup_is_green(self):
        ticket = {
            "id": "A",
            "status": "coding",
            "locks": ["src/a"],
            "pr": {},
        }
        orchestrator.note_pr_opened(ticket, "https://example.test/pull/4", "2026-01-01T00:00:00Z")
        ticket["status"] = "review"
        self.assertTrue(ticket["pr"]["agentFinished"])
        self.assertTrue(orchestrator.holds_slot(ticket, {}))
        self.assertTrue(orchestrator.holds_locks(ticket))
        ticket["pr"]["rollup"] = "pending"
        ticket["pr"]["ci"] = "green"
        ticket["pr"]["bugbot"] = "pass"
        self.assertTrue(orchestrator.holds_slot(ticket, {"bugbotRequired": True}))
        ticket["pr"]["rollup"] = "green"
        self.assertFalse(orchestrator.holds_slot(ticket, {"bugbotRequired": True}))
        self.assertTrue(orchestrator.holds_locks(ticket))
        self.assertEqual(orchestrator.bugbot_where(), "pull-request")

    def test_rejected_merge_is_not_merged_and_a_queue_enqueues(self):
        ticket = {"id": "A", "status": "review", "pr": {"url": "https://example.test/pull/4", "rollup": "green"}}
        self.assertEqual(orchestrator.merge_report({}, detected_queue=True, succeeded=True), "enqueue")
        self.assertFalse(orchestrator.apply_reported_merge(ticket, "enqueue"))
        self.assertEqual(ticket["status"], "review")
        self.assertEqual(
            orchestrator.merge_report({"mergeQueue": True}, protection_reject=False, succeeded=True),
            "enqueue",
        )
        self.assertEqual(
            orchestrator.merge_report({"mergeQueue": False}, protection_reject=True, succeeded=False),
            "rejected",
        )
        self.assertFalse(orchestrator.apply_reported_merge(ticket, "rejected"))
        self.assertNotEqual(ticket["status"], "merged")
        self.assertTrue(provider.protection_rejects("required status checks are failing; branch protection"))
        self.assertTrue(provider.payload_has_merge_queue({"rules": [{"type": "merge_queue"}]}))
        self.assertFalse(provider.payload_has_merge_queue({"rules": [{"type": "pull_request"}]}))
        self.assertEqual(
            provider.interpret_rollup([{"conclusion": "success"}, {"conclusion": "skipped"}]),
            "green",
        )
        self.assertEqual(provider.interpret_rollup([{"state": "INPROGRESS"}]), "pending")
        self.assertEqual(provider.interpret_rollup([{"state": "FAILED"}]), "red")
        self.assertTrue(orchestrator.apply_reported_merge(ticket, "merged", sha="abc1234"))
        self.assertEqual(ticket["status"], "merged")
        self.assertEqual(ticket["pr"]["sha"], "abc1234")


class GitignoreTests(unittest.TestCase):
    def test_old_warp_line_is_replaced_and_other_lines_stay(self):
        old = textwrap.dedent(
            """\
            dist/
            # Warp live state. Do not commit. Hundreds of status writes are not source history.
            .warp/
            *.log
            """
        )
        snippet = (ROOT / "assets" / "gitignore-snippet.txt").read_text()
        updated, action = install.apply_gitignore(old, snippet)
        self.assertEqual(action, "replace")
        self.assertIn("dist/", updated)
        self.assertIn("*.log", updated)
        self.assertNotRegex(updated, r"(?m)^\.warp/?\s*$")
        self.assertIn(".warp/config.yaml", updated)
        again, second = install.apply_gitignore(updated, snippet)
        self.assertEqual(second, "skip")
        self.assertEqual(again.strip(), updated.strip())


class StateCommitTests(GitRepo):
    def test_state_files_are_committed_and_secrets_are_not(self):
        snippet = (ROOT / "assets" / "gitignore-snippet.txt").read_text()
        (self.repo / ".gitignore").write_text("dist/\n\n" + snippet)
        warp = self.repo / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text('token: "config-token-value"\nslackChannel: "warp"\n')
        beam = {
            "config": {"runner": "local"},
            "tickets": {
                "T-1": {
                    "id": "T-1",
                    "status": "coding",
                    "token": "super-secret-token",
                    "usage": {"cost": 9.5, "tokensIn": 10},
                    "summary": "see https://hooks.slack.com/services/T00/B00/secret",
                }
            },
            "metrics": {"tokens": 10, "cost": 9.5},
        }
        (warp / "beam.json").write_text(json.dumps(beam, indent=2) + "\n")
        (warp / "journal.jsonl").write_text(json.dumps({"type": "set", "token": "journal-token", "id": "T-1"}) + "\n")
        (warp / "STATUS.md").write_text("# status\nT-1 coding\n")
        (warp / "BOARD.md").write_text("# board\nT-1\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "ignore")
        proc = run("state_commit.py", "commit", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ignored = git(self.repo, "check-ignore", "-q", ".warp/config.yaml")
        self.assertEqual(ignored.returncode, 0)
        for rel in (".warp/beam.json", ".warp/journal.jsonl", ".warp/STATUS.md", ".warp/BOARD.md"):
            shown = git(self.repo, "check-ignore", "-q", rel)
            self.assertNotEqual(shown.returncode, 0, rel)
        committed = git(self.repo, "show", "HEAD:.warp/beam.json")
        self.assertNotIn("super-secret-token", committed.stdout)
        self.assertNotIn("config-token-value", committed.stdout)
        self.assertNotIn("hooks.slack.com", committed.stdout)
        self.assertNotIn("9.5", committed.stdout)
        self.assertIn("T-1", committed.stdout)
        journal = git(self.repo, "show", "HEAD:.warp/journal.jsonl")
        self.assertNotIn("journal-token", journal.stdout)
        tracked = git(self.repo, "ls-files", ".warp/config.yaml")
        self.assertEqual(tracked.stdout.strip(), "")


class PromptGateTests(GitRepo):
    def _allow(self, tools):
        cursor = self.repo / ".cursor"
        cursor.mkdir()
        cursor.joinpath("permissions.json").write_text(
            json.dumps({"mcpAllowlist": ["*atlassian*:%s" % name if name != "slack_send_message" else "*slack*:slack_send_message" for name in tools]})
            + "\n"
        )

    def test_cloud_missing_tool_refuses_force_allows_and_local_does_not_block(self):
        self.beam(
            [self.ticket()],
            runner="cloud",
            jiraTransition=True,
            messenger="slack",
            notify="verbose",
        )
        (self.repo / ".warp" / "beam.json")
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        data["runState"] = "stopped"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        refused = run("scan.py", "start", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("transitionJiraIssue", refused.stdout)
        self.assertIn("addOrEditJiraIssueComment", refused.stdout)
        self.assertIn("slack_send_message", refused.stdout)
        self.assertIn("herald:", refused.stdout)
        saved = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertNotEqual(saved.get("runState"), "running")
        forced = run("scan.py", "start", "--force", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(json.loads((self.repo / ".warp" / "beam.json").read_text())["runState"], "running")
        local = json.loads((self.repo / ".warp" / "beam.json").read_text())
        local["runState"] = "stopped"
        local["config"]["runner"] = "local"
        local.pop("promptForced", None)
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(local) + "\n")
        started = run("scan.py", "start", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

    def test_later_claim_is_blocked_unless_force_started(self):
        self.beam([self.ticket()], runner="cloud", jiraTransition=True, messenger="teams")
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        data["runState"] = "running"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        claim = run(
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "T-1",
            "--status",
            "claimed",
            "--agent",
            "shuttle-T-1",
            cwd=self.repo,
        )
        self.assertNotEqual(claim.returncode, 0)
        self.assertIn("transitionJiraIssue", claim.stdout)
        self.assertEqual(json.loads((self.repo / ".warp" / "beam.json").read_text())["tickets"]["T-1"]["status"], "queued")
        data["promptForced"] = True
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        forced = run(
            "beam.py",
            "set",
            "--beam",
            ".warp/beam.json",
            "--id",
            "T-1",
            "--status",
            "claimed",
            "--agent",
            "shuttle-T-1",
            cwd=self.repo,
        )
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)

    def test_auto_runner_blocks_only_a_cloud_session(self):
        cfg = {"runner": "auto", "jiraTransition": True, "messenger": "teams", "notify": "verbose"}
        missing = prompt_gate.missing_tools(cfg, [], env={})
        self.assertEqual(missing, [])
        cloud = prompt_gate.missing_tools(cfg, [], env={"CURSOR_CLOUD": "1"})
        self.assertIn("transitionJiraIssue", cloud)
        self.assertNotIn("slack_send_message", cloud)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.source = self.tmp / "source"
        self.repo.mkdir()
        self.source.mkdir()
        (self.source / "VERSION").write_text("9.9.9\n")
        plugin = self.source / ".cursor-plugin"
        plugin.mkdir()
        (plugin / "plugin.json").write_text('{"version": "9.9.9"}\n')
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "marker.txt").write_text("upgraded\n")
        warp = self.repo / ".warp"
        warp.mkdir()
        (warp / "config.yaml").write_text("token: keep-me\n")
        (warp / "beam.json").write_text("{}\n")
        installed = self.repo / ".cursor" / "plugins" / "warp"
        installed.mkdir(parents=True)
        (installed / "VERSION").write_text("0.0.1\n")

    def test_upgrade_replaces_the_plugin_and_leaves_state(self):
        proc = run("upgrade.py", "--root", str(self.repo), "--source", str(self.source), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Warp v9.9.9", proc.stdout)
        self.assertIn("Reload Cursor", proc.stdout)
        plugin = self.repo / ".cursor" / "plugins" / "warp"
        self.assertEqual((plugin / "VERSION").read_text().strip(), "9.9.9")
        self.assertEqual((plugin / "scripts" / "marker.txt").read_text(), "upgraded\n")
        self.assertEqual((self.repo / ".warp" / "config.yaml").read_text(), "token: keep-me\n")
        self.assertEqual((self.repo / ".warp" / "beam.json").read_text(), "{}\n")
        again = run("upgrade.py", "--root", str(self.repo), "--source", str(self.source), cwd=self.repo)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("Warp v9.9.9", again.stdout)
        self.assertEqual((self.repo / ".warp" / "config.yaml").read_text(), "token: keep-me\n")

    def test_fetch_failure_keeps_the_installed_copy(self):
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.email", "warp@example.com")
        git(self.source, "config", "user.name", "Warp")
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", "src")
        git(self.source, "remote", "add", "origin", "https://127.0.0.1:1/nope/warp.git")
        installed = self.repo / ".cursor" / "plugins" / "warp" / "VERSION"
        before = installed.read_text()
        env = dict(**{k: v for k, v in __import__("os").environ.items()})
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = run(
            "upgrade.py",
            "--root",
            str(self.repo),
            "--source",
            str(self.source),
            cwd=self.repo,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("fetch failed", proc.stdout)
        self.assertIn("keeping the installed copy", proc.stdout)
        self.assertEqual(installed.read_text(), before)

    def test_help(self):
        proc = run("upgrade.py", "?", cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--root", proc.stdout)
        self.assertIn("--source", proc.stdout)
        self.assertIn("reload Cursor", proc.stdout)


if __name__ == "__main__":
    unittest.main()
