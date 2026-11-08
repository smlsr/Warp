#!/usr/bin/env python3
"""The blocker graph (1.7.15): what holds what up, and which blockers to work first.

Edges (blocker -> waiting) come from:
  stack parents      a pull request whose base is another ticket's branch waits on that ticket
  live holds         `holds` / `hold` / `heldOn` on a ticket: "#X held on G-Y / #Z"
  main-red waivers   a waiver's fix gap blocks every open pull request red on the waived test
  migration order    tickets touching migrations/00NN_*: a lower number blocks a higher one in the same folder
  deps and notes     `deps`, and "waits on" / "blocked by" in the park or hold text

Score = transitive dependents x age in hours (at least 1). The tick works the top `blockers.topN` (10) first: they are
merged first in the lane, refreshed first, staffed first.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_MIGRATION = re.compile(r"(?:^|/)(migrations?|migrate|db/migrate)/(\d{3,5})[_-]", re.I)


def _pr_number(url: str) -> str:
    match = re.search(r"/pull/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def _age_hours(ticket: dict, now: datetime) -> float:
    import beam as beam_mod

    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    for key in (pr.get("openedAt"), ticket.get("addedAt"), ticket.get("createdAt"), ticket.get("updatedAt")):
        ts = beam_mod.parse_ts(str(key or ""))
        if ts is not None:
            return max(1.0, (now - ts).total_seconds() / 3600.0)
    return 1.0


def edges(data: dict) -> dict:
    """{blocker id: set(waiting ids)} over ticket ids (a `#<n>` that is a ticket's pull request becomes its id)."""
    import unpark

    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    by_pr = {}
    by_branch = {}
    for tid, t in tickets.items():
        if not isinstance(t, dict):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        number = _pr_number(pr.get("url") or "")
        if number:
            by_pr["#" + number] = tid
        for branch in (t.get("branch"), pr.get("branch"), pr.get("headRef")):
            if branch:
                by_branch[str(branch)] = tid
    out: dict = {}

    def add(blocker: str, waiting: str) -> None:
        blocker = by_pr.get(blocker, blocker)
        if blocker and waiting and blocker != waiting:
            out.setdefault(blocker, set()).add(waiting)

    known = set(tickets)
    for tid, t in tickets.items():
        if not isinstance(t, dict):
            continue
        import terminal

        if terminal.is_terminal(t):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        base = str(pr.get("base") or pr.get("baseRef") or "")
        if base and base in by_branch:
            add(by_branch[base], tid)
        for dep in t.get("deps") or []:
            add(str(dep), tid)
        for name in unpark.named_blockers(t, known):
            add(name, tid)
        for key in ("holds", "hold", "heldOn"):
            value = t.get(key)
            values = value if isinstance(value, list) else [value] if value else []
            for item in values:
                for token in re.findall(r"#\d+|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+", str(item)):
                    add(token, tid)
    # Waivers: the fix gap blocks the pull requests red on the waived tests.
    for waiver in (data.get("waivers") or {}).values() if isinstance(data.get("waivers"), dict) else []:
        gap = str(waiver.get("gap") or "")
        tests = set(waiver.get("tests") or [])
        for tid, t in tickets.items():
            failed = set(((t.get("pr") or {}).get("failedChecks") or [])) if isinstance(t, dict) else set()
            if gap and tests & failed:
                add(gap, tid)
    # Migration order.
    chains: dict = {}
    for tid, t in tickets.items():
        if not isinstance(t, dict) or terminal.is_terminal(t):
            continue
        for path in list(t.get("locks") or []) + list(t.get("files") or []):
            match = _MIGRATION.search(str(path))
            if match:
                chains.setdefault(match.group(1).lower(), []).append((int(match.group(2)), tid))
    for rows in chains.values():
        rows.sort()
        for (n1, a), (n2, b) in zip(rows, rows[1:]):
            if n1 < n2 and a != b:
                add(a, b)
    return out


def dependents(graph: dict, node: str) -> set:
    seen: set = set()
    stack = list(graph.get(node, ()))
    while stack:
        cur = stack.pop()
        if cur in seen or cur == node:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, ()))
    return seen


def ranked(data: dict, now=None) -> list:
    """[(blocker, score, [dependents])], highest score first. Terminal blockers are left out (they block nothing)."""
    import beam as beam_mod
    import terminal

    now_dt = beam_mod.coerce_now(now)[0]
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    graph = edges(data)
    out = []
    for node in graph:
        row = tickets.get(node)
        if isinstance(row, dict) and terminal.is_terminal(row):
            continue
        deps = dependents(graph, node)
        if not deps:
            continue
        age = _age_hours(row, now_dt) if isinstance(row, dict) else 1.0
        out.append((node, round(len(deps) * age, 1), sorted(deps)))
    out.sort(key=lambda row: (-row[1], row[0]))
    return out


def scores(data: dict, now=None) -> dict:
    return {node: score for node, score, _deps in ranked(data, now=now)}


def top(data: dict, n: int, now=None) -> list:
    return ranked(data, now=now)[: max(0, int(n))]


def lines(data: dict, n: int, now=None) -> list:
    rows = top(data, n, now=now)
    if not rows:
        return ["blockers: none (nothing waits on an unmerged ticket)"]
    return ["blockers: top %d: %s" % (len(rows), "; ".join("%s (score %s, holds up %d: %s%s)" % (
        node, score, len(deps), ", ".join(deps[:4]), " …" if len(deps) > 4 else "") for node, score, deps in rows))]
