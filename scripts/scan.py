#!/usr/bin/env python3
"""Scan any repo for a plan, build or replace the Warp beam, export a plan
an outside model can critique, and write the status file.

Looks for, in order of richness:
  WARP_PLAN.json / warp-plan.json
  **/schedule.json with a tickets array
  **/CURSOR_PLAN.md and CURSOR_PLAN_FAST.md
  Jira exports (*tickets*.json, jira/*.json) with blockedBy / issuelinks
  markdown tables with an id column and a deps/blockers column

People are not read as a cap. An owner column is stored as a note only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beam import (  # noqa: E402
    ACTIVE,
    TERMINAL,
    atomic_write,
    default_config,
    ingest,
    journal,
    load_json,
    metrics,
    ready,
    utcnow,
)

SKIP = {".git", "node_modules", "vendor", "dist", ".warp", "coverage"}
ID_RE = re.compile(r"\b([A-Z]{1,4}-\d{1,4})\b")
AFTER_RE = re.compile(r"\bafter\s+([A-Z0-9][A-Z0-9,\s\-]{0,80})", re.I)


def walk(base: Path):
    for p in sorted(base.rglob("*")):
        if any(part in SKIP for part in p.relative_to(base).parts):
            continue
        if p.is_file():
            yield p


def resolve_folder(root: Path, arg: str) -> tuple[Path | None, list[Path], str | None]:
    """Resolve a folder argument to a directory inside root.

    An existing path under root wins. Otherwise the argument is matched as a
    folder name, or a trailing path such as spec/api, anywhere under root.
    Returns (folder, candidates, error). A folder of None with candidates means
    the name is ambiguous; with an error it was not found.
    """
    root = root.resolve()
    direct = (root / arg).resolve()
    if direct.is_dir():
        if direct != root and root not in direct.parents:
            return None, [], f"folder {arg!r} is outside the repo root {root}"
        return direct, [], None
    if Path(arg).is_absolute():
        return None, [], f"folder {arg!r} does not exist"
    want = arg.strip("/").replace("\\", "/").casefold()
    hits = []
    for d in sorted(root.rglob("*")):
        if not d.is_dir():
            continue
        rel = d.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        rp = rel.as_posix().casefold()
        if rp == want or rp.endswith("/" + want):
            hits.append(d)
    if not hits:
        return None, [], f"no folder named {arg!r} under {root}"
    if len(hits) == 1:
        return hits[0], [], None
    return None, hits, None


def has_plan_files(d: Path) -> bool:
    found = scan(d, d)
    return any(found[k] for k in ("warp", "schedules", "plans", "jira"))


def plan_folders(found: dict) -> list[str]:
    """Folders (relative to the repo root) that hold a plan, schedule, or export."""
    out = {str(Path(rel).parent) for k in ("warp", "schedules", "plans", "jira") for rel in found[k]}
    return sorted(out)


def scan(root: Path, folder: Path | None = None) -> dict:
    found = {"plans": [], "schedules": [], "jira": [], "warp": [], "maps": []}
    for p in walk(folder or root):
        name = p.name.lower()
        rel = str(p.relative_to(root))
        if name in {"warp_plan.json", "warp-plan.json"}:
            found["warp"].append(rel)
        elif name == "schedule.json":
            found["schedules"].append(rel)
        elif name in {"cursor_plan.md", "cursor_plan_fast.md"}:
            found["plans"].append(rel)
        elif name.endswith(".html") and "build" in name and "map" in name:
            found["maps"].append(rel)
        elif name.endswith(".json") and ("ticket" in name or "jira" in name or p.parent.name.lower() == "jira"):
            found["jira"].append(rel)
    found["root"] = str(root)
    if folder is not None:
        found["folder"] = str(folder.relative_to(root)) or "."
    found["scannedAt"] = utcnow()
    return found


def _size_from_label(text: str) -> str:
    t = (text or "").upper()
    if "XL" in t or "CRITICAL" in t:
        return "XL"
    if re.search(r"\bL\b", t) or "HIGH" in t:
        return "L"
    if re.search(r"\bS\b", t) or "LOW" in t:
        return "S"
    return "M"


def stamp_jira_keys(tickets: list[dict]) -> None:
    """Fill jiraKey from the id, summary, or branch when the plan did not set one."""
    import jira_sync

    for t in tickets:
        if jira_sync.jira_key(t):
            continue
        t["jiraKey"] = jira_sync.infer_key(t)


def from_schedule(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tickets"), list):
        return None
    if not data["tickets"] or "id" not in data["tickets"][0] and "tempId" not in data["tickets"][0]:
        return None
    tickets = []
    for t in data["tickets"]:
        tid = t.get("id") or t.get("tempId")
        if not tid:
            continue
        tickets.append(
            {
                "id": tid,
                "jiraKey": t.get("jiraKey"),
                "summary": t.get("summary") or t.get("title") or "",
                "deps": t.get("deps") or t.get("blockedBy") or t.get("blockedByTempIds") or [],
                "locks": t.get("locks") or [],
                "size": t.get("size") or "M",
                "hours": t.get("hours"),
                "critical": bool(t.get("critical")),
                "rankDays": t.get("rankDays") or 0,
                "gate": t.get("gate"),
                "module": t.get("module"),
                "layer": t.get("layer"),
                "priority": t.get("priority"),
                "acs": t.get("acs"),
            }
        )
    stamp_jira_keys(tickets)
    return {
        "tickets": tickets,
        "gates": data.get("gates") or [],
        "criticalPath": data.get("criticalPath") or [],
        "source": str(path),
        "format": "schedule.json",
    }


def from_jira(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    rows = None
    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        rows = data["tickets"]
    elif isinstance(data, dict) and data.get("projects"):
        rows = []
        for proj in data["projects"]:
            rows.extend(proj.get("issues") or [])
    elif isinstance(data, list):
        rows = data
    if not rows:
        return None
    tickets = []
    for t in rows:
        fields = t.get("fields") or t
        tid = t.get("tempId") or t.get("key") or fields.get("tempId") or t.get("id")
        if not tid:
            continue
        deps = list(t.get("blockedByTempIds") or t.get("blockedBy") or [])
        for link in fields.get("issuelinks") or []:
            if link.get("type", {}).get("name", "").lower() in {"blocks", "is blocked by"}:
                inward = (link.get("inwardIssue") or {}).get("key")
                if inward:
                    deps.append(inward)
        summary = t.get("summary") or fields.get("summary") or ""
        key = t.get("key") or fields.get("key")
        tickets.append(
            {
                "id": str(tid),
                "jiraKey": key if isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", key) else None,
                "summary": summary,
                "deps": deps,
                "locks": t.get("locks") or [],
                "size": t.get("size") or _size_from_label(str(t.get("priority") or "")),
                "critical": False,
                "rankDays": 0,
                "module": None,
            }
        )
    if len(tickets) < 1:
        return None
    stamp_jira_keys(tickets)
    return {"tickets": tickets, "gates": [], "criticalPath": [], "source": str(path), "format": "jira-json"}


def from_markdown(path: Path) -> dict | None:
    text = path.read_text(errors="ignore")
    if "ticket" not in text.lower() and not ID_RE.search(text):
        return None
    # table form: | id | ... | deps |
    tickets: dict[str, dict] = {}
    lines = text.splitlines()
    header = None
    for line in lines:
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set(cells) <= {"", "-", "---"} or all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
            continue
        low = [c.lower() for c in cells]
        if header is None and any(h in {"id", "ticket", "key"} for h in low):
            header = low
            continue
        if header and len(cells) == len(header):
            row = dict(zip(header, cells))
            tid = row.get("id") or row.get("ticket") or row.get("key")
            if not tid or not ID_RE.fullmatch(tid.strip()):
                continue
            deps_raw = row.get("deps") or row.get("blockers") or row.get("blocked by") or ""
            deps = ID_RE.findall(deps_raw)
            tickets[tid.strip()] = {
                "id": tid.strip(),
                "summary": row.get("summary") or row.get("title") or "",
                "deps": deps,
                "locks": [x.strip() for x in (row.get("locks") or "").split(",") if x.strip()],
                "size": _size_from_label(row.get("size") or row.get("complexity") or "M"),
                "critical": "yes" in (row.get("critical") or "").lower() or "★" in line,
                "rankDays": 0,
            }
    # CURSOR_PLAN wave form: ticket ids and "after X"
    wave = 0
    gates = []
    current_gate = None
    for line in lines:
        if re.match(r"^###\s+W\d+", line):
            wave += 1
        m = re.search(r"Gates before", line, re.I)
        if m:
            current_gate = f"G{len(gates)}"
            gates.append({"key": current_gate, "name": f"wave-{wave}", "blocking": True, "members": [], "checks": []})
        gm = re.search(r"^\s*-\s+\*([A-Z]{1,4}-\d+)\*\s+[—-]\s+(.*)", line)
        if gm and gates:
            gates[-1]["members"].append(gm.group(1))
            gates[-1]["checks"].append(gm.group(2).strip())
        ids = ID_RE.findall(line)
        after = AFTER_RE.search(line)
        after_ids = ID_RE.findall(after.group(1)) if after else []
        defined = [i for i in ids if i not in after_ids] or ids
        for tid in defined:
            if tid not in tickets:
                tickets[tid] = {
                    "id": tid,
                    "summary": "",
                    "deps": [],
                    "locks": [],
                    "size": "M",
                    "critical": "★" in line,
                    "rankDays": max(0, 20 - wave),
                    "wave": wave,
                }
            for d in after_ids:
                if d != tid and d not in tickets[tid]["deps"]:
                    tickets[tid]["deps"].append(d)
            if gates and tid in gates[-1]["members"]:
                tickets[tid]["gate"] = gates[-1]["key"]
    if len(tickets) < 2:
        return None
    rows = list(tickets.values())
    stamp_jira_keys(rows)
    return {
        "tickets": rows,
        "gates": gates,
        "criticalPath": [t["id"] for t in tickets.values() if t.get("critical")],
        "source": str(path),
        "format": "markdown",
    }


def pick(root: Path, found: dict) -> dict | None:
    for rel in found["warp"]:
        got = from_schedule(root / rel)
        if got:
            got["format"] = "warp-plan"
            return got
    for rel in found["schedules"]:
        got = from_schedule(root / rel)
        if got:
            return got
    best = None
    for rel in found["plans"]:
        got = from_markdown(root / rel)
        if got and (best is None or len(got["tickets"]) > len(best["tickets"])):
            best = got
    if best:
        return best
    for rel in found["jira"]:
        got = from_jira(root / rel)
        if got and (best is None or len(got["tickets"]) > len(best["tickets"])):
            best = got
    return best


HOURS = {"S": 4, "M": 7, "L": 11, "XL": 16}
APPROVAL_HOURS = 0.5


def ticket_hours(t: dict) -> float:
    if t.get("hours"):
        return float(t["hours"])
    return float(HOURS.get(t.get("size") or "M", 7))


def estimate(tickets: list[dict], gates: list[dict], critical: list[str], max_agents: int) -> dict:
    """Agent hours are implementation time. Human hours assume each approval
    and each gate check is answered within 30 minutes."""
    by_id = {t["id"]: t for t in tickets}
    agent = round(sum(ticket_hours(t) for t in tickets), 1)
    reviews = [t["id"] for t in tickets if t.get("size") in {"L", "XL"} or not t.get("autoMerge", t.get("size") in {"S", "M"})]
    reviews = [i for i in reviews if by_id[i].get("size") in {"L", "XL"} or by_id[i].get("autoMerge") is False]
    gate_n = len([g for g in gates if g.get("blocking", True)])
    human = round(APPROVAL_HOURS * (len(reviews) + gate_n), 1)
    memo: dict[str, float] = {}

    def chain(tid: str, seen: set[str]) -> float:
        if tid in memo:
            return memo[tid]
        if tid in seen or tid not in by_id:
            return 0.0
        seen.add(tid)
        t = by_id[tid]
        extra = APPROVAL_HOURS if tid in reviews else 0.0
        best = max((chain(d, seen) for d in (t.get("deps") or [])), default=0.0)
        seen.discard(tid)
        memo[tid] = best + ticket_hours(t) + extra
        return memo[tid]

    if critical:
        crit_agent = round(sum(ticket_hours(by_id[i]) for i in critical if i in by_id), 1)
        crit_human = round(APPROVAL_HOURS * len([i for i in critical if i in reviews]), 1)
        longest = round(crit_agent + crit_human + APPROVAL_HOURS * gate_n, 1)
    else:
        longest = round(max((chain(t["id"], set()) for t in tickets), default=0.0), 1)
        crit_agent = None
        crit_human = None
    parallel = round(agent / max(1, max_agents), 1)
    elapsed = round(max(longest, parallel), 1)
    return {
        "agentHours": agent,
        "humanHours": human,
        "elapsedHours": elapsed,
        "criticalAgentHours": crit_agent,
        "criticalHumanHours": crit_human,
        "approvalHours": APPROVAL_HOURS,
        "reviewTickets": len(reviews),
        "gates": gate_n,
        "maxAgents": max_agents,
        "assumptions": [
            "Agent hours are S=4, M=7, L=11, XL=16 unless the ticket has hours.",
            "Each L/XL approval is provided within 30 minutes.",
            "Each blocking gate check is signed within 30 minutes.",
            "S/M auto-merge and add no human wait.",
            "Elapsed hours are the longer of the critical chain plus those waits, and agent hours divided by maxAgents.",
        ],
    }


def to_schedule(graph: dict) -> dict:
    return {
        "module": "warp",
        "generated": utcnow(),
        "tickets": [
            {
                "id": t["id"],
                "jiraKey": t.get("jiraKey"),
                "summary": t.get("summary") or "",
                "deps": t.get("deps") or [],
                "unlocks": [],
                "locks": t.get("locks") or [],
                "size": t.get("size") or "M",
                "hours": t.get("hours") or {"S": 4, "M": 7, "L": 11, "XL": 16}.get(t.get("size") or "M", 7),
                "critical": bool(t.get("critical")),
                "rankDays": t.get("rankDays") or 0,
                "gate": t.get("gate"),
                "module": t.get("module"),
                "layer": t.get("layer"),
                "priority": t.get("priority"),
                "acs": t.get("acs"),
            }
            for t in graph["tickets"]
        ],
        "gates": graph.get("gates") or [],
        "criticalPath": graph.get("criticalPath") or [],
        "estimate": estimate(
            graph["tickets"],
            graph.get("gates") or [],
            graph.get("criticalPath") or [],
            int(graph.get("maxAgents") or 18),
        ),
    }


def write_status(beam_path: Path) -> None:
    beam = load_json(beam_path)
    beam["metrics"] = metrics(beam)
    done, left, working = [], [], []
    for t in sorted(beam["tickets"].values(), key=lambda x: x["id"]):
        row = {
            "id": t["id"],
            "status": t["status"],
            "summary": t.get("summary"),
            "size": t.get("size"),
            "pr": (t.get("pr") or {}).get("url"),
            "agent": t.get("agent"),
            "tokens": t.get("tokens") or 0,
            "minutes": t.get("minutes") or 0,
        }
        if t["status"] in TERMINAL:
            done.append(row)
        elif t["status"] in ACTIVE or t["status"] == "awaiting_approval":
            working.append(row)
        else:
            left.append(row)
    payload = {
        "updatedAt": utcnow(),
        "runState": beam.get("runState"),
        "paused": beam.get("paused"),
        "done": done,
        "working": working,
        "left": left,
        "counts": {"done": len(done), "working": len(working), "left": len(left)},
        "metrics": beam["metrics"],
    }
    out = beam_path.parent / "status.json"
    atomic_write(out, json.dumps(payload, indent=2) + "\n")
    try:
        import version

        ver = version.label(version.version_of(version.PLUGIN_ROOT))
    except Exception:
        ver = "Warp"
    lines = [
        "# Warp status",
        "",
        ver,
        "",
        f"Updated {payload['updatedAt']} · runState={payload['runState']}",
        "",
        f"Done {len(done)} · working {len(working)} · left {len(left)}",
        "",
        f"Estimate · agent {beam.get('program', {}).get('estimate', {}).get('agentHours', '—')}h · human {beam.get('program', {}).get('estimate', {}).get('humanHours', '—')}h · elapsed {beam.get('program', {}).get('estimate', {}).get('elapsedHours', '—')}h",
        "",
        "## Working now",
        "",
    ]
    lines += [f"- **{t['id']}** {t['status']} agent={t['agent']} pr={t['pr'] or '—'} — {t['summary']}" for t in working] or ["None."]
    lines += ["", "## Done", ""]
    lines += [f"- **{t['id']}** {t['status']} — {t['summary']}" for t in done[:40]] or ["None."]
    if len(done) > 40:
        lines.append(f"- … {len(done) - 40} more in status.json")
    lines += ["", "## Left", ""]
    lines += [f"- **{t['id']}** {t['status']} {t['size']} — {t['summary']}" for t in left[:40]] or ["None."]
    if len(left) > 40:
        lines.append(f"- … {len(left) - 40} more in status.json")
    lines.append("")
    atomic_write(beam_path.parent / "STATUS.md", "\n".join(lines))
    print(f"wrote {out} and {beam_path.parent / 'STATUS.md'}")
    try:
        import version

        print(version.describe(beam_path.parent.parent))
    except Exception as e:
        print(f"version: {e}")


def export_plan(beam_path: Path, dest: Path) -> None:
    beam = load_json(beam_path)
    nxt = ready({**beam, "runState": "running", "paused": False}, limit=25)
    batches = []
    # suggest parallel waves by peeling a ready set with no cap
    shadow = json.loads(json.dumps(beam))
    shadow["runState"] = "running"
    shadow["paused"] = False
    shadow["config"]["maxAgents"] = 10_000
    for n in range(1, 40):
        wave = ready(shadow, limit=10_000)
        if not wave:
            break
        batches.append([t["id"] for t in wave])
        for t in wave:
            shadow["tickets"][t["id"]]["status"] = "merged"
    est = estimate(
        list(beam["tickets"].values()),
        beam.get("gates") or [],
        beam.get("program", {}).get("criticalPath") or [],
        int((beam.get("config") or {}).get("maxAgents") or 18),
    )
    plan = {
        "kind": "warp-plan",
        "version": 1,
        "exportedAt": utcnow(),
        "estimate": est,
        "suggestion": {
            "summary": "Critical path first. Parallel only across non-overlapping locks. S/M auto-merge; L/XL wait for APPROVED.",
            "firstBatch": [t["id"] for t in nxt],
            "parallelBatches": batches[:12],
            "reviewRequired": [t["id"] for t in beam["tickets"].values() if not t.get("autoMerge")],
            "autoMerge": [t["id"] for t in beam["tickets"].values() if t.get("autoMerge")],
        },
        "gates": beam.get("gates"),
        "criticalPath": beam.get("program", {}).get("criticalPath"),
        "tickets": [
            {
                "id": t["id"],
                "jiraKey": t.get("jiraKey"),
                "summary": t.get("summary"),
                "deps": t.get("deps"),
                "locks": t.get("locks"),
                "size": t.get("size"),
                "complexity": t.get("complexity"),
                "autoMerge": t.get("autoMerge"),
                "critical": t.get("critical"),
                "status": t.get("status"),
                "gate": t.get("gate"),
            }
            for t in beam["tickets"].values()
        ],
    }
    atomic_write(dest, json.dumps(plan, indent=2) + "\n")
    md = dest.with_suffix(".md")
    lines = [
        "# Warp plan — analysis brief",
        "",
        "Feed this file to another model. It is Warp's suggested order, not a calendar.",
        "",
        f"Tickets: {len(plan['tickets'])}. First batch: {', '.join(plan['suggestion']['firstBatch'][:15]) or 'none'}.",
        "",
        "## Estimate",
        "",
        f"- Agent hours: **{est['agentHours']}**",
        f"- Human hours: **{est['humanHours']}** ({est['reviewTickets']} approvals and {est['gates']} gate checks at 30 minutes each)",
        f"- Elapsed hours: **{est['elapsedHours']}** at maxAgents={est['maxAgents']}",
        "",
        "Assumptions:",
        "",
    ]
    lines += [f"- {a}" for a in est["assumptions"]]
    lines += [
        "",
        "## Suggested parallel batches",
        "",
    ]
    for i, batch in enumerate(plan["suggestion"]["parallelBatches"], 1):
        lines.append(f"- Batch {i} ({len(batch)}): {', '.join(batch[:18])}{'…' if len(batch) > 18 else ''}")
    lines += ["", "## Gates", ""]
    for g in plan["gates"]:
        lines.append(f"- {g.get('key')} {g.get('name') or ''} members={', '.join(g.get('members') or [])} status={g.get('status')}")
    lines += ["", "## Holds for human approval", "", ", ".join(plan["suggestion"]["reviewRequired"][:40]) or "none", ""]
    atomic_write(md, "\n".join(lines))
    print(f"wrote {dest} and {md}")


def bundle(beam_path: Path, dest: Path) -> None:
    """Copy the human review files. Does not include the journal."""
    import zipfile
    base = beam_path.parent
    dest.parent.mkdir(parents=True, exist_ok=True)
    names = ["STATUS.md", "status.json", "BOARD.md", "board.html", "WARP_PLAN.md", "WARP_PLAN.json", "scan.json", "config.yaml"]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            path = base / name
            if path.exists():
                z.write(path, name)
    print(f"wrote {dest}")


def set_run(beam_path: Path, state: str, reason: str | None) -> None:
    beam = load_json(beam_path)
    beam["runState"] = state
    beam["paused"] = state != "running"
    beam["pauseReason"] = reason
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(beam_path, {"type": state, "reason": reason})
    print(state)


SCAN_HELP = """
examples:
  python3 scripts/scan.py ?
  python3 scripts/scan.py scan --root . --out .warp/beam.json
  python3 scripts/scan.py scan --folder HOS/spec
  python3 scripts/scan.py scan --folder spec
  python3 scripts/scan.py status --beam .warp/beam.json
  python3 scripts/scan.py export --beam .warp/beam.json
  python3 scripts/scan.py import --plan path/to/WARP_PLAN.json
  python3 scripts/scan.py start --beam .warp/beam.json
  python3 scripts/scan.py pause --beam .warp/beam.json --reason "hold"

scan looks for WARP_PLAN.json, schedule.json, CURSOR_PLAN.md, a Jira ticket
export, or a markdown table with id and deps. --folder is a path or a name.
Several matches and no single folder with plan files exits 3 and scans
nothing. No plan exits 2. --max-agents defaults to 18 and --model to
claude-sonnet-5-5-high; the live cap and slug are maxAgents and model in
config. start, stop, pause, and resume take --beam and --reason.
import --keep-status keeps status for ids that still exist (the default).

?, help, -h, and --help print this text. Quote ? if the shell expands it.
--folder help is a folder named help.
"""


def main() -> None:
    p = argparse.ArgumentParser(
        description="Warp scan / plan / status",
        epilog=SCAN_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("scan")
    ps.add_argument("--root", default=".")
    ps.add_argument("--folder", help="limit the scan to this folder (path or name under root)")
    ps.add_argument("--out", default=".warp/beam.json")
    ps.add_argument("--max-agents", type=int, default=18)
    ps.add_argument("--model", default="claude-sonnet-5-5-high")
    pe = sub.add_parser("export")
    pe.add_argument("--beam", default=".warp/beam.json")
    pe.add_argument("--out", default=".warp/WARP_PLAN.json")
    pi = sub.add_parser("import")
    pi.add_argument("--plan", required=True)
    pi.add_argument("--beam", default=".warp/beam.json")
    pi.add_argument("--keep-status", action="store_true", default=True)
    sub.add_parser("status").add_argument("--beam", default=".warp/beam.json")
    pb = sub.add_parser("bundle")
    pb.add_argument("--beam", default=".warp/beam.json")
    pb.add_argument("--out", default=".warp/warp-review.zip")
    for name in ("start", "stop", "pause", "resume"):
        sp = sub.add_parser(name)
        sp.add_argument("--beam", default=".warp/beam.json")
        sp.add_argument("--reason")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    if args.cmd == "scan":
        root = Path(args.root).resolve()
        folder = None
        if args.folder:
            folder, cands, err = resolve_folder(root, args.folder)
            if err:
                sys.exit(f"scan: {err}")
            if folder is None:
                withplans = [c for c in cands if has_plan_files(c)]
                if len(withplans) == 1:
                    folder = withplans[0]
                    others = ", ".join(str(c.relative_to(root)) for c in cands if c != folder)
                    print(f"note: {args.folder!r} matches {len(cands)} folders; only {folder.relative_to(root)} has plan files (also: {others})")
                else:
                    print(f"scan: {args.folder!r} is ambiguous. Matching folders:")
                    for c in cands:
                        print(f"  {c.relative_to(root)}  {'has plan files' if c in withplans else 'no plan files'}")
                    print("Re-run with the full path, for example: /warp-scan " + str(cands[0].relative_to(root)))
                    sys.exit(3)
        found = scan(root, folder)
        if not args.folder:
            where = plan_folders(found)
            if len(where) > 1:
                print(f"note: plans found in {len(where)} folders: {', '.join(where)}")
                print("      scanning all of them and using the richest. Pass a folder to scope: /warp-scan <folder>")
        warp = root / ".warp"
        warp.mkdir(parents=True, exist_ok=True)
        atomic_write(warp / "scan.json", json.dumps(found, indent=2) + "\n")
        graph = pick(root, found)
        if not graph:
            where = f" in {folder.relative_to(root)}" if folder else ""
            print(f"no plan found{where} — looked for CURSOR_PLAN.md, schedule.json, WARP_PLAN.json, jira ticket json")
            sys.exit(2)
        sched_path = warp / "_ingested_schedule.json"
        atomic_write(sched_path, json.dumps(to_schedule(graph), indent=2) + "\n")
        cfg = default_config()
        cfg["maxAgents"] = args.max_agents
        cfg["model"] = args.model
        beam = ingest(sched_path, Path(graph["source"]), Path(args.out), cfg)
        beam["runState"] = "stopped"
        beam["source"]["format"] = graph.get("format")
        beam["source"]["scan"] = found
        beam["source"]["folder"] = found.get("folder")
        beam.setdefault("program", {})["estimate"] = estimate(
            list(beam["tickets"].values()),
            beam.get("gates") or [],
            beam.get("program", {}).get("criticalPath") or [],
            args.max_agents,
        )
        atomic_write(Path(args.out), json.dumps(beam, indent=2) + "\n")
        est = beam["program"]["estimate"]
        if folder:
            print(f"scope folder={folder.relative_to(root)}")
        print(f"scan format={graph.get('format')} tickets={len(beam['tickets'])} source={graph.get('source')}")
        print(f"estimate agentHours={est['agentHours']} humanHours={est['humanHours']} elapsedHours={est['elapsedHours']}")
        print("runState=stopped — /warp-start to dispatch")
        try:
            import notify

            notify.report(
                notify.build(
                    "scan",
                    root,
                    {
                        "format": graph.get("format"),
                        "tickets": len(beam["tickets"]),
                        "gates": len(beam.get("gates") or []),
                        "estimate": est,
                        "source": graph.get("source"),
                        "found": found,
                        "folder": found.get("folder"),
                    },
                )
            )
        except Exception as e:
            print(f"herald: not posted ({e})")
    elif args.cmd == "export":
        export_plan(Path(args.beam), Path(args.out))
    elif args.cmd == "import":
        raw = json.loads(Path(args.plan).read_text())
        graph = raw if "tickets" in raw and isinstance(raw["tickets"], list) else None
        if graph and raw.get("kind") == "warp-plan":
            graph = {"tickets": raw["tickets"], "gates": raw.get("gates") or [], "criticalPath": raw.get("criticalPath") or []}
        if not graph:
            sys.exit("plan has no tickets")
        old = load_json(Path(args.beam)) if Path(args.beam).exists() else None
        sched_path = Path(args.beam).parent / "_imported_schedule.json"
        atomic_write(sched_path, json.dumps(to_schedule(graph), indent=2) + "\n")
        cfg = (old or {}).get("config") or default_config()
        beam = ingest(sched_path, Path(args.plan), Path(args.beam), cfg)
        if old and args.keep_status:
            for tid, t in beam["tickets"].items():
                prev = old["tickets"].get(tid)
                if not prev:
                    continue
                for k in ("status", "agent", "branch", "attempts", "tokens", "minutes", "alarm", "pr", "jira", "jiraKey"):
                    t[k] = prev.get(k, t.get(k))
        beam["runState"] = "stopped"
        atomic_write(Path(args.beam), json.dumps(beam, indent=2) + "\n")
        journal(Path(args.beam), {"type": "import", "plan": args.plan, "tickets": len(beam["tickets"])})
        print(f"replaced plan tickets={len(beam['tickets'])} runState=stopped")
    elif args.cmd == "status":
        write_status(Path(args.beam))
    elif args.cmd == "bundle":
        bundle(Path(args.beam), Path(args.out))
    elif args.cmd == "start":
        set_run(Path(args.beam), "running", args.reason)
    elif args.cmd == "resume":
        set_run(Path(args.beam), "running", args.reason)
    elif args.cmd == "pause":
        set_run(Path(args.beam), "paused", args.reason)
    elif args.cmd == "stop":
        set_run(Path(args.beam), "stopped", args.reason or "stop")


if __name__ == "__main__":
    main()
