#!/usr/bin/env python3
"""Build the channel payload for a Teams or Slack status post.

Does not call Teams or Slack. The agent posts this payload with the
connected MCP server. Inbound `warp:status` uses the same payload.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beam import load_json, utcnow  # noqa: E402
from scan import write_status  # noqa: E402
import herald_fmt as fmt  # noqa: E402


def payload(beam_path: Path) -> dict:
    write_status(beam_path)
    beam = load_json(beam_path)
    status_md = (beam_path.parent / "STATUS.md").read_text()
    counts = {
        "done": sum(1 for t in beam["tickets"].values() if t["status"] in {"merged", "done", "skipped"}),
        "working": sum(1 for t in beam["tickets"].values() if t["status"] in {"claimed", "planning", "coding", "review", "fix", "awaiting_approval", "merging"}),
        "left": sum(1 for t in beam["tickets"].values() if t["status"] in {"queued", "blocked", "alarm"}),
    }
    working = [
        f"{t['id']} {t['status']}"
        for t in beam["tickets"].values()
        if t["status"] in {"claimed", "planning", "coding", "review", "fix", "awaiting_approval", "merging"}
    ]
    root = beam_path.resolve().parent.parent
    msg = fmt.message(
        root,
        "Status",
        facts=[
            ("Updated", utcnow()),
            ("Run", str(beam.get("runState"))),
            ("Done", str(counts["done"])),
            ("Working", ", ".join(working) or "none"),
            ("Left", str(counts["left"])),
        ],
        footer="Reply warp:status to refresh. warp:pause / warp:resume / warp:stop / warp:start control the run.",
    )
    return {
        **msg,
        "files": ["STATUS.md", "status.json", "BOARD.md"],
        "statusMarkdown": status_md[:12000],
        "counts": counts,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--beam", default=".warp/beam.json")
    p.add_argument("--out", default=".warp/status-post.json")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    body = payload(Path(args.beam))
    Path(args.out).write_text(json.dumps(body, indent=2) + "\n")
    print(body["text"])
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
