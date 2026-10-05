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
            "beam.py": ("--via", "maxAgents", "ingest"),
            "version.py": (".warp/version",),
            "bump_version.py": ("--minor", "--major", "--note"),
            "notify.py": ("outbox", "quiet"),
            "status_post.py": ("--beam", "--out"),
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
            "check_version.py",
            "herald_fmt.py",
            "jira_view.py",
            "jira_match.py",
            "jira_external_id.py",
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
