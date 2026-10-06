"""Cloud runners get hook behavior from scripts, not from plugin hooks."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def run(script, *args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def bash(script, cwd, env):
    return subprocess.run(
        ["bash", str(ROOT / "hooks" / script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


class CloudHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.warp = self.repo / ".warp"
        self.warp.mkdir(parents=True)
        self.home = self.tmp / "cursor-home"
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self):
        env = dict(os.environ)
        env["CURSOR_PROJECT_DIR"] = str(self.repo)
        return env

    def write_beam(self, **metrics):
        beam = {
            "paused": False,
            "metrics": {
                "done": 2,
                "total": 9,
                "etaHours": 4,
                "byStatus": {"alarm": 1},
            },
        }
        beam["metrics"].update(metrics)
        (self.warp / "beam.json").write_text(json.dumps(beam) + "\n")

    def test_resume_hint_matches_the_old_hook_text(self):
        self.write_beam()
        direct = run("resume_hint.py", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        hooked = bash("session-start.sh", self.repo, self.env())
        self.assertEqual(hooked.returncode, 0, hooked.stderr)
        self.assertEqual(direct.stdout, hooked.stdout)
        self.assertIn("WARP beam: paused=False done=2/9 eta_h=4 alarms=1", direct.stdout)
        self.assertIn("Read .warp/BOARD.md then run /warp-status before dispatching.", direct.stdout)

    def test_resume_hint_when_the_beam_is_missing(self):
        proc = run("resume_hint.py", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "WARP: no .warp/beam.json — run /warp-ingest before /warp.\n")

    def test_session_stop_is_idempotent_and_skips_a_missing_warp_dir(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        missing = run("session_note.py", "--type", "session-stop", "--root", str(empty), cwd=empty)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertIn("not recorded", missing.stdout)
        self.assertFalse((empty / ".warp").exists())

        first = run("session_note.py", "--type", "session-stop", "--root", str(self.repo), cwd=self.repo)
        second = run("session_note.py", "--type", "session-stop", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("noted session-stop", first.stdout)
        self.assertIn("already noted session-stop", second.stdout)
        lines = (self.warp / "journal.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "session-stop")

        hooked = bash("on-stop.sh", self.repo, self.env())
        self.assertEqual(hooked.returncode, 0, hooked.stderr)
        self.assertIn("already noted session-stop", hooked.stdout)
        self.assertEqual(len((self.warp / "journal.jsonl").read_text().splitlines()), 1)

    def test_subagent_stop_creates_warp_and_matches_the_hook(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        proc = run("session_note.py", "--type", "subagent-stop", "--root", str(bare), cwd=bare)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((bare / ".warp" / "journal.jsonl").is_file())
        row = json.loads((bare / ".warp" / "journal.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["type"], "subagent-stop")
        self.assertIn("ts", row)

    def test_stop_and_pause_commands_record_session_stop(self):
        beam = {
            "paused": False,
            "runState": "running",
            "tickets": {},
            "config": {"reportOnComplete": False},
        }
        path = self.warp / "beam.json"
        path.write_text(json.dumps(beam) + "\n")
        stopped = run("scan.py", "stop", "--beam", str(path), cwd=self.repo)
        self.assertEqual(stopped.returncode, 0, stopped.stderr + stopped.stdout)
        self.assertIn("noted session-stop", stopped.stdout)
        types = [json.loads(line)["type"] for line in (self.warp / "journal.jsonl").read_text().splitlines()]
        self.assertEqual(types[-2:], ["stopped", "session-stop"])

        paused = run("beam.py", "pause", "--beam", str(path), "--reason", "hold", cwd=self.repo)
        self.assertEqual(paused.returncode, 0, paused.stderr + paused.stdout)
        self.assertIn("noted session-stop", paused.stdout)
        types = [json.loads(line)["type"] for line in (self.warp / "journal.jsonl").read_text().splitlines()]
        self.assertEqual(types[-2:], ["pause", "session-stop"])

        resumed = run("beam.py", "resume", "--beam", str(path), cwd=self.repo)
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertNotIn("session-stop", resumed.stdout)
        types = [json.loads(line)["type"] for line in (self.warp / "journal.jsonl").read_text().splitlines()]
        self.assertEqual(types[-1], "resume")

    def test_start_prints_the_resume_hint(self):
        self.write_beam()
        beam = json.loads((self.warp / "beam.json").read_text())
        beam["runState"] = "stopped"
        beam["tickets"] = {}
        (self.warp / "beam.json").write_text(json.dumps(beam) + "\n")
        proc = run("scan.py", "start", "--force", "--beam", str(self.warp / "beam.json"), cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WARP beam: paused=False done=2/9 eta_h=4 alarms=1", proc.stdout)
        self.assertIn("\nrunning\n", proc.stdout)

    def test_mcp_allow_uses_the_written_list_and_refuses_other_tools(self):
        allow = run(
            "allow_notify.py",
            "--root", str(self.repo),
            "--cursor-home", str(self.home),
            cwd=self.repo,
        )
        self.assertEqual(allow.returncode, 0, allow.stderr + allow.stdout)

        def decide(server, tool):
            proc = run(
                "mcp_allow.py",
                "--server", server,
                "--tool", tool,
                "--root", str(self.repo),
                "--cursor-home", str(self.home),
                cwd=self.repo,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        self.assertEqual(decide("slack", "slack_post_message"), "allow")
        self.assertEqual(decide("plugin-slack-slack", "slack_send_message"), "allow")
        self.assertEqual(decide("project-0-repo-slack", "slack_read_channel"), "allow")
        self.assertEqual(decide("claude_ai_Atlassian", "addOrEditJiraIssueComment"), "allow")
        self.assertEqual(decide("slack", "slack_list_channels"), "ask")
        self.assertEqual(decide("github", "add_issue_comment"), "ask")
        self.assertEqual(decide("slack", "conversations_delete"), "ask")
        self.assertEqual(decide("*", "slack_post_message"), "ask")
        self.assertEqual(decide("slack", "*"), "ask")
        self.assertEqual(decide("", "slack_post_message"), "ask")

        hook = self.repo / ".cursor" / "hooks" / "warp-mcp-allow.py"

        def hook_decide(server, tool):
            proc = subprocess.run(
                [sys.executable, "-B", str(hook)],
                input=json.dumps({"mcp_server_name": server, "tool_name": tool}),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["permission"]

        for server, tool in (
            ("slack", "slack_post_message"),
            ("slack", "slack_list_channels"),
            ("github", "add_issue_comment"),
            ("teams", "send_channel_message"),
        ):
            self.assertEqual(decide(server, tool), hook_decide(server, tool), (server, tool))

    def test_mcp_allow_without_a_pairs_file_stays_on_the_warp_list(self):
        def decide(server, tool):
            proc = run(
                "mcp_allow.py",
                "--server", server,
                "--tool", tool,
                "--root", str(self.repo),
                "--cursor-home", str(self.home),
                cwd=self.repo,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        self.assertFalse((self.repo / ".cursor" / "hooks" / "warp-allow.json").exists())
        self.assertEqual(decide("slack", "slack_post_message"), "allow")
        self.assertEqual(decide("atlassian", "transitionJiraIssue"), "allow")
        self.assertEqual(decide("github", "add_issue_comment"), "ask")
        self.assertEqual(decide("slack", "chat_delete"), "ask")
        self.assertEqual(decide("anywhere", "run_everything"), "ask")
        self.assertFalse((self.repo / ".cursor").exists())

    def test_plugin_hooks_only_delegate(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        self.assertEqual(
            [item["command"] for item in hooks["hooks"]["sessionStart"]],
            ["./hooks/session-start.sh"],
        )
        self.assertEqual(
            [item["command"] for item in hooks["hooks"]["stop"]],
            ["./hooks/on-stop.sh"],
        )
        self.assertEqual(
            [item["command"] for item in hooks["hooks"]["subagentStop"]],
            ["./hooks/subagent-stop.sh"],
        )
        self.assertNotIn("beforeMCPExecution", hooks["hooks"])
        for name, script in (
            ("session-start.sh", "resume_hint.py"),
            ("on-stop.sh", "session_note.py"),
            ("subagent-stop.sh", "session_note.py"),
        ):
            text = (ROOT / "hooks" / name).read_text()
            self.assertIn(script, text)
            self.assertIn("Cloud runners do not execute this hook", text)
