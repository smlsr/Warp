#!/usr/bin/env python3
"""beforeMCPExecution hook. Allow only pairs listed beside this script.

Local IDE duplicate. Cloud runners do not execute this hook. They run
scripts/mcp_allow.py, which uses the same pairs and prints allow or ask.

Reads `warp-allow.json` in the same directory. Each pair is a server name and
a tool name. A match prints `{"permission":"allow"}`. Anything else, including
a missing file or bad input, prints `{"permission":"ask"}`.

This never returns deny. A stored server pattern may contain `*` so
`plugin-slack-slack` matches `*slack*`. A stored tool of `*` matches every
tool on that server; `/warp-allow-notify` writes that only with
`--allow-server-tools`. Cursor's own docs say a hook `allow` does not skip
the MCP approval prompt; the IDE prompt is controlled by `permissions.json`.
Invalid JSON on stdout blocks the call, so this prints one JSON object and
exits 0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def glob_match(pattern: str, value: str) -> bool:
    """Cursor mcpAllowlist rule: * matches any sequence, case-insensitive."""
    if not pattern or not value:
        return False
    pat = re.escape(pattern.casefold()).replace(r"\*", ".*")
    return re.fullmatch(pat, value.casefold()) is not None


def decide(server: str, tool: str) -> str:
    path = Path(__file__).resolve().parent / "warp-allow.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "ask"
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, list):
        return "ask"
    if not server or not tool or "*" in server or "*" in tool:
        return "ask"
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if glob_match(str(pair.get("server") or ""), server) and glob_match(str(pair.get("tool") or ""), tool):
            return "allow"
    return "ask"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    permission = decide(str(data.get("mcp_server_name") or ""), str(data.get("tool_name") or ""))
    sys.stdout.write(json.dumps({"permission": permission}) + "\n")


if __name__ == "__main__":
    main()
