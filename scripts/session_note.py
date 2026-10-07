#!/usr/bin/env python3
"""Append a session-stop or subagent-stop line to the Warp journal.

Plugin stop and subagentStop hooks do not run on cloud runners. /warp-stop,
/warp-pause, and the end of a Shuttle run this script instead. A second call
is skipped while that type is still the last journal line.

session-stop does not create .warp. subagent-stop does, matching the hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam  # noqa: E402
import usage  # noqa: E402

TYPES = ("session-stop", "subagent-stop")


def project_root(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("CURSOR_PROJECT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def last_type(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    line = ""
    for item in text.splitlines():
        if item.strip():
            line = item
    if not line:
        return None
    try:
        doc = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("type"), str):
        return doc["type"]
    return None


def _release_listener(state_dir: Path) -> None:
    """The listener subagent ends with the parent turn. Clear a running flag."""
    beam_path = state_dir / "beam.json"
    if not beam_path.is_file():
        return
    try:
        import inbound

        inbound.release(beam_path)
    except Exception:
        return


def _publish_beam(state_dir: Path) -> None:
    """Push the live beam to the base branch so the next window can resume."""
    beam_path = state_dir / "beam.json"
    if not beam_path.is_file():
        return
    try:
        import state_commit

        root = state_dir.parent
        state_commit.publish_base(root, str(beam_path))
    except Exception as exc:
        print("state: not published (%s)" % exc)


def note(state_dir: Path, kind: str) -> str:
    """Append kind to state_dir/journal.jsonl. Return noted, already, or absent."""
    if kind not in TYPES:
        raise ValueError(kind)
    if kind == "session-stop" and not state_dir.is_dir():
        message = "no .warp — session-stop not recorded"
        print(message)
        return "absent"
    if kind == "session-stop":
        _release_listener(state_dir)
    journal_path = state_dir / "journal.jsonl"
    if last_type(journal_path) == kind:
        if kind == "session-stop":
            _publish_beam(state_dir)
        message = "already noted %s" % kind
        print(message)
        return "already"
    if kind == "subagent-stop":
        state_dir.mkdir(parents=True, exist_ok=True)
    # beam.journal writes beside the beam file, so point it at state_dir/beam.json.
    beam.journal(state_dir / "beam.json", {"type": kind})
    if kind == "session-stop":
        _publish_beam(state_dir)
    message = "noted %s" % kind
    print(message)
    return "noted"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a Warp session-stop or subagent-stop journal line.",
        epilog=(
            "examples:\n"
            "  python3 scripts/session_note.py ?\n"
            "  python3 scripts/session_note.py --type session-stop --root .\n"
            "  python3 scripts/session_note.py --type subagent-stop --beam .warp/beam.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--type", required=True, choices=TYPES, help="session-stop or subagent-stop")
    parser.add_argument("--root", help="project directory (default: CURSOR_PROJECT_DIR or cwd)")
    parser.add_argument("--beam", help="beam.json path; the note is written next to it")
    args = parser.parse_args(usage.normalize_argv(argv))
    if args.beam:
        beam_path = Path(args.beam).expanduser()
        if not beam_path.is_absolute():
            beam_path = project_root(args.root) / beam_path
        state_dir = beam_path.parent
    else:
        state_dir = project_root(args.root) / ".warp"
    note(state_dir, args.type)
    return 0


if __name__ == "__main__":
    sys.exit(main())
