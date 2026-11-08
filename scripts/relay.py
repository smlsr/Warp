#!/usr/bin/env python3
"""Send a fact or an instruction to the owner of a pull request, a coordinator, or any agent below one (1.7.15).

Warp records the parent of every agent it starts (`parent` on the agents.json row) and the sole pusher of every pull
request (`prOwners` on the beam), so a message can reach a grandchild: Warp -> coordinator -> worker.

Order of delivery:
  1. a cloud agent id with a reachable API key: POST /v1/agents/{id}/runs {"prompt": {"text": ...}} (a follow-up run;
     1.7.15 called /followup, which v1 does not have, so every message fell back to the outbox). A 409 (a run is still
     going) is queued and sent again on the next tick (`retry_pending`).
  2. its parent coordinator, the same way, with "pass this to <agent>"
  3. the coordinator's outbox file `.warp/outbox/<owner>.md`, and a `relay: MUST DO` line the parent acts on

The same text to the same owner is sent once (say_once).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

AGENT_URL = "https://cursor.com/agents?id=%s"


def key_for(row: Optional[dict], env: Optional[dict] = None) -> str:
    """The API key that created the agent: `keyEnv` on its registry row names the variable; else CURSOR_API_KEY."""
    env = env if env is not None else os.environ
    name = str((row or {}).get("keyEnv") or "")
    if name and env.get(name):
        return str(env[name])
    return str(env.get("CURSOR_API_KEY") or "")


def followup_status(agent_id: str, key: str, text: str, transport: Optional[Callable] = None) -> int:
    """HTTP status of POST /v1/agents/{id}/runs (a follow-up run). 0 when it could not be sent."""
    import agents

    if not agent_id or not key or not agents._cloud_id(str(agent_id)):
        return 0  # a local subagent id or an owner name: no API to send it through
    call = transport or agents.default_transport
    try:
        status, _body = call("POST", "%s/v1/agents/%s/runs" % (agents.API_ROOT, agent_id), key, {"prompt": {"text": text}})
    except Exception:
        return 0
    return int(status or 0)


def followup(agent_id: str, key: str, text: str, transport: Optional[Callable] = None) -> bool:
    return 200 <= followup_status(agent_id, key, text, transport) < 300


QUEUE = "relay-queue.json"


def _queue(beam_path: Path) -> list:
    try:
        rows = json.loads((Path(beam_path).parent / QUEUE).read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save_queue(beam_path: Path, rows: list) -> None:
    (Path(beam_path).parent / QUEUE).write_text(json.dumps(rows[-500:], indent=2) + "\n")


def retry_pending(beam_path: Path, transport: Optional[Callable] = None, env: Optional[dict] = None) -> list:
    """Messages an agent could not take because a run was still going (409): sent again, up to 12 ticks (an hour)."""
    rows = _queue(beam_path)
    if not rows:
        return []
    keep, out = [], []
    for row in rows:
        status = followup_status(row["agent"], key_for(_registry_row(beam_path, row["agent"]), env), row["text"], transport)
        if 200 <= status < 300:
            out.append("relay: sent to %s (%s) after its run finished: %s" % (row.get("owner"), AGENT_URL % row["agent"], row["text"][:200]))
        elif int(row.get("tries") or 0) + 1 < 12:
            keep.append(dict(row, tries=int(row.get("tries") or 0) + 1))
        else:
            out.append("relay: %s still busy after an hour; written to its outbox instead" % row["agent"])
            _outbox(beam_path, row.get("owner") or row["agent"], row["text"], "")
    _save_queue(beam_path, keep)
    return out


def _outbox(beam_path: Path, owner: str, text: str, now_s: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in owner)[:80] or "owner"
    path = Path(beam_path).parent / "outbox" / ("%s.md" % safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write("- %s %s\n" % (now_s, text.replace("\n", " ")))
    return path


def send(beam_path: Path, data: dict, owner: str, text: str, agent_id: str = "", parent: str = "",
         transport: Optional[Callable] = None, env: Optional[dict] = None, key: str = "") -> list:
    """Deliver `text` to `owner`. Lines say how. Never raises."""
    import beam as beam_mod
    import selfdrive

    now_s = beam_mod.utcnow()
    owner = str(owner or agent_id or "unowned")
    if not agent_id:
        agent_id = coordinator_agent(beam_path, owner)
    said = selfdrive.say_once(data, "relay:%s:%s" % (owner, agent_id), text)
    if not said:
        return []
    row = _registry_row(beam_path, agent_id)
    api_key = key or key_for(row, env)
    status = followup_status(agent_id, api_key, text, transport) if agent_id else 0
    if 200 <= status < 300:
        selfdrive.journal(beam_path, "relay", owner=owner, agent=agent_id, how="api", text=text[:300])
        return ["relay: sent to %s (%s): %s" % (owner, AGENT_URL % agent_id, text)]
    if status == 409:
        # A run is still going on that agent: send it when that run ends.
        rows = _queue(beam_path)
        rows.append({"agent": agent_id, "owner": owner, "text": text, "tries": 0, "at": now_s})
        _save_queue(beam_path, rows)
        return ["relay: %s is mid-run; the message to %s is queued and sent when that run ends" % (agent_id, owner)]
    parent = parent or str((row or {}).get("parent") or "") or coordinator_agent(beam_path, owner)
    parent_row = _registry_row(beam_path, parent)
    if parent and parent != agent_id and followup(parent, key_for(parent_row, env), "Pass this to %s: %s" % (
            agent_id or owner, text), transport):
        selfdrive.journal(beam_path, "relay", owner=owner, agent=agent_id, via=parent, how="api", text=text[:300])
        return ["relay: sent to %s through its parent %s: %s" % (owner, parent, text)]
    path = _outbox(beam_path, owner, text, now_s)
    selfdrive.journal(beam_path, "relay", owner=owner, agent=agent_id, how="outbox", text=text[:300])
    target = (AGENT_URL % agent_id) if agent_id else owner
    return ["relay: MUST DO tell %s: %s (also written to %s)" % (target, text, path.relative_to(Path(beam_path).parent.parent)
                                                                 if path.is_relative_to(Path(beam_path).parent.parent) else path)]


def coordinator_agent(beam_path: Path, name: str) -> str:
    """The cloud agent id of a coordinator Warp started (`.warp/coordinators.json`), so a message to `coord-ui` goes
    straight to it."""
    try:
        doc = json.loads((Path(beam_path).parent / "coordinators.json").read_text())
    except (OSError, ValueError):
        return ""
    info = ((doc.get("coordinators") or {}).get(str(name or "")) or {}) if isinstance(doc, dict) else {}
    return str(info.get("agentId") or "")


def _registry_row(beam_path: Path, agent_id: str) -> Optional[dict]:
    if not agent_id:
        return None
    try:
        doc = json.loads((Path(beam_path).parent / "agents.json").read_text())
    except (OSError, ValueError):
        return None
    rows = doc.get("agents") if isinstance(doc, dict) else doc
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and agent_id in {str(row.get("id") or ""), str(row.get("cloudId") or ""),
                                                  str(row.get("agentId") or "")}:
            return row
    return None


def owner_of(data: dict, pr_key: str) -> dict:
    """The recorded sole pusher of a pull request: {owner, agent, coordinator, since}."""
    owners = data.get("prOwners") if isinstance(data.get("prOwners"), dict) else {}
    return dict(owners.get(str(pr_key)) or {})


def set_owner(data: dict, pr_key: str, owner: str, agent: str = "", coordinator: str = "", now_s: str = "") -> None:
    owners = data.setdefault("prOwners", {})
    row = owners.get(str(pr_key)) if isinstance(owners.get(str(pr_key)), dict) else {}
    if row.get("owner") != owner or row.get("agent") != agent:
        row = {"owner": owner, "agent": agent, "coordinator": coordinator or row.get("coordinator") or "",
               "since": now_s, "previous": row.get("owner")}
    owners[str(pr_key)] = row
