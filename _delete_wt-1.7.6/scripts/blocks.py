#!/usr/bin/env python3
"""Run a plan in blocks: gates in order, and a range of them.

A plan's work comes in gates: G0, G1, G2 and so on. A ticket belongs to one
block, the gate it names, else the lowest gate that lists it as a member, else
the highest block among the tickets it depends on. A ticket with none of those
belongs to the first block. A Bugbot follow-up (`followUpOf`) belongs to its
parent's block and never holds a block that was cleared: it fixes work already
merged, so the plan does not wait on it. A range is not complete while one is open.

With the default `gateOrder: strict`, a ticket in block N does not start until
every block before it is cleared. A block is cleared when every ticket in it is
merged, done, or skipped and its gate is not red. A parked ticket holds the
blocks after it. `gateOrder: deps` turns that off: only dependencies and a
blocking gate that is not green hold a ticket back, as before. Open tickets
always finish. This only decides what starts.

`/warp-start` takes an optional range:

  /warp-start             no range: run until stopped, or until the end
  /warp-start G0-         G0 through the last block
  /warp-start G0-G3       G0 through G3
  /warp-start -G2         from the first block that is not cleared, through G2
  /warp-start G5          G5 only

A range is kept on the beam as `range`. When every block in it is cleared the
run pauses with a summary of each block. A start with no range clears it.

  python3 scripts/blocks.py ?
  python3 scripts/blocks.py show --beam .warp/beam.json
  python3 scripts/blocks.py check --beam .warp/beam.json

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
TERMINAL = {"merged", "done", "skipped"}
GATE_RE = re.compile(r"^G(\d+)$", re.I)
RANGE_RE = re.compile(r"^\s*(?:G?(\d+))?\s*(-|\.\.|:)?\s*(?:G?(\d+))?\s*$", re.I)
ORDERS = ("strict", "deps")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_range(text: str) -> bool:
    """True when `text` is a gate range: G3, G0-, -G2, G0-G3."""
    value = str(text or "").strip()
    if not value or not re.search(r"\d", value):
        return False
    return bool(RANGE_RE.match(value))


def keys(beam: dict) -> list:
    """Gate keys in run order.

    When every gate carries `at` (build-schedule/2: the hour it is reached in the plan), that order,
    so a named gate such as G-RBAC sits where the plan reaches it. Otherwise G0, G1, G2 by number,
    then any other key in plan order.
    """
    rows = [g for g in beam.get("gates") or [] if isinstance(g, dict) and g.get("key")]
    found = [str(g.get("key")) for g in rows]
    ats = [g.get("at") for g in rows]
    if rows and all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in ats):
        idx = {k: i for i, k in enumerate(found)}
        num = lambda k: int(GATE_RE.match(k).group(1)) if GATE_RE.match(k) else 10**6
        return [k for _, k in sorted(zip(ats, found), key=lambda p: (p[0], num(p[1]), idx[p[1]]))]
    numbered = sorted((k for k in found if GATE_RE.match(k)), key=lambda k: int(GATE_RE.match(k).group(1)))
    return numbered + [k for k in found if not GATE_RE.match(k)]


def order_mode(beam: dict) -> str:
    """beam gateOrder, else config gateOrder, else the schedule's (build-schedule/2: deps), else strict."""
    raw = beam.get("gateOrder") or (beam.get("config") or {}).get("gateOrder") or (beam.get("program") or {}).get("gateOrder") or "strict"
    text = str(raw).strip().lower()
    return text if text in ORDERS else "strict"


def _blockers(ticket: dict) -> list:
    found = []
    for key in ("deps", "after", "blockedBy"):
        raw = ticket.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        found.extend(str(x).strip() for x in raw if str(x).strip() and ":" not in str(x))
    return found


def assign(beam: dict) -> dict:
    """Ticket id -> block key, or None when the plan has no gates."""
    order = keys(beam)
    tickets = beam.get("tickets") or {}
    if not order:
        return {tid: None for tid in tickets}
    rank = {k: i for i, k in enumerate(order)}
    members: dict = {}
    for gate in beam.get("gates") or []:
        key = str(gate.get("key"))
        for tid in gate.get("members") or []:
            held = members.get(str(tid))
            if held is None or rank.get(key, 0) < rank.get(held, 0):
                members[str(tid)] = key
    found: dict = {}

    def block(tid: str, seen: tuple) -> str:
        if tid in found:
            return found[tid]
        row = tickets.get(tid) or {}
        explicit = str(row.get("gate") or "")
        parent = str(row.get("followUpOf") or "")
        if parent and parent in tickets and parent not in seen:
            # A Bugbot follow-up sits in its parent's block (it landed in G0 before 1.6.16).
            found[tid] = block(parent, seen + (tid,))
        elif explicit in rank:
            found[tid] = explicit
        elif tid in members:
            found[tid] = members[tid]
        else:
            best = order[0]
            for dep in _blockers(row):
                if dep in tickets and dep not in seen:
                    other = block(dep, seen + (tid,))
                    if rank[other] > rank[best]:
                        best = other
            found[tid] = best
        return found[tid]

    for tid in tickets:
        block(str(tid), ())
    return found


def states(beam: dict, mapping: Optional[dict] = None) -> list:
    """One row per block, in run order: its tickets, counts, and whether it is cleared."""
    order = keys(beam)
    mapping = mapping if mapping is not None else assign(beam)
    tickets = beam.get("tickets") or {}
    gates = {str(g.get("key")): g for g in beam.get("gates") or []}
    rows = []
    for key in order:
        ids = sorted(tid for tid, k in mapping.items() if k == key)
        counts = {"merged": 0, "open": 0, "parked": 0, "queued": 0}
        plan_ids = [tid for tid in ids if not (tickets.get(tid) or {}).get("followUpOf")]
        follow_open = [tid for tid in ids if (tickets.get(tid) or {}).get("followUpOf")
                       and (tickets.get(tid) or {}).get("status") not in TERMINAL and (tickets.get(tid) or {}).get("status") != "parked"]
        plan_merged = sum(1 for tid in plan_ids if (tickets.get(tid) or {}).get("status") in TERMINAL)
        minutes = tokens = 0
        cost = 0.0
        for tid in ids:
            row = tickets.get(tid) or {}
            status = row.get("status")
            if status in TERMINAL:
                counts["merged"] += 1
            elif status == "parked":
                counts["parked"] += 1
            elif status in {"queued", "blocked", None}:
                counts["queued"] += 1
            else:
                counts["open"] += 1
            minutes += int(row.get("minutes") or 0)
            tokens += int(row.get("tokens") or 0)
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            try:
                cost += float(usage.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        gate = gates.get(key) or {}
        red = gate.get("status") == "red"
        rows.append(
            {
                "key": key,
                "tickets": ids,
                "total": len(ids),
                **counts,
                "gate": gate.get("status") or "pending",
                # Follow-ups do not hold a cleared block; they are counted and listed on their own.
                "cleared": plan_merged == len(plan_ids) and not red,
                "followUps": follow_open,
                "minutes": minutes,
                "tokens": tokens,
                "cost": round(cost, 2),
            }
        )
    return rows


def span(beam: dict) -> tuple:
    """(low index or None, high index or None) of the range on the beam."""
    order = keys(beam)
    raw = beam.get("range") if isinstance(beam.get("range"), dict) else None
    if not raw or not order:
        return None, None
    lo = order.index(raw["from"]) if raw.get("from") in order else None
    hi = order.index(raw["to"]) if raw.get("to") in order else None
    return lo, hi


def plan(beam: dict) -> Optional[dict]:
    """What ready() needs, or None when the plan has no gates."""
    order = keys(beam)
    if not order:
        return None
    mapping = assign(beam)
    rows = states(beam, mapping)
    lo, hi = span(beam)
    return {
        "order": order,
        "rank": {k: i for i, k in enumerate(order)},
        "assign": mapping,
        "rows": rows,
        "lo": lo,
        "hi": hi,
        "strict": order_mode(beam) == "strict",
    }


def hold_reason(info: Optional[dict], tid: str) -> Optional[str]:
    """Why this ticket may not start yet, or None."""
    if not info:
        return None
    key = info["assign"].get(tid)
    if key is None:
        return None
    k = info["rank"][key]
    lo, hi = info["lo"], info["hi"]
    if lo is not None and k < lo:
        return "outside the range: %s is before %s" % (key, info["order"][lo])
    if hi is not None and k > hi:
        return "outside the range: %s is after %s" % (key, info["order"][hi])
    if info["strict"]:
        for j in range(lo or 0, k):
            if not info["rows"][j]["cleared"]:
                return "%s waits on %s" % (key, info["order"][j])
    return None


# --- ranges ----------------------------------------------------------------


def resolve(beam: dict, text: str) -> tuple:
    """(range dict or None, error or None). Empty text means no range."""
    spec = str(text or "").strip()
    if not spec or spec == "-":
        return None, None
    match = RANGE_RE.match(spec)
    if not (match and re.search(r"\d", spec)):
        named = _named_range(beam, spec)
        if named is not None:
            return named
    if not match or not re.search(r"\d", spec):
        return None, "refuse: %r is not a gate range. Use G0-, G0-G3, -G2, or G5." % spec
    order = keys(beam)
    if not order:
        return None, "refuse: this plan has no gates, so /warp-start takes no range."
    a, sep, b = match.groups()
    lo_key = "G%d" % int(a) if a is not None else None
    hi_key = "G%d" % int(b) if b is not None else None
    for key in (lo_key, hi_key):
        if key and key not in order:
            return None, "refuse: gate %s is not in this plan. Gates: %s." % (key, ", ".join(order))
    rows = states(beam)
    if not sep:
        lo_key = hi_key = lo_key or hi_key
    if lo_key is None:
        first = next((i for i, row in enumerate(rows) if not row["cleared"]), None)
        if first is None:
            return None, "refuse: every block is already cleared."
        lo_key = order[first]
    lo = order.index(lo_key)
    hi = order.index(hi_key) if hi_key else len(order) - 1
    if lo > hi:
        return None, "refuse: %s comes after %s. A range runs forward, for example %s-%s." % (lo_key, hi_key, hi_key, lo_key)
    if all(rows[i]["cleared"] for i in range(lo, hi + 1)):
        return None, "refuse: %s through %s is already cleared. Pick a later range." % (lo_key, order[hi])
    shown = lo_key if not sep else "%s-%s" % (a is not None and lo_key or "", b is not None and hi_key or "")
    return {"spec": shown, "from": lo_key, "to": hi_key, "setAt": utcnow()}, None


def _named_range(beam: dict, spec: str):
    """A gate named outright (G-RBAC) or two named with `..` (G0..G-RBAC). None when it is not that form."""
    order = keys(beam)
    lower = {k.casefold(): k for k in order}
    if spec.casefold() in lower:
        lo_key = hi_key = lower[spec.casefold()]
    elif ".." in spec:
        a, _, b = spec.partition("..")
        a, b = a.strip(), b.strip()
        if (a and a.casefold() not in lower) or (b and b.casefold() not in lower) or not (a or b):
            return None
        lo_key = lower[a.casefold()] if a else None
        hi_key = lower[b.casefold()] if b else None
        if lo_key is None:
            rows = states(beam)
            first = next((i for i, row in enumerate(rows) if not row["cleared"]), None)
            if first is None:
                return None, "refuse: every block is already cleared."
            lo_key = order[first]
    else:
        return None
    lo = order.index(lo_key)
    hi = order.index(hi_key) if hi_key else len(order) - 1
    if lo > hi:
        return None, "refuse: %s comes after %s. A range runs forward." % (lo_key, hi_key)
    rows = states(beam)
    if all(rows[i]["cleared"] for i in range(lo, hi + 1)):
        return None, "refuse: %s through %s is already cleared. Pick a later range." % (lo_key, order[hi])
    return {"spec": spec, "from": lo_key, "to": hi_key, "setAt": utcnow()}, None


def apply(beam: dict, text: str) -> tuple:
    """Set or clear the range on the beam. Returns (ok, lines). The caller writes the beam."""
    found, error = resolve(beam, text)
    if error:
        return False, [error]
    if found is None:
        had = beam.pop("range", None)
        if had:
            return True, ["range: none (was %s). The run goes until it is stopped or finished." % (had.get("spec") or "set")]
        return True, []
    beam["range"] = found
    order = keys(beam)
    last = found["to"] or order[-1]
    lines = ["range: %s through %s (%s)." % (found["from"], last, found["spec"])]
    if order_mode(beam) == "strict":
        lines.append("order: strict. A block starts when the one before it is cleared. gateOrder: deps turns that off.")
    return True, lines


def open_line(beam: dict) -> str:
    """One line for start and status: where the blocks stand."""
    info = plan(beam)
    if not info:
        return "blocks: none. The plan has no gates."
    bits = []
    for row in info["rows"]:
        if not row["total"]:
            continue
        if row["cleared"]:
            bits.append("%s cleared" % row["key"])
        else:
            bits.append("%s %d/%d" % (row["key"], row["merged"], row["total"]))
    lo, hi = info["lo"], info["hi"]
    tail = ""
    if lo is not None or hi is not None:
        tail = " range %s-%s." % (info["order"][lo or 0], info["order"][hi] if hi is not None else info["order"][-1])
    return "blocks: %s.%s" % (", ".join(bits) or "no tickets", tail)


def holds(beam: dict) -> list:
    """Lines saying how many queued tickets the block order is holding back, and why."""
    info = plan(beam)
    if not info:
        return []
    counts: dict = {}
    for tid, row in (beam.get("tickets") or {}).items():
        if row.get("status") in {"queued", "blocked"}:
            why = hold_reason(info, str(tid))
            if why:
                counts[why] = counts.get(why, 0) + 1
    return ["hold: %d tickets, %s" % (n, why) for why, n in sorted(counts.items())]


# --- end of a range --------------------------------------------------------


def summary_lines(beam: dict, lo: int, hi: int) -> list:
    info = plan(beam)
    lines = []
    for row in info["rows"][lo : hi + 1]:
        if not row["total"]:
            continue
        lines.append(
            "block %s: %d of %d merged%s, gate %s, %d min, %d tokens, $%.2f"
            % (
                row["key"],
                row["merged"],
                row["total"],
                (", %d parked" % row["parked"]) if row["parked"] else "",
                row["gate"],
                row["minutes"],
                row["tokens"],
                row["cost"],
            )
        )
    return lines


def range_done(beam: dict) -> bool:
    info = plan(beam)
    if not info or not isinstance(beam.get("range"), dict):
        return False
    lo, hi = info["lo"], info["hi"]
    if lo is None:
        return False
    hi = len(info["order"]) - 1 if hi is None else hi
    return all(info["rows"][i]["cleared"] and not info["rows"][i]["followUps"] for i in range(lo, hi + 1))


def check(beam_path: Path) -> list:
    """If the range is cleared, pause the run and say what was done. Else nothing."""
    path = Path(beam_path)
    try:
        beam = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(beam, dict) or beam.get("paused") or (beam.get("runState") or "running") != "running":
        return []
    if not range_done(beam):
        return []
    info = plan(beam)
    lo, hi = info["lo"], info["hi"]
    hi = len(info["order"]) - 1 if hi is None else hi
    first, last = info["order"][lo], info["order"][hi]
    spec = beam["range"].get("spec") or "%s-%s" % (first, last)
    body = summary_lines(beam, lo, hi)
    nxt = info["order"][hi + 1] if hi + 1 < len(info["order"]) else None
    lines = ["range: %s complete. %s through %s are cleared. Pausing." % (spec, first, last)] + body
    if nxt:
        row = info["rows"][hi + 1]
        lines.append("next: %s has %d tickets. /warp-start %s- continues, or /warp-start %s runs it alone." % (nxt, row["total"], nxt, nxt))
    else:
        lines.append("next: that was the last block. /warp-stop writes the completion report.")
    reason = "range %s complete" % spec
    import scan

    scan.set_run(path, "paused", reason)
    lines.append("herald: Range %s complete. %s" % (spec, " ".join(body[:6])))
    return lines


# --- command line ----------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Gate blocks and ranges", epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("show", "check"):
        p = sub.add_parser(name)
        p.add_argument("--beam", default=".warp/beam.json")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if args.cmd == "check":
        for line in check(path):
            print(line)
        return 0
    beam = json.loads(path.read_text())
    print(open_line(beam))
    info = plan(beam)
    if info:
        for row in info["rows"]:
            print(
                "block: %s tickets=%d merged=%d open=%d parked=%d queued=%d gate=%s%s"
                % (row["key"], row["total"], row["merged"], row["open"], row["parked"], row["queued"], row["gate"], " cleared" if row["cleared"] else "")
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
