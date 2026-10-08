#!/usr/bin/env python3
"""Registry of Warp agents, the reap check, the halt, and cloud-agent cleanup.

`.warp/agents.json` sits next to the beam and is the one registry. Every
spawn and every exit is a row: id, ticket, role, session, started, ended,
state. A ticket has one live Shuttle. A replacement starts only after the
previous one is confirmed dead (its turn returned, or its heartbeat is older
than staleMinutes). Fix rounds resume that Shuttle. Bugbot is requested once
per head commit. The parent is the only agent that starts another agent.

Nothing is spawned while the run is paused or stopped, or every ticket is
merged or parked. Hitting a spawn cap raises an alarm and does not try again.

reap is the check every Warp agent runs first and after each state it
writes. It prints `reap: continue`, or `reap: exit <reason>` and exits 3.
The reasons are paused, stopped, done, settled (the ticket is merged or
parked), replaced, halted (started before a pause or a stop), archived,
not-resumed, and not-launched. `--remote`
reads the beam from origin's base branch, for a Shuttle on its own VM.
`ticket_state.py append` and `beam.py heartbeat` print the same line.

Every agent Warp starts is tagged `warp:<instance>`. The instance is six
characters that name this Warp run: the last six of the parent's cloud agent
id, or a hash of this machine and this beam's path on a laptop. It is set
once by `/warp-start`, kept on the beam, and shown by start, status,
version, list, and every Slack or Teams message. Cursor has no tag field on
an agent, so the tag is the name: `[warp:<instance>] <ticket> <step>` for a
Shuttle and `[warp:<instance>] listener` for a poll. The same text is the
first line of the prompt. `checkout.py launch` prints the name for the
parent to use.

`/warp-pause` and `/warp-stop` run one teardown. Every registry row is
ended and marked halted, so nothing from before the halt is resumed. With
CURSOR_API_KEY, every cloud agent in the registry, and every agent in this
repo whose name carries this instance's tag, has its run cancelled
(POST /v1/agents/{id}/runs/{runId}/cancel) and is archived
(POST /v1/agents/{id}/archive). The agent running the command and this
run's parent stay. The key is read from the environment and is never
written. Without the key, cleanup prints https://cursor.com/agents/<id>.
stop here is the local half of that teardown: no network.

list is `/warp-list`: what is still out. It prints the instance, every
registry row, and, when CURSOR_API_KEY is set, each cloud agent that carries
this run's tag with its status, then `out: tag [warp:<instance>] running=N
idle=N kept=N`. It changes nothing.

cleanup is `/warp-cleanup`: the clear you run yourself. `--cloud` without
`--apply` is a dry run. `--cloud --apply --running` cancels the run of each
RUNNING or ACTIVE match and archives every match. Without `--running` a
running agent is skipped. `--running` is refused while this run is running
unless `--force`: pause or stop first.

list and cleanup take the same selection. By default it is this run: the
registry, and agents in this repo whose name carries `[warp:<instance>]`.
Other agents in the repo are not matched, and neither are another Warp
run's. `--tag a1b2c3` (or `warp:a1b2c3`) names another run's tag in place of
this one. `--tag all` is the tag of any Warp run. `--untagged` adds agents
with no Warp tag, from before 1.5.0, matched loosely on Warp role words in
the name or prompt. One of those that is still running is left alone unless
`--force`, because it may be someone else's. `--all-idle` drops the match
and stays on this repo. `--any-repo` drops the repo check.

hook-start and hook-stop are the local IDE duplicates of the gate. Cloud
runners do not execute hooks, and nothing depends on them.

  python3 scripts/agents.py ?
  python3 scripts/agents.py list --beam .warp/beam.json
  python3 scripts/agents.py list --beam .warp/beam.json --tag all
  python3 scripts/agents.py list --beam .warp/beam.json --untagged
  python3 scripts/agents.py check --beam .warp/beam.json
  python3 scripts/agents.py reap --beam .warp/beam.json --id subagent:WV-01 --ticket WV-01
  python3 scripts/agents.py reap --remote --id subagent:WV-01 --ticket WV-01
  python3 scripts/agents.py reap --beam .warp/beam.json --role parent --id <session>
  python3 scripts/agents.py stop --beam .warp/beam.json
  python3 scripts/agents.py cleanup --beam .warp/beam.json
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --tag a1b2c3
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --untagged
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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


def _signature(beam_path: Path) -> list:
    """Size and mtime of `.warp/agents.json`. Empty when the file is missing."""
    try:
        stat = registry_path(Path(beam_path)).stat()
    except OSError:
        return []
    return [stat.st_mtime_ns, stat.st_size]


def registry(data: dict, beam_path: Optional[Path] = None) -> dict:
    """The registry for this pass. `.warp/agents.json` is the source of truth.

    The beam carries a copy so one pass works on one object. That copy is
    used only while the file is unchanged since this process read or wrote
    it. A Shuttle's reap check, a hook, or another window may have written
    the file since, and then the file wins.
    """
    path = beam_path
    if path is None and data.get("_agentBeam"):
        path = Path(str(data["_agentBeam"]))
    cached = data.get("agentRegistry") if isinstance(data.get("agentRegistry"), dict) else None
    if path is None:
        if cached is None:
            cached = _empty()
            data["agentRegistry"] = cached
        return cached
    same = str(path) == data.get("_agentBeam")
    sig = _signature(Path(path))
    if cached is not None and same and (not sig or sig == data.get("_agentSig")):
        return cached
    loaded = load_file(Path(path))
    if cached is not None and not loaded.get("agents") and not loaded.get("spawns"):
        loaded = cached
    data["agentRegistry"] = loaded
    data["_agentBeam"] = str(path)
    data["_agentSig"] = sig
    return loaded


def flush(data: dict, beam_path: Optional[Path] = None) -> None:
    path = beam_path
    if path is None and data.get("_agentBeam"):
        path = Path(data["_agentBeam"])
    doc = data.get("agentRegistry")
    if isinstance(doc, dict):
        save_file(path, doc)
        if path is not None:
            data["_agentSig"] = _signature(Path(path))


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


def _retire_row(row: dict) -> None:
    """A Shuttle that is being replaced leaves its VM. The next pass cancels and archives it."""
    for ident in (str(row.get("id") or ""), str(row.get("cloudId") or "")):
        if not _cloud_id(ident) or ident in (row.get("archivedIds") or []):
            continue
        left = row.setdefault("retired", [])
        if ident not in left:
            left.append(ident)


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
        if reason == "dead":
            _retire_row(row)
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
        if row.get("halted") or row.get("archived"):
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


def _halted_id(doc: dict, agent_id: str) -> bool:
    if not agent_id:
        return False
    return any(isinstance(row, dict) and row.get("id") == agent_id and row.get("halted") for row in _rows(doc))


def launch_refused(data: dict, ticket: dict) -> list:
    """Why `checkout.py launch` must not print a prompt. Empty means go ahead.

    This is the gate. No prompt is printed while the run is paused, stopped,
    or finished, or for a ticket that is merged or parked.
    """
    tid = str(ticket.get("id") or "") if isinstance(ticket, dict) else ""
    halted = halt_state(data)
    if halted:
        word = "finished" if halted == "done" else halted
        return [
            "spawn: closed %s" % tid,
            "refuse: Warp is %s. No agent starts. /warp-resume or /warp-start opens the run." % word,
        ]
    if isinstance(ticket, dict) and ticket.get("status") in _SETTLED:
        return [
            "spawn: closed %s" % tid,
            "refuse: ticket %s is %s. Nothing starts for it." % (tid, ticket.get("status")),
        ]
    return []


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
    if _halted_id(doc, str(ticket.get("agent") or "")):
        # An id from before a pause or a stop is never reused. Whatever still
        # holds it reads `reap: exit` on its next check.
        ticket["agent"] = _new_id(ticket, doc)
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
        if _halted_id(doc, agent):
            ticket["agent"] = agent
            agent = _new_id(ticket, doc)
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
    key: Optional[str] = None,
    transport: Optional[Callable] = None,
) -> list:
    """End this ticket's agents. With CURSOR_API_KEY, cancel and archive every VM it used."""
    note_closed(data, ticket, reason, now=now, beam_path=beam_path)
    finish_bugbot(data, ticket, now=now, beam_path=beam_path)
    doc = registry(data, beam_path)
    tid = str(ticket.get("id") or "")
    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = []
    secret = api_key() if key is None else key
    kept = protected_ids(doc)
    mine = current_agent_id()
    if mine:
        kept.add(mine)
    wanted = []
    changed = False
    for row in _rows(doc):
        if not isinstance(row, dict) or str(row.get("ticket") or "") != tid:
            continue
        if row.get("role") == "bugbot" and row.get("state") in _LIVE:
            row["state"] = "ended"
            row["ended"] = now_s
            row["endReason"] = reason
            changed = True
        if row.get("state") not in {"ended", "stopped"} and not row.get("ended"):
            continue
        if row.get("archived"):
            continue
        done = row.get("archivedIds") if isinstance(row.get("archivedIds"), list) else []
        for ident in row_cloud_ids(row):
            if ident not in done and ident not in kept and ident not in wanted:
                wanted.append(ident)
    if changed:
        flush(data, beam_path)
    archived = []
    for ident in wanted:
        if not secret:
            lines.append("cleanup: link %s" % agent_url(ident))
            continue
        ok, said = _stop_cloud(ident, secret, transport)
        lines.extend(said)
        if ok:
            archived.append(ident)
    _commit_archived(data, beam_path, archived, now_s)
    return lines


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


LISTENER_ID = "listener"


def note_listener_poll(data: dict, now=None, beam_path: Optional[Path] = None) -> None:
    """One poll is out. The registry keeps one listener row, not one row per poll."""
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    row = None
    for item in _rows(doc):
        if not isinstance(item, dict) or item.get("role") != "listener":
            continue
        if item.get("id") == LISTENER_ID and row is None:
            row = item
            continue
        if item.get("state") in _LIVE:
            item["state"] = "ended"
            item["ended"] = now_s
            item["endReason"] = "replaced"
    if row is None:
        row = {"id": LISTENER_ID, "ticket": "", "role": "listener", "polls": 0}
        doc.setdefault("agents", []).append(row)
    row["session"] = str(data.get("parentSession") or "")
    row["started"] = now_s
    row["ended"] = None
    row["endReason"] = ""
    row["state"] = "running"
    row["polls"] = int(row.get("polls") or 0) + 1
    row.pop("halted", None)
    flush(data, beam_path)


def note_listener_done(data: dict, now=None, beam_path: Optional[Path] = None, reason: str = "polled") -> None:
    """The poll came back, or was lost. The listener row is ended until the next poll."""
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    changed = False
    for item in _rows(doc):
        if not isinstance(item, dict) or item.get("role") != "listener":
            continue
        if item.get("state") in _LIVE:
            item["state"] = "ended"
            item["ended"] = now_s
            item["endReason"] = reason
            changed = True
    if changed:
        flush(data, beam_path)


def stop_all(data: dict, now=None, beam_path: Optional[Path] = None, reason: str = "stopped") -> list:
    """Mark every live registered agent stopped. Pause, stop, and the end of a run.

    Every row is also marked halted, so nothing from before the halt is
    resumed. `/warp-resume` and `/warp-start` start new agents.
    """
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = []
    changed = False
    for row in _rows(doc):
        if not isinstance(row, dict):
            continue
        if not row.get("halted"):
            row["halted"] = True
            changed = True
        if row.get("state") not in _LIVE:
            continue
        row["state"] = "stopped"
        row["ended"] = now_s
        row["endReason"] = reason
        changed = True
        if row.get("id"):
            lines.append("stop: %s" % row["id"])
    raw = data.get("listener")
    if isinstance(raw, dict):
        raw["state"] = "stopped"
        raw.pop("pollStartedAt", None)
    if changed:
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # No network, a refused connection, a timeout. Status 0 is a failed
        # call. It never raises, so a pass or a halt is not cut off halfway.
        return 0, {"error": str(getattr(exc, "reason", exc))[:200]}
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


def _metadata(key: str, timeout: float = 1.5) -> str:
    """One value from the Cursor VM metadata socket. Empty when there is none."""
    sock_path = os.environ.get("CURSOR_AGENT_SOCKET", "").strip() or "/run/cursor/api.sock"
    if not os.path.exists(sock_path):
        return ""
    import socket

    chunks = []
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(timeout)
            conn.connect(sock_path)
            conn.sendall(("GET /v1/meta-data/%s HTTP/1.0\r\nHost: cursor-agent\r\n\r\n" % key).encode())
            while True:
                part = conn.recv(4096)
                if not part:
                    break
                chunks.append(part)
        finally:
            conn.close()
    except OSError:
        return ""
    raw = b"".join(chunks).decode("utf-8", "replace")
    head, _sep, body = raw.partition("\r\n\r\n")
    first = head.splitlines()[0] if head else ""
    if " 200" not in first:
        return ""
    return body.strip()


def current_agent_id() -> str:
    """Cloud agent id of this process: the environment, then the VM metadata socket."""
    for key in ("CURSOR_AGENT_ID", "CURSOR_CLOUD_AGENT_ID", "CURSOR_CONVERSATION_ID"):
        value = os.environ.get(key, "").strip()
        if value.startswith("bc-") or value.startswith("bc_"):
            return value
    value = _metadata("agent/id")
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


TAG = "warp"
_TAG_RE = re.compile(r"(?im)^\s*\[warp(?::([a-z0-9]{1,16}))?\](?=\s|$)")


def machine_id() -> str:
    """The machine this process is on: the Cursor cloud agent id on a cloud VM, else an id for this computer."""
    cloud = current_agent_id()
    if cloud:
        return cloud
    import socket
    import uuid

    try:
        node = "%012x" % uuid.getnode()
    except Exception:
        node = ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    return "local-%s-%s" % (host or "host", node or "0")


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()
    except Exception:
        return ""


def mint_instance(beam_path: Optional[Path], now=None) -> dict:
    """A new instance record for this beam: six characters that name this Warp run.

    On a cloud VM it is the last six characters of the parent's cloud agent
    id. On a laptop one machine can run Warp in several checkouts, so it is
    six characters of a hash of the machine id and this beam's path.
    """
    machine = machine_id()
    if _cloud_id(machine):
        ident = re.sub(r"[^a-z0-9]", "", machine.lower())[-6:]
    else:
        where = str(Path(beam_path).resolve()) if beam_path is not None else ""
        ident = hashlib.sha256(("%s|%s" % (machine, where)).encode()).hexdigest()[:6]
    return {"id": ident, "machine": machine, "host": _hostname(), "createdAt": beam_mod.coerce_now(now)[1]}


def instance_id(data: dict) -> str:
    """The six-character id on the beam. Empty until `/warp-start` or a launch sets it."""
    raw = data.get("instance") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        return str(raw.get("id") or "").strip().lower()
    return str(raw or "").strip().lower()


def ensure_instance(data: dict, beam_path: Optional[Path] = None, now=None) -> str:
    """The instance id, set once and kept. The caller writes the beam.

    It belongs to the beam, not to one parent. A later `/warp-resume` from
    another window or another VM keeps it, so that parent still finds the
    agents the earlier one started.
    """
    found = instance_id(data)
    if found:
        return found
    data["instance"] = mint_instance(beam_path, now=now)
    return data["instance"]["id"]


def tag(instance: str = "") -> str:
    """`warp:<instance>`, or `warp` when the beam has no instance yet."""
    return "%s:%s" % (TAG, instance) if instance else TAG


def instance_line(data: dict) -> str:
    """One line for start, status, version, and list: which Warp this is and where it started."""
    raw = data.get("instance") if isinstance(data, dict) and isinstance(data.get("instance"), dict) else {}
    ident = instance_id(data)
    if not ident:
        return "instance: none yet. /warp-start sets it."
    return "instance: %s host=%s machine=%s" % (tag(ident), raw.get("host") or "unknown", raw.get("machine") or "unknown")


def tag_name(ticket_id: str = "", step: str = "", role: str = "shuttle", instance: str = "") -> str:
    """The name every agent Warp starts carries: `[warp:<instance>] <ticket> <step>`.

    Cursor has no tag field on an agent. The name is the one label its API
    returns in the agent list, so the tag lives there, and on the first line
    of the prompt. The instance tells this Warp run's agents from another
    Warp run's, in this repo or any other.
    """
    head = "[%s]" % tag(instance)
    if role == "listener":
        return "%s listener" % head
    return " ".join(part for part in (head, str(ticket_id or ""), str(step or "")) if part)


def tag_instance(text: str) -> Optional[str]:
    """The instance in a `[warp:<instance>]` tag that starts a name or a prompt line.

    None when there is no tag. Empty for a bare `[warp]`.
    """
    match = _TAG_RE.search(str(text or ""))
    if not match:
        return None
    return str(match.group(1) or "").lower()


def has_tag(text: str) -> bool:
    """True when a name, or any line of a prompt, starts with a Warp tag of any instance."""
    return tag_instance(text) is not None


def parse_tag(value: str) -> tuple:
    """(instance, any) from `--tag`. `all` and `*` mean the tag of any Warp run.

    `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same instance.
    Empty means this beam's own instance.
    """
    text = str(value or "").strip().strip("[]").strip().lower()
    if not text:
        return "", False
    if text in {"*", "all", "any", "warp:*", "warp:all"}:
        return "", True
    if text.startswith("warp:"):
        text = text[len("warp:"):]
    return re.sub(r"[^a-z0-9]", "", text), False


def tag_match(text: str, instance: str = "", all_instances: bool = False) -> bool:
    """True when the text carries this instance's tag. `all_instances` takes any Warp tag."""
    found = tag_instance(text)
    if found is None:
        return False
    if all_instances or not instance:
        return True
    return found == instance


def warp_role(text: str) -> bool:
    """Loose match for agents from before the tag: Shuttle, listener, fix, rebase, or Bugbot in the text."""
    folded = str(text or "").casefold()
    if any(word in folded for word in _ROLE_WORDS):
        return True
    return re.search(r"\bfix\b", folded) is not None


def row_cloud_ids(row: dict) -> list:
    """Every cloud agent id this row has had: its id, its VM, and VMs it left behind."""
    found = []
    if not isinstance(row, dict):
        return found
    values = [row.get("id"), row.get("cloudId")]
    if isinstance(row.get("retired"), list):
        values.extend(row["retired"])
    for value in values:
        ident = str(value or "")
        if _cloud_id(ident) and ident not in found:
            found.append(ident)
    return found


def _registry_cloud_ids(doc: dict) -> set:
    found = set()
    for row in _rows(doc):
        found.update(row_cloud_ids(row))
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


_LAUNCH_RE = re.compile(r"(?m)^\s*(SUBAGENT|IMPLEMENT)\s+([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*$")
_LISTEN_RE = re.compile(r"(?m)^\s*LISTEN\s+once\s*$")


def launch_marker(text: str) -> tuple:
    """(`shuttle`, ticket id) or (`listener`, ``) for a prompt Warp printed. Else (``, ``)."""
    body = str(text or "")
    match = _LAUNCH_RE.search(body)
    if match:
        return "shuttle", match.group(2)
    if _LISTEN_RE.search(body):
        return "listener", ""
    return "", ""


def strict_role(text: str) -> bool:
    """True only for a prompt Warp printed: SUBAGENT <id>, IMPLEMENT <id>, or LISTEN once."""
    return bool(launch_marker(text)[0])


def classify_cloud(
    item: dict,
    origin_key: str,
    registry_ids: set,
    all_idle: bool = False,
    any_repo: bool = False,
    self_id: str = "",
    running: bool = False,
    protected: Optional[set] = None,
    untagged: bool = False,
    instance: str = "",
    all_instances: bool = False,
    cancel_loose: bool = False,
) -> str:
    """`archive`, `cancel`, `skip:<reason>`, or empty when the agent is out of scope.

    In scope means this repo (unless `--any-repo`) and one of: a registry
    row, or this instance's tag `[warp:<instance>]` on its name or prompt.
    `all_instances` takes the tag of any Warp run. `untagged` adds agents
    from before the tag: a prompt Warp printed, or Warp role words.
    `--all-idle` drops the match. This process and this run's parent are
    never touched. A RUNNING or ACTIVE agent is skipped unless `running`,
    which cancels its run before the archive.
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
    name = str(item.get("name") or "")
    prompt = cloud_prompt(item)
    role = tag_match(name, instance, all_instances) or tag_match(prompt, instance, all_instances)
    loose = False
    if not role and untagged and not has_tag(name) and not has_tag(prompt):
        # Untagged means it carries no Warp tag at all. An agent tagged for
        # another Warp run belongs to that run.
        loose = strict_role(prompt) or warp_role("%s\n%s" % (name, prompt))
    if not all_idle and not in_registry and not role and not loose:
        return ""
    if self_id and ident == self_id:
        return "skip:self"
    if protected and ident in protected:
        return "skip:parent"
    if status in _BUSY_STATUS:
        if not running:
            return "skip:running"
        if not in_registry and not role and not cancel_loose:
            # Matched on words alone, or only by --all-idle, and still working.
            # That may be someone else's agent. It takes --force to cancel it.
            return "skip:running-untagged"
        return "cancel"
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


def cancel_run(agent_id: str, key: str, transport: Optional[Callable] = None, run_id: str = "") -> tuple:
    """POST /v1/agents/{id}/runs/{runId}/cancel for the latest run. Returns (ok, detail).

    A run that already ended (409) counts as cancelled.
    """
    call = transport or default_transport
    run = str(run_id or "")
    if not run:
        run = str(fetch_agent(agent_id, key, transport=transport).get("latestRunId") or "")
    if not run:
        return False, "no run"
    status, _body = call("POST", "%s/v1/agents/%s/runs/%s/cancel" % (API_ROOT, agent_id, run), key, None)
    code = int(status)
    return (200 <= code < 300) or code == 409, status


def _needs_detail(
    item: dict,
    registry_ids: set,
    all_idle: bool,
    any_repo: bool,
    untagged: bool = False,
    name_only: bool = False,
    instance: str = "",
    all_instances: bool = False,
) -> bool:
    """Whether GET /v1/agents/{id} is needed to decide. List rows carry the name and not the repos.

    `name_only` is the halt: it is on a time limit, so an agent whose name
    has no tag and that is not in the registry is passed over from the list
    row alone, with no extra call.
    """
    ident = str(item.get("id") or "")
    name = str(item.get("name") or "")
    known = ident in registry_ids or tag_match(name, instance, all_instances)
    if name_only and not known:
        return False
    if not any_repo and not cloud_repos(item):
        return True
    if all_idle or known:
        return False
    if untagged and warp_role(name):
        return False
    return not cloud_prompt(item)


def _merge_detail(item: dict, detail: dict) -> dict:
    merged = dict(item)
    for field in ("name", "status", "repos", "prompt", "url", "updatedAt", "repository", "repo", "git", "latestRunId"):
        value = detail.get(field)
        if value not in (None, "", []):
            merged[field] = value
    return merged


def protected_ids(doc: dict) -> set:
    """Cloud ids of this run's parent. Cleanup never cancels or archives the orchestrator.

    The latest parent stays protected after a pause or a stop, so the
    conversation that ran the work is still there to read. A parent that a
    later start or resume replaced is an ordinary idle agent.
    """
    found = set()
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("role") != "parent":
            continue
        if row.get("superseded"):
            continue
        for key in ("id", "cloudId"):  # a parent has one VM
            ident = str(row.get(key) or "")
            if _cloud_id(ident):
                found.add(ident)
    return found


def _recorded_links(doc: dict) -> list:
    """Cloud ids the registry holds that are ended and not archived. The parent is never one."""
    ended = []
    kept = protected_ids(doc)
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("archived"):
            continue
        gone = row.get("state") in {"ended", "stopped"} or bool(row.get("ended"))
        left = row.get("retired") if isinstance(row.get("retired"), list) else []
        done = row.get("archivedIds") if isinstance(row.get("archivedIds"), list) else []
        for ident in row_cloud_ids(row):
            if ident in ended or ident in kept or ident in done:
                continue
            if gone or ident in left:
                ended.append(ident)
    return ended


def tag_scope(data: dict, tag_override: str = "") -> tuple:
    """(instance, any, foreign) for a listing or a cleanup.

    `foreign` is true when `--tag` names something other than this beam's
    own instance. Then this beam's registry is left out of it.
    """
    own = instance_id(data)
    wanted, every = parse_tag(tag_override)
    if every:
        return own, True, False
    if wanted and wanted != own:
        return wanted, False, True
    return own, False, False


def scope_text(instance: str, every: bool, untagged: bool = False, all_idle: bool = False) -> str:
    if all_idle:
        return "every idle agent"
    text = "any Warp run" if every else ("[%s]" % tag(instance))
    return text + (" and untagged Warp roles" if untagged else "")


def select_cloud(
    data: dict,
    beam_path: Optional[Path],
    secret: str,
    transport: Optional[Callable] = None,
    origin: Optional[str] = None,
    self_id: Optional[str] = None,
    tag_override: str = "",
    untagged: bool = False,
    all_idle: bool = False,
    any_repo: bool = False,
    running: bool = False,
    cancel_loose: bool = False,
    name_only: bool = False,
    skip: Optional[set] = None,
    deadline: Optional[float] = None,
    tick: Optional[Callable] = None,
    started: float = 0.0,
) -> dict:
    """The cloud agents a listing or a cleanup is about. Reads, never writes.

    Returns `error` (a line, or empty), `matches` as (agent, kind) with kind
    `archive`, `cancel`, or `skip:<reason>`, the repo key, and whether the
    time limit cut the read short.
    """
    doc = registry(data, beam_path)
    instance, every, foreign = tag_scope(data, tag_override)
    root = Path(beam_path).resolve().parent.parent if beam_path is not None else None
    remote = origin if origin is not None else origin_url(root)
    origin_key = repo_key(remote)
    found = {"error": "", "matches": [], "origin": origin_key, "late": False, "instance": instance, "every": every, "foreign": foreign}
    if not any_repo and not origin_key:
        found["error"] = "no origin; nothing matched"
        return found
    ok, items, err = list_cloud(secret, transport=transport)
    if not ok:
        found["error"] = str(err)
        return found
    mine = current_agent_id() if self_id is None else self_id
    registry_ids = set() if foreign else _registry_cloud_ids(doc)
    guarded = protected_ids(doc)
    for item in items:
        if not isinstance(item, dict):
            continue
        if deadline is not None and tick is not None and tick() - started > deadline:
            found["late"] = True
            break
        ident = str(item.get("id") or "")
        if skip and ident in skip:
            continue
        if _needs_detail(item, registry_ids, all_idle, any_repo, untagged=untagged, name_only=name_only, instance=instance, all_instances=every) and ident:
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
            running=running,
            protected=guarded,
            untagged=untagged,
            instance=instance,
            all_instances=every,
            cancel_loose=cancel_loose,
        )
        if kind:
            found["matches"].append((item, kind))
    return found


def out_lines(
    data: dict,
    beam_path: Optional[Path] = None,
    key: Optional[str] = None,
    transport: Optional[Callable] = None,
    origin: Optional[str] = None,
    self_id: Optional[str] = None,
    tag_override: str = "",
    untagged: bool = False,
    all_idle: bool = False,
    any_repo: bool = False,
) -> list:
    """What is still out: the registry, then the tagged cloud agents with their status. Changes nothing.

    This is `/warp-list`. The cloud half needs CURSOR_API_KEY. `--tag` names
    another run's tag, or `all` for any Warp run. `--untagged` adds agents
    from before the tag.
    """
    doc = registry(data, beam_path)
    instance, every, foreign = tag_scope(data, tag_override)
    lines = [instance_line(data)]
    scope = scope_text(instance, every, untagged, all_idle)
    if foreign:
        lines.append("list: tag %s is not this run. This run's registry is left out." % scope)
    else:
        rows = [row for row in _rows(doc) if isinstance(row, dict) and row.get("id")]
        lines.extend(_row_line(row) for row in rows)
        live = sum(1 for row in rows if row.get("state") in _LIVE)
        lines.append("registry: live=%d ended=%d" % (live, len(rows) - live))
    secret = api_key() if key is None else key
    if not secret:
        lines.append("cloud: not read. Set %s in the environment to list cloud agents." % ENV_KEY)
        if not foreign:
            for ident in _recorded_links(doc):
                lines.append("cloud: link %s" % agent_url(ident))
        return lines
    found = select_cloud(
        data,
        beam_path,
        secret,
        transport=transport,
        origin=origin,
        self_id=self_id,
        tag_override=tag_override,
        untagged=untagged,
        all_idle=all_idle,
        any_repo=any_repo,
        running=True,
        cancel_loose=False,
    )
    if found["error"]:
        lines.append("cloud: %s" % found["error"])
        return lines
    busy = idle = kept = 0
    for item, kind in found["matches"]:
        lines.append(cloud_match_line(item, found["origin"]))
        if kind == "cancel":
            busy += 1
        elif kind == "archive":
            idle += 1
        else:
            kept += 1
            lines.append("list: kept %s reason=%s" % (item.get("id"), kind.split(":", 1)[1]))
    lines.append("out: tag %s running=%d idle=%d kept=%d" % (scope, busy, idle, kept))
    if busy or idle:
        lines.append("out: /warp-cleanup cancels the running ones and archives all %d." % (busy + idle))
    else:
        lines.append("out: nothing to clean up.")
    return lines


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
    running: bool = False,
    force: bool = False,
    untagged: bool = False,
    all_instances: bool = False,
    name_only: bool = False,
    listing: bool = True,
    deadline: Optional[float] = None,
    clock: Optional[Callable] = None,
    skip: Optional[set] = None,
    tag_override: str = "",
) -> list:
    """Cancel and archive cloud agents. `--cloud` without `--apply` is a dry run.

    A cloud agent is in scope when its repository matches this repo's origin
    and it is in `.warp/agents.json`, or its name starts with this
    instance's tag, `[warp:<instance>]`. `--tag` names another run's tag,
    or `all` for the tag of any Warp run. `--untagged` adds agents from
    before the tag, matched loosely on Warp role words. `--all-idle` drops
    the match and stays on this repo. `--any-repo` drops the repo check.
    This process and this run's parent are never touched. A RUNNING or
    ACTIVE agent stays unless `--running`, which cancels its run first.
    `--running` is refused while this run is running, unless `--force`:
    pause or stop first. An agent that only matched loosely and is still
    running is also left unless `--force`.
    """
    import time

    tick = clock or time.monotonic
    started = tick()
    doc = registry(data, beam_path)
    if all_instances and not tag_override:
        tag_override = "all"
    instance, every, foreign = tag_scope(data, tag_override)
    lines = list_lines(data, beam_path) if listing and not foreign else []
    secret = api_key() if key is None else key
    if not secret:
        lines.append("cleanup: no %s; archive in the Cursor UI" % ENV_KEY)
        if not foreign:
            for ident in _recorded_links(doc):
                lines.append("cleanup: link %s" % agent_url(ident))
        if cloud:
            lines.append("cleanup: cloud list needs %s" % ENV_KEY)
        return lines
    if not cloud:
        lines.append("cleanup: dry-run" if not apply else "cleanup: cloud required to archive")
        return lines
    if foreign and listing:
        lines.append("cleanup: tag [%s] is not this run. Nothing checks whether that run is still going." % tag(instance))
    elif running and spawns_open(data) and not force:
        lines.append("cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.")
        running = False
    if listing:
        lines.append("cleanup: tag %s" % scope_text(instance, every, untagged, all_idle))
    found = select_cloud(
        data,
        beam_path,
        secret,
        transport=transport,
        origin=origin,
        self_id=self_id,
        tag_override=tag_override,
        untagged=untagged,
        all_idle=all_idle,
        any_repo=any_repo,
        running=running,
        cancel_loose=force,
        name_only=name_only,
        skip=skip,
        deadline=deadline,
        tick=tick,
        started=started,
    )
    if found["error"]:
        lines.append("cleanup: %s" % found["error"])
        if found["error"].startswith("no origin"):
            lines.append("cleanup: count 0")
            lines.append("cleanup: dry-run" if not apply else "cleanup: archived 0")
        return lines
    late = found["late"]
    chosen = []
    for item, kind in found["matches"]:
        ident = str(item.get("id") or "")
        lines.append(cloud_match_line(item, found["origin"]))
        if kind in {"archive", "cancel"}:
            chosen.append((item, kind))
            if kind == "cancel" and not apply:
                lines.append("cleanup: would cancel %s" % ident)
        elif kind.startswith("skip:"):
            lines.append("cleanup: skip %s reason=%s" % (ident, kind.split(":", 1)[1]))
    lines.append("cleanup: count %d" % len(chosen))
    if not apply:
        lines.append("cleanup: dry-run")
        if late:
            lines.append("cleanup: list cut short; run it again")
        return lines
    archived = 0
    marked = []
    seen = set()
    for item, kind in chosen:
        ident = str(item.get("id") or "")
        if not ident or ident in seen:
            continue
        if deadline is not None and tick() - started > deadline:
            late = True
            break
        seen.add(ident)
        if kind == "cancel":
            done, status = cancel_run(ident, secret, transport=transport, run_id=str(item.get("latestRunId") or ""))
            if not done:
                lines.append("cleanup: cancel failed %s (%s) %s" % (ident, status, agent_url(ident)))
                continue
            lines.append("cleanup: cancelled %s" % ident)
        posted, status = archive_id(ident, secret, transport=transport)
        if posted:
            archived += 1
            marked.append(ident)
            lines.append("cleanup: archived %s" % ident)
        else:
            lines.append("cleanup: archive failed %s (%s) %s" % (ident, status, agent_url(ident)))
    lines.append("cleanup: archived %d" % archived)
    if late:
        lines.append("cleanup: cut short; run /warp-cleanup for the rest")
    if not foreign:
        _commit_archived(data, beam_path, marked, beam_mod.coerce_now(now)[1])
    return lines


def _stop_cloud(ident: str, secret: str, transport: Optional[Callable]) -> tuple:
    """Cancel one cloud agent's run if it has one going, then archive it.

    Returns (archived, lines). An agent that cannot be read is left alone
    and reported, so a running agent is never archived without a cancel.
    """
    detail = fetch_agent(ident, secret, transport=transport)
    if not detail:
        return False, ["cleanup: unreachable %s %s" % (ident, agent_url(ident))]
    status = str(detail.get("status") or "").strip().upper()
    if status == "ARCHIVED":
        return True, []
    lines = []
    if status in _BUSY_STATUS:
        done, code = cancel_run(ident, secret, transport=transport, run_id=str(detail.get("latestRunId") or ""))
        if not done:
            return False, ["cleanup: cancel failed %s (%s) %s" % (ident, code, agent_url(ident))]
        lines.append("cleanup: cancelled %s" % ident)
    posted, code = archive_id(ident, secret, transport=transport)
    if not posted:
        return False, lines + ["cleanup: archive failed %s (%s) %s" % (ident, code, agent_url(ident))]
    return True, lines + ["cleanup: archived %s" % ident]


def _commit_archived(data: dict, beam_path: Optional[Path], idents: list, stamp: str) -> None:
    """Record archived agents on the registry as it is now.

    The API calls take seconds. Another process may have written the
    registry meanwhile, so it is read again before the marks go on.
    """
    if not idents:
        return
    doc = registry(data, beam_path)
    for ident in idents:
        _mark_archived(doc, ident, stamp)
    flush(data, beam_path)


def _mark_archived(doc: dict, ident: str, stamp: str) -> None:
    """Record one archived cloud agent. A VM a row left behind does not end the row."""
    for row in _rows(doc):
        if not isinstance(row, dict):
            continue
        left = row.get("retired") if isinstance(row.get("retired"), list) else []
        if ident in left:
            left.remove(ident)
            done = row.setdefault("archivedIds", [])
            if ident not in done:
                done.append(ident)
            continue
        if row.get("id") == ident or row.get("cloudId") == ident:
            if row.get("state") in _LIVE or not row.get("ended"):
                row["ended"] = stamp
                row["endReason"] = "archived"
            row["state"] = "ended"
            row["archived"] = True


def retire(
    data: dict,
    beam_path: Optional[Path] = None,
    now=None,
    key: Optional[str] = None,
    transport: Optional[Callable] = None,
) -> list:
    """Cancel and archive cloud agents a ticket left behind.

    A fix round that starts on a new VM leaves the previous VM idle. That
    VM is retired here, on the next pass, so a ticket never holds more than
    one cloud agent. Without CURSOR_API_KEY this prints the link once.
    """
    doc = registry(data, beam_path)
    waiting = []
    for row in _rows(doc):
        if not isinstance(row, dict) or not isinstance(row.get("retired"), list):
            continue
        for ident in list(row["retired"]):
            if _cloud_id(str(ident)):
                waiting.append((row, str(ident)))
    if not waiting:
        return []
    secret = api_key() if key is None else key
    guarded = protected_ids(doc)
    mine = current_agent_id()
    stamp = beam_mod.coerce_now(now)[1]
    lines = []
    todo = []
    changed = False
    for row, ident in waiting:
        if ident in guarded or ident == mine:
            row["retired"].remove(ident)
            changed = True
        elif not secret:
            linked = row.setdefault("linked", [])
            if ident not in linked:
                linked.append(ident)
                lines.append("cleanup: link %s" % agent_url(ident))
                changed = True
        elif ident not in todo:
            todo.append(ident)
    if changed:
        flush(data, beam_path)
    archived = []
    for ident in todo:
        ok, said = _stop_cloud(ident, secret, transport)
        lines.extend(said)
        if ok:
            archived.append(ident)
    _commit_archived(data, beam_path, archived, stamp)
    return lines


HALT_DEADLINE_SECONDS = 45.0


def halt_state(data: dict) -> str:
    """`paused`, `stopped`, `done`, or empty while the run may spawn."""
    if spawns_open(data):
        return ""
    state = data.get("runState") or ("paused" if data.get("paused") else "running")
    if state == "stopped":
        return "stopped"
    if data.get("paused") or state == "paused":
        return "paused"
    return "done"


def halted_meanwhile(data: dict, beam_path: Optional[Path]) -> Optional[dict]:
    """The beam on disk, when it was paused or stopped after `data` was read. Else None.

    A pass reads the beam, works for a while, and writes it back. A pause
    from another process in between must not be written over.
    """
    if beam_path is None or halt_state(data) in {"paused", "stopped"}:
        return None
    try:
        disk = beam_mod.load_json(Path(beam_path))
    except (OSError, ValueError):
        return None
    if not isinstance(disk, dict) or halt_state(disk) not in {"paused", "stopped"}:
        return None
    return disk


def halt_local(data: dict, beam_path: Optional[Path] = None, reason: str = "stopped", now=None) -> list:
    """End every registered agent and free every Shuttle slot. No network.

    Pause and stop are the same teardown. Each agent exits at its next reap
    check. A ticket that had a Shuttle out is marked so the next start or
    resume launches a new one without counting a recovery.
    """
    lines = stop_all(data, now=now, beam_path=beam_path, reason=reason)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    for ticket in tickets.values():
        if not isinstance(ticket, dict) or ticket.get("status") in _SETTLED:
            continue
        shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else None
        out = bool(shuttle and shuttle.get("pending"))
        # A status that is Shuttle work with no slot marked is the same thing:
        # a Shuttle was meant to be on it. Left alone, the watchdog would call
        # it a dead worker after the pause.
        if not out and ticket.get("status") not in beam_mod.SHUTTLE_WORK:
            continue
        if shuttle is not None:
            shuttle["pending"] = False
        ticket["needsReplacement"] = True
        ticket["haltResume"] = True
    lines.append("halt: %s agents=%d" % (reason, len(lines)))
    return lines


def halt_cloud(
    data: dict,
    beam_path: Optional[Path] = None,
    now=None,
    key: Optional[str] = None,
    transport: Optional[Callable] = None,
    origin: Optional[str] = None,
    self_id: Optional[str] = None,
    deadline: Optional[float] = HALT_DEADLINE_SECONDS,
    clock: Optional[Callable] = None,
) -> list:
    """Cancel and archive this run's cloud agents after a halt.

    With CURSOR_API_KEY: every cloud agent in the registry, and every agent
    in this repo whose name carries the `[warp]` tag, has its run cancelled
    and is archived. This process and this run's parent stay. Without the
    key it prints one link per recorded cloud agent.
    """
    import time

    secret = api_key() if key is None else key
    tick = clock or time.monotonic
    started = tick()
    lines = []
    handled = set()
    if secret:
        # The agents this run recorded come first, by id. They do not depend
        # on the account's agent list, which can be long or fail to load.
        lines, handled = _halt_registered(data, beam_path, secret, transport, now, self_id, deadline, tick, started)
    left = None if deadline is None else max(deadline - (tick() - started), 0.0)
    lines.extend(
        cleanup(
            data,
            beam_path=beam_path,
            cloud=bool(secret),
            apply=bool(secret),
            key=secret,
            transport=transport,
            now=now,
            origin=origin,
            self_id=self_id,
            running=True,
            force=True,
            name_only=True,
            listing=False,
            deadline=left,
            clock=clock,
            skip=handled,
        )
    )
    return lines


def _halt_registered(
    data: dict,
    beam_path: Optional[Path],
    secret: str,
    transport: Optional[Callable],
    now,
    self_id: Optional[str],
    deadline: Optional[float],
    tick: Callable,
    started: float,
) -> tuple:
    """Cancel and archive every cloud agent in the registry, by id. Returns (lines, ids handled)."""
    doc = registry(data, beam_path)
    guarded = protected_ids(doc)
    mine = current_agent_id() if self_id is None else self_id
    stamp = beam_mod.coerce_now(now)[1]
    lines = []
    handled = set()
    wanted = []
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("role") == "parent":
            continue
        done = row.get("archivedIds") if isinstance(row.get("archivedIds"), list) else []
        for ident in row_cloud_ids(row):
            if ident in wanted or ident in guarded or ident == mine or ident in done:
                continue
            if row.get("archived") and ident in {row.get("id"), row.get("cloudId")}:
                continue
            wanted.append(ident)
    archived = []
    for ident in wanted:
        if deadline is not None and tick() - started > deadline:
            break
        handled.add(ident)
        ok, said = _stop_cloud(ident, secret, transport)
        lines.extend(said)
        if ok:
            archived.append(ident)
    _commit_archived(data, beam_path, archived, stamp)
    return lines, handled


def finish(data: dict, beam_path: Optional[Path] = None, now=None, key: Optional[str] = None, transport: Optional[Callable] = None) -> list:
    """Every ticket is merged or parked. Tear the run down once."""
    if spawns_open(data) or halt_state(data) != "done":
        return []
    doc = registry(data, beam_path)
    if doc.get("finishedAt"):
        return []
    if not any(isinstance(row, dict) and row.get("id") for row in _rows(doc)):
        return []
    _now_dt, now_s = beam_mod.coerce_now(now)
    lines = halt_local(data, beam_path=beam_path, reason="done", now=now)
    doc = registry(data, beam_path)
    doc["finishedAt"] = now_s
    flush(data, beam_path)
    lines.extend(halt_cloud(data, beam_path=beam_path, now=now, key=key, transport=transport))
    return lines


def note_parent(data: dict, now=None, beam_path: Optional[Path] = None, cloud_id: Optional[str] = None) -> str:
    """Record this parent session. An earlier live parent is ended first: one parent per run."""
    doc = registry(data, beam_path)
    _now_dt, now_s = beam_mod.coerce_now(now)
    session = str(data.get("parentSession") or "")
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("role") != "parent":
            continue
        row["superseded"] = True
        if row.get("state") in _LIVE:
            row["state"] = "ended"
            row["ended"] = now_s
            row["endReason"] = "replaced"
    cloud = current_agent_id() if cloud_id is None else cloud_id
    ident = cloud or ("parent:%s" % session)
    doc.setdefault("agents", []).append(
        {
            "id": ident,
            "ticket": "",
            "role": "parent",
            "session": session,
            "started": now_s,
            "ended": None,
            "state": "running",
        }
    )
    doc.pop("finishedAt", None)
    flush(data, beam_path)
    return ident


def parent_superseded(data: dict, session: str, beam_path: Optional[Path] = None) -> bool:
    """True when a later /warp-start or /warp-resume took the run over."""
    wanted = str(session or "").strip()
    if not wanted:
        return False
    doc = registry(data, beam_path)
    mine = None
    for row in _rows(doc):
        if not isinstance(row, dict) or row.get("role") != "parent":
            continue
        if str(row.get("session") or "") == wanted or str(row.get("id") or "") == wanted:
            mine = row
    if mine is None:
        return False
    return bool(mine.get("superseded")) or mine.get("endReason") == "replaced"


_REAP_GONE = {"dead", "replaced", "stopped", "paused", "done", "archived", "merged", "parked", "closed", "settled"}


def reap_reason(
    data: dict,
    agent_id: str = "",
    ticket_id: str = "",
    role: str = "shuttle",
    now=None,
    beam_path: Optional[Path] = None,
    strict_local: bool = False,
) -> str:
    """Why this agent must exit now. Empty means carry on.

    The run is paused, stopped, or finished. The ticket is merged or parked.
    The agent's own row was ended by a halt, a replacement, or an archive.
    A Shuttle whose return was recorded more than staleMinutes ago and that
    was not resumed is not supposed to be running either.
    """
    halted = halt_state(data)
    doc = registry(data, beam_path)
    ident = str(agent_id or "").strip()
    if role == "parent":
        if halted:
            return halted
        return "replaced" if parent_superseded(data, ident, beam_path=beam_path) else ""
    if halted:
        return halted
    if role == "listener":
        return ""
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    ticket = tickets.get(ticket_id) if ticket_id else None
    if isinstance(ticket, dict) and ticket.get("status") in _SETTLED:
        return "settled"
    mine = None
    if ident:
        for row in _rows(doc):
            if not isinstance(row, dict) or row.get("role") not in {"shuttle", None, ""}:
                continue
            if str(row.get("id") or "") != ident and str(row.get("cloudId") or "") != ident:
                continue
            if ticket_id and str(row.get("ticket") or "") not in {"", str(ticket_id)}:
                continue
            mine = row
    if mine is None:
        if isinstance(ticket, dict) and ident:
            owner = str(ticket.get("agent") or "")
            if owner and owner != ident and live_rows(doc, str(ticket_id), "shuttle"):
                return "replaced"
            # checkout.py launch records a row before it prints a prompt. An id
            # with no row, on a ticket someone else owns, was not launched by
            # the parent. Origin can lag, so only the local registry decides.
            if strict_local and owner and owner != ident:
                return "not-launched"
        return ""
    if mine.get("archived"):
        return "archived"
    here = current_agent_id()
    if here and (here in (mine.get("retired") or []) or here in (mine.get("archivedIds") or [])):
        # Same agent id, but this is a VM the ticket already left.
        return "replaced"
    if mine.get("state") in _LIVE:
        return ""
    reason = str(mine.get("endReason") or "ended")
    if reason in _REAP_GONE:
        return "replaced" if reason == "dead" else reason
    if mine.get("halted"):
        # Started before a pause or a stop. The run moved on without it.
        return "halted"
    if reason == "returned":
        _now_dt, now_s = beam_mod.coerce_now(now)
        age = _minutes_between(str(mine.get("ended") or ""), now_s)
        if age is not None and age > stale_minutes(data):
            return "not-resumed"
    return ""


def reap_lines(reason: str, ticket_id: str = "", role: str = "shuttle") -> list:
    """What the agent reads. One answer: continue, or exit and how."""
    if not reason:
        return ["reap: continue"]
    if role == "parent":
        tail = "End this turn. Do not start an agent. Do not subscribe, set a timer, or wait."
    elif role == "listener":
        tail = "Return `listen: stopped`. Do not read the channel. Do not start an agent, subscribe, set a timer, or sleep."
    else:
        tail = (
            "Return `result: %s stopped %s`. Do not start an agent, subscribe, set a timer, or wait."
            % (ticket_id or "<id>", reason)
        )
    return ["reap: exit %s" % reason, "reap: stop now. %s" % tail]


def reap(
    data: dict,
    agent_id: str = "",
    ticket_id: str = "",
    role: str = "shuttle",
    now=None,
    beam_path: Optional[Path] = None,
    record: bool = True,
) -> list:
    """The self-check every Warp agent runs first and at each checkpoint.

    On exit the agent's row is ended, so the registry shows who is gone.
    """
    reason = reap_reason(
        data,
        agent_id,
        ticket_id,
        role,
        now=now,
        beam_path=beam_path,
        strict_local=bool(record and beam_path is not None),
    )
    if reason and record and beam_path is not None and agent_id:
        doc = registry(data, beam_path)
        _now_dt, now_s = beam_mod.coerce_now(now)
        changed = False
        for row in _rows(doc):
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "") != str(agent_id) and str(row.get("cloudId") or "") != str(agent_id):
                continue
            if row.get("reaped"):
                continue
            row["reaped"] = now_s
            if row.get("state") in _LIVE:
                row["state"] = "ended"
                row["ended"] = now_s
                row["endReason"] = reason
            changed = True
        if changed:
            flush(data, beam_path)
            try:
                beam_mod.journal(Path(beam_path), {"type": "reap", "agent": agent_id, "ticket": ticket_id, "reason": reason})
            except OSError:
                pass
    return reap_lines(reason, ticket_id, role)


def bind_cloud(data: dict, agent_id: str, ticket_id: str, beam_path: Optional[Path], cloud: Optional[str] = None) -> bool:
    """Store a Shuttle's cloud agent id on its row, so a halt can cancel it by id.

    `cloud` is the id the Shuttle wrote in its ticket folder. Without it this
    reads the id of the VM running the command. A worktree Shuttle shares the
    parent's VM, so the parent's own id is never bound to a Shuttle.
    """
    found = current_agent_id() if cloud is None else str(cloud or "").strip()
    if not _cloud_id(found):
        return False
    if cloud is not None and found == current_agent_id():
        # The folder was written on this VM, so the id is the parent's own.
        return False
    doc = registry(data, beam_path)
    for row in _rows(doc):
        if isinstance(row, dict) and row.get("role") == "parent" and found in {row.get("id"), row.get("cloudId")}:
            return False
    target = None
    for row in _rows(doc):
        if not isinstance(row, dict) or str(row.get("id") or "") != str(agent_id):
            continue
        if ticket_id and str(row.get("ticket") or "") not in {"", str(ticket_id)}:
            continue
        target = row
    if target is None or target.get("cloudId") == found:
        return False
    if found in (target.get("retired") or []) or found in (target.get("archivedIds") or []):
        # A VM the ticket already left wrote again. It never becomes the live one.
        return False
    previous = str(target.get("cloudId") or "")
    if _cloud_id(previous) and previous not in (target.get("archivedIds") or []):
        # The ticket moved to a new VM. The one it left is retired on the next pass.
        left = target.setdefault("retired", [])
        if previous not in left:
            left.append(previous)
    target["cloudId"] = found
    flush(data, beam_path)
    return True


def remote_state(root: Path) -> tuple:
    """(beam, registry) from origin's base branch, for a Shuttle on its own VM. (None, None) on a miss."""
    import state_commit

    root = Path(root)
    base = "main"
    try:
        local = json.loads((root / ".warp" / "beam.json").read_text())
        base = state_commit._base_name(root, local.get("config") or {}) or "main"
    except (OSError, ValueError):
        try:
            base = state_commit._base_name(root, {}) or "main"
        except Exception:
            base = "main"
    try:
        subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin", base], capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return None, None

    def show(rel: str):
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "show", "origin/%s:%s" % (base, rel)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        try:
            loaded = json.loads(out.stdout)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None

    beam = show(".warp/beam.json")
    if beam is None:
        return None, None
    return beam, show(".warp/agents.json") or _empty()


def reap_from(
    root: Path,
    agent_id: str = "",
    ticket_id: str = "",
    role: str = "shuttle",
    remote: bool = False,
    beam_path: Optional[Path] = None,
) -> list:
    """Reap check from a checkout. `remote` reads origin's base branch and writes nothing."""
    if remote:
        beam, doc = remote_state(Path(root))
        if beam is None:
            return ["reap: continue (no beam on origin)"]
        beam.pop("_agentBeam", None)
        beam.pop("_agentSig", None)
        beam["agentRegistry"] = doc
        return reap(beam, agent_id, ticket_id, role, beam_path=None, record=False)
    path = Path(beam_path) if beam_path is not None else Path(root) / ".warp" / "beam.json"
    if not path.is_file():
        return ["reap: continue (no beam at %s)" % path]
    try:
        data = beam_mod.load_json(path)
    except (OSError, ValueError):
        return ["reap: continue (beam unreadable)"]
    return reap(data, agent_id, ticket_id, role, beam_path=path)


SPAWN_FILE = "spawned.json"


def checkout_root(path: Path) -> Path:
    """The main checkout for a path that may be a git worktree of it."""
    path = Path(path)
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return path
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return path
    common_path = Path(common)
    if common_path.name == ".git":
        return common_path.parent
    return path


def hook_root(payload: dict) -> Optional[Path]:
    """The checkout that owns the live beam, from a hook payload."""
    candidates = []
    roots = payload.get("workspace_roots") if isinstance(payload, dict) else None
    if isinstance(roots, list):
        candidates.extend(str(item) for item in roots if item)
    for value in (os.environ.get("CURSOR_PROJECT_DIR", ""), os.getcwd()):
        if value:
            candidates.append(value)
    for raw in candidates:
        base = checkout_root(Path(raw).expanduser())
        if (base / ".warp" / "beam.json").is_file():
            return base
    return None


def _spawn_marks(beam_path: Path, update: Optional[Callable] = None) -> dict:
    """Read, and optionally change, the spawn marks under a file lock."""
    import fcntl

    path = Path(beam_path).parent / SPAWN_FILE
    lock_path = Path(beam_path).parent / ".spawned.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            marks = {}
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text())
                    if isinstance(loaded, dict):
                        marks = loaded
                except (OSError, ValueError):
                    marks = {}
            if update is not None and update(marks):
                beam_mod.atomic_write(path, json.dumps(marks, indent=2) + "\n")
            return marks
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _row_key(row: dict) -> str:
    return "%s|%s|%s" % (row.get("id") or "", row.get("started") or "", row.get("resumedAt") or "")


def _deny(message: str) -> dict:
    return {"permission": "deny", "user_message": message}


def hook_start(payload: dict, root: Optional[Path] = None, now=None) -> dict:
    """subagentStart. Deny a Warp launch the gate did not issue. Everything else is allowed.

    A launch is a prompt Warp printed: SUBAGENT <id>, IMPLEMENT <id>, or
    LISTEN once. It is denied while the run is paused, stopped, or finished,
    when the ticket is merged or parked, when `checkout.py launch` did not
    record a row for it, and when that row already started a different
    subagent. WARP_SPAWN_GATE=off allows everything.
    """
    allow = {"permission": "allow"}
    if os.environ.get("WARP_SPAWN_GATE", "").strip().casefold() in {"off", "0", "false", "no"}:
        return allow
    if not isinstance(payload, dict):
        return allow
    role, tid = launch_marker(str(payload.get("task") or ""))
    if not role:
        return allow
    base = Path(root) if root is not None else hook_root(payload)
    if base is None:
        return allow
    beam_path = base / ".warp" / "beam.json"
    try:
        data = beam_mod.load_json(beam_path)
    except (OSError, ValueError):
        return allow
    if not isinstance(data, dict):
        return allow
    halted = halt_state(data)
    if halted:
        word = "finished" if halted == "done" else halted
        return _spawn_denied(beam_path, role, tid, halted, "Warp is %s. No Warp agent may start. /warp-resume or /warp-start opens the run." % word)
    doc = load_file(beam_path)
    call = str(payload.get("tool_call_id") or "")
    sub = str(payload.get("subagent_id") or "")
    _now_dt, now_s = beam_mod.coerce_now(now)
    if role == "listener":
        raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
        if raw.get("state") != "running" or not raw.get("pollStartedAt"):
            return _spawn_denied(beam_path, role, tid, "not-due", "No listener poll is due. Start the listener only on `listener: poll`.")
        rows = [row for row in _rows(doc) if isinstance(row, dict) and row.get("role") == "listener" and row.get("state") in _LIVE]
    else:
        tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
        ticket = tickets.get(tid)
        if not isinstance(ticket, dict):
            return allow
        if ticket.get("status") in _SETTLED:
            return _spawn_denied(beam_path, role, tid, "settled", "Ticket %s is %s. Nothing starts for it." % (tid, ticket.get("status")))
        rows = live_rows(doc, tid, "shuttle")
        if not rows:
            return _spawn_denied(
                beam_path,
                role,
                tid,
                "no-launch",
                "No launch was issued for %s. Only the parent starts a Shuttle, and only after `checkout.py launch --id %s`." % (tid, tid),
            )
    if not rows:
        return allow
    key = _row_key(rows[-1])
    verdict = {}

    def update(marks: dict) -> bool:
        seen = marks.get(key)
        if isinstance(seen, dict):
            same = (call and call == seen.get("call")) or (sub and sub == seen.get("agent"))
            known = bool(seen.get("call") or seen.get("agent"))
            if same or not known or not (call or sub):
                return False
            verdict["deny"] = True
            return False
        marks[key] = {"call": call, "agent": sub, "at": now_s, "ticket": tid, "role": role}
        for old in [name for name in marks if name != key][:-200]:
            marks.pop(old, None)
        return True

    _spawn_marks(beam_path, update)
    if verdict.get("deny"):
        what = "The listener poll" if role == "listener" else "Ticket %s" % tid
        return _spawn_denied(beam_path, role, tid, "duplicate", "%s already has its agent. Do not start a second one." % what)
    return allow


def _spawn_denied(beam_path: Path, role: str, tid: str, reason: str, message: str) -> dict:
    try:
        beam_mod.journal(Path(beam_path), {"type": "spawn-denied", "role": role, "ticket": tid, "reason": reason})
    except OSError:
        pass
    return _deny(message)


def hook_stop(payload: dict, root: Optional[Path] = None) -> dict:
    """subagentStop. Journal which Warp agent ended. Never asks for a follow-up."""
    if not isinstance(payload, dict):
        return {}
    role, tid = launch_marker(str(payload.get("task") or ""))
    if not role:
        return {}
    base = Path(root) if root is not None else hook_root(payload)
    if base is None:
        return {}
    try:
        beam_mod.journal(
            base / ".warp" / "beam.json",
            {
                "type": "subagent-end",
                "role": role,
                "ticket": tid,
                "status": str(payload.get("status") or ""),
                "minutes": int((payload.get("duration_ms") or 0) / 60000) if isinstance(payload.get("duration_ms"), (int, float)) else None,
            },
        )
    except OSError:
        pass
    return {}


def check_line(data: dict) -> str:
    if spawns_open(data):
        return "spawn: open"
    state = data.get("runState") or ("paused" if data.get("paused") else "running")
    if data.get("paused") or state in {"paused", "stopped"}:
        return "spawn: closed %s" % ("paused" if data.get("paused") or state == "paused" else "stopped")
    return "spawn: closed done"


SELECTION_HELP = """\
/warp-list and /warp-cleanup take the same selection:

  (none)            This run: the registry, and cloud agents in this repo
                    tagged [warp:<instance>].
  --tag <instance>  Another run's agents in place of this one's. a1b2c3,
                    warp:a1b2c3, and [warp:a1b2c3] are the same.
  --tag all         Agents tagged for any Warp run. --tag '*' is the same.
                    Quote the star.
  --untagged        Also agents with no Warp tag, from before 1.5.0, matched
                    loosely on Warp role words in the name or prompt
                    (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot).
                    This can match someone else's agent.
  --all-idle        Every idle agent in this repo, tagged or not.
  --any-repo        Do not limit to this repo.

Other agents in the repo are not matched, whatever words are in their names,
and neither are another Warp run's. The agent running the command
(reason=self) and this run's parent (reason=parent) are never touched.
The cloud agents need CURSOR_API_KEY in the environment. It is never written.

?, help, -h, and --help print this text and do nothing else. Quote ? if the
shell expands it.
"""

LIST_HELP = (
    """\
/warp-list: what this Warp run still has out, running or idle. It changes
nothing.

It prints:
  instance: warp:<instance> host=<host> machine=<machine id>
  agent: ...                 one line per row in .warp/agents.json
  registry: live=<n> ended=<n>
  cloud: id= name= repo= status= updated= <link>
                             one line per matching cloud agent. status=ACTIVE
                             is still running. status=IDLE is not.
  list: kept <id> reason=parent|self|running-untagged|status
  out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>

Without CURSOR_API_KEY it prints the registry, `cloud: not read.`, and a
link for each recorded cloud agent.

"""
    + SELECTION_HELP
    + """
examples:
  /warp-list
  /warp-list --tag a1b2c3
  /warp-list --tag all
  /warp-list --untagged
  python3 scripts/agents.py list --beam .warp/beam.json
  python3 scripts/agents.py list --beam .warp/beam.json --tag all --untagged
"""
)

CLEANUP_HELP = (
    """\
/warp-cleanup: cancel and archive what this Warp run still has out.
/warp-list shows the same agents first and changes nothing.

The slash command runs:
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running

For each matching cloud agent:
  status=ACTIVE or RUNNING   its run is cancelled, then it is archived
                             (cleanup: cancelled <id>, cleanup: archived <id>)
  status=IDLE                it is archived
Archive is reversible in the Cursor UI. Warp never deletes an agent.

While this run is running, the idle ones are archived and the running ones
are left: `cleanup: --running refused while the run is running`. /warp-pause
or /warp-stop first, or pass --force. An agent that only matched --untagged
and is still running is left (reason=running-untagged) unless --force,
because it may be someone else's.

  --cloud     Read the account's cloud agents. Needed for anything below.
  --apply     Act. Without it this is a dry run: each match, `cleanup: would
              cancel <id>` for the running ones, and `cleanup: count`.
  --running   Also cancel the run of a RUNNING or ACTIVE match before the
              archive. Without it a running agent is skipped.
  --force     Cancel running agents while this run is running, and cancel a
              running agent that only matched loosely.

"""
    + SELECTION_HELP
    + """
examples:
  /warp-cleanup
  /warp-cleanup --tag a1b2c3
  /warp-cleanup --tag all
  /warp-cleanup --untagged
  /warp-cleanup --force
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud                      (dry run)
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
  python3 scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --untagged
"""
)


def _read_hook_input() -> dict:
    import sys

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        loaded = json.loads(raw or "{}")
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def main(argv: Optional[list] = None) -> int:
    import usage

    parser = argparse.ArgumentParser(
        description="Warp agent registry, reap check, and cleanup",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    texts = {"list": LIST_HELP, "cleanup": CLEANUP_HELP}
    for name in ("list", "check", "cleanup", "stop", "reap"):
        if name in texts:
            # The whole text, not only the flags: `/warp-list ?` and
            # `/warp-cleanup ?` print the same help the command files carry.
            cmd = sub.add_parser(name, description=texts[name], formatter_class=argparse.RawDescriptionHelpFormatter)
        else:
            cmd = sub.add_parser(name)
        cmd.add_argument("--beam", default=".warp/beam.json")
        if name in {"list", "cleanup"}:
            cmd.add_argument("--tag", default="", help="Another run's tag in place of this beam's: a1b2c3, warp:a1b2c3, or all for the tag of any Warp run.")
            cmd.add_argument("--untagged", action="store_true", help="Also match agents with no Warp tag, from before 1.5.0, loosely, on Warp role words in the name or prompt.")
            cmd.add_argument("--all-idle", action="store_true", help="Drop the match. Every idle agent in this repo, unless --any-repo.")
            cmd.add_argument("--any-repo", action="store_true", help="Include other repositories.")
        if name == "list":
            cmd.add_argument("--cloud", action="store_true", help="Accepted and not needed. The cloud agents are listed whenever CURSOR_API_KEY is set.")
        if name == "cleanup":
            cmd.add_argument("--cloud", action="store_true", help="Read GET /v1/agents. Without --apply this is a dry run.")
            cmd.add_argument("--apply", action="store_true", help="Archive the dry-run matches.")
            cmd.add_argument("--running", action="store_true", help="Also cancel the run of a RUNNING or ACTIVE match, then archive it. Pause or stop first.")
            cmd.add_argument("--force", action="store_true", help="Allow --running while this run is running, and cancel a running agent that only matched loosely.")
        if name == "reap":
            cmd.add_argument("--id", default="", help="This agent's id from the prompt line `agent:`.")
            cmd.add_argument("--ticket", default="", help="The ticket this Shuttle works on.")
            cmd.add_argument("--role", default="shuttle", choices=["shuttle", "listener", "parent"])
            cmd.add_argument("--root", default="", help="Checkout to read. Default is the beam's checkout.")
            cmd.add_argument("--remote", action="store_true", help="Read the beam from origin's base branch. For a Shuttle on its own VM.")
    sub.add_parser("hook-start", help="subagentStart hook. Reads the hook JSON on stdin.")
    sub.add_parser("hook-stop", help="subagentStop hook. Reads the hook JSON on stdin.")
    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "hook-start":
        try:
            verdict = hook_start(_read_hook_input())
        except Exception:
            verdict = {"permission": "allow"}
        print(json.dumps(verdict))
        return 0
    if args.cmd == "hook-stop":
        try:
            hook_stop(_read_hook_input())
        except Exception:
            pass
        print("{}")
        return 0
    path = Path(args.beam)
    if args.cmd == "reap":
        root = Path(args.root) if args.root else path.resolve().parent.parent
        lines = reap_from(root, args.id, args.ticket, args.role, remote=bool(args.remote), beam_path=None if args.remote else path)
        for line in lines:
            print(line)
        return 3 if lines and lines[0].startswith("reap: exit") else 0
    if not path.is_file():
        print("agents: no beam at %s" % path)
        return 2
    data = beam_mod.load_json(path)
    if args.cmd == "check":
        print(check_line(data))
        return 0
    if args.cmd == "list":
        for line in out_lines(
            data,
            beam_path=path,
            tag_override=args.tag,
            untagged=args.untagged,
            all_idle=args.all_idle,
            any_repo=args.any_repo,
        ):
            print(line)
        return 0
    if args.cmd == "stop":
        for line in halt_local(data, beam_path=path, reason="stopped"):
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
        running=args.running,
        force=args.force,
        untagged=args.untagged,
        tag_override=args.tag,
    ):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
