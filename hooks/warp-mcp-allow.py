#!/usr/bin/env python3
"""beforeMCPExecution hook. Allow only pairs listed beside this script.

Reads `warp-allow.json` in the same directory. Each pair is a server name and
a tool name. A match prints `{"permission":"allow"}`. Anything else, including
a missing file or bad input, prints `{"permission":"ask"}`.

This never returns deny, and it never allows every tool on a server. Cursor's
own docs say a hook `allow` does not skip the MCP approval prompt; the IDE
prompt is controlled by `permissions.json`. Invalid JSON on stdout blocks the
call, so this prints one JSON object and exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def decide(server: str, tool: str) -> str:
    path = Path(__file__).resolve().parent / "warp-allow.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "ask"
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, list):
        return "ask"
    server_l, tool_l = server.casefold(), tool.casefold()
    if not server_l or not tool_l or "*" in server_l or "*" in tool_l:
        return "ask"
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("server") or "").casefold() == server_l and str(pair.get("tool") or "").casefold() == tool_l:
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
