#!/usr/bin/env python3
"""Gap tickets: create, add to the beam, staff, no approval needed (1.7.15).

A gap is any blocker with no ticket: a main red, a live-run product gap, an audit finding, a Bugbot finding outside
the pull request's scope, a GAP-REPORT row nobody owns, a RESULT.json follow-up.

  1. dedupe   by label warp:<id>, by summary, and by GAP-REPORT row (`gapRow`); a match is linked, not created
  2. beam     wake.add_ticket, id G-<AREA>-<SHORT>, with `blocks` edges to the waiting tickets (they get a hold that
              names the gap and lifts when it merges)
  3. Jira     the parent creates the issue with label warp:<id> and summary "[<id>] …" (MUST DO), then records the key
  4. ledger   a row in `.warp/gap-owners.md`
  5. staff    at once when its blocker score is in the top `gaps.autoStaffTopN` (rank 0, priority high); else queued

Batching (1.7.15): `extend` adds a new failure to an open gap instead of opening a second one. All of main's red
tests from one break are one gap and one fix pull request, so CI runs once for the whole fix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

LEDGER = "gap-owners.md"


def gap_id(area: str, short: str) -> str:
    clean = lambda text, n: re.sub(r"[^A-Z0-9]+", "-", str(text or "").upper()).strip("-")[:n] or "X"
    return "G-%s-%s" % (clean(area, 12), clean(short, 24))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def find(data: dict, gid: str = "", summary: str = "", row: str = "") -> str:
    """An existing ticket for this gap: same id or label, same summary, or the same GAP-REPORT row."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if gid and gid in tickets:
        return gid
    want = _norm(summary)
    for tid, t in tickets.items():
        if not isinstance(t, dict):
            continue
        if gid and ("warp:%s" % gid) in (t.get("labels") or []):
            return tid
        if want and _norm(re.sub(r"^\[[^\]]+\]\s*", "", str(t.get("summary") or ""))) == want:
            return tid
        if row and str(t.get("gapRow") or "") == row:
            return tid
    return ""


def create(beam_path: Path, data_reader, area: str, short: str, summary: str, blocks: Optional[list] = None,
           acs: Optional[list] = None, locks: Optional[list] = None, source: str = "", row: str = "",
           score_rank: Optional[int] = None, top_n: int = 10, now=None) -> tuple:
    """(gap id, lines). `data_reader()` returns the current beam (the beam is written under the run lock)."""
    import beam as beam_mod
    import beamlock
    import wake

    path = Path(beam_path)
    gid = gap_id(area, short)
    data = data_reader()
    found = find(data, gid, summary, row)
    lines: list = []
    if found:
        lines.extend(_hold(path, found, blocks or [], now))
        return found, ["gap: %s already covers it (%s); linked%s" % (found, summary[:80],
                                                                  (", holds on %s" % ", ".join(blocks)) if blocks else "")] + lines
    lines.extend(wake.add_ticket(path, gid, summary, from_id=(blocks or [""])[0], locks=locks or [], size="M",
                                 module=area))
    with beamlock.run_lock(path):
        data = beam_mod.load_json(path)
        ticket = data["tickets"].get(gid)
        if isinstance(ticket, dict):
            ticket["labels"] = sorted(set((ticket.get("labels") or []) + ["warp:%s" % gid]))
            ticket["gap"] = {"area": area, "source": source, "blocks": list(blocks or [])}
            ticket["acs"] = [{"id": "AC-%d" % (n + 1), "text": text} for n, text in enumerate(acs or [])] or ticket.get("acs") or []
            if row:
                ticket["gapRow"] = row
            staffed = score_rank is not None and score_rank < top_n
            if staffed:
                ticket["rank"] = 0
                ticket["priority"] = "high"
            beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
        else:
            staffed = False
    lines.extend(_hold(path, gid, blocks or [], now))
    _ledger(path, gid, area, summary, source, now)
    lines.append("gap: %s created (%s): %s%s" % (gid, source or area, summary[:120],
                                                 "; staffed at the top of the queue" if staffed else "; queued"))
    return gid, lines


def extend(beam_path: Path, gid: str, acs: list, note: str = "") -> list:
    """Add acceptance criteria (new failures) to an open gap: one fix round covers them all (1.7.15)."""
    import beam as beam_mod
    import beamlock

    path = Path(beam_path)
    with beamlock.run_lock(path):
        data = beam_mod.load_json(path)
        ticket = (data.get("tickets") or {}).get(gid)
        if not isinstance(ticket, dict):
            return []
        have = {str(ac.get("text") if isinstance(ac, dict) else ac) for ac in ticket.get("acs") or []}
        added = [text for text in acs if text not in have]
        if not added:
            return []
        rows = list(ticket.get("acs") or [])
        for text in added:
            rows.append({"id": "AC-%d" % (len(rows) + 1), "text": text})
        ticket["acs"] = rows
        ticket["gapExtendedAt"] = beam_mod.utcnow()
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    return ["gap: %s now also covers %s%s (one fix, one push, one CI run)" % (gid, "; ".join(added[:5]),
                                                                              (" — %s" % note) if note else "")]


def _hold(path: Path, gid: str, waiting: list, now=None) -> list:
    import beam as beam_mod
    import beamlock

    if not waiting:
        return []
    out = []
    with beamlock.run_lock(path):
        data = beam_mod.load_json(path)
        tickets = data.get("tickets") or {}
        for tid in waiting:
            t = tickets.get(tid)
            if not isinstance(t, dict):
                continue
            holds = list(t.get("holds") or [])
            if gid not in holds:
                holds.append(gid)
                t["holds"] = holds
                out.append("hold: %s held on %s; it lifts when %s merges" % (tid, gid, gid))
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n", data=data)
    return out


def _ledger(path: Path, gid: str, area: str, summary: str, source: str, now=None) -> None:
    import beam as beam_mod

    ledger = Path(path).parent / LEDGER
    if not ledger.is_file():
        ledger.write_text("# Gap owners\n\n| gap | area | owner | source | summary | since |\n|---|---|---|---|---|---|\n")
    with ledger.open("a") as handle:
        handle.write("| %s | %s | warp | %s | %s | %s |\n" % (gid, area, source, summary.replace("|", "/")[:120],
                                                              beam_mod.coerce_now(now)[1]))


def lift_holds(data: dict, merged: str) -> list:
    """Every hold that names `merged` (a ticket id, a gap id, or `#<pr>`) is lifted. Lines name what is free."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    names = {merged}
    row = tickets.get(merged)
    if isinstance(row, dict):
        number = re.search(r"/pull/(\d+)", str((row.get("pr") or {}).get("url") or ""))
        if number:
            names.add("#" + number.group(1))
    out = []
    for tid, t in sorted(tickets.items()):
        if not isinstance(t, dict):
            continue
        holds = list(t.get("holds") or [])
        kept = [h for h in holds if str(h) not in names]
        if len(kept) != len(holds):
            t["holds"] = kept
            if t.get("status") == "blocked" and not kept:
                t["status"] = "queued"
            out.append("hold: lifted %s on %s (%s merged)%s" % (", ".join(sorted(names & set(map(str, holds)))), tid,
                                                                merged, "" if kept else "; nothing holds it now"))
    return out


import json  # noqa: E402  (used by create and the helpers above)
