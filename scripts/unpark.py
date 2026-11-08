#!/usr/bin/env python3
"""Parked tickets are reconsidered every pass, and `external` ones are polled (1.7.15).

Before 1.7.15 a parked ticket stayed parked for good unless a person merged its pull request by hand. On Forge-QA
run core 163 rows sat at `next=parked: owner outside Warp (e49-restart coordinator)` while their pull requests were
being driven and merged elsewhere. Each pass, for every parked row:

  pull request merged             -> merged (its dependents start in the same pass)
  pull request closed, unmerged   -> queued again, or skipped with the reason when it was closed as won't-do/duplicate
  every named blocker terminal    -> queued again; the refill in the same pass prints its `start` line
  open pull request, outside owner-> external: never dispatched, still polled for merge and close

An `external` row is polled the same way: merged -> merged, closed -> queued.

`warp: unpark <id|all-runnable>` (Slack) and `wake.py unpark <id|all-runnable>` reopen by hand.
"""

from __future__ import annotations

import re
from typing import Optional

import terminal

_OUTSIDE = re.compile(r"(owner outside warp|outside warp|outside owner|external owner|owned by .*coordinator|"
                      r"\bcoordinator\b|not warp'?s|another (run|agent|coordinator))", re.I)
_NAMED = re.compile(r"(?:waits? on|waiting on|blocked (?:by|on)|held (?:by|on)|holds? on|depends on)\s+"
                    r"((?:#\d+|[A-Za-z][\w]*(?:-[\w]+)*)(?:\s*(?:,|and|&|/)\s*(?:#\d+|[A-Za-z][\w]*(?:-[\w]+)*))*)")
_TOKEN = re.compile(r"#\d+|[A-Za-z][\w]*(?:-[\w]+)*")
_SHAPED = re.compile(r"^(#\d+|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)$")
_WORDS = {"and", "the", "a", "an", "its", "main", "merge", "review", "ci", "it"}
_TEXT_KEYS = ("parkReason", "next", "note", "notes", "holdReason", "skipReason")
_LIST_KEYS = ("waitsOn", "blockedBy", "parkedOn", "heldOn", "holds")


def _text(ticket: dict) -> str:
    out = []
    for key in _TEXT_KEYS:
        value = ticket.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(str(v) for v in value if isinstance(v, (str, int)))
    return " | ".join(out)


def outside_owner(ticket: dict) -> str:
    """Who owns the ticket outside Warp, or "". `externalOwner`/`owner` fields win over the park text."""
    for key in ("externalOwner", "outsideOwner", "coordinator"):
        value = ticket.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    owner = ticket.get("owner")
    if isinstance(owner, str) and owner.strip() and owner.strip().casefold() not in {"warp", "shuttle"}:
        return owner.strip()
    text = _text(ticket)
    match = _OUTSIDE.search(text)
    if not match:
        return ""
    named = re.search(r"\(([^)]+coordinator[^)]*)\)", text, re.I)
    return named.group(1).strip() if named else "outside Warp"


def named_blockers(ticket: dict, known: Optional[set] = None) -> list:
    """Ticket ids or `#<pr>` the park names as what it waits on: a known ticket id, `#<n>`, or an id-shaped word
    (`UI-12`, `G-AUTH-RBAC`) after "waits on", "blocked by", "held on", "depends on"."""
    known = known or set()
    found: list = []
    for key in _LIST_KEYS:
        value = ticket.get(key)
        values = value if isinstance(value, list) else ([value] if isinstance(value, str) else [])
        for item in values:
            for token in _TOKEN.findall(str(item)):
                if token not in found:
                    found.append(token)
    for match in _NAMED.finditer(_text(ticket)):
        for token in _TOKEN.findall(match.group(1)):
            if token.casefold() in _WORDS:
                continue
            if (token in known or _SHAPED.match(token)) and token not in found:
                found.append(token)
    own = str(ticket.get("id") or "")
    return [token for token in found if token != own]


def _pr_number(url: str) -> str:
    match = re.search(r"/pull/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def _by_pr(tickets: dict) -> dict:
    out = {}
    for tid, row in tickets.items():
        if isinstance(row, dict) and isinstance(row.get("pr"), dict):
            number = _pr_number(row["pr"].get("url") or "")
            if number:
                out["#" + number] = tid
    return out


def blocker_state(data: dict, ticket: dict) -> tuple:
    """(names, open_names): what it waits on, and which of those are not terminal yet. An unknown name is open."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    prs = _by_pr(tickets)
    merged_prs = set(str(n) for n in (data.get("mergedPrs") or []))
    names = named_blockers(ticket, set(tickets))
    still = []
    for name in names:
        tid = prs.get(name, name)
        row = tickets.get(tid)
        if isinstance(row, dict):
            if not terminal.is_terminal(row):
                still.append(name)
        elif not (name.startswith("#") and name[1:] in merged_prs):
            still.append(name)
    return names, still


def _reopen(ticket: dict, why: str, now_s: str) -> None:
    ticket["status"] = "queued"
    ticket.pop("phase", None)
    ticket["alarm"] = None
    ticket["agent"] = None
    ticket["attempts"] = 0
    ticket["unparkedAt"] = now_s
    ticket["unparkReason"] = why
    for key in ("parkReason",):
        if ticket.get(key):
            ticket["lastParkReason"] = ticket.pop(key)
    shuttle = ticket.get("shuttle")
    if isinstance(shuttle, dict):
        for key in ("pending", "step", "startedAt", "launching", "launchLines"):
            shuttle.pop(key, None)


def reconsider(data: dict, ticket: dict, snap: Optional[dict], lines: list, now_s: str = "") -> str:
    """One parked or external row. Returns the new status when it changed, else "". The caller merges with
    pipeline.finish_merge when this returns `merged` (it needs the pass's lines and Jira moves)."""
    status = str(ticket.get("status") or "")
    if status not in {"parked", "external"}:
        return ""
    tid = str(ticket.get("id") or "")
    snap = snap or {}
    state = str(snap.get("prState") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        closed = pr.get("url") or ""
        reason = terminal.skip_reason(ticket)
        if terminal.terminal_skip({"status": "skipped", "skipReason": reason}):
            ticket["status"] = "skipped"
            ticket["skipReason"] = reason
            ticket["phase"] = "parked"
            lines.append("unpark: %s pull request %s closed unmerged (%s); skipped" % (tid, closed, reason))
            return "skipped"
        if closed:
            ticket.setdefault("closedPrs", []).append(closed)
            ticket["pr"] = {"closedUrl": closed}
        _reopen(ticket, "pull request %s closed unmerged" % closed, now_s)
        lines.append("unpark: %s pull request %s was closed without a merge; queued again, so it starts fresh" % (tid, closed))
        return "queued"
    names, still = blocker_state(data, ticket)
    if names and not still and status == "parked":
        _reopen(ticket, "blockers merged: %s" % ", ".join(names), now_s)
        lines.append("unpark: %s waited on %s, now merged; queued, and it starts in this pass when a slot is free"
                     % (tid, ", ".join(names)))
        return "queued"
    owner = outside_owner(ticket)
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if status == "parked" and owner and (state == "OPEN" or (pr.get("url") and not state)):
        ticket["status"] = "external"
        ticket["phase"] = "external"
        ticket["externalOwner"] = owner
        ticket["externalSince"] = now_s
        lines.append("unpark: %s pull request %s is open and owned by %s; external (kept open, polled for merge and "
                     "close, never dispatched)" % (tid, pr.get("url") or "?", owner))
        return "external"
    return ""


def runnable(data: dict, ticket: dict) -> bool:
    """A parked row that can start again: no open pull request owned elsewhere, every named blocker terminal."""
    if str(ticket.get("status") or "") != "parked":
        return False
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if outside_owner(ticket) and pr.get("url"):
        return False
    _names, still = blocker_state(data, ticket)
    return not still


def unpark(data: dict, which: str, now_s: str) -> list:
    """`warp: unpark <id|all-runnable>`. Lines say what reopened and what did not, and why."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    which = str(which or "").strip()
    if not which:
        return ["unpark: name a ticket id or all-runnable"]
    lines = []
    if which.casefold() in {"all-runnable", "all", "runnable"}:
        picked = [tid for tid, t in sorted(tickets.items()) if isinstance(t, dict) and runnable(data, t)]
        held = [tid for tid, t in sorted(tickets.items())
                if isinstance(t, dict) and t.get("status") == "parked" and tid not in picked]
        for tid in picked:
            _reopen(tickets[tid], "warp: unpark all-runnable", now_s)
        lines.append("unpark: reopened %d (%s)%s" % (
            len(picked), ", ".join(picked[:20]) + (" …" if len(picked) > 20 else "") if picked else "none",
            ("; %d stay parked (an open pull request owned elsewhere, or a blocker not merged): %s"
             % (len(held), ", ".join(held[:10]) + (" …" if len(held) > 10 else ""))) if held else ""))
        return lines
    ticket = tickets.get(which)
    if not isinstance(ticket, dict):
        return ["unpark: no ticket %s" % which]
    if ticket.get("status") not in {"parked", "external", "skipped"}:
        return ["unpark: %s is %s, not parked; left as is" % (which, ticket.get("status"))]
    _reopen(ticket, "warp: unpark %s" % which, now_s)
    return ["unpark: %s reopened (queued); it starts when its deps, locks and a slot allow" % which]
