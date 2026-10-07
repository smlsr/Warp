"""Docs stay aligned with config keys and the scripts people run."""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import usage  # noqa: E402


def run(script, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


DOC_FILES = (
    "README.md",
    "docs/COMMANDS.md",
    "docs/CONFIG.md",
    "docs/RUNBOOK.md",
    "docs/STATE.md",
    "docs/CONNECTORS.md",
    "docs/GUIDE.md",
    "AGENTS.md",
)


def mentioned(name: str, text: str) -> bool:
    """Match a command token without treating /warp as a prefix of /warp-init."""
    return re.search(rf"(?<![\w/]){re.escape(name)}(?![\w-])", text) is not None


class WorkerRecoveryDocTests(unittest.TestCase):
    def test_docs_describe_heartbeat_and_recovery(self):
        files = (
            "README.md",
            "docs/COMMANDS.md",
            "docs/CONFIG.md",
            "docs/GUIDE.md",
            "docs/RUNBOOK.md",
            "docs/STATE.md",
        )
        blobs = {rel: (ROOT / rel).read_text() for rel in files}
        for rel, text in blobs.items():
            self.assertIn("staleMinutes", text, rel)
            self.assertIn("lastSeenAt", text, rel)
            self.assertIn("do not run on cloud", text, rel)
        for rel in ("README.md", "docs/CONFIG.md", "docs/RUNBOOK.md", "docs/STATE.md", "docs/COMMANDS.md", "docs/GUIDE.md"):
            self.assertIn("maxRecoveries", blobs[rel], rel)
            self.assertIn("worker-died", blobs[rel], rel)
        self.assertIn("Listener died. A new one started.", blobs["docs/RUNBOOK.md"])
        self.assertIn("worker died. A new Shuttle started.", blobs["docs/RUNBOOK.md"])
        self.assertIn("beam.py heartbeat", blobs["docs/COMMANDS.md"])
        self.assertIn("inbound.py heartbeat", blobs["docs/COMMANDS.md"])
        self.assertIn("beam.py watchdog", blobs["docs/GUIDE.md"])

    def test_readme_has_a_dead_worker_section(self):
        readme = (ROOT / "README.md").read_text()
        start = readme.find("## Dead workers")
        self.assertGreaterEqual(start, 0, "README has no Dead workers section")
        section, sep, _after = readme[start:].partition("\n## ")
        self.assertTrue(sep, "dead-worker section has no following heading")
        for needle in (
            "staleMinutes",
            "lastSeenAt",
            "maxRecoveries",
            "worker-died",
            "recovering",
            "Listener died. A new one started.",
            "worker died. A new Shuttle started.",
            "beam.py heartbeat",
            "inbound.py heartbeat",
            "beam.py watchdog",
            "do not run on cloud",
            "fresh heartbeat",
        ):
            self.assertIn(needle, section, needle)
        install = readme[readme.find("## Install"):].partition("\n## ")[0]
        self.assertNotIn("## Dead workers", install)

    def test_recovery_keys_are_named_on_upgrade(self):
        readme = (ROOT / "README.md").read_text()
        start = readme.find("## Upgrade")
        self.assertGreaterEqual(start, 0)
        section = readme[start:].partition("\n## ")[0]
        self.assertIn("staleMinutes", section)
        self.assertIn("maxRecoveries", section)


class Since193DocTests(unittest.TestCase):
    """Behaviors shipped after the 1.3.19 docs pass stay required."""

    USER_DOCS = (
        "README.md",
        "docs/COMMANDS.md",
        "docs/CONFIG.md",
        "docs/GUIDE.md",
        "docs/RUNBOOK.md",
        "docs/STATE.md",
        "docs/CONNECTORS.md",
        "docs/MERGE-POLICY.md",
        "docs/EXPORT.md",
        "AGENTS.md",
    )

    def test_l_and_xl_follow_the_size_list(self):
        for rel in (
            "README.md",
            "docs/GUIDE.md",
            "docs/MERGE-POLICY.md",
            "docs/CONFIG.md",
            "rules/warp-operating.mdc",
            "examples/BOARD.sample.md",
        ):
            text = (ROOT / rel).read_text()
            self.assertNotIn("above MEDIUM", text, rel)
            self.assertNotIn("L and XL run that same", text, rel)
        for rel in ("README.md", "docs/MERGE-POLICY.md", "docs/CONFIG.md", "rules/warp-operating.mdc", "docs/GUIDE.md"):
            self.assertIn("L and XL are not special", (ROOT / rel).read_text(), rel)
            self.assertIn("autoMergeSizes", (ROOT / rel).read_text(), rel)
        guide = (ROOT / "docs/GUIDE.md").read_text()
        self.assertNotIn("| L | 11 | HIGH | wait for APPROVED |", guide)
        self.assertIn("auto when listed in `autoMergeSizes`", guide)

    def test_connectors_cover_listener_recovery_and_agreed_project(self):
        text = (ROOT / "docs/CONNECTORS.md").read_text()
        for needle in (
            "lastSeenAt",
            "staleMinutes",
            "watchdog",
            "recovering",
            "worker-died",
            "inbound.py heartbeat",
            "not one per",
            "Listener died. A new one started.",
            "set jiraProject to WAR (every stored key is in project WAR)",
            "Do not run `project --set`",
            "the run stops",
            "do not run on cloud",
        ):
            self.assertIn(needle, text, needle)

    def test_agents_and_export_name_recovery(self):
        agents = (ROOT / "AGENTS.md").read_text()
        for needle in ("staleMinutes", "maxRecoveries", "recovering", "worker-died", "lastSeenAt", "beam.py watchdog"):
            self.assertIn(needle, agents, needle)
        export = (ROOT / "docs/EXPORT.md").read_text()
        self.assertIn("lastSeenAt", export)
        self.assertIn("recovering", export)
        self.assertIn("are not copied", export)

    def test_no_doc_requires_a_listener_per_ticket_or_hooks_on_cloud(self):
        for rel in self.USER_DOCS:
            text = (ROOT / rel).read_text()
            self.assertNotIn("each waiting ticket has its own listener", text, rel)
            self.assertNotIn("hooks are required on cloud", text, rel)
            self.assertNotIn("its own listener", text, rel)
        for rel in ("README.md", "docs/CONNECTORS.md", "docs/RUNBOOK.md", "docs/STATE.md", "docs/GUIDE.md"):
            self.assertIn("not one per", (ROOT / rel).read_text(), rel)

    def test_first_ticket_miss_stops_the_run(self):
        sentence = "Run stopped because Jira issues are not linked"
        for rel in ("docs/RUNBOOK.md", "docs/GUIDE.md", "README.md"):
            text = (ROOT / rel).read_text()
            self.assertTrue(
                sentence in text or "stops the run" in text or "the run stops" in text,
                rel,
            )
        self.assertIn(sentence, (ROOT / "docs/RUNBOOK.md").read_text())
        self.assertIn("jira.startedAt", (ROOT / "docs/CONNECTORS.md").read_text())

    def test_lock_escape_repair_widens_the_lock(self):
        files = (
            "README.md",
            "docs/COMMANDS.md",
            "docs/CONFIG.md",
            "docs/GUIDE.md",
            "docs/RUNBOOK.md",
        )
        for rel in files:
            text = (ROOT / rel).read_text()
            self.assertIn("alarmRepairMinutes", text, rel)
            self.assertIn("maxAlarmRepairs", text, rel)
            self.assertIn("lock-escape", text, rel)
            self.assertIn("widens", text, rel)
        readme = (ROOT / "README.md").read_text()
        start = readme.find("## Alarms")
        self.assertGreaterEqual(start, 0)
        section = readme[start:].partition("\n## ")[0]
        self.assertIn("escaped", section)
        self.assertIn("in-flight", section)
        self.assertIn("alarm_repair.py", section)

    def test_pending_gates_recompute_when_members_are_merged(self):
        sentence = "G1 pending cleared. Members merged. Tick ran."
        for rel in ("README.md", "docs/GUIDE.md", "docs/RUNBOOK.md", "docs/COMMANDS.md"):
            text = (ROOT / rel).read_text()
            self.assertIn(sentence, text, rel)
            self.assertIn("every member is merged or done", text, rel)
        guide = (ROOT / "docs/GUIDE.md").read_text()
        self.assertNotIn("Do not mark a gate green because the member PRs merged", guide)


class OrchestratorDocTests(unittest.TestCase):
    FILES = (
        "README.md",
        "docs/COMMANDS.md",
        "docs/CONFIG.md",
        "docs/GUIDE.md",
        "docs/RUNBOOK.md",
        "docs/STATE.md",
        "rules/warp-operating.mdc",
    )

    def test_orchestrator_is_the_only_merger_and_new_keys_are_named(self):
        for rel in self.FILES:
            text = (ROOT / rel).read_text()
            self.assertIn("orchestrator is the only merger", text, rel)
            self.assertIn("checkCommand", text, rel)
            self.assertIn("appendOnlyPaths", text, rel)
            self.assertIn("maxAgents", text, rel)


class ConfigDocTests(unittest.TestCase):
    def test_every_example_key_is_in_config_doc(self):
        example = (ROOT / "assets/config.example.yaml").read_text()
        doc = (ROOT / "docs/CONFIG.md").read_text()
        keys = [
            m.group(1)
            for line in example.splitlines()
            if (m := re.match(r"^([A-Za-z][A-Za-z0-9_]*):", line))
        ]
        self.assertGreater(len(keys), 20)
        missing = [key for key in keys if key not in doc]
        self.assertEqual(missing, [])


class MatchedProjectDocTests(unittest.TestCase):
    def test_docs_write_the_agreed_project_without_a_manual_set(self):
        sentence = "set jiraProject to WAR (every stored key is in project WAR)"
        for rel in ("README.md", "docs/COMMANDS.md", "docs/GUIDE.md", "docs/RUNBOOK.md"):
            text = (ROOT / rel).read_text()
            self.assertIn(sentence, text, rel)
            self.assertIn("getVisibleJiraProjects", text, rel)


class CloudHookDocTests(unittest.TestCase):
    def test_docs_do_not_require_hooks_on_cloud(self):
        blobs = {rel: (ROOT / rel).read_text() for rel in DOC_FILES}
        joined = "\n".join(blobs.values())
        for name in ("resume_hint.py", "session_note.py", "mcp_allow.py"):
            self.assertIn(name, joined, name)
        for rel in (
            "README.md",
            "docs/COMMANDS.md",
            "docs/CONFIG.md",
            "docs/STATE.md",
            "docs/RUNBOOK.md",
            "docs/CONNECTORS.md",
            "docs/GUIDE.md",
            "AGENTS.md",
        ):
            self.assertIn("do not run on cloud", blobs[rel], rel)
        self.assertIn("Not every MCP tool", blobs["README.md"])
        self.assertIn("does not approve shell", blobs["docs/CONFIG.md"])

    def test_readme_install_lists_mcp_allow_globs(self):
        readme = (ROOT / "README.md").read_text()
        start = readme.find("## Install")
        self.assertGreaterEqual(start, 0)
        section, sep, _after = readme[start:].partition("\n## ")
        self.assertTrue(sep, "install section has no following heading")
        strings = (
            "*slack*:slack_send_message",
            "*atlassian*:transitionJiraIssue",
            "*atlassian*:addOrEditJiraIssueComment",
            "*teams*:send_channel_message",
            "*teams*:teams_send_message",
            "*teams*:teams_read_channel",
            "*teams*:teams_read_thread",
            "*teams*:teams_search_channels",
            "*github*:add_issue_comment",
        )
        missing = [item for item in strings if "`%s`" % item not in section]
        self.assertEqual(missing, [], "allowlist globs missing from the README install section")

    def test_readme_install_explains_customize_github_url(self):
        readme = (ROOT / "README.md").read_text()
        start = readme.find("## Install")
        self.assertGreaterEqual(start, 0)
        section, sep, _after = readme[start:].partition("\n## ")
        self.assertTrue(sep, "install section has no following heading")
        for needle in (
            "https://github.com/smlsr/Warp",
            "Customize",
            "Reload Window",
            ".cursor-plugin/marketplace.json",
            ".cursor-plugin/plugin.json",
            "assets/logo.svg",
            "Plugins & MCPs",
        ):
            self.assertIn(needle, section, needle)


class Since140DocTests(unittest.TestCase):
    """1.4.1 through 1.4.5 behavior, and the /warp-upgrade steps, stay in the user docs."""

    USER_DOCS = (
        "README.md",
        "docs/COMMANDS.md",
        "docs/CONFIG.md",
        "docs/GUIDE.md",
        "docs/RUNBOOK.md",
        "docs/STATE.md",
        "docs/CONNECTORS.md",
        "docs/MERGE-POLICY.md",
        "AGENTS.md",
        "rules/warp-operating.mdc",
    )

    def test_readme_install_update_lists_upgrade_steps_in_order(self):
        readme = (ROOT / "README.md").read_text()
        install = readme[readme.find("## Install"):].partition("\n## ")[0]
        self.assertIn("### Update", install)
        self.assertIn("*slack*:slack_send_message", install)
        update = install.split("### Update", 1)[1].split("After install, these entries", 1)[0]
        steps = (
            "in the repo that has Warp installed",
            "replaces `.cursor/plugins/warp`",
            "fetches the default branch",
            "git checkout",
            "does not overwrite `.warp/config.yaml`",
            "or the beam",
            "`Warp vX.Y.Z`",
            "Reload Cursor",
            "/warp-version",
            "`fetch failed`",
            "`keeping the installed copy`",
            "/warp-init` does not upgrade an existing copy",
            "This command does",
            "`--force` is not an upgrade flag",
            "`--root`",
            "`--source`",
        )
        positions = []
        for step in steps:
            at = update.find(step)
            self.assertGreaterEqual(at, 0, step)
            positions.append(at)
        self.assertEqual(positions, sorted(positions))
        help_text = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "upgrade.py"), "?"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        help_flags = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", help_text))
        self.assertEqual(help_flags, {"--root", "--source", "--help"})
        advertised = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", update)) - {"--force"}
        self.assertEqual(advertised, {"--root", "--source"})
        self.assertNotIn("--force", help_flags)

    def test_new_commands_and_config_keys_stay_required(self):
        keys = (
            "alarmRepairMinutes",
            "maxAlarmRepairs",
            "checkCommand",
            "appendOnlyPaths",
            "mergeQueue",
            "maxAgents",
        )
        phrases = (
            "/warp-upgrade",
            "lock-escape",
            "make ci",
            "P-002",
            "P-003",
            "P-004",
            "P-018",
            "members merged: P-002, P-003, P-004, P-018",
            "G1 pending cleared. Members merged. Tick ran.",
            "`bugbot-failed`",
            "`worker-died`",
            "`stuck`",
            "`ci-red`",
            "`gate-red`",
            "name alone",
            "one ticket at a time",
            "parent folder",
            "serial",
        )
        for rel in self.USER_DOCS:
            text = (ROOT / rel).read_text()
            for key in keys:
                self.assertIn(key, text, "%s missing %s" % (rel, key))
            for phrase in phrases:
                self.assertIn(phrase, text, "%s missing %s" % (rel, phrase))


class TicketAgentDocTests(unittest.TestCase):
    def test_docs_start_a_new_agent_and_not_a_subagent(self):
        needles = (
            "new Agent",
            "Do not start a Subagent",
            "Do not use the Task tool",
            "fresh clone of main",
            ".warp/beam.json",
        )
        banned = (
            "sub-agent",
            "subagent_type",
            "with the Task tool",
            "cloud_base_branch",
        )
        for rel in ("README.md", "docs/GUIDE.md", "AGENTS.md", "rules/warp-operating.mdc"):
            text = (ROOT / rel).read_text()
            for needle in needles:
                self.assertIn(needle, text, "%s missing %s" % (rel, needle))
            for bad in banned:
                self.assertNotIn(bad, text, "%s still says %s" % (rel, bad))


class HelpTokenTests(unittest.TestCase):
    def test_question_mark_is_always_help_and_help_can_be_a_value(self):
        self.assertEqual(usage.normalize_argv(["?"]), ["--help"])
        self.assertEqual(usage.normalize_argv(["help"]), ["--help"])
        self.assertEqual(usage.normalize_argv(["-h"]), ["-h"])
        self.assertEqual(usage.normalize_argv(["--help"]), ["--help"])
        self.assertEqual(usage.normalize_argv(["init", "help"]), ["init", "--help"])
        self.assertEqual(usage.normalize_argv(["scan", "--folder", "help"]), ["scan", "--folder", "help"])
        self.assertEqual(usage.normalize_argv(["scan", "--folder", "?"]), ["scan", "--folder", "--help"])
        self.assertEqual(usage.normalize_argv(["--channel", "help"]), ["--channel", "help"])


class ScriptHelpTests(unittest.TestCase):
    def help_text(self, script, *args):
        proc = run(script, *args)
        self.assertEqual(proc.returncode, 0, f"{script} {args}: {proc.stderr}\n{proc.stdout}")
        return proc.stdout

    def test_allow_notify_prints_every_option(self):
        for token in ("?", "help", "-h", "--help"):
            text = self.help_text("allow_notify.py", token)
            for needle in (
                "--with-jira",
                "--with-git",
                "--user",
                "--yes",
                "--revoke",
                "--dry-run",
                "--cursor-home",
                "--list",
                "--check",
                "--server",
                "--allow-server-tools",
                "notifyAllow",
                "mcpAllowlist",
                "slack_post_message",
                "addOrEditJiraIssueComment",
            ):
                self.assertIn(needle, text)

    def test_user_scripts_accept_question_mark(self):
        cases = {
            "install.py": ("--channel", "--dry-run", "--remove-gitignore", "--yes"),
            "scan.py": ("--folder", "--max-agents", "claude-sonnet-5-5-high", "--keep-status"),
            "jira_sync.py": ("--write", "qa-ready", "no-transition", "--no-category"),
            "provider.py": ("merge-local", "--reason", "pushMerge"),
            "beam.py": ("--via", "maxAgents", "ingest", "heartbeat", "watchdog", "worker-died", "staleMinutes"),
            "orchestrator.py": ("checkCommand", "appendOnlyPaths", "--facts", "send-back", "parked", "--output"),
            "checkout.py": ("--beam", "--id", "--root", "--agent", "implement", "new Agent"),
            "prompt_gate.py": ("--beam", "--force", "--cursor-home", "transitionJiraIssue", "slack_send_message"),
            "state_commit.py": ("--root", "--beam", "--base", "config.yaml"),
            "upgrade.py": ("--root", "--source", "Warp v", "reload Cursor"),
            "version.py": (".warp/version",),
            "bump_version.py": ("--minor", "--major", "--note"),
            "notify.py": ("outbox", "quiet"),
            "status_post.py": ("--beam", "--out"),
            "proceed.py": ("--beam", "--by", "WV-01", "awaiting_approval"),
            "inbound.py": ("--beam", "--text", "--by", "--agent-id", "--pid", "--message-id", "--source", "claim", "release", "heartbeat", "already running"),
            "report.py": ("--out", "--open", "--partial", "warp-complete"),
            "check_version.py": ("--against",),
            "jira_view.py": ("--results", "--comments", "--links", "--all", "--full", "--verbose", "getJiraIssue"),
            "jira_match.py": (
                "--chars",
                "--min-score",
                "--include-done",
                "--write-external-id",
                "--set-external-id",
                "--force-external-id",
                "editJiraIssue",
            ),
            "resume_hint.py": ("--root", "--beam", "Does not dispatch"),
            "session_note.py": ("--type", "session-stop", "subagent-stop", "--beam"),
            "mcp_allow.py": ("--server", "--tool", "--root", "--cursor-home", "ask"),
            "alarm_repair.py": (
                "--beam",
                "--now",
                "--returned",
                "--error",
                "--path",
                "lock-escape",
                "alarmRepairMinutes",
                "maxAlarmRepairs",
            ),
            "jira_external_id.py": (
                "--apply",
                "--yes",
                "--ticket",
                "--force",
                "--force-external-id",
                "--create-field",
                "--recheck",
                "--results",
                "editJiraIssue",
                "record-external-id",
            ),
        }
        for script, needles in cases.items():
            text = self.help_text(script, "?")
            for needle in needles:
                self.assertIn(needle, text, f"{script} ? missing {needle}")

    def test_documented_flags_exist_in_help(self):
        blobs = []
        for script in (
            "install.py",
            "scan.py",
            "jira_sync.py",
            "provider.py",
            "beam.py",
            "version.py",
            "bump_version.py",
            "notify.py",
            "allow_notify.py",
            "status_post.py",
            "proceed.py",
            "inbound.py",
            "report.py",
            "check_version.py",
            "herald_fmt.py",
            "jira_view.py",
            "jira_match.py",
            "jira_external_id.py",
            "resume_hint.py",
            "session_note.py",
            "mcp_allow.py",
            "alarm_repair.py",
            "orchestrator.py",
            "checkout.py",
            "prompt_gate.py",
            "state_commit.py",
            "upgrade.py",
        ):
            # herald_fmt requires --title, so ask for help explicitly.
            blobs.append(self.help_text(script, "?" if script != "herald_fmt.py" else "--help"))
            if script == "scan.py":
                blobs.append(self.help_text(script, "scan", "?"))
            if script == "install.py":
                blobs.append(self.help_text(script, "init", "?"))
                blobs.append(self.help_text(script, "uninstall", "?"))
        help_text = "\n".join(blobs)
        documented = set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", (ROOT / "docs/COMMANDS.md").read_text()))
        documented |= set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", (ROOT / "README.md").read_text()))
        missing = sorted(flag for flag in documented if flag not in help_text)
        self.assertEqual(missing, [], "documented flags missing from --help")

    def test_every_command_file_is_in_readme_and_commands(self):
        readme = (ROOT / "README.md").read_text()
        commands = (ROOT / "docs/COMMANDS.md").read_text()
        names = sorted("/" + path.stem for path in (ROOT / "commands").glob("*.md"))
        self.assertGreaterEqual(len(names), 15)
        missing_readme = [name for name in names if not mentioned(name, readme)]
        missing_commands = [name for name in names if not mentioned(name, commands)]
        self.assertEqual(missing_readme, [], "command files missing from README")
        self.assertEqual(missing_commands, [], "command files missing from COMMANDS.md")

    def test_every_jira_sync_subcommand_is_documented(self):
        source = (SCRIPTS / "jira_sync.py").read_text()
        subs = re.findall(r'add_parser\("([^"]+)"', source)
        self.assertGreaterEqual(len(subs), 8)
        blobs = [(ROOT / rel).read_text() for rel in DOC_FILES]
        docs = "\n".join(blobs)
        missing = []
        for name in subs:
            pattern = rf"jira_sync\.py {re.escape(name)}\b|`{re.escape(name)}`"
            if not re.search(pattern, docs):
                missing.append(name)
        self.assertEqual(missing, [], "jira_sync subcommands missing from docs")
