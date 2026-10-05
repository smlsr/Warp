#!/usr/bin/env python3
"""Print the Warp resume hint. Read-only. Does not dispatch.

This is the sessionStart behavior. Plugin hooks do not run on cloud runners,
so /warp, /warp-start, /warp-resume, and /warp-status run this script. The
local sessionStart hook calls the same script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage  # noqa: E402

MISSING = "WARP: no .warp/beam.json — run /warp-ingest before /warp."
FOLLOW = "Read .warp/BOARD.md then run /warp-status before dispatching."


def project_root(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("CURSOR_PROJECT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def print_hint(beam_path: Path) -> None:
    """Print the hint for this beam. Missing .warp/beam.json uses the hook text."""
    if not beam_path.is_file():
        name = beam_path.as_posix()
        if name.endswith(".warp/beam.json"):
            print(MISSING)
        else:
            print("WARP: no %s — run /warp-ingest before /warp." % beam_path)
        return
    try:
        beam = json.loads(beam_path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        print(MISSING)
        return
    if not isinstance(beam, dict):
        print(MISSING)
        return
    metrics = beam.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    by_status = metrics.get("byStatus") or {}
    if not isinstance(by_status, dict):
        by_status = {}
    print(
        "WARP beam: paused=%s done=%s/%s eta_h=%s alarms=%s"
        % (
            beam.get("paused"),
            metrics.get("done"),
            metrics.get("total"),
            metrics.get("etaHours"),
            by_status.get("alarm", 0),
        )
    )
    print(FOLLOW)
    try:
        import inbound

        print(inbound.status_line(beam))
    except Exception:
        print("listener: stopped")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the Warp resume hint. Does not dispatch.",
        epilog=(
            "examples:\n"
            "  python3 scripts/resume_hint.py ?\n"
            "  python3 scripts/resume_hint.py --root .\n"
            "  python3 scripts/resume_hint.py --beam .warp/beam.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", help="project directory (default: CURSOR_PROJECT_DIR or cwd)")
    parser.add_argument("--beam", help="beam.json path (default: <root>/.warp/beam.json)")
    args = parser.parse_args(usage.normalize_argv(argv))
    if args.beam:
        beam_path = Path(args.beam).expanduser()
        if not beam_path.is_absolute():
            beam_path = project_root(args.root) / beam_path
    else:
        beam_path = project_root(args.root) / ".warp" / "beam.json"
    print_hint(beam_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
