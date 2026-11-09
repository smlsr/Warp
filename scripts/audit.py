#!/usr/bin/env python3
"""The audit, every 30 minutes (1.7.15): throughput, load, rebalancing, coordinators, gap discovery, the report.

Throughput (REST): merges into main in the last 1, 3 and 12 hours; open pull requests by state (CI pending, red,
Bugbot pending, dirty, waiting on the lane, held); the median time from READY to merged; the share of pull requests
that went dirty while their CI was running.

Load: one row per coordinator: running children, open pull requests owned, queued gaps, last activity, refresh
backlog. Coordinators are `.warp/coordinators.json`; Warp itself is the coordinator `warp`.

Rebalance: whole stacked chains (never one pull request out of a chain) move from a coordinator over `load.high` (25)
to one under `load.low` (8) or idle, same area first. Handoff: the old owner stops its workers and writes
`HANDED-OFF <coordinator> <head>`; the new owner pushes only after it sees that mark (handoff_ready).

Coordinators: a new one is started when open work per coordinator exceeds `load.spawnAt` (30), or an area has more
than `load.unownedSpawnAt` (15) unowned items. It is named by area and gets the rules file and the waiver registry.
One with zero owned items for `load.retireAfter` (2) audits is retired. `maxAgents` is per coordinator; the sum stays
under `agents.ceiling` (120).

Gap discovery: live-run failures citing a product gap, open GAP-REPORT rows with no ticket, RESULT.json
`follow_ups[]`, and the same red check across several pull requests; each becomes a gap ticket.

Report: `.warp/audit/<ts>.md` (headline, throughput, load, top 10 blockers, actions, decisions needed) and a 5-line
Slack summary.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from statistics import median
from typing import Callable, Optional

COORDINATORS = "coordinators.json"


# ---- throughput ---------------------------------------------------------------------------------------------------


def throughput(pulls: list, lane_merged: list, now=None, base: str = "main") -> dict:
    import beam as beam_mod

    now_dt = beam_mod.coerce_now(now)[0]
    merges = {1: 0, 3: 0, 12: 0}
    states = {"ci-pending": 0, "red": 0, "bugbot-pending": 0, "dirty": 0, "lane": 0, "held": 0, "other": 0}
    dirty_during_ci = 0
    open_count = 0
    for pull in pulls:
        merged = beam_mod.parse_ts(str(pull.get("merged_at") or ""))
        if merged is not None and str(((pull.get("base") or {}).get("ref")) or base) == base:
            hours = (now_dt - merged).total_seconds() / 3600
            for window in merges:
                if hours <= window:
                    merges[window] += 1
        if str(pull.get("state") or "").lower() != "open":
            continue
        open_count += 1
        state = pull.get("_state") or "other"
        states[state if state in states else "other"] += 1
        if pull.get("_dirtyDuringCi"):
            dirty_during_ci += 1
    waits = []
    for row in lane_merged:
        ready = beam_mod.parse_ts(str(row.get("readyAt") or ""))
        done = beam_mod.parse_ts(str(row.get("at") or ""))
        if ready is not None and done is not None:
            waits.append((done - ready).total_seconds() / 60)
    return {"merges": merges, "open": open_count, "states": states,
            "readyToMergedMinutes": round(median(waits), 1) if waits else None,
            "dirtyDuringCi": round(100.0 * dirty_during_ci / open_count, 1) if open_count else 0.0}


# ---- coordinators and load ------------------------------------------------------------------------------------------


def load_coordinators(beam_path: Path) -> dict:
    try:
        doc = json.loads((Path(beam_path).parent / COORDINATORS).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_coordinators(beam_path: Path, doc: dict) -> None:
    (Path(beam_path).parent / COORDINATORS).write_text(json.dumps(doc, indent=2) + "\n")


def load_table(data: dict, coords: dict, agents: list) -> list:
    """[{name, area, children, prs, gaps, lastActivity, refresh}] for every live coordinator (and warp)."""
    owners = data.get("prOwners") if isinstance(data.get("prOwners"), dict) else {}
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    refresh = data.get("refresh") if isinstance(data.get("refresh"), dict) else {}
    names = {"warp"} | {n for n, c in (coords.get("coordinators") or {}).items() if not (c or {}).get("retiredAt")}
    rows = []
    for name in sorted(names):
        info = (coords.get("coordinators") or {}).get(name) or {}
        prs = sorted(k for k, v in owners.items() if isinstance(v, dict) and (v.get("coordinator") or "warp") == name)
        rows.append({
            "name": name, "area": info.get("area") or "",
            "children": sum(1 for a in agents if a.get("parent") == name and a.get("class") in {"active", "stalled", "stale-wait"}),
            "prs": prs,
            "gaps": sum(1 for t in tickets.values() if isinstance(t, dict) and t.get("gap") and t.get("status") == "queued"
                        and (t.get("coordinator") or "warp") == name),
            "lastActivity": max([a.get("lastActivity") or "" for a in agents if a.get("parent") == name] or [""]),
            "refresh": sum(1 for k in prs if k in refresh),
        })
    return rows


def chains(data: dict) -> list:
    """Stacked chains of pull request numbers (parent first), from each ticket's pr.base -> another ticket's branch."""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    by_branch, parent_of = {}, {}
    numbers = {}
    for tid, t in tickets.items():
        if not isinstance(t, dict):
            continue
        pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
        number = re.search(r"/pull/(\d+)", str(pr.get("url") or ""))
        if not number:
            continue
        numbers[tid] = number.group(1)
        for b in (t.get("branch"), pr.get("headRef")):
            if b:
                by_branch[str(b)] = tid
    for tid, t in tickets.items():
        base = str(((t or {}).get("pr") or {}).get("base") or "") if isinstance(t, dict) else ""
        if tid in numbers and base in by_branch:
            parent_of[numbers[tid]] = numbers[by_branch[base]]
    roots: dict = {}

    def root(n):
        seen = set()
        while n in parent_of and n not in seen:
            seen.add(n)
            n = parent_of[n]
        return n

    for n in numbers.values():
        roots.setdefault(root(n), []).append(n)
    out = []
    for r, members in roots.items():
        order = sorted(set(members), key=lambda n: (0 if n == r else 1, int(n)))
        out.append(order)
    return out


def rebalance(data: dict, table: list, cfg: dict) -> list:
    """[(chain, from, to)]: whole chains from over load.high to under load.low (same area first)."""
    import selfdrive

    high, low = selfdrive.num(cfg, "load", "high"), selfdrive.num(cfg, "load", "low")
    owners = data.get("prOwners") if isinstance(data.get("prOwners"), dict) else {}
    load = {row["name"]: len(row["prs"]) for row in table}
    area = {row["name"]: row["area"] for row in table}
    moves = []
    for row in sorted(table, key=lambda r: -len(r["prs"])):
        if load[row["name"]] <= high:
            continue
        mine = set(row["prs"])
        for chain in sorted(chains(data), key=len, reverse=True):
            if load[row["name"]] <= high:
                break
            if not chain or not set(chain) <= mine:
                continue
            light = [r for r in table if r["name"] != row["name"] and load[r["name"]] < low]
            if not light:
                break
            light.sort(key=lambda r: (0 if area[r["name"]] == row["area"] and row["area"] else 1, load[r["name"]]))
            target = light[0]["name"]
            moves.append((chain, row["name"], target))
            load[row["name"]] -= len(chain)
            load[target] += len(chain)
            for n in chain:
                if isinstance(owners.get(n), dict):
                    owners[n]["handoff"] = {"from": row["name"], "to": target, "state": "requested"}
    return moves


def handoff_ready(beam_path: Path, pr: str, new_owner: str, head: str, outbox_reader: Optional[Callable] = None) -> bool:
    """The new owner may push only after the old owner wrote `HANDED-OFF <new owner> <head>`."""
    text = (outbox_reader or _outboxes)(beam_path)
    pattern = re.compile(r"HANDED-OFF\s+%s\s+([0-9a-f]{7,40})" % re.escape(new_owner))
    for match in pattern.finditer(text):
        if head.startswith(match.group(1)) or match.group(1).startswith(head[:7]):
            return True
    return False


def _outboxes(beam_path: Path) -> str:
    root = Path(beam_path).resolve().parent.parent
    texts = []
    for pattern in (".warp/outbox/*.md", ".warp/coordinators/*/outbox*.md", ".warp/outbox.md"):
        for name in glob.glob(str(root / pattern)):
            try:
                texts.append(Path(name).read_text(errors="replace"))
            except OSError:
                pass
    return "\n".join(texts)


def coordinators_step(beam_path: Path, data: dict, coords: dict, table: list, unowned_by_area: dict, cfg: dict,
                      now=None) -> list:
    """Spawn by area when overloaded; retire after retireAfter empty audits. Mutates coords. Lines say what to do."""
    import beam as beam_mod
    import selfdrive

    now_s = beam_mod.coerce_now(now)[1]
    rows = coords.setdefault("coordinators", {})
    live = [r for r in table]
    total = sum(len(r["prs"]) + r["gaps"] for r in live) + sum(unowned_by_area.values())
    out = []
    per = total / max(1, len(live))
    ceiling = selfdrive.num(cfg, "agents", "ceiling")
    max_agents = int((data.get("config") or {}).get("maxAgents") or 18)
    room = ceiling - max_agents * len(live)
    want = []
    if per > selfdrive.num(cfg, "load", "spawnAt"):
        busiest = max(unowned_by_area.items(), key=lambda kv: kv[1])[0] if unowned_by_area else (live[0]["area"] or "general")
        want.append(busiest or "general")
    for area, count in sorted(unowned_by_area.items()):
        if count > selfdrive.num(cfg, "load", "unownedSpawnAt") and area not in want:
            want.append(area)
    for area in want:
        name = "coord-%s" % re.sub(r"[^a-z0-9]+", "-", str(area).lower()).strip("-")
        if name in rows and not rows[name].get("retiredAt"):
            continue
        if room < max_agents:
            out.append("coordinator: %s not started: agents.ceiling %d would be passed (%d coordinators x maxAgents %d)" % (
                name, ceiling, len(live), max_agents))
            continue
        room -= max_agents
        rows[name] = {"area": area, "createdAt": now_s, "emptyAudits": 0, "maxAgents": max_agents}
        rows[name]["start"] = True
        out.append("coordinator: start %s for area %s (open work per coordinator %.0f > %d, or %d unowned in %s); "
                   "maxAgents %d is its own cap" % (name, area, per, selfdrive.num(cfg, "load", "spawnAt"),
                                                     unowned_by_area.get(area, 0), area, max_agents))
    by_name = {r["name"]: r for r in table}
    for name, info in rows.items():
        if info.get("retiredAt") or name not in by_name and info.get("createdAt") == now_s:
            continue
        row = by_name.get(name)
        empty = not row or (not row["prs"] and not row["gaps"] and not row["children"])
        info["emptyAudits"] = int(info.get("emptyAudits") or 0) + 1 if empty else 0
        if info["emptyAudits"] >= selfdrive.num(cfg, "load", "retireAfter"):
            info["retiredAt"] = now_s
            info["stop"] = True
            out.append("coordinator: retire %s (no owned items for %d audits)" % (name, info["emptyAudits"]))
    return out


# ---- gap discovery ---------------------------------------------------------------------------------------------------


_GAP_ROW = re.compile(r"^\|\s*(?P<id>[A-Z][A-Z0-9-]+)\s*\|(?P<rest>.*)\|\s*$")
_CLOSED = re.compile(r"^[\W_]*(closed|done|fixed|resolved|merged|landed|won'?t[ -]?do|duplicate)\b", re.I)
_TICKET_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[A-Z]?\d+[A-Z0-9]*\b")
# The living report only (1.7.17). A pack folder's spec/GAP-REPORT.md is a read-only baseline: reading it as well made
# every row twice, and resolved baseline rows became tickets again.
REPORT_PATHS = ("docs/spec/GAP-REPORT.md", "docs/GAP-REPORT.md", "GAP-REPORT.md")


def report_paths(root: Path, configured=None) -> list:
    for rel in list(configured or []) or list(REPORT_PATHS):
        path = Path(root) / str(rel)
        if path.is_file():
            return [path]
    return []


def _lands_column(header: list) -> int:
    for index, cell in enumerate(header):
        low = cell.strip().lower()
        if low.startswith("lands") or low.startswith("landed") or low in {"landing", "fixed by", "tickets", "fix"}:
            return index
    return -1


def row_closed(cells: list, lands: int, tickets: dict) -> bool:
    """Closed when any cell starts with closed/done/fixed/resolved/merged (`closed (E27-S06)` counts), or every
    ticket id in its lands-in column is merged or done in the beam."""
    import terminal

    if any(_CLOSED.match(c) for c in cells):
        return True
    if 0 <= lands < len(cells):
        ids = _TICKET_ID.findall(cells[lands])
        rows = [tickets.get(i) for i in ids]
        if ids and all(isinstance(r, dict) and terminal.is_terminal(r) for r in rows):
            return True
    return False


def discover(root: Path, data: dict, red_by_check: dict, report_files=None) -> list:
    """[{area, short, summary, source, row, gid}] for causes nobody owns."""
    import gaps

    found = []
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for path in report_paths(root, report_files):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        lands = -1
        import gapreport

        # 1.7.20: a repeated row or a stale open copy of a closed gap is not an open gap (the audit used to file new
        # work for gaps that were already closed).
        shape = gapreport.classify(text)
        skip = set(shape["copies"]) | set(shape["stale"])
        for number, line in enumerate(text.splitlines()):
            if number in skip:
                continue
            if line.startswith("|") and not _GAP_ROW.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if cells and not set("".join(cells)) <= set("-: "):
                    lands = _lands_column(cells[1:])
                continue
            match = _GAP_ROW.match(line)
            if not match or match.group("id") in {"ID", "GAP"}:
                if match and match.group("id") in {"ID", "GAP"}:
                    lands = _lands_column([c.strip() for c in match.group("rest").split("|")])
                continue
            cells = [c.strip() for c in match.group("rest").split("|")]
            if row_closed(cells, lands, tickets):
                continue
            rid = match.group("id")
            summary = next((c for c in cells if len(c) > 12), cells[0] if cells else rid)
            if gaps.find(data, rid, summary, rid):
                continue
            # The row id is the gap id (1.7.17): G-001 stays G-001, not G-G-G-001.
            found.append({"area": rid.split("-")[0], "short": rid, "summary": summary, "source": "GAP-REPORT",
                          "row": rid, "gid": rid if rid.startswith("G-") else gaps.gap_id(rid.split("-")[0], rid)})
    for name in glob.glob(str(Path(root) / ".warp" / "tickets" / "*" / "RESULT.json")):
        try:
            doc = json.loads(Path(name).read_text())
        except (OSError, ValueError):
            continue
        owner = Path(name).parent.name
        for item in doc.get("follow_ups") or [] if isinstance(doc, dict) else []:
            text = item if isinstance(item, str) else str((item or {}).get("summary") or (item or {}).get("title") or "")
            if text and not gaps.find(data, "", text):
                found.append({"area": str((tickets.get(owner) or {}).get("module") or owner.split("-")[0]),
                              "short": re.sub(r"[^A-Za-z0-9]+", "-", text)[:20], "summary": text,
                              "source": "follow-up of %s" % owner, "row": ""})
    for check, prs in sorted(red_by_check.items()):
        if len(prs) >= 3:
            summary = "%s is red on %d pull requests (%s)" % (check, len(prs), ", ".join("#%s" % p for p in prs[:5]))
            if not gaps.find(data, "", summary.split(" is red")[0] + " is red"):
                found.append({"area": "CI", "short": re.sub(r"[^A-Za-z0-9]+", "-", check)[:20], "summary": summary,
                              "source": "repeated red", "row": ""})
    return found


def report(beam_path: Path, headline: str, through: dict, table: list, top: list, actions: list, decisions: list,
           now=None) -> tuple:
    import beam as beam_mod
    import selfdrive

    now_s = beam_mod.coerce_now(now)[1]
    path = selfdrive.report_path(beam_path, "audit", now_s)
    m = through["merges"]
    body = ["# Audit %s" % now_s, "", headline, "", "## Throughput", "",
            "- merges into main: %d in 1h, %d in 3h, %d in 12h" % (m[1], m[3], m[12]),
            "- open pull requests: %d (%s)" % (through["open"], ", ".join("%s %d" % kv for kv in through["states"].items() if kv[1])),
            "- median READY to merged: %s" % ("%s min" % through["readyToMergedMinutes"] if through["readyToMergedMinutes"] is not None else "-"),
            "- went dirty while CI ran: %s%%" % through["dirtyDuringCi"], "", "## Load", "",
            "| coordinator | area | children | open PRs | queued gaps | last activity | refresh backlog |",
            "|---|---|---|---|---|---|---|"]
    body += ["| %s | %s | %d | %d | %d | %s | %d |" % (r["name"], r["area"] or "-", r["children"], len(r["prs"]), r["gaps"],
                                                     r["lastActivity"] or "-", r["refresh"]) for r in table]
    body += ["", "## Top blockers", ""] + (["- %s (score %s): holds up %s" % (n, s, ", ".join(d[:6])) for n, s, d in top] or ["- none"])
    body += ["", "## Actions", ""] + (["- " + a for a in actions] or ["- none"])
    body += ["", "## Decisions needed", ""] + (["- " + d for d in decisions] or ["- none"])
    path.write_text("\n".join(body) + "\n")
    slack = [
        "Audit: %s" % headline,
        "Merges: %d (1h) / %d (3h) / %d (12h); %d open" % (m[1], m[3], m[12], through["open"]),
        "Load: %s" % ", ".join("%s %d" % (r["name"], len(r["prs"])) for r in table),
        "Top blocker: %s" % (("%s holds up %d" % (top[0][0], len(top[0][2]))) if top else "none"),
        "Actions: %d; decisions needed: %d" % (len(actions), len(decisions)),
    ]
    return path, slack
