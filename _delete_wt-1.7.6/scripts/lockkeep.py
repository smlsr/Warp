"""A parked or settled ticket gives its locks back; a ticket that goes back to work gets them back.

Releasing a parked ticket's locks is right: other tickets can take its files. Before 1.7.3 the list was simply
emptied (`reconcile` and the repair sweep), and a retry (`warp:retry`, the auto-scan, start and resume healing)
relaunched the ticket with no locks, so its Shuttle edited its own files and hit `lock-escape` at once, again and
again. Now the released list is kept on the ticket (`releasedLocks`), and every path back to work restores it.
A ticket whose locks were emptied before 1.7.3 gets them from the plan (`.warp/_ingested_schedule.json`), plus
any paths a lock-escape repair added.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PLAN_FILE = "_ingested_schedule.json"


def release(ticket: dict) -> list:
    """Empty the ticket's locks and keep them. Returns the paths released."""
    locks = [str(p) for p in (ticket.get("locks") or []) if str(p).strip()]
    if not locks:
        return []
    kept = [str(p) for p in (ticket.get("releasedLocks") or []) if str(p).strip()]
    for path in locks:
        if path not in kept:
            kept.append(path)
    ticket["releasedLocks"] = kept
    ticket["locks"] = []
    return locks


def plan_locks(beam_path, ticket_id: str) -> list:
    """The ticket's locks as the scan ingested them, or []."""
    if beam_path is None:
        return []
    path = Path(beam_path).parent / PLAN_FILE
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    for row in doc.get("tickets") or [] if isinstance(doc, dict) else []:
        if isinstance(row, dict) and str(row.get("id")) == str(ticket_id):
            return [str(p) for p in (row.get("locks") or []) if str(p).strip()]
    return []


def restore(ticket: dict, beam_path=None) -> Optional[str]:
    """Give a ticket with no locks its locks back. A line saying what came back, or None when nothing was needed."""
    if [p for p in (ticket.get("locks") or []) if str(p).strip()]:
        ticket.pop("releasedLocks", None)
        return None
    kept = [str(p) for p in (ticket.get("releasedLocks") or []) if str(p).strip()]
    source = "kept when it was released"
    if not kept:
        kept = plan_locks(beam_path, str(ticket.get("id") or ""))
        source = "from the plan"
        info = ticket.get("alarmRepair") if isinstance(ticket.get("alarmRepair"), dict) else {}
        for path in info.get("added") or []:
            if str(path).strip() and str(path) not in kept:
                kept.append(str(path))
                source = "from the plan and its lock-escape repairs"
    if not kept:
        return None
    ticket["locks"] = kept
    ticket.pop("releasedLocks", None)
    shown = ", ".join(kept[:6]) + (" +%d" % (len(kept) - 6) if len(kept) > 6 else "")
    return "locks: %s restored %d lock%s (%s): %s" % (ticket.get("id"), len(kept), "" if len(kept) == 1 else "s", source, shown)
