#!/usr/bin/env python3
"""Warp beam tools: ingest, ready-set, status transitions, ETA, board.

The beam (.warp/beam.json) is the restartable source of truth. Agents call
these scripts; they do not hand-edit the JSON.

  ingest   build a beam from HOS plan/schedule.json
  ready    tickets that may start now (deps, locks, gates, cap)
  set      transition a ticket and append the journal
  spend    add tokens / minutes
  eta      remaining critical path and rough finish
  board    write BOARD.md and board.html
  check    validate beam invariants
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SIZE_CLASS = {"S": "LOW", "M": "MEDIUM", "L": "HIGH", "XL": "CRITICAL"}
AUTO_AT_OR_BELOW = {"S", "M"}  # <= MEDIUM
TERMINAL = {"merged", "done", "skipped"}
ACTIVE = {"claimed", "planning", "coding", "review", "bugbot_running", "fix", "awaiting_approval", "merging"}
STATUSES = [
    "queued",
    "claimed",
    "planning",
    "coding",
    "review",
    "bugbot_running",
    "fix",
    "awaiting_approval",
    "merging",
    "merged",
    "done",
    "blocked",
    "alarm",
    "skipped",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def journal(beam_path: Path, event: dict) -> None:
    path = beam_path.parent / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": utcnow(), **event}
    with path.open("a") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def complexity(size: str, auto_sizes: set[str]) -> str:
    return SIZE_CLASS.get(size, "HIGH")


def auto_merge(size: str, auto_sizes: set[str]) -> bool:
    return size in auto_sizes


def ingest(schedule_path: Path, plan_path: Optional[Path], out: Path, config: dict, previous: Optional[dict] = None) -> dict:
    sched = load_json(schedule_path)
    auto_sizes = set(config.get("autoMergeSizes", ["S", "M"]))
    tickets = {}
    for t in sched["tickets"]:
        size = t.get("size") or "M"
        if t.get("autoMerge") is True:
            merged = True
        elif t.get("autoMerge") is False:
            merged = False
        else:
            merged = auto_merge(size, auto_sizes)
        jira = {"status": t.get("jiraStatus") or None, "lastCommentAt": None}
        if t.get("externalId"):
            jira["externalId"] = t.get("externalId")
        prev = (previous or {}).get(t["id"]) or {}
        prev_jira = prev.get("jira") if isinstance(prev.get("jira"), dict) else {}
        for field in ("id", "cloudId", "lastAttempt"):
            if prev_jira.get(field):
                jira[field] = prev_jira[field]
        tickets[t["id"]] = {
            "id": t["id"],
            "jiraKey": t.get("jiraKey") or None,
            "jiraKeySource": t.get("jiraKeySource"),
            "jiraKeyForced": bool(t.get("jiraKeyForced")),
            "jiraKeyConfidence": t.get("jiraKeyConfidence"),
            "summary": t.get("summary", ""),
            "module": t.get("module"),
            "layer": t.get("layer"),
            "group": t.get("group"),
            "owner": t.get("owner") or t.get("home"),
            "size": size,
            "complexity": complexity(size, auto_sizes),
            "autoMerge": merged,
            "hours": t.get("hours"),
            "deps": list(t.get("deps") or []),
            "unlocks": list(t.get("unlocks") or []),
            "locks": list(t.get("locks") or []),
            "critical": bool(t.get("critical")),
            "rankDays": t.get("rankDays"),
            "gate": t.get("gate") or None,
            "priority": t.get("priority"),
            "acs": t.get("acs"),
            "plannedStart": t.get("startLabel"),
            "plannedMerge": t.get("mergeLabel"),
            "status": "queued",
            "agent": None,
            "branch": None,
            "attempts": 0,
            "tokens": 0,
            "minutes": 0,
            "alarm": None,
            "pr": {
                "url": None,
                "id": None,
                "openedAt": None,
                "reviewedAt": None,
                "bugbot": None,
                "bugbotEvidence": None,
                "ci": None,
                "approvedAt": None,
                "mergedAt": None,
            },
            "jira": jira,
            "updatedAt": None,
        }
    try:
        import jira_sync

        jira_sync.assign_keys(list(tickets.values()), config, out, previous)
    except Exception:
        pass
    gates = []
    for g in sched.get("gates") or []:
        gates.append(
            {
                "key": g["key"],
                "name": g.get("name"),
                "blocking": bool(g.get("blocking", True)),
                "members": list(g.get("members") or []),
                "checks": list(g.get("checks") or []),
                "ifRed": g.get("ifRed"),
                "status": "pending",
                "evidence": None,
                "greenAt": None,
            }
        )
    beam = {
        "version": 1,
        "name": "warp-beam",
        "generatedAt": utcnow(),
        "source": {
            "schedule": str(schedule_path),
            "plan": str(plan_path) if plan_path else None,
            "generated": sched.get("generated"),
            "module": sched.get("module"),
        },
        "program": {
            "tickets": len(tickets),
            "criticalPath": list(sched.get("criticalPath") or []),
            "criticalHours": sched.get("criticalHours"),
            "people": sched.get("people"),
            "agentsPerPerson": sched.get("agentsPerPerson"),
            "mergeWindows": sched.get("mergeWindows"),
            "durationsHours": sched.get("durationsHours"),
            "lockConflicts": sched.get("lockConflicts") or [],
            "peakAgents": sched.get("peakAgents"),
            "agentHours": sched.get("agentHours"),
        },
        "config": config,
        "paused": False,
        "pauseReason": None,
        "runState": "stopped",
        "gates": gates,
        "tickets": tickets,
        "agents": {},
        "metrics": {},
    }
    beam["metrics"] = metrics(beam)
    atomic_write(out, json.dumps(beam, indent=2) + "\n")
    journal(out, {"type": "ingest", "tickets": len(tickets), "gates": len(gates)})
    return beam


def gate_index(beam: dict) -> dict[str, dict]:
    return {g["key"]: g for g in beam["gates"]}


def member_gate(beam: dict) -> dict[str, list[str]]:
    m: dict[str, list[str]] = defaultdict(list)
    for g in beam["gates"]:
        for tid in g["members"]:
            m[tid].append(g["key"])
    return m


def ancestors(tickets: dict, tid: str, cache: dict, stack: Optional[set] = None) -> set[str]:
    if tid in cache:
        return cache[tid]
    stack = stack or set()
    if tid in stack:
        return set()
    stack.add(tid)
    acc: set[str] = set()
    for d in tickets.get(tid, {}).get("deps") or []:
        if d == tid:
            continue
        acc.add(d)
        acc |= ancestors(tickets, d, cache, stack)
    stack.discard(tid)
    cache[tid] = acc
    return acc


def lock_overlap(a: list[str], b: list[str]) -> bool:
    sa, sb = set(a), set(b)
    if sa & sb:
        return True
    # prefix overlap: HumanifyOS/internal/app blocks HumanifyOS/internal/app/stub
    for x in sa:
        for y in sb:
            if x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/"):
                return True
    return False


def held_locks(beam: dict) -> list[tuple[str, list[str]]]:
    held = []
    for t in beam["tickets"].values():
        if t["status"] in ACTIVE:
            held.append((t["id"], t["locks"]))
    return held


def gate_blocks(beam: dict, tid: str, anc_cache: dict) -> Optional[str]:
    """A blocking gate that is not green blocks non-members that depend on a member."""
    anc = ancestors(beam["tickets"], tid, anc_cache)
    for g in beam["gates"]:
        if not g.get("blocking", True) or g.get("status") == "green":
            continue
        if tid in g["members"]:
            continue
        if anc & set(g["members"]):
            return g["key"]
    return None


def ready(beam: dict, limit: Optional[int] = None) -> list[dict]:
    # People are not a scheduling axis. A human roster belonged to the manual
    # plan. The only cap is maxAgents (concurrent Shuttles).
    state = beam.get("runState") or ("paused" if beam.get("paused") else "running")
    if beam.get("paused") or state in {"paused", "stopped"}:
        return []
    cfg = beam.get("config") or {}
    cap = int(cfg.get("maxAgents", 18))
    running = sum(1 for t in beam["tickets"].values() if t["status"] in ACTIVE)
    slots = max(0, cap - running)
    if limit is not None:
        slots = min(slots, limit)
    if slots == 0:
        return []
    anc_cache: dict = {}
    held = held_locks(beam)
    candidates = []
    for t in beam["tickets"].values():
        if t["status"] not in {"queued", "blocked"}:
            continue
        unmet = [d for d in t["deps"] if beam["tickets"].get(d, {}).get("status") not in TERMINAL]
        if unmet:
            continue
        gb = gate_blocks(beam, t["id"], anc_cache)
        if gb:
            continue
        conflict = None
        for oid, locks in held:
            if lock_overlap(t["locks"], locks):
                conflict = oid
                break
        if conflict:
            continue
        candidates.append(t)
    candidates.sort(key=lambda t: (not t["critical"], -(t.get("rankDays") or 0), t["id"]))
    chosen = []
    for t in candidates:
        if len(chosen) >= slots:
            break
        if any(lock_overlap(t["locks"], c["locks"]) for c in chosen):
            continue
        chosen.append(t)
    return chosen


def metrics(beam: dict) -> dict:
    by = defaultdict(int)
    tokens = 0
    minutes = 0
    by_size = defaultdict(int)
    by_mod = defaultdict(int)
    for t in beam["tickets"].values():
        by[t["status"]] += 1
        tokens += int(t.get("tokens") or 0)
        minutes += int(t.get("minutes") or 0)
        by_size[t["size"]] += 1
        by_mod[t["module"]] += 1
    done = by["merged"] + by["done"]
    total = len(beam["tickets"]) or 1
    remaining_hours = sum(
        (t.get("hours") or 0)
        for t in beam["tickets"].values()
        if t["status"] not in TERMINAL
    )
    cap = int((beam.get("config") or {}).get("maxAgents", 18))
    # rough: remaining agent-hours / parallelism, critical path still bounds it
    crit = beam.get("program", {}).get("criticalPath") or []
    crit_left = [
        beam["tickets"][i]["hours"]
        for i in crit
        if i in beam["tickets"] and beam["tickets"][i]["status"] not in TERMINAL
    ]
    crit_hours = sum(crit_left)
    parallel_hours = remaining_hours / max(1, min(cap, 8))
    eta_hours = max(crit_hours, parallel_hours)
    return {
        "total": total,
        "byStatus": dict(by),
        "done": done,
        "pct": round(100.0 * done / total, 1),
        "tokens": tokens,
        "minutes": minutes,
        "remainingHours": remaining_hours,
        "criticalHoursLeft": crit_hours,
        "etaHours": round(eta_hours, 1),
        "bySize": dict(by_size),
        "byModule": dict(by_mod),
        "computedAt": utcnow(),
    }


def cmd_set(beam_path: Path, args: argparse.Namespace) -> None:
    beam = load_json(beam_path)
    t = beam["tickets"].get(args.id)
    if not t:
        sys.exit(f"unknown ticket {args.id}")
    if args.status and args.status not in STATUSES:
        sys.exit(f"bad status {args.status}; use {STATUSES}")
    pr = t.get("pr") or {}
    before = {
        "status": t.get("status"),
        "pr_url": pr.get("url"),
        "bugbot": pr.get("bugbot"),
        "ci": pr.get("ci"),
        "alarm": t.get("alarm"),
        "branch": t.get("branch"),
        "agent": t.get("agent"),
        "sha": pr.get("sha"),
    }
    prev = t["status"]
    if args.agent is not None:
        t["agent"] = args.agent
    if args.branch is not None:
        t["branch"] = args.branch
    if args.jira is not None:
        import jira_sync

        cfg = jira_sync.settings(beam_path, beam)
        prefixes = jira_sync.prefixes_from(cfg)
        key = jira_sync.normalize_key(args.jira)
        if not key:
            sys.exit(f"jira: {args.jira!r} is not a Jira issue key (PROJECT-123)")
        if prefixes and not jira_sync.prefix_ok(key, prefixes) and not args.force:
            sys.exit(
                f"jira: {key} does not match jiraProject/jiraKeyPrefixes ({', '.join(prefixes)}). Pass --force to store it."
            )
        t["jiraKey"] = key
        t["jiraKeySource"] = "manual"
        t["jiraKeyForced"] = bool(args.force and prefixes and not jira_sync.prefix_ok(key, prefixes))
        t["jiraMapping"] = "mapped"
        jira_sync.remember_map(beam_path, args.id, key)
    if args.pr is not None:
        t["pr"]["url"] = args.pr
    if getattr(args, "via", None) is not None:
        t["pr"]["via"] = args.via
    import jira_sync

    cfg = jira_sync.settings(beam_path, beam)
    note = jira_sync.apply_review(t, cfg, args)
    if note and note.startswith("refusing "):
        print(f"{args.id}: {note}")
        sys.exit(1)
    if args.alarm is not None:
        t["alarm"] = None if args.alarm == "-" else args.alarm
    if args.status in {"coding", "claimed"} and not t["pr"]["openedAt"] and args.pr:
        t["pr"]["openedAt"] = utcnow()
    try:
        prefixes = jira_sync.prefixes_from(cfg)
        if t.get("jiraKey") and not jira_sync.jira_key(t, prefixes) and not t.get("jiraKeyForced"):
            t["jiraKey"] = None
            t["jiraKeySource"] = None
            t["jiraMapping"] = "needs mapping"
        if not jira_sync.jira_key(t, prefixes):
            inferred = jira_sync.infer_key(t, prefixes)
            if inferred:
                t["jiraKey"] = inferred
                t["jiraKeySource"] = "inferred"
                t["jiraMapping"] = "mapped"
    except Exception:
        jira_sync = None
    t["updatedAt"] = utcnow()
    beam["metrics"] = metrics(beam)
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(
        beam_path,
        {"type": "set", "id": args.id, "from": prev, "to": t["status"], "agent": t.get("agent")},
    )
    extra = f" ({note})" if note else ""
    print(f"{args.id} {prev} -> {t['status']}{extra}")
    try:
        if jira_sync is None:
            import jira_sync as jira_sync  # noqa: F811
        jira_sync.on_set(beam_path, beam, t, before)
    except Exception as e:  # fail-soft: Jira sync never blocks a transition
        print(f"jira: skipped ({e})")


def cmd_spend(beam_path: Path, args: argparse.Namespace) -> None:
    beam = load_json(beam_path)
    t = beam["tickets"].get(args.id)
    if not t:
        sys.exit(f"unknown ticket {args.id}")
    t["tokens"] = int(t.get("tokens") or 0) + int(args.tokens or 0)
    t["minutes"] = int(t.get("minutes") or 0) + int(args.minutes or 0)
    t["updatedAt"] = utcnow()
    beam["metrics"] = metrics(beam)
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(beam_path, {"type": "spend", "id": args.id, "tokens": args.tokens, "minutes": args.minutes})
    print(f"{args.id} tokens={t['tokens']} minutes={t['minutes']}")


def cmd_gate(beam_path: Path, args: argparse.Namespace) -> None:
    beam = load_json(beam_path)
    g = next((x for x in beam["gates"] if x["key"] == args.key), None)
    if not g:
        sys.exit(f"unknown gate {args.key}")
    if args.status not in {"pending", "green", "red"}:
        sys.exit("gate status must be pending|green|red")
    g["status"] = args.status
    g["evidence"] = args.evidence
    g["greenAt"] = utcnow() if args.status == "green" else None
    beam["metrics"] = metrics(beam)
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(beam_path, {"type": "gate", "key": args.key, "status": args.status})
    print(f"{args.key} -> {args.status}")


def cmd_pause(beam_path: Path, paused: bool, reason: Optional[str]) -> None:
    beam = load_json(beam_path)
    beam["paused"] = paused
    beam["pauseReason"] = reason
    beam["runState"] = "paused" if paused else "running"
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(beam_path, {"type": "pause" if paused else "resume", "reason": reason})
    print("paused" if paused else "resumed")


def render_board(beam: dict) -> str:
    m = beam.get("metrics") or metrics(beam)
    by = m.get("byStatus") or {}
    lines = [
        "# Warp board",
        "",
        f"Updated {m.get('computedAt', utcnow())} · paused={beam.get('paused')}",
        "",
        f"- Tickets: **{m.get('done', 0)}/{m.get('total', 0)}** ({m.get('pct', 0)}%)",
        f"- Tokens: **{m.get('tokens', 0):,}** · agent minutes: **{m.get('minutes', 0):,}**",
        f"- Remaining agent-hours: **{m.get('remainingHours', 0)}** · critical path left: **{m.get('criticalHoursLeft', 0)}h** · ETA ~**{m.get('etaHours', 0)}h** at current cap",
        f"- Cap: {beam.get('config', {}).get('maxAgents')} agents · model `{beam.get('config', {}).get('model')}`",
        "",
        "## Status",
        "",
        "| status | n |",
        "|---|---|",
    ]
    for s in STATUSES:
        if by.get(s):
            lines.append(f"| {s} | {by[s]} |")
    lines += ["", "## Gates", "", "| gate | status | members |", "|---|---|---|"]
    for g in beam["gates"]:
        lines.append(f"| {g['key']} {g.get('name') or ''} | {g['status']} | {', '.join(g['members'])} |")
    alarms = [t for t in beam["tickets"].values() if t["status"] == "alarm" or t.get("alarm")]
    lines += ["", "## Alarms", ""]
    if not alarms:
        lines.append("None.")
    else:
        for t in alarms:
            lines.append(f"- **{t['id']}** {t.get('alarm') or t['status']} — {t['summary']}")
    waiting = [t for t in beam["tickets"].values() if t["status"] == "awaiting_approval"]
    lines += ["", "## Awaiting approval (above MEDIUM)", ""]
    if not waiting:
        lines.append("None.")
    else:
        for t in waiting:
            bug = t["pr"].get("bugbot") or "none"
            ci = t["pr"].get("ci") or "none"
            lines.append(f"- **{t['id']}** ({t['size']}) bugbot={bug} ci={ci} {t['pr'].get('url') or 'no PR'} — {t['summary']}")
    active = [t for t in beam["tickets"].values() if t["status"] in ACTIVE]
    lines += ["", "## In flight", ""]
    if not active:
        lines.append("None.")
    else:
        for t in active:
            lines.append(f"- **{t['id']}** {t['status']} agent={t.get('agent')} locks={', '.join(t['locks'][:2])}")
    nxt = ready(beam, limit=12)
    lines += ["", "## Next ready (critical first)", ""]
    if not nxt:
        lines.append("None — paused, capped, gated, or lock-blocked.")
    else:
        for t in nxt:
            lines.append(
                f"- **{t['id']}** {t['size']}/{t['complexity']} {'AUTO' if t['autoMerge'] else 'REVIEW'} — {t['summary']}"
            )
    lines.append("")
    return "\n".join(lines)


def render_html(beam: dict) -> str:
    m = beam.get("metrics") or metrics(beam)
    by = m.get("byStatus") or {}
    rows = []
    order = {"alarm": 0, "awaiting_approval": 1, "fix": 2, "bugbot_running": 3, "review": 4, "coding": 5, "claimed": 6}
    shown = [t for t in beam["tickets"].values() if t["status"] != "queued"]
    shown.sort(key=lambda t: (order.get(t["status"], 9), t["id"]))
    for t in shown[:80]:
        rows.append(
            "<tr><td>{id}</td><td>{st}</td><td>{sz}</td><td>{mod}</td><td>{sum}</td></tr>".format(
                id=t["id"],
                st=t["status"],
                sz=t["size"],
                mod=t["module"],
                sum=(t["summary"] or "").replace("<", "<")[:80],
            )
        )
    gates = "".join(
        f"<li><b>{g['key']}</b> {g['status']} — {g.get('name') or ''}</li>" for g in beam["gates"]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Warp board</title>
<style>
body {{ margin:0; background:#0E1719; color:#F7FAFA; font:15px/1.45 ui-sans-serif, system-ui, sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:32px 24px 64px; }}
h1 {{ font-weight:560; letter-spacing:-.03em; }}
.stat {{ display:flex; gap:18px; flex-wrap:wrap; color:#43B3AD; }}
table {{ width:100%; border-collapse:collapse; margin-top:18px; }}
td,th {{ text-align:left; padding:6px 8px; border-bottom:1px solid #1c2c30; }}
.muted {{ color:#9bb; }}
</style></head><body><main>
<h1>Warp</h1>
<p class="muted">HumanifyOS beam · {m.get('computedAt','')}</p>
<div class="stat">
  <span>{m.get('done',0)}/{m.get('total',0)} done ({m.get('pct',0)}%)</span>
  <span>{m.get('tokens',0):,} tokens</span>
  <span>{m.get('minutes',0):,} min</span>
  <span>ETA ~{m.get('etaHours',0)}h</span>
</div>
<p class="muted">in flight {sum(by.get(s,0) for s in ACTIVE)} · alarms {by.get('alarm',0)} · awaiting approval {by.get('awaiting_approval',0)}</p>
<h2>Gates</h2><ul>{gates}</ul>
<h2>Not queued</h2>
<table><thead><tr><th>id</th><th>status</th><th>size</th><th>module</th><th>summary</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5>Nothing started.</td></tr>'}</tbody></table>
</main></body></html>
"""


def cmd_check(beam: dict) -> int:
    errors = []
    ids = set(beam["tickets"])
    for t in beam["tickets"].values():
        for d in t["deps"]:
            if d not in ids:
                errors.append(f"{t['id']} dep missing {d}")
        if t["status"] not in STATUSES:
            errors.append(f"{t['id']} bad status {t['status']}")
    # cycle check
    color = {}

    def dfs(n: str) -> None:
        color[n] = 1
        for d in beam["tickets"][n]["deps"]:
            if d not in beam["tickets"]:
                continue
            if color.get(d) == 1:
                errors.append(f"cycle at {n}->{d}")
            elif color.get(d) != 2:
                dfs(d)
        color[n] = 2

    for i in ids:
        if color.get(i) != 2:
            dfs(i)
    active = [t for t in beam["tickets"].values() if t["status"] in ACTIVE]
    for i, a in enumerate(active):
        for b in active[i + 1 :]:
            if lock_overlap(a["locks"], b["locks"]):
                errors.append(f"lock overlap {a['id']} vs {b['id']}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"ok {len(ids)} tickets, {len(beam['gates'])} gates")
    return 0


def default_config() -> dict:
    return {
        "model": "claude-sonnet-5-5-high",
        "maxAgents": 18,
        "autoMergeSizes": ["S", "M"],
        "messenger": "both",
        "notify": "verbose",
        "runner": "cloud",
        "jiraProject": "",
        "jiraKeyPrefixes": "",
        "jiraKeyMap": {},
        "jiraExternalIdField": "externalId",
        "jiraExternalIdFieldName": "External ID",
        "jiraWriteExternalId": False,
        "jiraCreateExternalIdField": False,
        "jiraExternalIdFallback": "label",
        "jiraTransition": True,
        "jiraInProgressStatus": "In Progress",
        "jiraRestoreOnRelease": False,
        "jiraQaReadyStatus": "QA Ready",
        "jiraDoneStatus": "Done",
        "jiraMcp": "atlassian",
        "jiraSite": "",
        "bugbotRequired": True,
        "bugbotManual": True,
        "maxFixAttempts": 3,
        "stuckAfterMinutes": 90,
        "respectMergeWindows": False,
        "mergeWindows": ["08:30", "13:00", "17:00"],
        "pollSeconds": 300,
    }


BEAM_HELP = """
examples:
  python3 scripts/beam.py ?
  python3 scripts/beam.py ready --beam .warp/beam.json
  python3 scripts/beam.py set --beam .warp/beam.json --id T-9 --status claimed
  python3 scripts/beam.py board --beam .warp/beam.json

Subcommands: ingest, ready, set, spend, gate, pause, resume, board, check, eta.
set takes --status, --agent, --branch, --jira, --pr, --sha,
--via local|connected, --bugbot pass|fail, --ci green, --alarm, --attempts, --force.
awaiting_approval, merging, and merged wait for CI green and, when Bugbot
applies, --bugbot pass. A new --sha while awaiting_approval returns the ticket
to bugbot_running and does not move Jira backwards.
--jira KEY is rejected when it does not match jiraProject or jiraKeyPrefixes,
unless --force is set. A plan id is not a Jira key.
ready's only cap is maxAgents. Do not hand-edit beam.json.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main() -> None:
    p = argparse.ArgumentParser(
        description="Warp beam",
        epilog=BEAM_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest")
    pi.add_argument("--schedule", required=True)
    pi.add_argument("--plan")
    pi.add_argument("--out", required=True)
    pi.add_argument("--config")
    pi.add_argument("--max-agents", type=int)
    pi.add_argument("--model")

    pr = sub.add_parser("ready")
    pr.add_argument("--beam", required=True)
    pr.add_argument("--limit", type=int)

    ps = sub.add_parser("set")
    ps.add_argument("--beam", required=True)
    ps.add_argument("--id", required=True)
    ps.add_argument("--status")
    ps.add_argument("--agent")
    ps.add_argument("--branch")
    ps.add_argument("--jira")
    ps.add_argument("--force", action="store_true", help="store a Jira key whose project prefix is not configured")
    ps.add_argument("--pr")
    ps.add_argument("--sha", help="merge commit sha, stored on pr.sha and included in the Jira comment")
    ps.add_argument("--via", choices=["local", "connected"], help="how this merge happened; local skips pull-request comments")
    ps.add_argument("--bugbot")
    ps.add_argument("--ci")
    ps.add_argument("--alarm")
    ps.add_argument("--attempts", type=int)

    psp = sub.add_parser("spend")
    psp.add_argument("--beam", required=True)
    psp.add_argument("--id", required=True)
    psp.add_argument("--tokens", type=int, default=0)
    psp.add_argument("--minutes", type=int, default=0)

    pg = sub.add_parser("gate")
    pg.add_argument("--beam", required=True)
    pg.add_argument("--key", required=True)
    pg.add_argument("--status", required=True)
    pg.add_argument("--evidence")

    pp = sub.add_parser("pause")
    pp.add_argument("--beam", required=True)
    pp.add_argument("--reason")
    sub.add_parser("resume").add_argument("--beam", required=True)

    pb = sub.add_parser("board")
    pb.add_argument("--beam", required=True)
    pb.add_argument("--md")
    pb.add_argument("--html")

    pc = sub.add_parser("check")
    pc.add_argument("--beam", required=True)

    pe = sub.add_parser("eta")
    pe.add_argument("--beam", required=True)

    import usage

    args = p.parse_args(usage.normalize_argv(None))
    if args.cmd == "ingest":
        cfg = default_config()
        if args.config:
            cfg.update(load_json(Path(args.config)))
        if args.max_agents:
            cfg["maxAgents"] = args.max_agents
        if args.model:
            cfg["model"] = args.model
        beam = ingest(Path(args.schedule), Path(args.plan) if args.plan else None, Path(args.out), cfg)
        print(f"wrote {args.out} tickets={len(beam['tickets'])} gates={len(beam['gates'])}")
    elif args.cmd == "ready":
        beam = load_json(Path(args.beam))
        rows = ready(beam, args.limit)
        for t in rows:
            print(f"{t['id']}\t{t['size']}\t{t['complexity']}\t{'AUTO' if t['autoMerge'] else 'REVIEW'}\t{t['summary']}")
        if not rows:
            print("(none)")
    elif args.cmd == "set":
        cmd_set(Path(args.beam), args)
    elif args.cmd == "spend":
        cmd_spend(Path(args.beam), args)
    elif args.cmd == "gate":
        cmd_gate(Path(args.beam), args)
    elif args.cmd == "pause":
        cmd_pause(Path(args.beam), True, args.reason)
    elif args.cmd == "resume":
        cmd_pause(Path(args.beam), False, None)
    elif args.cmd == "board":
        beam = load_json(Path(args.beam))
        beam["metrics"] = metrics(beam)
        md = render_board(beam)
        html = render_html(beam)
        md_path = Path(args.md) if args.md else Path(args.beam).parent / "BOARD.md"
        html_path = Path(args.html) if args.html else Path(args.beam).parent / "board.html"
        atomic_write(md_path, md)
        atomic_write(html_path, html)
        print(f"wrote {md_path} and {html_path}")
    elif args.cmd == "check":
        sys.exit(cmd_check(load_json(Path(args.beam))))
    elif args.cmd == "eta":
        beam = load_json(Path(args.beam))
        print(json.dumps(metrics(beam), indent=2))


if __name__ == "__main__":
    main()
