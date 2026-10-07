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
        self.assertTrue((path / ".warp" / "beam.json").is_file(), added.stdout)
        claim = json.loads((path / ".warp" / "beam.json").read_text())
        self.assertEqual(claim["tickets"]["T-1"]["id"], "T-1")
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

    def _origin(self):
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        pushed = git(self.repo, "push", "-u", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        return bare

    def test_launch_adds_a_worktree_and_prints_the_subagent_prompt(self):
        row = self.ticket(status="claimed", agent="shuttle-T-1")
        row["jiraKey"] = "WAR-1"
        row["acs"] = ["viewer serves the page"]
        row["token"] = "super-secret-token"
        row["usage"] = {"cost": 9.5, "tokensIn": 10}
        row["summary"] = "see https://hooks.slack.com/services/T00/B00/secret"
        self.beam([row], runner="cloud", baseBranch="main", launch="worktree", subagentVm=False)
        warp = self.repo / ".warp"
        (warp / "config.yaml").write_text('token: "config-token-value"\n')
        (warp / "journal.jsonl").write_text(json.dumps({"type": "claim", "id": "T-1", "token": "journal-token"}) + "\n")
        (warp / "BOARD.md").write_text("# board\nT-1 claimed\n")
        refused = run("checkout.py", "implement", "--id", "T-1", cwd=self.repo)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("refuse: in-process", refused.stdout)
        self.assertIn("subagent", refused.stdout)
        self.assertIn("new Agent", refused.stdout)
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        text = launched.stdout
        worktree = (self.repo / ".warp" / "worktrees" / "T-1").resolve()
        self.assertTrue(worktree.is_dir(), text)
        self.assertIn("SUBAGENT T-1", text)
        self.assertIn("ticket: T-1", text)
        self.assertIn("jira: WAR-1", text)
        self.assertIn("locks: src/T-1", text)
        self.assertIn("acceptance: viewer serves the page", text)
        self.assertIn("branch: warp/T-1-WAR-1", text)
        self.assertIn("worktree: %s" % worktree, text)
        self.assertIn(orchestrator.SUBAGENT_INSTRUCTION, text)
        self.assertIn("start one subagent per ticket", text.casefold())
        self.assertIn("maxAgents", text)
        self.assertIn("Never merge", text)
        self.assertIn("Never call Jira or Slack", text)
        self.assertIn(str(self.repo.resolve()), text)
        self.assertIn("state.json", text)
        self.assertIn("result: T-1 ok", text)
        self.assertNotIn("Do not start a Subagent", text)
        self.assertNotIn("Do not use the Task tool", text)
        self.assertNotIn("super-secret-token", text)
        self.assertNotIn("hooks.slack.com", text)
        self.assertNotIn("9.5", text)
        self.assertNotIn("journal-token", text)
        self.assertNotIn("config-token-value", text)
        head = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(head.stdout.strip(), "main")
        branch = git(self.repo, "rev-parse", "--verify", "-q", "refs/heads/warp/T-1-WAR-1")
        self.assertEqual(branch.returncode, 0, text)
        again = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("reused: worktree", again.stdout)
        self.assertIn("reused: branch", again.stdout)
        clean = run("checkout.py", "verify", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertIn("verify: ok T-1", clean.stdout)
        (self.repo / "leaked.txt").write_text("parent edit\n")
        escaped = run("checkout.py", "verify", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(escaped.returncode, 2, escaped.stdout + escaped.stderr)
        self.assertIn("lock-escape: T-1", escaped.stdout)
        self.assertIn("escaped: leaked.txt", escaped.stdout)
        saved = json.loads((warp / "beam.json").read_text())
        self.assertEqual(saved["tickets"]["T-1"]["alarm"], "lock-escape")
        (self.repo / "leaked.txt").unlink()
        saved["tickets"]["T-1"]["alarm"] = None
        saved["tickets"]["T-1"]["status"] = "coding"
        saved["tickets"]["T-1"].pop("escaped", None)
        (warp / "beam.json").write_text(json.dumps(saved) + "\n")
        outside = worktree / "src" / "other.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("nope\n")
        outside_lock = run("checkout.py", "verify", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(outside_lock.returncode, 2, outside_lock.stdout + outside_lock.stderr)
        self.assertIn("src/other.txt", outside_lock.stdout)
        outside.unlink()
        inside = worktree / "src" / "T-1" / "ok.txt"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("yes\n")
        held = run("checkout.py", "verify", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
        failed = run(
            "checkout.py",
            "result",
            "--root",
            str(self.repo),
            "--id",
            "T-1",
            "--line",
            "result: T-1 failed boom",
            cwd=self.repo,
        )
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("worker-died", failed.stdout)
        died = json.loads((warp / "beam.json").read_text())["tickets"]["T-1"]
        self.assertEqual(died["alarm"], "worker-died")
        merged = run(
            "orchestrator.py",
            "merge",
            "--beam",
            str(warp / "beam.json"),
            "--id",
            "T-1",
            "--outcome",
            "merged",
            cwd=self.repo,
        )
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertFalse(worktree.exists(), merged.stdout)
        self.assertIn("pruned", merged.stdout)

    def _advance_origin_main(self, bare):
        other = self.tmp / "other"
        cloned = git(self.tmp, "clone", "--branch", "main", str(bare), str(other))
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        git(other, "config", "user.email", "warp@example.com")
        git(other, "config", "user.name", "Warp")
        (other / "merged.txt").write_text("merged on main\n")
        git(other, "add", "merged.txt")
        committed = git(other, "commit", "-m", "merge onto main")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        pushed = git(other, "push", "origin", "main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        stale = git(self.repo, "cat-file", "-e", "main:merged.txt")
        self.assertNotEqual(stale.returncode, 0)

    def test_beam_only_branch_tells_the_agent_to_start_from_main(self):
        row = self.ticket(status="claimed", agent="shuttle-T-1")
        row["jiraKey"] = "WAR-1"
        row["acs"] = ["viewer serves the page"]
        self.beam([row], runner="cloud", baseBranch="main", launch="agent")
        self._origin()
        published = state_commit.publish_ticket(self.repo, str(self.repo / ".warp" / "beam.json"), "T-1", push=True)
        self.assertEqual(published, 0)
        self.assertEqual(git(self.repo, "cat-file", "-e", "warp/T-1-WAR-1:.warp/beam.json").returncode, 0)
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn("IMPLEMENT T-1", launched.stdout)
        self.assertIn("stale-beam: warp/T-1-WAR-1", launched.stdout)
        self.assertIn("Do not keep that stale beam as your working tree.", launched.stdout)
        self.assertIn("Start from latest main.", launched.stdout)
        self.assertNotIn("has no .warp beam", launched.stdout)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_in_flight_work_is_not_reset(self):
        row = self.ticket(status="claimed", agent="shuttle-T-1", branch="warp/T-1")
        self.beam([row], runner="cloud", baseBranch="main", launch="agent")
        git(self.repo, "checkout", "-b", "warp/T-1")
        (self.repo / "ticket-only.txt").write_text("keep\n")
        git(self.repo, "add", "ticket-only.txt")
        committed = git(self.repo, "commit", "-m", "ticket work")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        ticket_tip = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "checkout", "main")
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn("branch-kept: warp/T-1", launched.stdout)
        self.assertNotIn("stale-beam:", launched.stdout)
        self.assertIn("IMPLEMENT T-1", launched.stdout)
        self.assertEqual(git(self.repo, "rev-parse", "warp/T-1").stdout.strip(), ticket_tip)
        self.assertEqual(git(self.repo, "cat-file", "-e", "warp/T-1:ticket-only.txt").returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_dispatch_text_requires_a_new_agent_and_forbids_subagent_and_task(self):
        data = {
            "version": 1,
            "runState": "running",
            "baseGreen": True,
            "config": {
                "runner": "cloud",
                "maxAgents": 2,
                "baseBranch": "main",
                "launch": "worktree",
                "subagentVm": True,
            },
            "tickets": {
                "T-1": {
                    "id": "T-1",
                    "status": "queued",
                    "deps": [],
                    "locks": ["src/t"],
                    "rank": 1,
                    "size": "S",
                    "module": "core",
                }
            },
            "gates": [],
        }
        lines = orchestrator.format_dispatch(data)
        text = "\n".join(lines)
        self.assertIn("SUBAGENT T-1", text)
        self.assertIn("ticket: T-1", text)
        self.assertIn("locks: src/t", text)
        self.assertIn("start one subagent per ticket", text.casefold())
        self.assertIn("maxAgents", text)
        self.assertNotIn("Do not start a Subagent", text)
        self.assertNotIn("Do not use the Task tool", text)
        self.assertNotIn("subagent_type", text)
        self.assertTrue(any(line.startswith("start T-1 ") for line in lines))
        self.assertIn("launch=subagent", text)
        self.assertIn("checkout=subagent-vm", text)
        self.assertIn(orchestrator.VM_LINE, text)
        self.assertIn("hostname", text)
        self.assertIn("free -g", text)
        local = dict(data)
        local["config"] = dict(data["config"], subagentVm=False)
        local_text = "\n".join(orchestrator.format_dispatch(local))
        self.assertIn("checkout=worktree", local_text)
        plain = dict(data)
        plain["config"] = {"runner": "cloud", "maxAgents": 2, "baseBranch": "main", "launch": "worktree"}
        plain_text = "\n".join(orchestrator.format_dispatch(plain))
        self.assertIn("checkout=worktree", plain_text)
        self.assertNotIn(orchestrator.VM_LINE, plain_text)
        self.assertIn("Run checkout.py launch", local_text)
        agent = dict(data)
        agent["config"] = dict(data["config"], launch="agent")
        agent_text = "\n".join(orchestrator.format_dispatch(agent))
        self.assertIn("IMPLEMENT T-1", agent_text)
        self.assertIn("new Agent", agent_text)
        self.assertIn("launch=new-agent", agent_text)


class VmLaunchTests(GitRepo):
    def test_default_launch_asks_for_a_dedicated_vm(self):
        row = self.ticket(status="claimed", agent="shuttle-T-1")
        row["jiraKey"] = "WAR-1"
        row["acs"] = ["viewer serves the page"]
        self.beam([row], runner="cloud", baseBranch="main", subagentVm=True)
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertIn(orchestrator.VM_LINE, launched.stdout)
        self.assertIn("free -g", launched.stdout)
        self.assertIn("hostname", launched.stdout)
        self.assertIn("warp/T-1-WAR-1", launched.stdout)
        self.assertIn("cursor.com/agents", launched.stdout)
        self.assertNotIn("created: worktree", launched.stdout)
        self.assertFalse((self.repo / ".warp" / "worktrees" / "T-1").exists())
        host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
        other = run(
            "checkout.py",
            "result",
            "--root",
            str(self.repo),
            "--id",
            "T-1",
            "--line",
            "result: T-1 ok hostname=other-vm branch=warp/T-1-WAR-1 cwd=/tmp/clone memory=8",
            cwd=self.repo,
        )
        self.assertEqual(other.returncode, 0, other.stdout + other.stderr)
        self.assertIn("verify: vm T-1", other.stdout)
        saved = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertEqual(saved["tickets"]["T-1"]["isolation"], "vm")
        self.assertNotIn("subagentVmWarned", saved)
        saved["tickets"]["T-1"]["status"] = "coding"
        saved["tickets"]["T-1"]["checkout"] = "subagent-vm"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(saved) + "\n")
        same = run(
            "checkout.py",
            "result",
            "--root",
            str(self.repo),
            "--id",
            "T-1",
            "--line",
            "result: T-1 ok hostname=%s cwd=/tmp/does-not-exist" % host,
            cwd=self.repo,
        )
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        self.assertIn("herald: subagent fell back to a worktree on this machine", same.stdout)
        log = (self.repo / ".warp" / "tickets" / "T-1" / "log.jsonl").read_text()
        self.assertIn("same hostname as parent", log)
        warned = json.loads((self.repo / ".warp" / "beam.json").read_text())
        self.assertTrue(warned.get("subagentVmWarned"))
        self.assertTrue(warned.get("subagentVmFallback"))
        warned["tickets"]["T-1"]["status"] = "coding"
        warned["tickets"]["T-1"]["checkout"] = "subagent-vm"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(warned) + "\n")
        again = run(
            "checkout.py",
            "result",
            "--root",
            str(self.repo),
            "--id",
            "T-1",
            "--line",
            "result: T-1 ok hostname=%s" % host,
            cwd=self.repo,
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("herald: subagent fell back", again.stdout)
        self.assertIn("fallback: T-1", again.stdout)

    def test_local_cap_and_memory(self):
        self.assertEqual(orchestrator.local_subagent_cap({}, available=None), 18)
        self.assertEqual(orchestrator.local_subagent_cap({"maxLocalSubagents": 4}, available=16), 4)
        self.assertEqual(orchestrator.local_subagent_cap({"maxLocalSubagents": 4}, available=3), 4)
        self.assertEqual(orchestrator.local_subagent_cap({}, available=0), 18)
        self.assertFalse(orchestrator.memory_check({}))
        self.assertEqual(
            orchestrator.local_subagent_cap({"maxLocalSubagents": 4, "memoryCheck": True}, available=16),
            4,
        )
        self.assertEqual(
            orchestrator.local_subagent_cap({"maxLocalSubagents": 4, "memoryCheck": True}, available=3),
            1,
        )
        self.assertEqual(orchestrator.local_subagent_cap({"memoryCheck": True}, available=0), 1)
        first = self.ticket("T-1", status="coding", agent="shuttle-T-1")
        second = self.ticket("T-2", status="claimed", agent="shuttle-T-2")
        self.beam([first, second], subagentVm=False, maxLocalSubagents=1, runner="local")
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-2", cwd=self.repo)
        self.assertEqual(launched.returncode, 2, launched.stdout + launched.stderr)
        self.assertIn("refuse: local subagent cap", launched.stdout)
        self.assertIn("local-cap:", launched.stdout)

    def test_local_runner_overrides_cloud_only_settings(self):
        note = "note: runner local overrides cloud-only settings: subagentVm, launch"
        effective = "effective: runner=local subagentVm=false memoryCheck=false maxLocalSubagents=18 launch=worktree"
        cfg = {"runner": "local", "subagentVm": True, "launch": "agent"}
        self.assertEqual(orchestrator.override_note(cfg), note)
        self.assertEqual(orchestrator.effective_text(cfg), effective)
        self.assertFalse(orchestrator.subagent_vm(cfg))
        self.assertEqual(orchestrator.launch_mode(cfg), "worktree")
        auto = {"runner": "auto", "subagentVm": True, "launch": "agent"}
        self.assertEqual(orchestrator.override_note(auto, {}), note)
        self.assertEqual(orchestrator.runner_name({"runner": "auto"}, {"CURSOR_CLOUD": "1"}), "cloud")
        self.assertTrue(orchestrator.subagent_vm({"runner": "cloud", "subagentVm": True}))
        self.assertEqual(orchestrator.override_note({"runner": "cloud", "subagentVm": True}), "")
        row = self.ticket(status="claimed", agent="shuttle-T-1")
        self.beam(
            [row],
            runner="local",
            subagentVm=True,
            launch="agent",
            jiraTransition=False,
            messenger="teams",
            notify="off",
        )
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        data["runState"] = "stopped"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        started = run("scan.py", "start", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(started.stdout.count(note), 1)
        self.assertIn(effective, started.stdout)
        self.assertNotIn("no cloud environment", started.stdout)
        shown = run("version.py", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn(note, shown.stdout)
        self.assertIn(effective, shown.stdout)
        status = run("scan.py", "status", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn(effective, status.stdout)
        board = (self.repo / ".warp" / "STATUS.md").read_text()
        self.assertIn(note, board)
        self.assertIn(effective, board)
        launched = run("checkout.py", "launch", "--root", str(self.repo), "--id", "T-1", cwd=self.repo)
        self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
        self.assertNotIn(orchestrator.VM_LINE, launched.stdout)
        self.assertIn("created: worktree", launched.stdout)
        self.assertTrue((self.repo / ".warp" / "worktrees" / "T-1").is_dir())


class ParentExitTests(unittest.TestCase):
    def test_parent_does_not_exit_while_a_ticket_is_claimed(self):
        beam = {"runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "claimed"}}}
        self.assertFalse(orchestrator.parent_may_exit(beam))
        queued = {"runState": "running", "tickets": {"T-2": {"id": "T-2", "status": "queued"}}}
        self.assertFalse(orchestrator.parent_may_exit(queued))
        review = {
            "runState": "running",
            "tickets": {"T-3": {"id": "T-3", "status": "review", "pr": {"rollup": "pending"}}},
        }
        self.assertFalse(orchestrator.parent_may_exit(review))

    def test_parent_may_exit_when_paused(self):
        beam = {
            "runState": "paused",
            "paused": True,
            "tickets": {"T-1": {"id": "T-1", "status": "claimed"}},
        }
        self.assertTrue(orchestrator.parent_may_exit(beam))
        stopped = {"runState": "stopped", "tickets": {"T-1": {"id": "T-1", "status": "queued"}}}
        self.assertTrue(orchestrator.parent_may_exit(stopped))
        idle = {"runState": "running", "tickets": {"T-1": {"id": "T-1", "status": "merged"}}}
        self.assertFalse(orchestrator.parent_may_exit(idle))


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

    def test_cloud_vm_start_needs_an_environment_unless_forced(self):
        self.beam(
            [self.ticket()],
            runner="cloud",
            jiraTransition=False,
            messenger="teams",
            notify="off",
            subagentVm=True,
        )
        data = json.loads((self.repo / ".warp" / "beam.json").read_text())
        data["runState"] = "stopped"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(data) + "\n")
        refused = run("scan.py", "start", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no cloud environment", refused.stdout)
        self.assertIn("cursor.com/agents", refused.stdout)
        self.assertNotEqual(json.loads((self.repo / ".warp" / "beam.json").read_text()).get("runState"), "running")
        forced = run("scan.py", "start", "--force", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertIn("warn: no cloud environment", forced.stdout)
        self.assertEqual(json.loads((self.repo / ".warp" / "beam.json").read_text())["runState"], "running")
        stopped = json.loads((self.repo / ".warp" / "beam.json").read_text())
        stopped["runState"] = "stopped"
        stopped.pop("promptForced", None)
        stopped["config"]["cloudSnapshot"] = "snap-1"
        (self.repo / ".warp" / "beam.json").write_text(json.dumps(stopped) + "\n")
        started = run("scan.py", "start", "--beam", ".warp/beam.json", cwd=self.repo)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIn("prompt-gate: ok", started.stdout)


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

    def test_upgrade_writes_the_project_slash_command(self):
        command = self.source / "commands"
        command.mkdir()
        body = "---\nname: warp-upgrade\ndescription: Replace the installed Warp plugin.\n---\n\nRun it.\n"
        (command / "warp-upgrade.md").write_text(body)
        proc = run("upgrade.py", "--root", str(self.repo), "--source", str(self.source), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("/warp-upgrade -> .cursor/commands/warp-upgrade.md", proc.stdout)
        written = self.repo / ".cursor" / "commands" / "warp-upgrade.md"
        self.assertEqual(written.read_text(), body)
        plugin_copy = self.repo / ".cursor" / "plugins" / "warp" / "commands" / "warp-upgrade.md"
        self.assertEqual(plugin_copy.read_text(), body)
        again = run("upgrade.py", "--root", str(self.repo), "--source", str(self.source), cwd=self.repo)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(written.read_text(), body)

    def test_help(self):
        proc = run("upgrade.py", "?", cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--root", proc.stdout)
        self.assertIn("--source", proc.stdout)
        self.assertIn("reload Cursor", proc.stdout)


if __name__ == "__main__":
    unittest.main()
