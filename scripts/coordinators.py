#!/usr/bin/env python3
"""Start, message and stop coordinators and replacement workers through the Cloud Agents API (1.7.16).

1.7.15 printed `coordinator: MUST DO start …` and left it to the parent. Warp already holds CURSOR_API_KEY (it
cancels and archives agents with it), so it now does it itself:

  start    POST /v1/agents {name, prompt: {text}, repos: [{url, startingRef}], model?, autoCreatePR: false}
           with the coordinator brief (agents/coordinator.md) filled in for the area
  message  POST /v1/agents/{id}/runs {prompt: {text}}          (relay.followup)
  stop     POST /v1/agents/{id}/runs/{runId}/cancel, then POST /v1/agents/{id}/archive

A worker for an orphaned or stalled pull request that no coordinator can take is started the same way on that pull
request (`repos[0].prUrl`), told to fetch the remote head first. Every agent started here gets a registry row with
`parent` and `role`, so the sweep can join it and relay can reach it. Without a key, the old MUST DO line is printed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

BRIEF = Path(__file__).resolve().parent.parent / "agents" / "coordinator.md"


def https_url(remote: str) -> str:
    text = str(remote or "").strip()
    match = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", text)
    if match:
        return "https://%s/%s" % (match.group(1), match.group(2))
    return re.sub(r"\.git$", "", text)


def brief(name: str, area: str, repo: str, owned: list, max_agents: int, beam_path: Path) -> str:
    try:
        text = BRIEF.read_text()
    except OSError:
        text = "You are {name}, the Warp coordinator for area {area} in {repo}. You own: {owned}."
    body = text.split("---", 2)[-1] if text.startswith("---") else text
    return body.format(name=name, area=area or "general", repo=repo, owned=", ".join(owned) or "nothing yet (Warp assigns it)",
                       max_agents=max_agents, rules=RULES, waivers=".warp/waivers.json", beam=".warp/beam.json").strip()


RULES = ".warp/rules.md"
RULES_TEXT = """# Warp shared rules

Every coordinator and worker on this repo follows these. Warp's tick, sweep and audit enforce them.

1. One sole pusher per pull request (`prOwners` on the beam). Push only for a real conflict, a real failure, or a needed
   main merge. Never rerun old CI. Never force-push, except an approved one-off in `.warp/exceptions.json`.
2. One round, one push: wait for every check and Bugbot on the head, fix all of them, push once.
3. Ready means `READY <pr> <sha> waive=<tests|none> bugbot=pass` (or `GAPPR <pr> <sha> gap=<id>`) in your outbox.
   Warp verifies it over REST and merges in blocker order.
4. A waived main-red test (`.warp/waivers.json`) is not yours unless you own its gap.
5. Handoff: the old owner writes `HANDED-OFF <new owner> <head>`; the new owner pushes only after it sees that mark.
6. Ask a person only for product or permission-model decisions, data deletion, public API removal, budget, or a merge
   into a protected branch outside policy. Everything else has a next step; take it.
"""


def ensure_rules(beam_path: Path) -> None:
    path = Path(beam_path).parent / "rules.md"
    if not path.is_file():
        path.write_text(RULES_TEXT)


def _agent_id(body) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("id", "agentId"):
        if body.get(key):
            return str(body[key])
    inner = body.get("agent") if isinstance(body.get("agent"), dict) else {}
    return str(inner.get("id") or "")


def create(key: str, payload: dict, transport: Optional[Callable] = None) -> tuple:
    """(agent id, detail). Empty id when the API refused or could not be reached."""
    import agents

    call = transport or agents.default_transport
    try:
        status, body = call("POST", "%s/v1/agents" % agents.API_ROOT, key, payload)
    except Exception as exc:
        return "", str(exc)
    if 200 <= int(status or 0) < 300 and _agent_id(body):
        return _agent_id(body), ""
    return "", "create refused (%s): %s" % (status, json.dumps(body)[:200] if body else "")


def _register(beam_path: Path, row: dict) -> None:
    path = Path(beam_path).parent / "agents.json"
    try:
        doc = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("agents", []).append(row)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _model(data: dict) -> Optional[dict]:
    cfg = data.get("config") or {}
    slug = str(cfg.get("coordinatorModel") or "").strip()
    return {"id": slug} if slug else None


def start(beam_path: Path, data: dict, name: str, area: str, owned: list, key: str, root: Path,
          transport: Optional[Callable] = None, now_s: str = "") -> tuple:
    """(agent id, lines). Starts the coordinator through the API and records it."""
    import agents

    repo = https_url(agents.origin_url(root))
    base = str((data.get("config") or {}).get("baseBranch") or "main")
    max_agents = int((data.get("config") or {}).get("maxAgents") or 18)
    if not key:
        return "", ["coordinator: MUST DO start %s for area %s: no CURSOR_API_KEY here, so Warp cannot start it. Start a "
                    "cloud agent on %s with the brief in agents/coordinator.md." % (name, area, repo or "this repo")]
    ensure_rules(beam_path)
    payload = {"name": "warp %s" % name, "prompt": {"text": brief(name, area, repo, owned, max_agents, beam_path)},
               "repos": [{"url": repo, "startingRef": base}], "autoCreatePR": False}
    model = _model(data)
    if model:
        payload["model"] = model
    aid, why = create(key, payload, transport)
    if not aid:
        return "", ["coordinator: %s not started: %s. Retried next audit." % (name, why)]
    _register(beam_path, {"id": aid, "cloudId": aid, "role": "coordinator", "name": name, "area": area, "parent": "warp",
                          "started": now_s, "state": "running"})
    return aid, ["coordinator: started %s (%s) for area %s: %s" % (name, aid, area, agents_url(aid))]


def worker(beam_path: Path, data: dict, pr_url: str, text: str, key: str, root: Path, parent: str = "warp",
           transport: Optional[Callable] = None, now_s: str = "") -> tuple:
    """(agent id, lines). A replacement worker on one pull request: it fetches the remote head first."""
    import agents

    repo = https_url(agents.origin_url(root))
    if not key:
        return "", []
    payload = {"name": "warp restaff %s" % pr_url.rsplit("/", 1)[-1],
               "prompt": {"text": "%s\n\nFetch the remote head of %s first and build on it. You are its sole pusher now. "
                                  "Fix every open check and finding together and push once, then write `READY <pr> <sha>` "
                                  "to .warp/outbox/%s.md." % (text, pr_url, parent)},
               "repos": [{"url": repo, "prUrl": pr_url}], "autoCreatePR": False}
    model = _model(data)
    if model:
        payload["model"] = model
    aid, why = create(key, payload, transport)
    if not aid:
        return "", ["restaff: %s not started: %s" % (pr_url, why)]
    _register(beam_path, {"id": aid, "cloudId": aid, "role": "worker", "pr": pr_url, "parent": parent, "started": now_s,
                          "state": "running"})
    return aid, ["restaff: started %s on %s (%s)" % (aid, pr_url, agents_url(aid))]


def stop(agent_id: str, key: str, transport: Optional[Callable] = None) -> tuple:
    import agents

    if not agent_id or not key:
        return False, "no key"
    ok, detail = agents.cancel_run(agent_id, key, transport=transport)
    archived, _ = agents.archive_id(agent_id, key, transport=transport)
    return ok or archived, str(detail)


def agents_url(agent_id: str) -> str:
    return "https://cursor.com/agents?id=%s" % agent_id
