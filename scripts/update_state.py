#!/usr/bin/env python3
"""Insurance sync: main's beam, ticket folders, then the parent's own state.

Fetch origin and load that beam. Fetch each in-flight ticket branch and patch
from `.warp/tickets/<id>/` only. Do not copy a ticket branch's beam.json.
Then put back pause, stop, claims, and runState the parent has that the
remote beam does not. Write the reconciled beam, journal, and board, strip
tokens, cost, API keys, and webhook URLs, and push that beam to main.
`.warp/config.yaml` is not committed.

`/warp-pause` and `/warp-stop` run this after they set paused or stopped, so
the repo shows that state before the command returns.

  python3 scripts/update_state.py ?
  python3 scripts/update_state.py --beam .warp/beam.json
  python3 scripts/update_state.py --root . --beam .warp/beam.json

From an installed copy, without the slash command:

  python3 .cursor/plugins/warp/scripts/update_state.py --beam .warp/beam.json
  python3 .cursor/plugins/warp/scripts/update_state.py ?

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import beam  # noqa: E402
import state_commit  # noqa: E402
import ticket_state  # noqa: E402

HELP = __doc__


def _read(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _has_claim(ticket) -> bool:
    if not isinstance(ticket, dict):
        return False
    status = ticket.get("status")
    if status == "claimed":
        return True
    if ticket.get("branch") or ticket.get("agent"):
        return status not in {None, "", "queued"}
    return False


def _root(beam_path: Path) -> Path:
    return beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent


def _summary(pulled: list, won: list, sha: Optional[str], base: Optional[str]) -> None:
    if not pulled:
        print("pulled: none")
    for item in pulled:
        print("pulled: %s" % item)
    if not won:
        print("local won: none")
    for item in won:
        print("local won: %s" % item)
    if sha and base:
        print("commit: %s %s" % (sha, base))
    else:
        print("commit: none")


def update_state(beam_path: Path, push: bool = True) -> int:
    """Reconcile main, ticket folders, and local parent state. Push the beam."""
    beam_path = Path(beam_path)
    if not beam_path.is_absolute():
        beam_path = Path.cwd() / beam_path
    local = _read(beam_path)
    root = _root(beam_path)
    if state_commit._git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        if local is not None:
            beam.journal(beam_path, {"type": "update-state", "pulled": ["no git repo"], "won": []})
        _summary(["no git repo"], [], None, None)
        return 0
    if local is None and not beam_path.is_file():
        load_lines = state_commit.load_main_beam(beam_path, replace=True)
    else:
        load_lines = state_commit.load_main_beam(beam_path, replace=True)
    loaded_ref = ""
    for line in load_lines:
        if line.startswith("beam: loaded from "):
            loaded_ref = line.split("beam: loaded from ", 1)[1].strip()
    data = _read(beam_path)
    if data is None:
        print("refuse: no beam")
        _summary(["main has no beam"], [], None, None)
        return 2
    before = {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for tid, row in tickets.items():
        if isinstance(row, dict):
            before[tid] = (row.get("status"), row.get("lastSeenAt"))
    ticket_state.observe_locked(beam_path, require_live=False)
    patched = _read(beam_path) or data
    pulled = []
    if loaded_ref:
        pulled.append("beam %s" % loaded_ref)
    elif any(line.startswith("beam: fetch failed") for line in load_lines):
        pulled.append("fetch failed")
    elif any("has no beam" in line for line in load_lines):
        pulled.append("main has no beam")
    else:
        pulled.append("beam kept local")
    after = patched.get("tickets") if isinstance(patched.get("tickets"), dict) else {}
    for tid in sorted(set(before) | set(after)):
        row = after.get(tid) if isinstance(after.get(tid), dict) else {}
        old_status, old_seen = before.get(tid, (None, None))
        new_status = row.get("status")
        new_seen = row.get("lastSeenAt")
        if new_status != old_status:
            pulled.append(".warp/tickets/%s/ %s -> %s" % (tid, old_status or "missing", new_status or "missing"))
        elif new_seen and new_seen != old_seen:
            pulled.append(".warp/tickets/%s/ heartbeat" % tid)
    won = []
    if loaded_ref and isinstance(local, dict):
        local_state = local.get("runState")
        if local_state in {"paused", "stopped"} and patched.get("runState") != local_state:
            won.append("runState %s" % local_state)
            patched["runState"] = local_state
            patched["paused"] = True
            patched["pauseReason"] = local.get("pauseReason")
            if local_state == "stopped":
                patched["stoppedAt"] = local.get("stoppedAt")
        local_tickets = local.get("tickets") if isinstance(local.get("tickets"), dict) else {}
        remote_tickets = patched.get("tickets") if isinstance(patched.get("tickets"), dict) else {}
        patched["tickets"] = remote_tickets
        for tid in sorted(local_tickets):
            row = local_tickets[tid]
            if not _has_claim(row):
                continue
            if _has_claim(remote_tickets.get(tid)):
                continue
            remote_tickets[tid] = copy.deepcopy(row)
            won.append("claim %s" % tid)
    patched["metrics"] = beam.metrics(patched)
    beam.atomic_write(beam_path, json.dumps(patched, indent=2) + "\n")
    beam.journal(beam_path, {"type": "update-state", "pulled": pulled, "won": won})
    beam.write_board_files(beam_path, patched)
    code = 0
    if push:
        code = state_commit.publish_base(root, str(beam_path))
    config = patched.get("config") if isinstance(patched.get("config"), dict) else {}
    if isinstance(local, dict) and isinstance(local.get("config"), dict) and not config.get("baseBranch"):
        config = local["config"]
    base = state_commit._base_name(root, config if isinstance(config, dict) else {})
    shown = state_commit._git(root, "rev-parse", "--short", base)
    sha = shown.stdout.strip() if shown.returncode == 0 else ""
    _summary(pulled, won, sha or None, base if sha else None)
    return code


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the live beam onto main", epilog=HELP)
    parser.add_argument("--root", default=".")
    parser.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    beam_path = Path(args.beam)
    if not beam_path.is_absolute():
        beam_path = root / beam_path
    return update_state(beam_path)


if __name__ == "__main__":
    sys.exit(main())
