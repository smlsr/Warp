#!/usr/bin/env python3
"""Registry of Warp agents, spawn caps, and cloud-agent cleanup.

`.warp/agents.json` sits next to the beam. Every spawn and every exit is a
row: id, ticket, role, session, started, ended, state. A ticket has one live
Shuttle. A replacement starts only after the previous one is confirmed dead
(its turn returned, or its heartbeat is older than staleMinutes). Fix rounds
resume that Shuttle. Bugbot is requested once per head commit.

Nothing is spawned while the run is paused or stopped, or every ticket is
merged or parked. Hitting a spawn cap raises an alarm and does not try again.

Cloud agents are archived with POST /v1/agents/{id}/archive when
CURSOR_API_KEY is set. The key is read from the environment and is never
written. Without the key, cleanup prints https://cursor.com/agents/<id>.

`--cloud` without `--apply` is a dry run. It lists idle agents for this
repo that are in the registry or whose name or prompt is a Warp role.
It does not archive the agent running the command, or any RUNNING or
ACTIVE agent. `--all-idle` drops the role check and stays on this repo.
`--any-repo` drops the repo check and is only for use with `--all-idle`
when you mean every idle agent the key can see.

  python3 scripts/agents.py ?
  python3 scripts/agents.py list --beam .warp/beam.json
  python3 scripts/agents.py check --beam .warp/beam.json
  python3 scripts/agents.py cleanup --beam .warp/beam.json
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import beam as beam_mod

API_ROOT = "https://api.cursor.com"
ENV_KEY = "CURSOR_API_KEY"
HOUR_MINUTES = 60
DEFAULT_MAX_SPAWNS_PER_TICKET = 8
DEFAULT_MAX_SPAWNS_PER_HOUR = 40
DEFAULT_MAX_LISTENER_RESTARTS_PER_HOUR = 8
_LIVE = {"starting", "running"}
_SETTLED = {"merged", "done", "parked", "skipped"}


def registry_path(beam_path: Path) -> Path:
    return Path(beam_path).parent / "agents.json"


def _empty() -> dict:
    return {"agents": [], "spawns": []}


def load_file(beam_path: Optional[Path]) -> dict:
    if beam_path is None:
        return _empty()
    path = registry_path(Path(beam_path))
    if not path.is_file():
        return _empty()
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(loaded, dict):
        return _empty()
    loaded.setdefault("agents", [])
    loaded.setdefault("spawns", [])
    return loaded


def save_file(beam_path: Optional[Path], doc: dict) -> None:
    if beam_path is None:
        return
    path = registry_path(Path(beam_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    beam_mod.atomic_write(path, json.dumps(doc, indent=2) + "\n")


def registry(data: dict, beam_path: Optional[Path] = None) -> dict:
    """The registry for this pass. A beam path loads and owns the file."""
    current = data.get("_agentBeam")
    if isinstance(data.get("agentRegistry"), dict):
        if beam_path is None or str(beam_path) == current:
            return data["agentRegistry"]
    loaded = load_file(beam_path)
    if isinstance(data.get("agentRegistry"), dict) and not loaded.get("agents") and not loaded.get("spawns"):
        loaded = data["agentRegistry"]
    data["agentRegistry"] = loaded
    if beam_path is not None:
        data["_agentBeam"] = str(beam_path)
    return loaded


def flush(data: dict, beam_path: Optional[Path] = None) -> None:
    path = beam_path
    if path is None and data.get("_agentBeam"):
        path = Path(data["_agentBeam"])
    doc = data.get("agentRegistry")
    if isinstance(doc, dict):
        save_file(path, doc)


def _cfg(data: dict, key: str, default: int) -> int:
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    try:
        number = int(cfg.get(key) if key in cfg and cfg.get(key) not in (None, "") else default)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def spawns_open(data: dict) -> bool:
    """False when paused, stopped, or every ticket is merged or parked."""
    if not isinstance(data, dict):
        return False
    if data.get("paused") or (data.get("runState") or "running") in {"paused", "stopped"}:
        return False
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    if not tickets:
        return True
    for ticket in tickets.values():
        if isinstance(ticket, dict) and ticket.get("status") not in _SETTLED:
            return True
    return False


def stale_minutes(data: dict) -> int:
    return _cfg(data, "staleMinutes", 15)


def confirmed_dead(ticket: dict, data: dict, now=None, turn_returned: bool = False) -> bool:
    """Dead only when the turn has returned, or the heartbeat is past the limit.

    A fresh heartbeat is alive. A new parent session does not make it dead.
    No heartbeat and no start is not dead: the age is not proven.
    """
    if turn_returned:
        return True
    if not isinstance(ticket, dict):
        return False
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("returned"):
        return True
    report = ticket.get("report")
    if isinstance(report, dict) and report.get("returned"):
        return True
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    if pr.get("shuttleReturned"):
        return True
    now_dt, _now_s = beam_mod.coerce_now(now)
    started = ticket.get("workerStartedAt") or ticket.get("claimedAt") or shuttle.get("startedAt")
    return beam_mod.worker_is_dead(ticket.get("lastSeenAt"), started, now_dt, stale_minutes(data))


def _rows(doc: dict) -> list:
    rows = doc.get("agents")
    return rows if isinstance(rows, list) else []


def _spawns(doc: dict) -> list:
    rows = doc.get("spawns")
    return rows if isinstance(rows, list) else []


def live_rows(doc: dict, ticket_id: str, role: str = "shuttle") -> list:
    found = []
    for row in _rows(doc):
        if not isinstance(row, dict):
            continue
        if str(row.get("ticket") or "") != str(ticket_id):
            continue
        if role and row.get("role") != role:
            continue
        if row.get("state") in _LIVE and not row.get("ended"):
            found.append(row)
    return found


def shuttle_busy(data: dict, ticket: dict, now=None, beam_path: Optional[Path] = None) -> bool:
    """A Shuttle is still out. Do not start another.

    A pending flag with no heartbeat and no start is the launch about to be
    recorded. It is not a second Shuttle.
    """
    if not isinstance(ticket, dict):
        return False
    if confirmed_dead(ticket, data, now=now):
        return False
    doc = registry(data, beam_path)
    if live_rows(doc, str(ticket.get("id") or ""), "shuttle"):
        return True
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if not shuttle.get("pending"):
        return False
    return bool(ticket.get("lastSeenAt") or ticket.get("workerStartedAt"))


def _minutes_between(older: str, newer: str) -> Optional[float]:
    start = beam_mod.parse_ts(older)
    end = beam_mod.parse_ts(newer)
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def _within_hour(stamp: str, now_s: str) -> bool:
    age = _minutes_between(stamp, now_s)
    if age is None:
        return False
    return 0 <= age <= HOUR_MINUTES


def spawn_blocked(data: dict, ticket_id: str, now=None, beam_path: Optional[Path] = None) -> str:
    """Empty when a new spawn is allowed. Otherwise `closed` or `cap`."""
    if not spawns_open(data):
        return "closed"
    doc = registry(data, beam_path)
    if any(row.get("ticket") == ticket_id and row.get("capped") for row in _rows(doc) if isinstance(row, dict)):
        return "cap"
    _now_dt, now_s = beam_mod.coerce_now(now)
    per_ticket = _cfg(data, "maxSpawnsPerTicket", DEFAULT_MAX_SPAWNS_PER_TICKET)
    per_hour = _cfg(data, "maxSpawnsPerHour", DEFAULT_MAX_SPAWNS_PER_HOUR)
    mine = [
        row
        for row in _spawns(doc)
        if isinstance(row, dict) and str(row.get("ticket") or "") == str(ticket_id) and row.get("role") != "listener"
    ]
    if len(mine) >= per_ticket:
        return "cap"
    recent = [row for row in _spawns(doc) if isinstance(row, dict) and _within_hour(str(row.get("at") or ""), now_s)]
    if len(recent) >= per_hour:
        return "cap"
    return ""


def _mark_capped(doc: dict, ticket_id: str, role: str) -> None:
    for row in live_rows(doc, ticket_id, role):
        row["capped"] = True
    if not any(isinstance(row, dict) and row.get("ticket") == ticket_id and row.get("capped") for row in _rows(doc)):
        doc.setdefault("agents", []).append(
            {"id": "", "ticket": ticket_id, "role": role, "capped": True, "state": "ended"}
        )


def end_id(doc: dict, agent_id: str, now_s: str, reason: str) -> None:
    if not agent_id:
        return
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("id") != agent_id:
            continue
        if row.get("state") in _LIVE or not row.get("ended"):
            row["state"] = "ended"
            row["ended"] = now_s
            row["endReason"] = reason


def end_ticket(data: dict, ticket: dict, reason: str, now=None, beam_path: Optional[Path] = None, role: str = "shuttle") -> None:
    """Record the current Shuttle as ended before a replacement or a resume."""
    if not isinstance(ticket, dict):
        return
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    tid = str(ticket.get("id") or "")
    for row in live_rows(doc, tid, role):
        row["state"] = "ended"
        row["ended"] = now_s
        row["endReason"] = reason
    agent = str(ticket.get("agent") or "")
    if agent:
        known = any(isinstance(row, dict) and row.get("id") == agent for row in _rows(doc))
        end_id(doc, agent, now_s, reason)
        if not known:
            doc.setdefault("agents", []).append(
                {
                    "id": agent,
                    "ticket": tid,
                    "role": role,
                    "session": str(data.get("parentSession") or ""),
                    "started": ticket.get("workerStartedAt") or ticket.get("claimedAt") or "",
                    "ended": now_s,
                    "state": "ended",
                    "endReason": reason,
                }
            )
    flush(data, beam_path)


def can_resume(data: dict, ticket: dict, beam_path: Optional[Path] = None) -> str:
    """Agent id to resume for a fix round, or empty when a new spawn is required.

    Resume is the Shuttle whose last turn returned. A stale death is not resumed.
    """
    doc = registry(data, beam_path)
    tid = str(ticket.get("id") or "")
    if live_rows(doc, tid, "shuttle"):
        return ""
    chosen = ""
    for row in _rows(doc):
        if not isinstance(row, dict):
            continue
        if str(row.get("ticket") or "") != tid or row.get("role") != "shuttle":
            continue
        if row.get("state") == "ended" and row.get("endReason") == "returned" and row.get("id"):
            chosen = str(row["id"])
    return chosen


def _new_id(ticket: dict, doc: dict) -> str:
    tid = str(ticket.get("id") or "ticket")
    current = str(ticket.get("agent") or "")
    number = 1
    while True:
        candidate = "shuttle-%s-r%d" % (tid, number)
        if candidate != current and not any(
            isinstance(row, dict) and row.get("id") == candidate for row in _rows(doc)
        ):
            return candidate
        number += 1


def prepare_launch(
    data: dict,
    ticket: dict,
    step: str,
    replacing: bool = False,
    now=None,
    beam_path: Optional[Path] = None,
) -> dict:
    """Decide start, resume, hold, closed, or cap. Records the registry row."""
    tid = str(ticket.get("id") or "")
    if shuttle_busy(data, ticket, now=now, beam_path=beam_path) and not replacing:
        return {"action": "hold", "id": str(ticket.get("agent") or "")}
    if not replacing:
        resume_id = can_resume(data, ticket, beam_path=beam_path)
        if resume_id and step in {"fix", "restart"}:
            doc = registry(data, beam_path)
            _now_dt, now_s = beam_mod.coerce_now(now)
            for row in _rows(doc):
                if isinstance(row, dict) and row.get("id") == resume_id:
                    row["state"] = "running"
                    row["ended"] = None
                    row["endReason"] = ""
                    row["resumedAt"] = now_s
            ticket["agent"] = resume_id
            flush(data, beam_path)
            return {"action": "resume", "id": resume_id}
    reason = spawn_blocked(data, tid, now=now, beam_path=beam_path)
    doc = registry(data, beam_path)
    if reason == "cap":
        _mark_capped(doc, tid, "shuttle")
        flush(data, beam_path)
        return {"action": "cap", "id": ""}
    if reason == "closed":
        return {"action": "closed", "id": ""}
    if replacing:
        end_ticket(data, ticket, "dead", now=now, beam_path=beam_path)
        doc = registry(data, beam_path)
        ticket["agent"] = _new_id(ticket, doc)
    elif not str(ticket.get("agent") or "").strip():
        ticket["agent"] = "subagent:%s" % tid
    agent = str(ticket.get("agent") or "")
    if any(isinstance(row, dict) and row.get("id") == agent and row.get("state") in _LIVE for row in _rows(doc)):
        return {"action": "hold", "id": agent}
    _now_dt, now_s = beam_mod.coerce_now(now)
    session = str(data.get("parentSession") or "")
    doc.setdefault("agents", []).append(
        {
            "id": agent,
            "ticket": tid,
            "role": "shuttle",
            "session": session,
            "started": now_s,
            "ended": None,
            "state": "starting",
            "lastSeenAt": ticket.get("lastSeenAt") or "",
        }
    )
    doc.setdefault("spawns", []).append({"at": now_s, "ticket": tid, "role": "shuttle", "id": agent, "step": step})
    flush(data, beam_path)
    return {"action": "start", "id": agent}


def record_replacement(
    data: dict,
    ticket: dict,
    new_id: str,
    now=None,
    beam_path: Optional[Path] = None,
) -> str:
    """End the Shuttle being replaced, then record new_id. Empty when the cap blocks it."""
    tid = str(ticket.get("id") or "")
    reason = spawn_blocked(data, tid, now=now, beam_path=beam_path)
    doc = registry(data, beam_path)
    if reason:
        if reason == "cap":
            _mark_capped(doc, tid, "shuttle")
            ticket["spawnCapped"] = True
            flush(data, beam_path)
        return reason
    end_ticket(data, ticket, "dead", now=now, beam_path=beam_path)
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    doc.setdefault("agents", []).append(
        {
            "id": new_id,
            "ticket": tid,
            "role": "shuttle",
            "session": str(data.get("parentSession") or ""),
            "started": now_s,
            "ended": None,
            "state": "starting",
        }
    )
    doc.setdefault("spawns", []).append({"at": now_s, "ticket": tid, "role": "shuttle", "id": new_id, "step": "replace"})
    flush(data, beam_path)
    return ""


def note_returned(data: dict, ticket: dict, now=None, beam_path: Optional[Path] = None) -> None:
    """The Shuttle's turn came back. The same agent can take the next fix."""
    end_ticket(data, ticket, "returned", now=now, beam_path=beam_path)


def note_closed(data: dict, ticket: dict, reason: str, now=None, beam_path: Optional[Path] = None) -> None:
    end_ticket(data, ticket, reason, now=now, beam_path=beam_path)


def note_checkout(data: dict, ticket: dict, beam_path: Optional[Path] = None, now=None) -> list:
    """Record a checkout. A second checkout of a live Shuttle prints `resume` and adds no agent.

    A row left in `starting` by a spawn becomes `running`. A checkout with no
    row is the first launch and is recorded. The worktree is still reused.
    """
    if not isinstance(ticket, dict):
        return []
    doc = registry(data, beam_path)
    tid = str(ticket.get("id") or "")
    live = live_rows(doc, tid, "shuttle")
    _now_dt, now_s = beam_mod.coerce_now(now)
    if not live:
        agent = str(ticket.get("agent") or "").strip() or ("subagent:%s" % tid)
        ticket["agent"] = agent
        doc.setdefault("agents", []).append(
            {
                "id": agent,
                "ticket": tid,
                "role": "shuttle",
                "session": str(data.get("parentSession") or ""),
                "started": now_s,
                "ended": None,
                "state": "running",
            }
        )
        flush(data, beam_path)
        return []
    if all(row.get("state") == "starting" for row in live):
        for row in live:
            row["state"] = "running"
        flush(data, beam_path)
        return []
    ident = str(live[0].get("id") or ticket.get("agent") or "")
    return ["resume: %s agent=%s" % (tid, ident)]


def release_ticket(
    data: dict,
    ticket: dict,
    reason: str = "closed",
    now=None,
    beam_path: Optional[Path] = None,
) -> list:
    """End this ticket's agents. Archive a cloud id when CURSOR_API_KEY is set."""
    note_closed(data, ticket, reason, now=now, beam_path=beam_path)
    finish_bugbot(data, ticket, now=now, beam_path=beam_path)
    doc = registry(data, beam_path)
    tid = str(ticket.get("id") or "")
    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = []
    secret = api_key()
    for row in _rows(doc):
        if not isinstance(row, dict) or str(row.get("ticket") or "") != tid:
            continue
        if row.get("role") == "bugbot" and row.get("state") in _LIVE:
            row["state"] = "ended"
            row["ended"] = now_s
            row["endReason"] = reason
        ident = str(row.get("id") or "")
        if not _cloud_id(ident):
            continue
        if row.get("state") not in {"ended", "stopped"} and not row.get("ended"):
            continue
        if not secret:
            lines.append("cleanup: link %s" % agent_url(ident))
            continue
        ok, status = archive_id(ident, secret)
        if ok:
            row["state"] = "ended"
            row["archived"] = True
            lines.append("cleanup: archived %s" % ident)
        else:
            lines.append("cleanup: archive failed %s (%s) %s" % (ident, status, agent_url(ident)))
    flush(data, beam_path)
    return lines


def cleanup_settled(data: dict, beam_path: Optional[Path] = None, now=None) -> list:
    """Full cleanup once nothing may spawn. Empty when the registry is empty."""
    if spawns_open(data):
        return []
    doc = registry(data, beam_path)
    if not any(isinstance(row, dict) and row.get("id") for row in _rows(doc)):
        return []
    return cleanup(data, beam_path=beam_path, now=now)


def launch_lines(data: dict, ticket: dict, step: str, detail: str = "", replacing: bool = False, now=None, beam_path: Optional[Path] = None) -> list:
    """The one line the parent uses. `resume` does not create an agent."""
    import orchestrator

    decision = prepare_launch(data, ticket, step, replacing=replacing, now=now, beam_path=beam_path)
    tid = ticket.get("id")
    action = decision["action"]
    if action == "hold":
        return ["shuttle: hold %s" % tid]
    if action == "closed":
        return ["spawn: closed %s" % tid]
    if action == "cap":
        ticket["status"] = "alarm"
        ticket["alarm"] = "spawn-cap"
        ticket["spawnCapped"] = True
        return [
            "spawn: cap %s" % tid,
            "herald: %s spawn cap reached. No new Shuttle." % tid,
        ]
    cfg = data.get("config") or {}
    kind = orchestrator.dispatch_checkout(cfg, data)
    branch = orchestrator.branch_name(ticket, cfg)
    launch = orchestrator.launch_for(cfg, data)
    extra = " launch=%s" % launch.get("launch")
    if launch.get("refuseInProcess"):
        extra += " refuse-in-process"
    if decision.get("id"):
        extra += " agent=%s" % decision["id"]
    verb = "resume" if action == "resume" else "start"
    lines = ["%s %s checkout=%s branch=%s%s step=%s" % (verb, tid, kind, branch, extra, step)]
    if detail:
        lines.append("fix: %s %s" % (tid, detail))
    if action == "resume":
        lines.append("resume: %s agent=%s" % (tid, decision.get("id") or ""))
    return lines


def same_commit_bugbot(ticket: dict) -> bool:
    """True when Bugbot was already requested for this head commit."""
    pr = ticket.get("pr") if isinstance(ticket, dict) and isinstance(ticket.get("pr"), dict) else {}
    if not pr.get("bugbotRequestedAt") and not pr.get("bugbotRequested"):
        return False
    sha = str(pr.get("headSha") or "").strip()
    asked = str(pr.get("bugbotRequestedSha") or "")
    if pr.get("bugbotRequestedAt") or pr.get("bugbotRequested"):
        return asked == sha
    return False


def allow_bugbot(ticket: dict) -> bool:
    return not same_commit_bugbot(ticket)


def note_bugbot(data: dict, ticket: dict, now=None, beam_path: Optional[Path] = None) -> None:
    pr = ticket.setdefault("pr", {})
    sha = str(pr.get("headSha") or "").strip()
    _now_dt, now_s = beam_mod.coerce_now(now)
    pr["bugbotRequested"] = True
    pr["bugbotRequestedSha"] = sha
    pr["bugbotRequestedAt"] = now_s
    doc = registry(data, beam_path)
    ident = "bugbot:%s:%s" % (ticket.get("id") or "", sha or "none")
    if not any(isinstance(row, dict) and row.get("id") == ident for row in _rows(doc)):
        doc.setdefault("agents", []).append(
            {
                "id": ident,
                "ticket": str(ticket.get("id") or ""),
                "role": "bugbot",
                "session": str(data.get("parentSession") or ""),
                "started": now_s,
                "ended": None,
                "state": "running",
                "headSha": sha,
            }
        )
        doc.setdefault("spawns", []).append(
            {"at": now_s, "ticket": ticket.get("id"), "role": "bugbot", "id": ident}
        )
    flush(data, beam_path)


def finish_bugbot(data: dict, ticket: dict, now=None, beam_path: Optional[Path] = None) -> None:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    label = str(pr.get("bugbot") or "").strip().casefold()
    if label not in {"pass", "fail", "failed", "failure"}:
        return
    sha = str(pr.get("headSha") or "").strip()
    ident = "bugbot:%s:%s" % (ticket.get("id") or "", sha or "none")
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    end_id(doc, ident, now_s, label)
    flush(data, beam_path)


def listener_backoff_minutes(count: int) -> int:
    """Minutes to wait after this many restarts. The first is immediate."""
    if count <= 1:
        return 0
    return min(2 ** (count - 1), 30)


def listener_decision(data: dict, now=None, beam_path: Optional[Path] = None, returned: str = "") -> str:
    """`start`, `backoff`, `cap`, or `closed`."""
    if not spawns_open(data):
        return "closed"
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    if raw.get("restartCapped"):
        return "cap"
    _now_dt, now_s = beam_mod.coerce_now(now)
    nxt = str(raw.get("nextRestartAt") or "")
    if nxt and not returned:
        start = beam_mod.parse_ts(now_s)
        until = beam_mod.parse_ts(nxt)
        if start is not None and until is not None and start < until:
            return "backoff"
    cap = _cfg(data, "maxListenerRestartsPerHour", DEFAULT_MAX_LISTENER_RESTARTS_PER_HOUR)
    stamps = [str(item) for item in (raw.get("restartAts") or []) if str(item)]
    recent = [item for item in stamps if _within_hour(item, now_s)]
    if len(recent) >= cap:
        return "cap"
    return "start"


def note_listener_restart(data: dict, now=None, beam_path: Optional[Path] = None, previous: str = "") -> None:
    """End the listener that is being replaced, then record the new start."""
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    if previous:
        end_id(doc, previous, now_s, "replaced")
    for row in _rows(doc):
        if isinstance(row, dict) and row.get("role") == "listener" and row.get("state") in _LIVE:
            row["state"] = "ended"
            row["ended"] = now_s
            row["endReason"] = "replaced"
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    count = int(raw.get("restarts") or 0)
    ident = "listener-r%d" % count
    doc.setdefault("agents", []).append(
        {
            "id": ident,
            "ticket": "",
            "role": "listener",
            "session": str(data.get("parentSession") or ""),
            "started": now_s,
            "ended": None,
            "state": "starting",
        }
    )
    stamps = [str(item) for item in (raw.get("restartAts") or []) if str(item)]
    stamps.append(now_s)
    raw["restartAts"] = stamps
    wait = listener_backoff_minutes(count)
    if wait:
        from datetime import timedelta

        start = beam_mod.parse_ts(now_s)
        if start is not None:
            raw["nextRestartAt"] = (start + timedelta(minutes=wait)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["listener"] = raw
    flush(data, beam_path)


def cap_listener(data: dict, now=None, beam_path: Optional[Path] = None) -> None:
    raw = data.setdefault("listener", {})
    if not isinstance(raw, dict):
        raw = {}
        data["listener"] = raw
    raw["restartCapped"] = True
    flush(data, beam_path)


def stop_all(data: dict, now=None, beam_path: Optional[Path] = None) -> list:
    """Mark every live registered agent stopped. Used by /warp-stop."""
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = []
    for row in _rows(doc):
        if not isinstance(row, dict):
            continue
        if row.get("state") not in _LIVE:
            continue
        row["state"] = "stopped"
        row["ended"] = now_s
        row["endReason"] = "stopped"
        if row.get("id"):
            lines.append("stop: %s" % row["id"])
    raw = data.get("listener")
    if isinstance(raw, dict):
        raw["state"] = "stopped"
    flush(data, beam_path)
    return lines


def _cloud_id(agent_id: str) -> bool:
    text = str(agent_id or "")
    return text.startswith("bc-") or text.startswith("bc_")


def agent_url(agent_id: str) -> str:
    return "https://cursor.com/agents/%s" % agent_id


def api_key() -> str:
    return os.environ.get(ENV_KEY, "").strip()


def _basic(key: str) -> str:
    return base64.b64encode(("%s:" % key).encode()).decode()


def default_transport(method: str, url: str, key: str, body: Optional[dict] = None) -> tuple:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", "Basic %s" % _basic(key))
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    if not raw:
        return status, {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw[:200]}
    return status, parsed if isinstance(parsed, dict) else {"raw": parsed}


def archive_id(agent_id: str, key: str, transport: Optional[Callable] = None) -> tuple:
    """POST /v1/agents/{id}/archive. Returns (ok, detail)."""
    call = transport or default_transport
    status, body = call("POST", "%s/v1/agents/%s/archive" % (API_ROOT, agent_id), key, None)
    return 200 <= int(status) < 300, status


def list_cloud(key: str, transport: Optional[Callable] = None, limit: int = 100) -> tuple:
    """GET /v1/agents pages. Returns (ok, items, error)."""
    call = transport or default_transport
    items = []
    cursor = ""
    for _page in range(20):
        url = "%s/v1/agents?limit=%d&includeArchived=false" % (API_ROOT, limit)
        if cursor:
            url += "&cursor=%s" % cursor
        status, body = call("GET", url, key, None)
        if not (200 <= int(status) < 300):
            return False, items, "list failed (%s)" % status
        batch = body.get("items") if isinstance(body, dict) else None
        if isinstance(batch, list):
            items.extend(row for row in batch if isinstance(row, dict))
        cursor = str(body.get("nextCursor") or "") if isinstance(body, dict) else ""
        if not cursor:
            break
    return True, items, ""


def _row_line(row: dict) -> str:
    ident = row.get("id") or ""
    link = agent_url(ident) if _cloud_id(ident) else ""
    bits = [
        "agent: %s" % (ident or "(local)"),
        "role=%s" % (row.get("role") or ""),
        "ticket=%s" % (row.get("ticket") or ""),
        "state=%s" % (row.get("state") or ""),
        "started=%s" % (row.get("started") or ""),
        "ended=%s" % (row.get("ended") or ""),
    ]
    if link:
        bits.append(link)
    return " ".join(bits)


def list_lines(data: dict, beam_path: Optional[Path] = None) -> list:
    doc = registry(data, beam_path)
    rows = [row for row in _rows(doc) if isinstance(row, dict) and row.get("id")]
    if not rows:
        return ["agents: none"]
    return [_row_line(row) for row in rows]


_BUSY_STATUS = {"RUNNING", "ACTIVE", "CREATING"}
_ROLE_WORDS = ("shuttle", "implement", "rebase", "listener", "bugbot")


def repo_key(url: str) -> str:
    """github.com/org/repo, from an https, ssh, or git@ remote."""
    text = str(url or "").strip().casefold()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if text.startswith("git@"):
        host, _, path = text[4:].partition(":")
        text = "%s/%s" % (host, path)
    else:
        for prefix in ("ssh://", "https://", "http://"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        if text.startswith("git@"):
            host, _, path = text[4:].partition(":")
            text = "%s/%s" % (host, path)
    text = text.strip("/")
    if ":" in text and "/" in text.split(":", 1)[0]:
        text = text.split(":", 1)[0]
    return text


def origin_url(root: Optional[Path]) -> str:
    """origin remote of the repo that holds the beam. Empty when git has none."""
    if root is None:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def current_agent_id() -> str:
    """Cloud agent id of this process, when Cursor put one in the environment."""
    for key in ("CURSOR_AGENT_ID", "CURSOR_CLOUD_AGENT_ID", "CURSOR_CONVERSATION_ID"):
        value = os.environ.get(key, "").strip()
        if value.startswith("bc-") or value.startswith("bc_"):
            return value
    return ""


def cloud_repos(item: dict) -> list:
    found = []
    repos = item.get("repos")
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, str) and repo.strip():
                found.append(repo.strip())
            elif isinstance(repo, dict):
                url = str(repo.get("url") or repo.get("repoUrl") or "").strip()
                if url:
                    found.append(url)
    for key in ("repository", "repo", "repoUrl"):
        value = str(item.get(key) or "").strip()
        if value:
            found.append(value)
    source = item.get("source")
    if isinstance(source, dict):
        value = str(source.get("repository") or source.get("repoUrl") or "").strip()
        if value:
            found.append(value)
    git = item.get("git")
    if isinstance(git, dict):
        branches = git.get("branches")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    url = str(branch.get("repoUrl") or "").strip()
                    if url:
                        found.append(url)
    return found


def cloud_prompt(item: dict) -> str:
    prompt = item.get("prompt")
    if isinstance(prompt, dict):
        return str(prompt.get("text") or "")
    if isinstance(prompt, str):
        return prompt
    return ""


def warp_role(text: str) -> bool:
    """True when a name or prompt is a Warp Shuttle, listener, fix, rebase, or Bugbot."""
    folded = str(text or "").casefold()
    if any(word in folded for word in _ROLE_WORDS):
        return True
    return re.search(r"\bfix\b", folded) is not None


def _registry_cloud_ids(doc: dict) -> set:
    found = set()
    for row in _rows(doc):
        if isinstance(row, dict) and _cloud_id(str(row.get("id") or "")):
            found.add(str(row["id"]))
    return found


def _same_repo(item: dict, origin_key: str) -> bool:
    if not origin_key:
        return False
    return any(repo_key(url) == origin_key for url in cloud_repos(item))


def _display_repo(item: dict, origin_key: str) -> str:
    urls = cloud_repos(item)
    for url in urls:
        if origin_key and repo_key(url) == origin_key:
            return repo_key(url)
    if urls:
        return repo_key(urls[0]) or urls[0]
    return ""


def _updated(item: dict) -> str:
    return str(item.get("updatedAt") or item.get("updated") or item.get("lastUpdatedAt") or "")


def cloud_match_line(item: dict, origin_key: str) -> str:
    ident = str(item.get("id") or "")
    name = str(item.get("name") or "").replace("\n", " ").strip()
    status = str(item.get("status") or "")
    link = str(item.get("url") or agent_url(ident))
    return "cloud: id=%s name=%s repo=%s status=%s updated=%s %s" % (
        ident,
        name,
        _display_repo(item, origin_key),
        status,
        _updated(item),
        link,
    )


def classify_cloud(
    item: dict,
    origin_key: str,
    registry_ids: set,
    all_idle: bool = False,
    any_repo: bool = False,
    self_id: str = "",
) -> str:
    """`archive`, `skip:<reason>`, or empty when the agent is out of scope.

    In scope means this repo (unless `--any-repo`) and either a registry row
    or a Warp role in the name or prompt (unless `--all-idle`). RUNNING,
    ACTIVE, and this process are never archived.
    """
    if not isinstance(item, dict):
        return ""
    ident = str(item.get("id") or "")
    if not _cloud_id(ident):
        return ""
    status = str(item.get("status") or "").strip().upper()
    if status == "ARCHIVED":
        return ""
    if not any_repo and not _same_repo(item, origin_key):
        return ""
    in_registry = ident in registry_ids
    role = warp_role("%s\n%s" % (item.get("name") or "", cloud_prompt(item)))
    if not all_idle and not in_registry and not role:
        return ""
    if self_id and ident == self_id:
        return "skip:self"
    if status in _BUSY_STATUS:
        return "skip:running"
    if status != "IDLE":
        return "skip:status"
    return "archive"


def fetch_agent(agent_id: str, key: str, transport: Optional[Callable] = None) -> dict:
    """GET /v1/agents/{id}. List rows omit repos and the prompt."""
    call = transport or default_transport
    status, body = call("GET", "%s/v1/agents/%s" % (API_ROOT, agent_id), key, None)
    if not (200 <= int(status) < 300) or not isinstance(body, dict):
        return {}
    return body


def _needs_detail(item: dict, registry_ids: set, all_idle: bool, any_repo: bool) -> bool:
    ident = str(item.get("id") or "")
    if not any_repo and not cloud_repos(item):
        return True
    if all_idle or ident in registry_ids:
        return False
    if warp_role(str(item.get("name") or "")):
        return False
    return not cloud_prompt(item)


def _merge_detail(item: dict, detail: dict) -> dict:
    merged = dict(item)
    for field in ("name", "status", "repos", "prompt", "url", "updatedAt", "repository", "repo", "git"):
        value = detail.get(field)
        if value not in (None, "", []):
            merged[field] = value
    return merged


def cleanup(
    data: dict,
    beam_path: Optional[Path] = None,
    cloud: bool = False,
    apply: bool = False,
    key: Optional[str] = None,
    transport: Optional[Callable] = None,
    now=None,
    all_idle: bool = False,
    any_repo: bool = False,
    origin: Optional[str] = None,
    self_id: Optional[str] = None,
) -> list:
    """List registered agents. `--cloud` without `--apply` is a dry run.

    A cloud agent is in scope when its repository matches this repo's origin
    and it is in `.warp/agents.json` or its name or prompt is a Warp role.
    `--all-idle` drops the role check and stays on this repo. `--any-repo`
    drops the repo check. RUNNING, ACTIVE, and this process are never archived.
    """
    doc = registry(data, beam_path)
    lines = list_lines(data, beam_path)
    secret = api_key() if key is None else key
    ended = []
    for row in _rows(doc):
        if not isinstance(row, dict) or not _cloud_id(str(row.get("id") or "")):
            continue
        if row.get("state") in {"ended", "stopped"} or row.get("ended"):
            ended.append(str(row["id"]))
    if not secret:
        lines.append("cleanup: no %s; archive in the Cursor UI" % ENV_KEY)
        for ident in ended:
            lines.append("cleanup: link %s" % agent_url(ident))
        if cloud:
            lines.append("cleanup: cloud list needs %s" % ENV_KEY)
        flush(data, beam_path)
        return lines
    if not cloud:
        lines.append("cleanup: dry-run" if not apply else "cleanup: cloud required to archive")
        flush(data, beam_path)
        return lines
    root = None
    if beam_path is not None:
        root = Path(beam_path).resolve().parent.parent
    remote = origin if origin is not None else origin_url(root)
    origin_key = repo_key(remote)
    mine = "" if self_id is None else self_id
    if self_id is None:
        mine = current_agent_id()
    if not any_repo and not origin_key:
        lines.append("cleanup: no origin; nothing matched")
        lines.append("cleanup: count 0")
        lines.append("cleanup: dry-run" if not apply else "cleanup: archived 0")
        flush(data, beam_path)
        return lines
    ok, items, err = list_cloud(secret, transport=transport)
    if not ok:
        lines.append("cleanup: %s" % err)
        flush(data, beam_path)
        return lines
    registry_ids = _registry_cloud_ids(doc)
    chosen = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or "")
        if _needs_detail(item, registry_ids, all_idle, any_repo) and ident:
            detail = fetch_agent(ident, secret, transport=transport)
            if detail:
                item = _merge_detail(item, detail)
        kind = classify_cloud(
            item,
            origin_key,
            registry_ids,
            all_idle=all_idle,
            any_repo=any_repo,
            self_id=mine,
        )
        if not kind:
            continue
        lines.append(cloud_match_line(item, origin_key))
        if kind == "archive":
            chosen.append(item)
        elif kind.startswith("skip:"):
            lines.append("cleanup: skip %s reason=%s" % (ident, kind.split(":", 1)[1]))
    lines.append("cleanup: count %d" % len(chosen))
    if not apply:
        lines.append("cleanup: dry-run")
        flush(data, beam_path)
        return lines
    archived = 0
    seen = set()
    for item in chosen:
        ident = str(item.get("id") or "")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        posted, status = archive_id(ident, secret, transport=transport)
        if posted:
            archived += 1
            end_id(doc, ident, beam_mod.coerce_now(now)[1], "archived")
            for row in _rows(doc):
                if isinstance(row, dict) and row.get("id") == ident:
                    row["state"] = "ended"
                    row["archived"] = True
            lines.append("cleanup: archived %s" % ident)
        else:
            lines.append("cleanup: archive failed %s (%s) %s" % (ident, status, agent_url(ident)))
    lines.append("cleanup: archived %d" % archived)
    flush(data, beam_path)
    return lines


def check_line(data: dict) -> str:
    if spawns_open(data):
        return "spawn: open"
    state = data.get("runState") or ("paused" if data.get("paused") else "running")
    if data.get("paused") or state in {"paused", "stopped"}:
        return "spawn: closed %s" % ("paused" if data.get("paused") or state == "paused" else "stopped")
    return "spawn: closed done"


def main(argv: Optional[list] = None) -> int:
    import usage

    parser = argparse.ArgumentParser(
        description="Warp agent registry and cleanup",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("list", "check", "cleanup", "stop"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--beam", default=".warp/beam.json")
        if name == "cleanup":
            cmd.add_argument("--cloud", action="store_true", help="Read GET /v1/agents. Without --apply this is a dry run.")
            cmd.add_argument("--apply", action="store_true", help="Archive the dry-run matches. IDLE only.")
            cmd.add_argument("--all-idle", action="store_true", help="Drop the Warp role check. Still this repo unless --any-repo.")
            cmd.add_argument("--any-repo", action="store_true", help="Include other repositories. Use with --all-idle for every idle agent.")
    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if not path.is_file():
        print("agents: no beam at %s" % path)
        return 2
    data = beam_mod.load_json(path)
    if args.cmd == "check":
        print(check_line(data))
        return 0
    if args.cmd == "list":
        for line in list_lines(data, path):
            print(line)
        return 0
    if args.cmd == "stop":
        for line in stop_all(data, beam_path=path):
            print(line)
        beam_mod.atomic_write(path, json.dumps(data, indent=2) + "\n")
        return 0
    for line in cleanup(
        data,
        beam_path=path,
        cloud=args.cloud,
        apply=args.apply,
        all_idle=args.all_idle,
        any_repo=args.any_repo,
    ):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
