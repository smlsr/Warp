#!/usr/bin/env python3
"""Say whether one MCP call is on Warp's allowlist. Prints allow or ask.

Cloud runners do not execute beforeMCPExecution. Warp skills run this script
before an MCP tool that is not already named in the skill. allow means the
pair is on the same list /warp-allow-notify writes. ask means do not call it.

This does not approve the call in Cursor, and it does not approve shell
commands. It never allows an arbitrary tool. A missing hook is ask, not allow,
unless the pair is one Warp already allowlists (Slack, Teams, Jira, optional
GitHub add_issue_comment, and notifyAllow). server:* is honored only when
that pair is already in warp-allow.json because someone passed
--allow-server-tools. The fallback list used when no pairs file exists does
not include server:*.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import allow_notify  # noqa: E402
import herald_fmt  # noqa: E402
import usage  # noqa: E402


def read_pairs(path: Path) -> Optional[list]:
    """None when the file is absent. A list when it exists, including empty."""
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    raw = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return []
    pairs = []
    for pair in raw:
        if isinstance(pair, dict) and pair.get("server") and pair.get("tool"):
            pairs.append((str(pair["server"]), str(pair["tool"])))
    return pairs


def stored_pairs(root: Path, home: Path) -> Optional[list]:
    found = []
    present = False
    for path in (
        root / ".cursor" / "hooks" / "warp-allow.json",
        home / "hooks" / "warp-allow.json",
    ):
        loaded = read_pairs(path)
        if loaded is None:
            continue
        present = True
        found.extend(loaded)
    if not present:
        return None
    return found


def builtin_pairs(root: Path, home: Path) -> list:
    """Same pairs allow_notify would write, without server:*."""
    cfg = herald_fmt.read_config(root)
    text = ""
    config_path = root / ".warp" / "config.yaml"
    if config_path.is_file():
        text = config_path.read_text()
    extras = allow_notify.parse_notify_allow(text)
    with_git, _note = allow_notify.git_comment_wanted(root)
    discovered = allow_notify.discover_servers(root, home)
    return allow_notify.build_pairs(
        cfg,
        extras,
        True,
        with_git,
        allow_server_tools=False,
        discovered=discovered,
    )


def judge(server: str, tool: str, pairs: list) -> str:
    if not server or not tool or "*" in server or "*" in tool:
        return "ask"
    for pattern_server, pattern_tool in pairs:
        if allow_notify.glob_match(pattern_server, server) and allow_notify.glob_match(pattern_tool, tool):
            return "allow"
    return "ask"


def decide(server: str, tool: str, root: Path, home: Path) -> str:
    pairs = stored_pairs(root, home)
    if pairs is None:
        pairs = builtin_pairs(root, home)
    return judge(server, tool, pairs)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print allow or ask for one Warp MCP server:tool pair.",
        epilog=(
            "examples:\n"
            "  python3 scripts/mcp_allow.py ?\n"
            "  python3 scripts/mcp_allow.py --server slack --tool slack_post_message --root .\n"
            "\n"
            "Prints allow or ask. ask means do not call the tool. This does not\n"
            "approve shell commands and does not allow every MCP tool.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", required=True, help="MCP server id from the tool call")
    parser.add_argument("--tool", required=True, help="MCP tool name from the tool call")
    parser.add_argument("--root", help="repo root (default: git toplevel or cwd)")
    parser.add_argument("--cursor-home", help="directory standing in for ~/.cursor (tests)")
    args = parser.parse_args(usage.normalize_argv(argv))
    root = allow_notify.find_root(args.root)
    home = allow_notify.cursor_home(args.cursor_home)
    print(decide(args.server, args.tool, root, home))
    return 0


if __name__ == "__main__":
    sys.exit(main())
