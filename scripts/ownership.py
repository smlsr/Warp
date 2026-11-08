#!/usr/bin/env python3
"""Who already owns a pull request, before Warp starts an agent on it (1.7.17).

The first wake on 1.7.16 wanted to start about 28 fix agents to rebase pull requests the coordinators already owned
(#685, #548, #1418, #1442): two pushers on one branch. Before any fix, rebase, CI start or restaff, Warp now asks who
owns the pull request. An owner outside Warp gets the instruction (one message per head and kind, through relay.py,
a follow-up run on its agent) and no Warp agent starts. Warp starts its own fix only on a pull request Warp itself
owns, or one nobody owns.

A pull request is owned outside Warp when any of these says so:
  - `prOwners` names a coordinator other than `warp`, or an owner that is not the ticket
  - the ticket is `external`, or has `externalOwner` / `coordinator`
  - a coordinator outbox or ticket log has `WORKER <agent> <pr>`, `READY|GAPPR <pr> …`, `HANDED-OFF <owner> …`,
    or `OWNS <pr>` for it
  - the last sweep saw a running cloud agent on it that Warp did not start (the coordinators' workers are cloud
    agents on the same account, so the sweep sees them)
  - its park or hold notes name an owner outside Warp ("owner outside Warp (e49-restart coordinator)")
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Optional

_LINE = re.compile(r"\b(?:WORKER\s+(?P<agent>\S+)\s+#?(?P<pr1>\d+)|(?:READY|GAPPR)\s+#?(?P<pr2>\d+)\s+[0-9a-f]{7,40}"
                   r"|OWNS\s+#?(?P<pr3>\d+))")
_CACHE: dict = {}


def pr_number(ticket: dict) -> str:
    match = re.search(r"/pull/(\d+)", str(((ticket or {}).get("pr") or {}).get("url") or ""))
    return match.group(1) if match else ""


def _outbox_claims(beam_path: Path) -> dict:
    """{pr: (owner, agent)} from coordinator outboxes, read at most once per pass (cached by mtime)."""
    root = Path(beam_path).resolve().parent.parent
    files = []
    for pattern in (".warp/outbox/*.md", ".warp/coordinators/*/outbox*.md"):
        files.extend(sorted(glob.glob(str(root / pattern))))
    stamp = tuple((name, Path(name).stat().st_mtime) for name in files if Path(name).is_file())
    if _CACHE.get("stamp") == stamp:
        return _CACHE["claims"]
    claims: dict = {}
    for name in files:
        owner = Path(name).stem if "/outbox/" in name else Path(name).parent.name
        if owner in {"warp", "outbox"}:
            continue
        try:
            text = Path(name).read_text(errors="replace")
        except OSError:
            continue
        for match in _LINE.finditer(text):
            pr = match.group("pr1") or match.group("pr2") or match.group("pr3")
            claims[pr] = (owner, match.group("agent") or claims.get(pr, ("", ""))[1])
    _CACHE.update(stamp=stamp, claims=claims)
    return claims


def _live(beam_path: Path) -> dict:
    """{pr: (owner, agent)} read live from the Cloud Agents API when the sweep has not run in the last 20 minutes (a
    fresh upgrade's first wake): running agents on a pull request that Warp's registry did not start. Once a minute."""
    import os
    import time

    if _CACHE.get("liveAt") and time.time() - _CACHE["liveAt"] < 60:
        return _CACHE.get("live") or {}
    _CACHE["liveAt"] = time.time()
    _CACHE["live"] = {}
    key = os.environ.get("CURSOR_API_KEY") or ""
    if not key:
        return {}
    try:
        import agents

        ok, items, _err = agents.list_cloud(key)
        doc = json.loads((Path(beam_path).parent / "agents.json").read_text()) if (Path(beam_path).parent / "agents.json").is_file() else {}
    except Exception:
        return {}
    mine = {str(r.get(k)) for r in (doc.get("agents") or []) if isinstance(r, dict) for k in ("id", "cloudId", "agentId") if r.get(k)
            and str(r.get("role") or "") not in {"coordinator", "worker"}}
    out = {}
    for item in items if ok else []:
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        match = re.search(r"/pull/(\d+)", str(target.get("prUrl") or ""))
        if match and str(item.get("id")) not in mine and str(item.get("status") or "").upper() in {"RUNNING", "CREATING", "PENDING", "ACTIVE"}:
            out[match.group(1)] = (str(item.get("name") or "another agent"), str(item.get("id")))
    _CACHE["live"] = out
    return out


def _swept(beam_path: Path) -> dict:
    """{pr: agent id} for running agents the last sweep saw that Warp did not start; live from the API when the sweep
    is older than 20 minutes."""
    import time

    path = Path(beam_path).parent / "sweep" / "last.json"
    try:
        mtime = path.stat().st_mtime if path.is_file() else 0
        fresh = bool(mtime) and time.time() - mtime < 1200
        if fresh and _CACHE.get("sweptAt") == (str(path), mtime):
            return _CACHE["swept"]
        rows = json.loads(path.read_text()) if fresh else None
    except (OSError, ValueError):
        rows = None
        mtime = 0
    if rows is None:
        return _live(beam_path)
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("pr") and row.get("class") in {"active", "stalled", "stale-wait"} \
                and str(row.get("parent") or "") not in {"", "warp"}:
            out[str(row["pr"])] = (str(row.get("parent")), str(row.get("id") or ""))
    _CACHE.update(swept=out, sweptAt=(str(path), mtime))
    return out


def warp_started(ticket: dict) -> bool:
    """Warp launched a Shuttle for this ticket at some point."""
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    return bool(ticket.get("workerStartedAt") or ticket.get("claimedAt") or shuttle.get("startedAt")
                or shuttle.get("hadPr") or ticket.get("recoveries") or ticket.get("spawns")
                or str(ticket.get("agent") or "").startswith(("shuttle-", "subagent:")))


def owner(data: dict, ticket: dict, beam_path: Optional[Path] = None) -> tuple:
    """(owner, agent, why) when someone other than Warp owns this ticket's pull request; ("", "", "") otherwise."""
    number = pr_number(ticket)
    if not number:
        return "", "", ""
    tid = str(ticket.get("id") or "")
    row = ((data.get("prOwners") or {}).get(number) or {}) if isinstance(data.get("prOwners"), dict) else {}
    coord = str(row.get("coordinator") or "")
    if coord and coord != "warp":
        return coord, str(row.get("agent") or ""), "prOwners"
    if row.get("owner") and row.get("owner") not in {tid, "warp", "lane"} and not coord:
        return str(row["owner"]), str(row.get("agent") or ""), "prOwners"
    if ticket.get("status") == "external" or ticket.get("externalOwner") or ticket.get("coordinator"):
        return str(ticket.get("externalOwner") or ticket.get("coordinator") or "outside Warp"), "", "external"
    if beam_path is not None:
        claim = _outbox_claims(Path(beam_path)).get(number)
        if claim:
            return claim[0], claim[1], "outbox"
        seen = _swept(Path(beam_path)).get(number)
        if seen:
            return seen[0], seen[1], "sweep"
    import unpark

    named = unpark.outside_owner(ticket)
    if named:
        return named, "", "its notes name an owner outside Warp"
    return "", "", ""


def hands_off(data: dict, ticket: dict, beam_path: Optional[Path] = None) -> str:
    """Who Warp must leave this ticket's pull request to: an outside owner, or "" when Warp may work it (1.7.18).
    External tickets are always hands off. Every repair path asks this before it touches a ticket: the repair sweep,
    the watch fixer, the auto-scan, heal on resume, begin_fix, start_rebase and start_ci."""
    if not isinstance(ticket, dict):
        return ""
    if ticket.get("status") == "external":
        return str(ticket.get("externalOwner") or "outside Warp")
    if beam_path is None:
        raw = data.get("_agentBeam") or ""
        root = data.get("_repoRoot") or ""
        beam_path = Path(raw) if raw else (Path(root) / ".warp" / "beam.json" if root else None)
    return owner(data, ticket, beam_path)[0]


_ASK = {
    "rebase": "rebase #{pr} onto {base}: it conflicts. Merge {base} once, resolve, run the gate, push once",
    "ci-start": "#{pr} has no CI run on its head. Push one real commit (or re-run the workflow once if none ever started)",
    "ci": "fix the red checks on #{pr}: {reasons}",
    "bugbot": "fix the Bugbot findings on #{pr}: {reasons}",
    "continue": "finish the fix on #{pr}: {reasons}",
}


def delegate(data: dict, ticket: dict, kind: str, reasons: list, lines: list, beam_path: Optional[Path]) -> bool:
    """When an outside owner has the pull request: tell it (once per head and kind), start nothing. True when handed
    over (or already handed over for this head)."""
    who, agent, why = owner(data, ticket, beam_path)
    if not who:
        return False
    pr = ticket.setdefault("pr", {})
    number = pr_number(ticket)
    head = str(pr.get("headSha") or "")
    key = "%s|%s" % (kind, head)
    if pr.get("delegated") == key:
        return True
    pr["delegated"] = key
    text = _ASK.get(kind, _ASK["ci"]).format(pr=number, base=str(pr.get("base") or "main"),
                                              reasons="; ".join(str(r) for r in reasons[:8]) or kind)
    text = ("%s. You own #%s, so Warp starts no agent on it. One round, one push: cover every red check and finding "
            "together, then write `READY %s <sha>` in your outbox." % (text[0].upper() + text[1:], number, number))
    lines.append("fix: %s #%s is owned by %s (%s); told it to %s, and no Warp agent starts" % (
        ticket.get("id"), number, who, why, kind))
    if beam_path is not None:
        try:
            import relay

            lines.extend(relay.send(Path(beam_path), data, who, text, agent_id=agent))
        except Exception as exc:
            lines.append("relay: not sent to %s (%s)" % (who, exc))
    return True
