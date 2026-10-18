"""Run with: python3 -m unittest discover -s tests"""

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
import mcp_tools  # noqa: E402

ALLOW = SCRIPTS / "allow_notify.py"
INSTALL = SCRIPTS / "install.py"


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(script), *args], cwd=cwd, capture_output=True, text=True
    )


def load(path):
    return json.loads(path.read_text())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def allow(self, *args):
        return run(ALLOW, "--root", str(self.repo), *args, cwd=self.repo)

    def write_config(self, text):
        (self.repo / ".warp").mkdir(exist_ok=True)
        (self.repo / ".warp" / "config.yaml").write_text(text)


class AllowNotifyTests(Base):
    def test_default_writes_messengers_only_and_is_idempotent(self):
        r = self.allow()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("Reload Cursor", r.stdout)
        self.assertIn("does not change the IDE Run button", r.stdout)
        self.assertIn("does not skip the MCP Run prompt", r.stdout)
        self.assertIn("Cloud agents", r.stdout)
        perm = load(self.repo / ".cursor" / "permissions.json")
        cli = load(self.repo / ".cursor" / "cli.json")
        hooks = load(self.repo / ".cursor" / "hooks.json")
        allow = perm["mcpAllowlist"]
        for entry in (
            "slack:slack_send_message",
            "slack:slack_read_channel",
            "user-slack:slack_send_message",
            "plugin-slack-slack:slack_send_message",
            "*slack*:slack_send_message",
            "atlassian:addOrEditJiraIssueComment",
            "*atlassian*:addOrEditJiraIssueComment",
            "atlassian-rovo:editJiraIssue",
            "claude_ai_Atlassian:getJiraIssue",
            "teams:teams_read_thread",
        ):
            self.assertIn(entry, allow)
        self.assertNotIn("github:add_issue_comment", allow)
        for entry in allow:
            _server, tool = entry.split(":", 1)
            self.assertNotEqual(tool, "*")
            self.assertFalse(entry.endswith(":*"))
        self.assertNotIn("mcpAllowlist", {k for k in perm if k != "mcpAllowlist"})
        self.assertEqual(cli["permissions"]["allow"], [f"Mcp({e})" for e in allow])
        self.assertNotIn("*:*", json.dumps(cli))
        cmd = hooks["hooks"]["beforeMCPExecution"][0]
        self.assertEqual(cmd, {"command": ".cursor/hooks/warp-mcp-allow.py"})
        self.assertNotIn("failClosed", json.dumps(hooks))
        self.assertTrue(os.access(self.repo / ".cursor" / "hooks" / "warp-mcp-allow.py", os.X_OK))
        manifest = load(self.repo / ".cursor" / "warp-allow.json")
        self.assertEqual(manifest["addedBy"], "warp-allow-notify")
        self.assertEqual(manifest["mcpAllowlistAdded"], allow)
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        r2 = self.allow()
        self.assertIn("Nothing changed", r2.stdout)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        plugin_hooks = (ROOT / "hooks" / "hooks.json").read_text()
        self.assertNotIn("beforeMCPExecution", plugin_hooks)

    def test_dry_run_writes_nothing_and_shows_a_diff(self):
        r = self.allow("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Dry run", r.stdout)
        self.assertIn("permissions.json", r.stdout)
        self.assertIn("---", r.stdout)
        self.assertFalse((self.repo / ".cursor").exists())

    def test_merges_and_revoke_removes_only_what_was_added(self):
        cursor = self.repo / ".cursor"
        cursor.mkdir()
        (cursor / "permissions.json").write_text(json.dumps({
            "terminalAllowlist": ["git"],
            "autoRun": {"allow_instructions": ["keep me"]},
            "mcpAllowlist": ["slack:slack_post_message", "other:already"],
        }, indent=2) + "\n")
        (cursor / "cli.json").write_text(json.dumps({
            "permissions": {"allow": ["Shell(ls)"], "deny": ["Shell(rm)"]},
            "editor": {"vimMode": True},
        }, indent=2) + "\n")
        (cursor / "hooks.json").write_text(json.dumps({
            "version": 1,
            "hooks": {"sessionStart": [{"command": "./hooks/session-start.sh"}]},
        }, indent=2) + "\n")
        original_perm = (cursor / "permissions.json").read_text()
        r = self.allow()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((cursor / "permissions.json.bak").read_text(), original_perm)
        perm = load(cursor / "permissions.json")
        self.assertEqual(perm["terminalAllowlist"], ["git"])
        self.assertEqual(perm["autoRun"]["allow_instructions"], ["keep me"])
        self.assertIn("other:already", perm["mcpAllowlist"])
        self.assertIn("slack:slack_post_message", perm["mcpAllowlist"])
        self.assertIn("teams:teams_send_message", perm["mcpAllowlist"])
        cli = load(cursor / "cli.json")
        self.assertIn("Shell(ls)", cli["permissions"]["allow"])
        self.assertEqual(cli["permissions"]["deny"], ["Shell(rm)"])
        self.assertTrue(cli["editor"]["vimMode"])
        self.assertIn("Mcp(teams:send_channel_message)", cli["permissions"]["allow"])
        hooks = load(cursor / "hooks.json")
        self.assertEqual(hooks["hooks"]["sessionStart"], [{"command": "./hooks/session-start.sh"}])
        self.assertEqual(hooks["hooks"]["beforeMCPExecution"], [{"command": ".cursor/hooks/warp-mcp-allow.py"}])
        manifest = load(cursor / "warp-allow.json")
        self.assertNotIn("slack:slack_post_message", manifest["mcpAllowlistAdded"])
        self.assertNotIn("other:already", manifest["mcpAllowlistAdded"])
        bak_after_first = (cursor / "permissions.json.bak").read_text()
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        r2 = self.allow()
        self.assertIn("Nothing changed", r2.stdout)
        self.assertEqual(before, {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})
        self.assertEqual((cursor / "permissions.json.bak").read_text(), bak_after_first)

        r3 = self.allow("--revoke")
        self.assertEqual(r3.returncode, 0, r3.stderr + r3.stdout)
        perm = load(cursor / "permissions.json")
        self.assertEqual(perm["mcpAllowlist"], ["slack:slack_post_message", "other:already"])
        self.assertEqual(perm["terminalAllowlist"], ["git"])
        cli = load(cursor / "cli.json")
        self.assertEqual(cli["permissions"]["allow"], ["Shell(ls)"])
        self.assertEqual(cli["permissions"]["deny"], ["Shell(rm)"])
        hooks = load(cursor / "hooks.json")
        self.assertNotIn("beforeMCPExecution", hooks["hooks"])
        self.assertIn("sessionStart", hooks["hooks"])
        self.assertFalse((cursor / "warp-allow.json").exists())
        r4 = self.allow("--revoke")
        self.assertIn("Nothing removed", r4.stdout)

    def test_revoke_deletes_created_files_and_drops_empty_allowlist_key(self):
        self.allow()
        self.allow("--revoke")
        self.assertFalse((self.repo / ".cursor" / "permissions.json").exists())
        self.assertFalse((self.repo / ".cursor" / "cli.json").exists())
        self.assertFalse((self.repo / ".cursor" / "hooks.json").exists())
        self.assertFalse((self.repo / ".cursor" / "hooks" / "warp-mcp-allow.py").exists())
        self.assertFalse((self.repo / ".cursor" / "permissions.json.bak").exists())

    def test_notify_allow_and_optional_tools(self):
        self.write_config('slackMcp: my-slack\nteamsMcp: my-teams\nnotifyAllow:\n  - "my-slack:extra_tool"\n')
        r = self.allow("--with-jira", "--with-git")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        allow = load(self.repo / ".cursor" / "permissions.json")["mcpAllowlist"]
        self.assertIn("my-slack:extra_tool", allow)
        self.assertIn("my-slack:slack_post_message", allow)
        self.assertIn("my-teams:send_channel_message", allow)
        for tool in mcp_tools.JIRA_TOOLS:
            self.assertIn(f"atlassian:{tool}", allow)
        self.assertIn(f"github:{mcp_tools.GITHUB_COMMENT}", allow)
        self.assertFalse(any(entry.startswith("bitbucket:") for entry in allow))
        self.assertIn("Bitbucket", r.stdout)
        for entry in allow:
            _server, tool = entry.split(":", 1)
            self.assertNotEqual(tool, "*")

    def test_wildcard_and_bare_name_are_rejected(self):
        self.write_config("notifyAllow:\n  - slack:*\n")
        r = self.allow()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.repo / ".cursor").exists())
        self.write_config('notifyAllow: ["post_message"]\n')
        r = self.allow()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("server:tool", r.stderr)
        self.assertFalse((self.repo / ".cursor").exists())

    def test_user_scope_requires_yes_and_uses_home_files(self):
        home = self.tmp / "cursor-home"
        r = self.allow("--user", "--cursor-home", str(home))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("explicit yes", r.stdout)
        self.assertFalse(home.exists())
        self.assertFalse((self.repo / ".cursor").exists())
        r = self.allow("--user", "--dry-run", "--cursor-home", str(home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(home.exists())
        r = self.allow("--user", "--yes", "--cursor-home", str(home))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((home / "permissions.json").is_file())
        self.assertTrue((home / "cli-config.json").is_file())
        self.assertFalse((home / "cli.json").exists())
        self.assertFalse((self.repo / ".cursor").exists())
        hooks = load(home / "hooks.json")
        self.assertEqual(hooks["hooks"]["beforeMCPExecution"][0]["command"], "./hooks/warp-mcp-allow.py")
        r = self.allow("--user", "--revoke", "--cursor-home", str(home))
        self.assertEqual(r.returncode, 2)
        self.assertTrue((home / "warp-allow.json").is_file())
        r = self.allow("--user", "--revoke", "--yes", "--cursor-home", str(home))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse((home / "warp-allow.json").exists())
        self.assertFalse((home / "permissions.json").exists())

    def test_hook_allows_only_listed_pairs(self):
        self.allow()
        script = self.repo / ".cursor" / "hooks" / "warp-mcp-allow.py"

        def decide(payload):
            proc = subprocess.run(
                [sys.executable, "-B", str(script)],
                input=payload if isinstance(payload, str) else json.dumps(payload),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["permission"]

        self.assertEqual(decide({"mcp_server_name": "slack", "tool_name": "slack_post_message", "tool_input": "{}"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "Slack", "tool_name": "SLACK_SEND_MESSAGE"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "plugin-slack-slack", "tool_name": "slack_send_message"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "project-0-repo-slack", "tool_name": "slack_read_channel"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "claude_ai_Atlassian", "tool_name": "addOrEditJiraIssueComment"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "teams", "tool_name": "send_channel_message"}), "allow")
        self.assertEqual(decide({"mcp_server_name": "slack", "tool_name": "slack_list_channels"}), "ask")
        self.assertEqual(decide({"mcp_server_name": "github", "tool_name": "add_issue_comment"}), "ask")
        self.assertEqual(decide({"mcp_server_name": "", "tool_name": "slack_post_message"}), "ask")
        self.assertEqual(decide("not json"), "ask")

    def test_uninstall_removes_project_entries_only(self):
        home = self.tmp / "cursor-home"
        self.allow("--user", "--yes", "--cursor-home", str(home))
        run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.allow()
        preview = run(INSTALL, "uninstall", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("allow-notify", preview.stdout)
        self.assertIn("Nothing deleted", preview.stdout)
        self.assertTrue((self.repo / ".cursor" / "warp-allow.json").is_file())
        self.assertTrue((home / "permissions.json").is_file())
        done = run(INSTALL, "uninstall", "--root", str(self.repo), "--yes", cwd=self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((self.repo / ".cursor" / "warp-allow.json").exists())
        self.assertFalse((self.repo / ".cursor" / "hooks" / "warp-mcp-allow.py").exists())
        self.assertFalse((self.repo / ".warp").exists())
        self.assertTrue((home / "permissions.json").is_file())
        self.assertTrue((home / "warp-allow.json").is_file())

    def test_uninstall_removes_allow_entries_when_nothing_else_is_installed(self):
        self.allow()
        preview = run(INSTALL, "uninstall", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("warp-allow.json", preview.stdout)
        self.assertNotIn("Nothing to remove", preview.stdout)
        self.assertTrue((self.repo / ".cursor" / "permissions.json").is_file())
        done = run(INSTALL, "uninstall", "--root", str(self.repo), "--yes", cwd=self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((self.repo / ".cursor").exists())

    def _fixture_servers(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp.json").write_text(
            '{"mcpServers": {"slack": {"url": "https://mcp.slack.com/mcp"}}}\n'
        )
        plugin = self.repo / ".cursor" / "plugins" / "slack"
        plugin.mkdir(parents=True)
        (plugin / "mcp.json").write_text('{"mcpServers": {"slack": {"url": "https://mcp.slack.com/mcp"}}}\n')
        (plugin / ".cursor-plugin").mkdir()
        (plugin / ".cursor-plugin" / "plugin.json").write_text('{"name": "slack"}\n')
        cache = home / "plugins" / "cache" / "cursor-public" / "slack" / "abc123def"
        (cache / ".cursor-plugin").mkdir(parents=True)
        (cache / "mcp.json").write_text('{"slack": {"type": "http", "url": "https://mcp.slack.com/mcp"}}\n')
        (cache / ".cursor-plugin" / "plugin.json").write_text('{"name": "slack"}\n')
        atlassian = self.repo / ".cursor" / "mcp.json"
        atlassian.parent.mkdir(parents=True, exist_ok=True)
        atlassian.write_text('{"mcpServers": {"atlassian": {"url": "https://mcp.atlassian.com/v1/mcp"}}}\n')

    def test_discovery_variants_server_override_and_check(self):
        home = self.tmp / "cursor-home"
        self._fixture_servers(home)
        listed = self.allow("--list", "--cursor-home", str(home))
        self.assertEqual(listed.returncode, 0, listed.stderr + listed.stdout)
        self.assertIn("Detected MCP servers:", listed.stdout)
        self.assertIn("user-slack", listed.stdout)
        self.assertIn("plugin-slack-slack", listed.stdout)
        self.assertFalse((self.repo / ".cursor" / "permissions.json").exists())
        checked = self.allow("--check", "--cursor-home", str(home), "--server", "claude_ai_Atlassian")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Run Mode", checked.stdout)
        self.assertIn("Ask Every Time", checked.stdout)
        self.assertIn("Cloud agents", checked.stdout)
        self.assertIn("plugin-slack-slack:slack_send_message", checked.stdout)
        self.assertIn("Entries that would fix the IDE prompt", checked.stdout)
        self.assertFalse((self.repo / ".cursor" / "permissions.json").exists())
        wrote = self.allow("--cursor-home", str(home), "--server", "claude_ai_Atlassian")
        self.assertEqual(wrote.returncode, 0, wrote.stderr + wrote.stdout)
        allow = load(self.repo / ".cursor" / "permissions.json")["mcpAllowlist"]
        self.assertIn("user-slack:slack_send_message", allow)
        self.assertIn("plugin-slack-slack:slack_send_message", allow)
        self.assertNotIn("plugin-slack-slack:addOrEditJiraIssueComment", allow)
        self.assertIn("project-atlassian:editJiraIssue", allow)
        self.assertIn("claude_ai_Atlassian:addOrEditJiraIssueComment", allow)
        self.assertNotIn("slack:*", allow)
        again = self.allow("--check", "--cursor-home", str(home))
        self.assertIn("IDE: covered", again.stdout)

    def test_server_wildcard_is_gated(self):
        home = self.tmp / "empty-home"
        plain = self.allow("--cursor-home", str(home))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        allow = load(self.repo / ".cursor" / "permissions.json")["mcpAllowlist"]
        self.assertNotIn("slack:*", allow)
        self.assertNotIn("*slack*:*", allow)
        self.allow("--revoke")
        flagged = self.allow("--allow-server-tools", "--cursor-home", str(home))
        self.assertEqual(flagged.returncode, 0, flagged.stderr + flagged.stdout)
        self.assertIn("destructive", flagged.stdout)
        allow = load(self.repo / ".cursor" / "permissions.json")["mcpAllowlist"]
        self.assertIn("slack:*", allow)
        self.assertIn("*slack*:*", allow)
        self.assertIn("atlassian:*", allow)
        self.assertNotIn("*:*", allow)

    def test_init_writes_allowlist_idempotently_and_no_allow_skips(self):
        home = self.tmp / "cursor-home"
        self._fixture_servers(home)
        # init discovers the real home plus the repo. The fixture servers live in the repo.
        first = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertIn("allowed Warp MCP tools", first.stdout)
        self.assertIn("--revoke", first.stdout)
        allow = load(self.repo / ".cursor" / "permissions.json")["mcpAllowlist"]
        self.assertIn("atlassian:addOrEditJiraIssueComment", allow)
        self.assertIn("*slack*:slack_send_message", allow)
        self.assertIn("autoAllowTools: true", (self.repo / ".warp" / "config.yaml").read_text())
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        second = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("Nothing changed", second.stdout)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.allow("--revoke")
        skipped = run(INSTALL, "init", "--root", str(self.repo), "--no-allow", cwd=self.repo)
        self.assertEqual(skipped.returncode, 0, skipped.stderr + skipped.stdout)
        self.assertIn("--no-allow", skipped.stdout)
        self.assertFalse((self.repo / ".cursor" / "permissions.json").exists())
        (self.repo / ".warp" / "config.yaml").write_text(
            (self.repo / ".warp" / "config.yaml").read_text().replace("autoAllowTools: true", "autoAllowTools: false")
        )
        off = run(INSTALL, "init", "--root", str(self.repo), cwd=self.repo)
        self.assertIn("autoAllowTools is false", off.stdout)
        self.assertFalse((self.repo / ".cursor" / "permissions.json").exists())


class ToolCoverageTests(unittest.TestCase):
    def test_referenced_mcp_tools_are_allowed_or_excluded(self):
        import re

        pattern = re.compile(
            r"\b("
            r"slack_[a-z0-9_]+|"
            r"teams_[a-z0-9_]+|"
            r"send_channel_message|"
            r"add_issue_comment|"
            r"(?:get|add|edit|create|list|search|transition|lookup|update)[A-Za-z0-9]*Jira[A-Za-z0-9]*|"
            r"getAccessibleAtlassianResources|"
            r"lookupJiraAccountId"
            r")\b"
        )
        roots = [ROOT / "agents", ROOT / "skills", ROOT / "commands", ROOT / "docs", ROOT / "README.md"]
        found = set()
        for root in roots:
            files = [root] if root.is_file() else list(root.rglob("*"))
            for path in files:
                if path.suffix not in {".md", ".txt"} and path.name != "README.md":
                    continue
                if not path.is_file():
                    continue
                found.update(pattern.findall(path.read_text()))
        allowed = mcp_tools.warp_tool_names(with_jira=True, with_git=True)
        missing = sorted(name for name in found if name not in allowed and name not in mcp_tools.EXCLUDED_TOOLS)
        self.assertEqual(missing, [], "MCP tool names must be in the allowlist or EXCLUDED_TOOLS")
        self.assertIn("lookupJiraAccountId", mcp_tools.EXCLUDED_TOOLS)
        self.assertIn("slack_send_message", allowed)
        self.assertIn("addOrEditJiraIssueComment", allowed)
        self.assertIn("slack_read_channel", allowed)
