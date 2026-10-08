"""Turn `?` and `help` into `--help` for the scripts people run directly.

`-h` and `--help` already work. A bare `help` is the same, unless it is the
value of an option (`--folder help` stays a folder name). `?` is always help.
"""

from __future__ import annotations

import sys
from typing import Optional

# Flags that do not take the next argument.
SWITCHES = {
    "-h",
    "--help",
    "--dry-run",
    "--yes",
    "--user",
    "--with-jira",
    "--with-git",
    "--revoke",
    "--no-allow",
    "--check",
    "--allow-server-tools",
    "--remove-gitignore",
    "--json",
    "--write",
    "--no-category",
    "--keep-status",
    "--patch",
    "--minor",
    "--major",
    "--force",
    "--search",
    "--from-jira",
    "--auto",
    "--list",
    "--link",
    "--probe",
    "--all",
    "--full",
    "--verbose",
    "--comments",
    "--links",
    "--include-done",
    "--write-external-id",
    "--force-external-id",
    "--open",
    "--partial",
    "--cloud",
    "--apply",
    "--all-idle",
    "--any-repo",
}


def normalize_argv(argv: Optional[list[str]], extra_switches: Optional[set[str]] = None) -> list[str]:
    switches = SWITCHES | set(extra_switches or ())
    args = list(sys.argv[1:] if argv is None else argv)
    out: list[str] = []
    take_value = False
    for arg in args:
        # `?` is help even when it sits where a value would go.
        if arg == "?":
            out.append("--help")
            take_value = False
            continue
        if take_value:
            out.append(arg)
            take_value = False
            continue
        if arg == "help":
            out.append("--help")
            continue
        out.append(arg)
        if arg.startswith("-") and arg not in switches and "=" not in arg:
            take_value = True
    return out
