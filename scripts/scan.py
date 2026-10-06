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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beam import (  # noqa: E402
    ACTIVE,
    TERMINAL,
    atomic_write,
    auto_merge,
    configured_cap,
    default_config,
    ingest,
    journal,
    load_json,
    metrics,
    normalize_size,
    ready,
    resolve_auto_merge_sizes,
    utcnow,
    watchdog,
)

SKIP = {".git", "node_modules", "vendor", "dist", ".warp", "coverage"}
ID_RE = re.compile(r"\b([A-Z]{1,4}-\d{1,4})\b")
AFTER_RE = re.compile(r"\bafter\s+([A-Z0-9][A-Z0-9,\s\-]{0,80})", re.I)
BRACKET_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d+)\]")
JIRA_FIELD_RE = re.compile(r"\bJira(?:\s*Key)?\s*[:=]\s*([A-Z][A-Z0-9]+-\d+)\b", re.I)


def walk(base: Path):
    for p in sorted(base.rglob("*")):
        if any(part in SKIP for part in p.relative_to(base).parts):
            continue
        if p.is_file():
            yield p


def resolve_folder(root: Path, arg: str) -> tuple[Optional[Path], list[Path], Optional[str]]:
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


def scan(root: Path, folder: Optional[Path] = None) -> dict:
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
    return normalize_size(text) or "M"


def _note_jira(ticket: dict, raw: Optional[str]) -> None:
    if not raw or ticket.get("jiraKeySource") == "plan":
        return
    text = raw.strip()
    if not text or text.lower() in {"-", "—", "none", "n/a"}:
        return
    ticket["jiraKey"] = text
    ticket["jiraKeySource"] = "plan"


def from_schedule(path: Path) -> Optional[dict]:
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
        raw_key = t.get("jiraKey") or t.get("jira")
        tickets.append(
            {
                "id": tid,
                "jiraKey": raw_key or None,
                "jiraKeySource": t.get("jiraKeySource") if t.get("jiraKeySource") in {"plan", "export", "map", "manual", "external", "label", "link", "summary"} else ("plan" if raw_key else None),
                "jiraKeyConfidence": t.get("jiraKeyConfidence"),
                "jiraKeyForced": bool(t.get("jiraKeyForced")),
                "summary": t.get("summary") or t.get("title") or "",
                "deps": t.get("deps") or t.get("blockedBy") or t.get("blockedByTempIds") or [],
                "after": t.get("after") or [],
                "locks": t.get("locks") or [],
                "starred": bool(t.get("starred") or t.get("star")),
                "rank": t.get("rank"),
                "size": _size_from_label(str(t.get("size") or "M")),
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
    return {
        "tickets": tickets,
        "gates": data.get("gates") or [],
        "criticalPath": data.get("criticalPath") or [],
        "source": str(path),
        "format": "schedule.json",
    }


_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_H2_RE = re.compile(r"(?im)^(?:h[1-6]\.\s*|#{1,6}\s+)(.+?)\s*$")


def _issue_key(raw) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    return text if _ISSUE_KEY_RE.fullmatch(text) else None


def _wiki_sections(text: str) -> dict[str, str]:
    """Jira wiki `h2. Heading` and markdown `## Heading` bodies, keyed by heading."""
    matches = list(_H2_RE.finditer(text or ""))
    out = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[match.group(1).strip().casefold()] = text[start:end].strip()
    return out


def _section(sections: dict, *names: str) -> str:
    for name in names:
        body = sections.get(name.casefold())
        if body:
            return body
    return ""


def _lines_of(body: str) -> list[str]:
    rows = []
    for line in (body or "").splitlines():
        text = re.sub(r"^\s*(?:[*\-]|#)+\s*", "", line).strip()
        if text:
            rows.append(text)
    return rows


def _is_none(text: str) -> bool:
    return text.strip().casefold() in {"", "none", "n/a", "na", "-", "—"}


def _apply_import_labels(ticket: dict, labels) -> None:
    for raw in labels or []:
        text = str(raw).strip()
        low = text.casefold()
        if low == "auto-merge":
            ticket["autoMerge"] = True
        elif low == "warp-harness" or low.startswith("warp:"):
            continue
        elif low.startswith("size:"):
            ticket["size"] = _size_from_label(text.split(":", 1)[1])
        elif low.startswith("area:"):
            ticket["module"] = text.split(":", 1)[1].strip() or ticket.get("module")


def _from_import_issue(row: dict, fields: dict, external: str) -> dict:
    """Jira import JSON. externalId is the plan id. It is never the issue key."""
    sections = _wiki_sections(str(row.get("description") or fields.get("description") or ""))
    size_body = _section(sections, "size")
    size_line = _lines_of(size_body)[:1]
    size = _size_from_label(size_line[0]) if size_line else None
    locks_body = _section(sections, "locks")
    locks = [] if _is_none(locks_body) else [line for line in _lines_of(locks_body) if not _is_none(line)]
    blocked = _section(sections, "blocked by", "blockedby")
    deps = [] if _is_none(blocked) else ID_RE.findall(blocked)
    acceptance = _lines_of(_section(sections, "acceptance"))
    raw_key = _issue_key(row.get("key") or fields.get("key"))
    jira_key = raw_key if raw_key and raw_key != external.strip().upper() else None
    ticket = {
        "id": external.strip(),
        "externalId": external.strip(),
        "jiraKey": jira_key,
        "jiraKeySource": "export" if jira_key else None,
        "summary": row.get("summary") or fields.get("summary") or "",
        "deps": deps,
        "locks": locks or list(row.get("locks") or []),
        "size": size or row.get("size") or "M",
        "critical": False,
        "rankDays": 0,
        "module": None,
        "priority": row.get("priority") or fields.get("priority"),
        "acs": acceptance,
        "jiraStatus": row.get("status") or fields.get("status") if isinstance(row.get("status") or fields.get("status"), str) else None,
    }
    if "auto-merge" in size_body.casefold():
        ticket["autoMerge"] = True
    _apply_import_labels(ticket, row.get("labels") or fields.get("labels"))
    return ticket


def from_jira(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    rows = None
    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        rows = data["tickets"]
    elif isinstance(data, dict) and isinstance(data.get("issues"), list):
        rows = data["issues"]
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
        if not isinstance(t, dict):
            continue
        fields = t.get("fields") if isinstance(t.get("fields"), dict) else {}
        external = str(t.get("externalId") or fields.get("externalId") or "").strip()
        if external:
            tickets.append(_from_import_issue(t, fields, external))
            continue
        body = fields or t
        tid = t.get("tempId") or t.get("key") or body.get("tempId") or t.get("id")
        if not tid:
            continue
        deps = list(t.get("blockedByTempIds") or t.get("blockedBy") or [])
        for link in body.get("issuelinks") or []:
            if link.get("type", {}).get("name", "").lower() in {"blocks", "is blocked by"}:
                inward = (link.get("inwardIssue") or {}).get("key")
                if inward:
                    deps.append(inward)
        summary = t.get("summary") or body.get("summary") or ""
        explicit = _issue_key(t.get("key") or body.get("key"))
        tickets.append(
            {
                "id": str(tid),
                "jiraKey": explicit,
                "jiraKeySource": "export" if explicit else None,
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
    return {"tickets": tickets, "gates": [], "criticalPath": [], "source": str(path), "format": "jira-json"}


def from_markdown(path: Path) -> Optional[dict]:
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
                "jiraKey": None,
                "deps": deps,
                "locks": [x.strip() for x in (row.get("locks") or "").split(",") if x.strip()],
                "size": _size_from_label(row.get("size") or row.get("complexity") or "M"),
                "critical": "yes" in (row.get("critical") or "").lower() or "★" in line,
                "rankDays": 0,
            }
            jraw = row.get("jira key") or row.get("jirakey") or row.get("jira") or ""
            _note_jira(tickets[tid.strip()], jraw)
            field = JIRA_FIELD_RE.search(line)
            bracket = BRACKET_RE.search(line)
            _note_jira(tickets[tid.strip()], field.group(1) if field else None)
            _note_jira(tickets[tid.strip()], bracket.group(1) if bracket else None)
    # CURSOR_PLAN wave form: ticket ids and "after X"
    wave = 0
    gates = []
    current_gate = None
    last_tid = None
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
        if line.strip().startswith("|"):
            continue
        field = JIRA_FIELD_RE.search(line)
        field_keys = {field.group(1).upper()} if field else set()
        bracketed = {k.upper() for k in BRACKET_RE.findall(line)}
        excluded = bracketed | field_keys
        ids = [i for i in ID_RE.findall(line) if i.upper() not in excluded]
        after = AFTER_RE.search(line)
        after_ids = [i for i in (ID_RE.findall(after.group(1)) if after else []) if i.upper() not in excluded]
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
            last_tid = tid
            field = JIRA_FIELD_RE.search(line)
            _note_jira(tickets[tid], field.group(1) if field else None)
            if len(bracketed) == 1:
                _note_jira(tickets[tid], next(iter(bracketed)))
            for d in after_ids:
                if d != tid and d not in tickets[tid]["deps"]:
                    tickets[tid]["deps"].append(d)
            if gates and tid in gates[-1]["members"]:
                tickets[tid]["gate"] = gates[-1]["key"]
        if not defined and last_tid and last_tid in tickets:
            field = JIRA_FIELD_RE.search(line)
            if field:
                _note_jira(tickets[last_tid], field.group(1))
    if len(tickets) < 2:
        return None
    rows = list(tickets.values())
    return {
        "tickets": rows,
        "gates": gates,
        "criticalPath": [t["id"] for t in tickets.values() if t.get("critical")],
        "source": str(path),
        "format": "markdown",
    }


def pick(root: Path, found: dict) -> Optional[dict]:
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


def _needs_review(ticket: dict, auto_sizes) -> bool:
    """A size in autoMergeSizes is not a human review. L and XL are not special."""
    if normalize_size(ticket.get("size")):
        return not auto_merge(ticket.get("size"), auto_sizes)
    if ticket.get("autoMerge") is False:
        return True
    if ticket.get("autoMerge") is True:
        return False
    return True


def estimate(tickets: list[dict], gates: list[dict], critical: list[str], max_agents: int, auto_sizes=None) -> dict:
    """Agent hours are implementation time. Human hours assume each approval
    and each gate check is answered within 30 minutes."""
    by_id = {t["id"]: t for t in tickets}
    allowed = auto_sizes if auto_sizes is not None else ["S", "M"]
    agent = round(sum(ticket_hours(t) for t in tickets), 1)
    reviews = [t["id"] for t in tickets if _needs_review(t, allowed)]
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
            "Each approval for a size not in autoMergeSizes is provided within 30 minutes.",
            "Each blocking gate check is signed within 30 minutes.",
            "Sizes in autoMergeSizes add no human wait. The default list is S, M.",
            "Elapsed hours are the longer of the critical chain plus those waits, and agent hours divided by maxAgents.",
        ],
    }


def to_schedule(graph: dict, auto_sizes=None) -> dict:
    return {
        "module": "warp",
        "generated": utcnow(),
        "tickets": [
            {
                "id": t["id"],
                "jiraKey": t.get("jiraKey"),
                "jiraKeySource": t.get("jiraKeySource"),
                "summary": t.get("summary") or "",
                "deps": t.get("deps") or [],
                "unlocks": [],
                "locks": t.get("locks") or [],
                "size": (size := _size_from_label(str(t.get("size") or "M"))),
                "hours": t.get("hours") or {"S": 4, "M": 7, "L": 11, "XL": 16}.get(size, 7),
                "critical": bool(t.get("critical")),
                "starred": bool(t.get("starred") or t.get("star")),
                "rank": t.get("rank"),
                "after": list(t.get("after") or []),
                "rankDays": t.get("rankDays") or 0,
                "gate": t.get("gate"),
                "module": t.get("module"),
                "layer": t.get("layer"),
                "priority": t.get("priority"),
                "acs": t.get("acs"),
                "autoMerge": auto_merge(size, auto_sizes if auto_sizes is not None else ["S", "M"]),
                "externalId": t.get("externalId"),
                "jiraStatus": t.get("jiraStatus"),
            }
            for t in graph["tickets"]
        ],
        "gates": graph.get("gates") or [],
        "criticalPath": graph.get("criticalPath") or [],
        "estimate": estimate(
            graph["tickets"],
            graph.get("gates") or [],
            graph.get("criticalPath") or [],
            configured_cap({"maxAgents": graph.get("maxAgents")} if graph.get("maxAgents") not in (None, "") else {}),
            auto_sizes,
        ),
    }


def write_status(beam_path: Path) -> None:
    beam = load_json(beam_path)
    beam["metrics"] = metrics(beam)
    done, left, working = [], [], []
    for t in sorted(beam["tickets"].values(), key=lambda x: x["id"]):
        pr = t.get("pr") or {}
        row = {
            "id": t["id"],
            "status": t["status"],
            "summary": t.get("summary"),
            "size": t.get("size"),
            "autoMerge": bool(t.get("autoMerge")),
            "pr": pr.get("url"),
            "bugbot": pr.get("bugbot"),
            "ci": pr.get("ci"),
            "bugbotFixed": pr.get("bugbotFixed") or 0,
            "sha": pr.get("sha"),
            "via": pr.get("via"),
            "jiraDone": bool((t.get("jira") or {}).get("doneAt")),
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
    lines += [
        f"- **{t['id']}** {t['status']} bugbot={t.get('bugbot') or 'none'} ci={t.get('ci') or 'none'} fixed={t.get('bugbotFixed') or 0} agent={t['agent']} pr={t['pr'] or '—'} — {t['summary']}"
        for t in working
    ] or ["None."]
    lines += ["", "## Done", ""]
    lines += [
        f"- **{t['id']}** {t['status']} sha={t.get('sha') or 'none'} via={t.get('via') or 'none'} jira={'Done' if t.get('jiraDone') else 'not Done'} — {t['summary']}"
        for t in done[:40]
    ] or ["None."]
    if len(done) > 40:
        lines.append(f"- … {len(done) - 40} more in status.json")
    lines += ["", "## Left", ""]
    lines += [f"- **{t['id']}** {t['status']} {t['size']} — {t['summary']}" for t in left[:40]] or ["None."]
    if len(left) > 40:
        lines.append(f"- … {len(left) - 40} more in status.json")
    if (beam_path.parent / "warp-complete.html").is_file():
        lines += ["", f"Report: warp-complete.html generated {beam.get('reportGeneratedAt') or 'unknown'}"]
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
        configured_cap(beam.get("config") or {}),
        (beam.get("config") or {}).get("autoMergeSizes"),
    )
    plan = {
        "kind": "warp-plan",
        "version": 1,
        "exportedAt": utcnow(),
        "estimate": est,
        "suggestion": {
            "summary": "Critical path first. Parallel only across non-overlapping locks. Sizes in autoMergeSizes auto-merge; sizes not in autoMergeSizes wait for APPROVED.",
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
                "jiraKeySource": t.get("jiraKeySource"),
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


def set_run(beam_path: Path, state: str, reason: Optional[str], announce_report: bool = True) -> None:
    beam = load_json(beam_path)
    beam["runState"] = state
    beam["paused"] = state != "running"
    beam["pauseReason"] = reason
    if state == "running":
        beam["runComplete"] = False
        beam["stoppedAt"] = None
    if state == "stopped":
        beam["stoppedAt"] = utcnow()
    atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
    journal(beam_path, {"type": state, "reason": reason})
    if state in {"paused", "stopped"}:
        import session_note

        session_note.note(beam_path.parent, "session-stop")
        try:
            import inbound

            print(inbound.release(beam_path))
        except Exception as e:
            print("listener: not stopped (%s)" % e)
    print(state)
    if state == "running":
        print("orchestrator: rebuild from the base branch, open branches, and open pull requests, then dispatch")
        for line in watchdog(beam_path):
            print(line)
    if state == "stopped":
        try:
            import report

            report.maybe_complete(beam_path, announce=announce_report, because_stopped=True)
        except Exception as e:
            print(f"report: skipped ({e})")


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
  python3 scripts/scan.py start --beam .warp/beam.json --force
  python3 scripts/scan.py pause --beam .warp/beam.json --reason "hold"

scan looks for WARP_PLAN.json, schedule.json, CURSOR_PLAN.md, a Jira ticket
export, or a markdown table with id and deps. --folder is a path or a name.
Several matches and no single folder with plan files exits 3 and scans
nothing. No plan exits 2. --max-agents defaults to 18 and --model to
claude-sonnet-5-5-high; the live cap and slug are maxAgents and model in
config. start, stop, pause, and resume take --beam and --reason.
start --force starts a cloud run even when the MCP allow list is missing
the Jira or Slack tools. Without --force, that start does not claim.
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
        if name == "start":
            sp.add_argument("--force", action="store_true", help="start even if a cloud run would prompt for MCP tools")
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
        cfg = default_config()
        cfg["maxAgents"] = args.max_agents
        cfg["model"] = args.model
        cfg["autoMergeSizes"] = resolve_auto_merge_sizes(cfg, warp / "config.yaml")
        sched_path = warp / "_ingested_schedule.json"
        atomic_write(sched_path, json.dumps(to_schedule(graph, cfg["autoMergeSizes"]), indent=2) + "\n")
        try:
            import jira_project

            detected = jira_project.ensure(root, write=True, folder=folder)
            if detected:
                print(detected)
        except Exception as e:
            print(f"jira: project detection skipped ({e})")
        try:
            import jira_sync

            loaded = jira_sync.settings(warp / "beam.json", {"config": cfg})
            for key in ("jiraProject", "jiraKeyPrefixes", "jiraKeyMap", "jiraTransition", "jiraMcp", "jiraSite"):
                cfg[key] = loaded.get(key, cfg.get(key))
        except Exception:
            jira_sync = None
        previous = {}
        out_path = Path(args.out)
        if out_path.is_file():
            try:
                previous = load_json(out_path).get("tickets") or {}
            except Exception:
                previous = {}
        beam = ingest(sched_path, Path(graph["source"]), out_path, cfg, previous=previous)
        beam["runState"] = "stopped"
        beam["source"]["format"] = graph.get("format")
        beam["source"]["scan"] = found
        beam["source"]["folder"] = found.get("folder")
        beam.setdefault("program", {})["estimate"] = estimate(
            list(beam["tickets"].values()),
            beam.get("gates") or [],
            beam.get("program", {}).get("criticalPath") or [],
            args.max_agents,
            (beam.get("config") or {}).get("autoMergeSizes"),
        )
        atomic_write(Path(args.out), json.dumps(beam, indent=2) + "\n")
        try:
            import jira_project

            adopted = jira_project.adopt_from_beam(root)
            if adopted:
                print(adopted)
        except Exception as e:
            print(f"jira: project from stored keys skipped ({e})")
        est = beam["program"]["estimate"]
        if folder:
            print(f"scope folder={folder.relative_to(root)}")
        print(f"scan format={graph.get('format')} tickets={len(beam['tickets'])} source={graph.get('source')}")
        print(f"estimate agentHours={est['agentHours']} humanHours={est['humanHours']} elapsedHours={est['elapsedHours']}")
        print("runState=stopped — /warp-start to dispatch")
        tickets = list(beam["tickets"].values())
        unmapped = [t["id"] for t in tickets if t.get("jiraMapping") == "needs mapping"]
        keyed = len(tickets) - len(unmapped)
        if jira_sync is not None:
            print(jira_sync.prepare_resolve(Path(args.out)))
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
                        "unmapped": unmapped,
                        "keyed": keyed,
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
        cfg = (old or {}).get("config") or default_config()
        cfg["autoMergeSizes"] = resolve_auto_merge_sizes(cfg, Path(args.beam).parent / "config.yaml")
        sched_path = Path(args.beam).parent / "_imported_schedule.json"
        atomic_write(sched_path, json.dumps(to_schedule(graph, cfg["autoMergeSizes"]), indent=2) + "\n")
        beam = ingest(
            sched_path,
            Path(args.plan),
            Path(args.beam),
            cfg,
            previous=(old or {}).get("tickets") or {},
        )
        if old and args.keep_status:
            for tid, t in beam["tickets"].items():
                prev = old["tickets"].get(tid)
                if not prev:
                    continue
                for k in ("status", "agent", "branch", "attempts", "tokens", "minutes", "alarm", "pr", "jira"):
                    t[k] = prev.get(k, t.get(k))
        beam["runState"] = "stopped"
        atomic_write(Path(args.beam), json.dumps(beam, indent=2) + "\n")
        journal(Path(args.beam), {"type": "import", "plan": args.plan, "tickets": len(beam["tickets"])})
        print(f"replaced plan tickets={len(beam['tickets'])} runState=stopped")
    elif args.cmd == "status":
        import resume_hint

        resume_hint.print_hint(Path(args.beam))
        write_status(Path(args.beam))
    elif args.cmd == "bundle":
        bundle(Path(args.beam), Path(args.out))
    elif args.cmd == "start":
        import prompt_gate
        import resume_hint

        gate = prompt_gate.gate_start(Path(args.beam), force=bool(getattr(args, "force", False)))
        if gate != 0:
            sys.exit(gate)
        resume_hint.print_hint(Path(args.beam))
        set_run(Path(args.beam), "running", args.reason)
    elif args.cmd == "resume":
        import resume_hint

        resume_hint.print_hint(Path(args.beam))
        set_run(Path(args.beam), "running", args.reason)
    elif args.cmd == "pause":
        set_run(Path(args.beam), "paused", args.reason)
    elif args.cmd == "stop":
        set_run(Path(args.beam), "stopped", args.reason or "stop")


if __name__ == "__main__":
    main()
