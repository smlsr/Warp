"""When a ticket closes the run, and when Warp has no work on it (1.7.15). One place, so they cannot drift.

Before 1.7.15 five copies of a "settled" set ({merged, done, parked, skipped}) decided both things at once. A run with
160 parked tickets whose pull requests coordinators outside Warp were still driving read as done: spawns closed,
`halt_state` said `done`, `parent_may_exit` was true, and the listener started and exited in a loop.

  terminal   merged, done, or skipped with a reason of won't-do or duplicate. Only terminal tickets close the run.
  inactive   terminal, or parked, external, or skipped for any other reason: Warp starts nothing for it, but a
             non-terminal one keeps the run open (and parked and external ones are polled every pass).
  external   a ticket whose pull request is open and whose owner is outside Warp. Never dispatched; polled for
             merge and close.

`beam.is_terminal`, `beam.is_inactive`, `beam.run_open`, and `beam.dispatchable` are these functions.
"""

from __future__ import annotations

import re

TERMINAL_STATUSES = frozenset({"merged", "done"})
INACTIVE = frozenset({"merged", "done", "parked", "skipped", "external"})
NOT_STARTED = frozenset({"queued", "blocked"})
_TERMINAL_SKIP = re.compile(r"\b(won'?t[ -]?do|wont[ -]?do|wontdo|duplicate|dup(e|licate)?\b|duplicate of)", re.I)
SKIP_REASON_KEYS = ("skipReason", "skippedReason", "skipResolution", "reason", "resolution", "note")


def skip_reason(ticket: dict) -> str:
    if not isinstance(ticket, dict):
        return ""
    for key in SKIP_REASON_KEYS:
        value = ticket.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    jira = ticket.get("jira") if isinstance(ticket.get("jira"), dict) else {}
    value = jira.get("resolution")
    return str(value).strip() if value else ""


def terminal_skip(ticket: dict) -> bool:
    """A skip that closes the ticket for good: won't-do or duplicate."""
    return bool(_TERMINAL_SKIP.search(skip_reason(ticket)))


def is_terminal(ticket: dict) -> bool:
    if not isinstance(ticket, dict):
        return False
    status = str(ticket.get("status") or "")
    if status in TERMINAL_STATUSES:
        return True
    return status == "skipped" and terminal_skip(ticket)


def is_inactive(ticket: dict) -> bool:
    """Warp starts nothing for it (it may still keep the run open)."""
    return isinstance(ticket, dict) and str(ticket.get("status") or "") in INACTIVE


def run_open(data: dict) -> bool:
    """Any ticket that is not terminal. A run with no tickets is open (it has not been scanned yet)."""
    tickets = data.get("tickets") if isinstance(data, dict) and isinstance(data.get("tickets"), dict) else {}
    if not tickets:
        return True
    return any(isinstance(t, dict) and not is_terminal(t) for t in tickets.values())


def dispatchable(data: dict) -> list:
    """Ids of tickets Warp may still start or work: queued, blocked, or in its own pipeline (not inactive)."""
    tickets = data.get("tickets") if isinstance(data, dict) and isinstance(data.get("tickets"), dict) else {}
    return sorted(str(tid) for tid, t in tickets.items() if isinstance(t, dict) and not is_inactive(t))


def open_breakdown(data: dict) -> dict:
    """Counts of the non-terminal tickets by why they keep the run open: {status: n}."""
    tickets = data.get("tickets") if isinstance(data, dict) and isinstance(data.get("tickets"), dict) else {}
    out: dict = {}
    for t in tickets.values():
        if isinstance(t, dict) and not is_terminal(t):
            key = str(t.get("status") or "queued")
            out[key] = out.get(key, 0) + 1
    return out
