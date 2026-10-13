#!/usr/bin/env python3
"""The planned picture of a ticket graph: slot, start and merge per ticket (build-schedule/2).

Same rule as Warp's dispatch, on a continuous estimate-hour clock:
  - `slots` agents; a ready ticket takes the lowest free slot;
  - ready = every in-run blocker merged and, with gate order strict, every earlier gate block
    merged (a block is a ticket's gate, else the highest block among its blockers, else the first);
  - two tickets in flight never share a lock path (a parent folder overlaps its children);
  - priority = critical chain first, then the path of a priority gate, then rank (ascending
    latest start), then natural id;
  - a size outside autoMergeSizes waits `approval` hours after it finishes before it merges.

Used by /warp-from-jira (jira_pull.py) to write a schedule.json from Jira issues.
"""

from __future__ import annotations

import re
from collections import defaultdict

SIZE_HOURS = {"S": 4, "M": 7, "L": 11, "XL": 16}
EPS = 1e-9


def natkey(s) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(s))]


def overlap(a: str, b: str) -> bool:
    a, b = str(a).strip().rstrip("/"), str(b).strip().rstrip("/")
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def gate_keys(keys) -> list:
    """G-number keys in numeric order, then the other keys in the order given."""
    keys = list(dict.fromkeys(str(k) for k in keys if k))
    nums = sorted((k for k in keys if re.fullmatch(r"(?i)G\d+", k)), key=lambda k: int(k[1:]))
    return nums + [k for k in keys if k not in nums]


def find_cycle(deps: dict) -> list:
    """One dependency cycle as [a, b, ..., a], or [] when there is none."""
    state, stack = {}, []

    def visit(i):
        state[i] = 1
        stack.append(i)
        for d in deps.get(i, []):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d in deps and not state.get(d):
                got = visit(d)
                if got:
                    return got
        stack.pop()
        state[i] = 2
        return []

    for i in sorted(deps, key=natkey):
        if not state.get(i):
            got = visit(i)
            if got:
                return got
    return []


def blocks_of(ids: list, deps: dict, gate_of: dict, order: list) -> dict:
    """Warp's block rule (blocks.py): own gate, else the highest block among blockers, else the first."""
    rank = {k: n for n, k in enumerate(order)}
    found = {}

    def block(i, seen):
        if i in found:
            return found[i]
        g = gate_of.get(i)
        if g in rank:
            k = g
        else:
            k = order[0] if order else None
            for d in deps.get(i, []):
                if d in seen:
                    continue
                o = block(d, seen | {i})
                if o is not None and (k is None or rank[o] > rank[k]):
                    k = o
        found[i] = k
        return k

    for i in ids:
        block(i, frozenset())
    return found


def plan(tickets: list, gates: list, slots: int = 18, auto_sizes=("S", "M"), approval: float = 0.5, strict: bool = True) -> dict:
    """tickets: [{id, hours, size, after (in-run ids), locks, gate}]; gates: [{key, members, priority}].

    Returns per-ticket rows (slot 1..slots, start, end, merge, ready, waited, why, rank, latest_start,
    critical, wave, priority_gate), the critical chain, the steps, gate times and totals.
    """
    T = {str(t["id"]): t for t in tickets}
    ids = sorted(T, key=natkey)
    deps = {i: [d for d in T[i].get("after") or [] if d in T and d != i] for i in ids}
    cyc = find_cycle(deps)
    if cyc:
        raise ValueError("dependency cycle: " + " → ".join(cyc))
    kids = defaultdict(list)
    for i, ds in deps.items():
        for d in ds:
            kids[d].append(i)
    hours = {i: float(T[i].get("hours") or SIZE_HOURS.get(str(T[i].get("size") or "M").upper(), 7)) for i in ids}
    autos = {str(s).upper() for s in (auto_sizes or [])}
    wait = {i: 0.0 if str(T[i].get("size") or "M").upper() in autos else float(approval) for i in ids}
    locks = {i: [str(p) for p in T[i].get("locks") or [] if str(p).strip()] for i in ids}

    # longest remaining chain -> latest start, critical chain, rank
    tail = {}

    def tl(i):
        if i not in tail:
            tail[i] = hours[i] + wait[i] + max((tl(k) for k in kids[i]), default=0.0)
        return tail[i]

    for i in ids:
        tl(i)
    cp_len = max(tail.values(), default=0.0)
    latest = {i: cp_len - tail[i] for i in ids}
    chain = []
    if ids:
        heads = [i for i in ids if not deps[i]]
        cur = max(heads, key=lambda i: (tail[i], [-n for n in natkey(i) if isinstance(n, int)]))
        chain = [cur]
        while kids[cur]:
            cur = max(kids[cur], key=lambda k: (tail[k], [-n for n in natkey(k) if isinstance(n, int)]))
            chain.append(cur)
    crit = set(chain)
    rank = {i: n + 1 for n, i in enumerate(sorted(ids, key=lambda i: (latest[i], natkey(i))))}

    # priority gates: members and their ancestors
    anc = {}

    def A(i):
        if i not in anc:
            s = set()
            for d in deps[i]:
                s |= {d} | A(d)
            anc[i] = s
        return anc[i]

    pgate = {}
    order = gate_keys(g["key"] for g in gates)
    gate_by = {g["key"]: g for g in gates}
    for k in order:
        g = gate_by[k]
        if not g.get("priority"):
            continue
        for m in g.get("members") or []:
            if m in T:
                for i in {m} | A(m):
                    pgate.setdefault(i, k)
    prio = lambda i: (0 if i in crit else 1, 0 if i in pgate else 1, rank[i], natkey(i))

    gate_of = {i: T[i].get("gate") or None for i in ids}
    block = blocks_of(ids, deps, gate_of, order)
    brank = {k: n for n, k in enumerate(order)}
    left = defaultdict(int)
    for i in ids:
        left[block[i]] += 1

    pending = set(ids)
    start, end, merge, slot, ready_at, why = {}, {}, {}, {}, {}, {}
    cap = max(1, int(slots or 1))
    free = [0.0] * cap
    running = []
    now = 0.0
    steps = []
    guard = 0
    while pending and guard < 100000:
        guard += 1
        merged_now = []
        for r in list(running):
            if merge[r] <= now + EPS:
                running.remove(r)
                left[block[r]] -= 1
                merged_now.append(r)

        def open_block(i):
            if not strict or len(order) < 2 or block[i] is None:
                return True
            return all(left[k] == 0 for k in order if brank[k] < brank[block[i]])

        ready = [i for i in pending if all(d in merge and merge[d] <= now + EPS for d in deps[i]) and open_block(i)]
        for i in ready:
            ready_at.setdefault(i, now)
        ready.sort(key=prio)
        starts, held = [], {}
        for i in ready:
            s = next((n for n, f in enumerate(free) if f <= now + EPS), None)
            if s is None:
                break
            holders = [r for r in running if any(overlap(a, b) for a in locks[i] for b in locks[r])]
            if holders:
                held[i] = sorted(holders, key=natkey)
                continue
            slot[i], start[i] = s + 1, now
            end[i] = now + hours[i]
            merge[i] = end[i] + wait[i]
            free[s] = end[i]
            running.append(i)
            pending.discard(i)
            starts.append(i)
            dd = [d for d in deps[i] if abs(merge[d] - now) < EPS]
            if not T[i].get("after"):
                why[i] = "no blockers"
            elif now - ready_at[i] > EPS:
                why[i] = f"queued since T+{ready_at[i]:g}h"
            elif dd:
                why[i] = "unblocked by " + ", ".join(sorted(dd, key=natkey))
            elif not deps[i]:
                why[i] = "blocker in another run, met when this run starts"
            else:
                why[i] = "its gate block opened"
        queued = sorted([i for i in ready if i in pending and i not in held], key=prio)
        steps.append({"t": now, "merges": sorted(merged_now, key=natkey), "starts": starts, "queued": queued, "held": held,
                      "running": sorted(running, key=lambda r: slot[r]), "free": sum(1 for f in free if f <= now + EPS)})
        nxt = [f for f in free if f > now + EPS] + [merge[r] for r in running if merge[r] > now + EPS]
        if not nxt:
            if pending:
                raise ValueError("cannot place: " + ", ".join(sorted(pending, key=natkey)))
            break
        now = min(nxt)
    while running:
        now = min(merge[r] for r in running)
        merged_now = [r for r in running if abs(merge[r] - now) < EPS]
        for r in merged_now:
            running.remove(r)
            left[block[r]] -= 1
        steps.append({"t": now, "merges": sorted(merged_now, key=natkey), "starts": [], "queued": [], "held": {},
                      "running": sorted(running, key=lambda r: slot[r]), "free": cap - len(running)})
    out = []
    for s in steps:
        if out and abs(out[-1]["t"] - s["t"]) < EPS:
            o = out[-1]
            o["merges"] += [m for m in s["merges"] if m not in o["merges"]]
            o["starts"] += s["starts"]
            o["queued"], o["held"], o["running"], o["free"] = s["queued"], s["held"], s["running"], s["free"]
        else:
            out.append(s)
    wave = {}

    def wv(i):
        if i not in wave:
            wave[i] = 1 + max((wv(d) for d in deps[i]), default=0)
        return wave[i]

    for i in ids:
        wv(i)
    gate_at, closes = {}, {}
    for k in order:
        mem = [m for m in gate_by[k].get("members") or [] if m in merge]
        if mem:
            at = max(merge[m] for m in mem)
            gate_at[k] = at
            closes[k] = sorted([m for m in mem if abs(merge[m] - at) < EPS], key=natkey)
    for s in out:
        s["gates"] = [k for k, at in gate_at.items() if abs(at - s["t"]) < EPS]
    rows = {i: {"slot": slot[i], "start": start[i], "end": end[i], "merge": merge[i], "ready": ready_at[i],
                "waited": start[i] - ready_at[i], "why": why[i], "rank": rank[i], "latest_start": latest[i],
                "critical": i in crit, "wave": wave[i], "priority_gate": pgate.get(i, ""), "block": block[i],
                "unlocks": sorted(kids[i], key=lambda k: rank[k])} for i in ids}
    finish = max(merge.values(), default=0.0)
    peak = max((len(s["running"]) for s in out), default=0)
    return {"tickets": rows, "chain": chain, "steps": out, "finish": finish, "cp_len": cp_len, "peak": peak,
            "waves": max(wave.values(), default=0), "gate_at": gate_at, "closes_with": closes, "order": order,
            "agent_hours": sum(hours.values()), "slots": cap}
