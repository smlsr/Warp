#!/usr/bin/env python3
"""A launch that hits the agent cap is queued and retried, never parked (1.7.15).

"Launch hit agent cap 20" (Cursor's limit on running cloud agents) used to come back as a failed Shuttle: it counted as
a recovery and, after maxRecoveries, parked the ticket. It is a wait. The ticket goes back to `queued` with
`capRetries` counted, and the next tick's refill starts it when a slot frees. The concurrency Warp itself asks for is
per coordinator (`maxAgents`), under the run-wide hard ceiling `agents.ceiling` (default 120).
"""

from __future__ import annotations

import re

_CAP = re.compile(r"(agent cap|hit (the )?agent (cap|limit)|too many (running|active|concurrent) (cloud )?agents|"
                  r"concurrent agent limit|limit of \d+ (running |active |concurrent )?agents|max(imum)? (number of )?"
                  r"(running |active |concurrent )?agents|agents? limit (reached|exceeded))", re.I)


def is_cap_error(text: str) -> bool:
    return bool(_CAP.search(str(text or "")))


def requeue(data: dict, ticket: dict, text: str) -> None:
    import beam as beam_mod

    now_s = beam_mod.utcnow()
    ticket["status"] = "queued"
    ticket.pop("phase", None)
    ticket["alarm"] = None
    ticket["agent"] = None
    ticket["capRetries"] = int(ticket.get("capRetries") or 0) + 1
    ticket["capHitAt"] = now_s
    ticket["capText"] = str(text or "")[:200]
    shuttle = ticket.get("shuttle")
    if isinstance(shuttle, dict):
        for key in ("pending", "step", "startedAt", "launching", "launchLines"):
            shuttle.pop(key, None)
    data.setdefault("capHits", []).append(now_s)
    data["capHits"] = data["capHits"][-50:]


def ceiling(data: dict, beam_path=None) -> int:
    import selfdrive

    return int(selfdrive.num(selfdrive.config(data, beam_path), "agents", "ceiling"))
