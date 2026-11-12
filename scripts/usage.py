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
    "--running",
    "--remote",
    "--untagged",
}


def normalize_argv(argv: Optional[list[str]], extra_switches: Optional[set[str]] = None) -> list[str]:
    switches = SWITCHES | set(extra_switches or ())
    args = list(sys.argv[1:] if argv is None else argv)
    import os

    try:
        import runs as _runs

        _runs.reset_binding()
    except Exception:
        _runs = None
    os.environ.pop("WARP_MIRROR_ROOT", None)
    if "--mirror-root" in args:
        # 1.7.26: every script that takes --beam accepts --mirror-root.
        args = [a for a in args if a != "--mirror-root"]
        os.environ["WARP_MIRROR_ROOT"] = "1"
    if _runs is not None:
        # 1.7.26: bind --beam once, before any handler reads it: exact path, cross-run refusal, the pack's run.
        for index, arg in enumerate(args):
            value = args[index + 1] if arg == "--beam" and index + 1 < len(args) else \
                (arg.split("=", 1)[1] if arg.startswith("--beam=") else None)
            if value and not value.startswith("-"):
                _runs.bind_path(value)
                break
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
