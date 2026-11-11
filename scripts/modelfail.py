#!/usr/bin/env python3
"""The backup model (1.7.22): a Shuttle that fails because of its model starts again on `backupModel`.

A start or a turn can fail for reasons that have nothing to do with the ticket: the model is on a trial that ran out,
the vendor is down or overloaded, the account hit its usage or rate limit, or the model was renamed or withdrawn.
Before 1.7.22 that came back as a failed Shuttle: a recovery was counted, the same model was tried again, and after
`maxRecoveries` the ticket parked.

Now, when a result line (`checkout.py result`), a Shuttle's `failed` state (with `--error`), or Cursor's hook says the
model failed:
  - the model is marked down for the whole run for `backupModelMinutes` (default 60): every Shuttle that would start on
    it starts on `backupModel` instead (default `grok-4.7-high`), with `model: <backup> (backupModel: <model> failed:
    <why>)` on its start line
  - this ticket starts again at once on the backup model, at the same step; no recovery is counted and it never parks
  - after the window the first Shuttle tries the primary model again; a new failure opens a new window
  - when the backup model fails too, the ticket goes to alarm `model-failure` and a person is asked: there is nothing
    left to fall back to
`backupModel: none` turns the fallback off (a model failure is then an ordinary failure).
"""

from __future__ import annotations

import re
from typing import Optional

DEFAULT_BACKUP = "grok-4.7-high"
DEFAULT_MINUTES = 60
_OFF = {"none", "off", "false", "no", "0", "-"}

# A failure is a model failure when it names the model or its vendor and says why the model could not serve, or uses
# a phrase only a model or vendor failure uses. Test failures, CI and code errors do not match.
_SUBJECT = r"(model|provider|vendor|upstream|anthropic|openai|xai|x\.ai|grok|claude|gpt|gemini|llm|inference)"
_WHY = (r"(not available|unavailable|is down|went down|outage|overloaded|over capacity|capacity|rate[- ]?limit|"
        r"too many requests|quota|usage limit|spend(ing)? limit|credit|billing|payment|trial|subscription|plan does not|"
        r"not included|not enabled|not allowed|no access|access denied|forbidden|unauthori[sz]ed|not supported|"
        r"unsupported|deprecated|retired|withdrawn|not found|does not exist|unknown|invalid|5\d\d|429|529|timed? ?out|"
        r"timeout|internal error|server error|bad gateway|service unavailable|try again later|temporarily)")
_NEAR = re.compile(r"%s[^\n]{0,80}?%s|%s[^\n]{0,80}?%s" % (_SUBJECT, _WHY, _WHY, _SUBJECT), re.I)
_PHRASES = re.compile(r"(model_not_found|invalid_model|unknown model|model is not available|model not available|"
                      r"overloaded_error|insufficient_quota|rate_limit_exceeded|trial (has )?(ended|expired)|"
                      r"free trial|upgrade (your )?plan|usage[- ]based pricing|out of (fast )?requests|"
                      r"you've hit your (usage )?limit|switch (to )?(another|a different) model)", re.I)


def is_model_error(text: str) -> bool:
    text = str(text or "")
    if not text.strip():
        return False
    try:
        import agentcap

        if agentcap.is_cap_error(text):
            return False  # the agent cap is a wait, not the model (agentcap.py)
    except Exception:
        pass
    return bool(_PHRASES.search(text) or _NEAR.search(text))


def backup(config: Optional[dict]) -> str:
    raw = (config or {}).get("backupModel")
    text = str(raw if raw is not None else DEFAULT_BACKUP).strip().strip('"').strip("'")
    return "" if text.casefold() in _OFF else text


def window_minutes(config: Optional[dict]) -> float:
    try:
        return max(1.0, float((config or {}).get("backupModelMinutes") or DEFAULT_MINUTES))
    except (TypeError, ValueError):
        return float(DEFAULT_MINUTES)


def _live(mark, now_dt) -> bool:
    import beam as beam_mod

    if not isinstance(mark, dict):
        return False
    until = beam_mod.parse_ts(str(mark.get("until") or ""))
    return until is not None and until > now_dt


def fallback(ticket: dict, config: Optional[dict], primary: str, down: Optional[dict] = None, now=None) -> Optional[tuple]:
    """(backup, why) when `primary` is marked down for this ticket or the run, else None."""
    import beam as beam_mod

    spare = backup(config)
    if not spare or not primary or spare == primary:
        return None
    now_dt, _ = beam_mod.coerce_now(now)
    mark = ticket.get("modelFallback") if isinstance(ticket.get("modelFallback"), dict) else None
    if mark and str(mark.get("from") or "") == primary and _live(mark, now_dt):
        return spare, "backupModel: %s failed: %s" % (primary, mark.get("reason") or "model failure")
    run_mark = (down or {}).get(primary) if isinstance(down, dict) else None
    if _live(run_mark, now_dt):
        return spare, "backupModel: %s is down for this run: %s" % (primary, run_mark.get("reason") or "model failure")
    return None


def _model_of(data: dict, ticket: dict) -> str:
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    if shuttle.get("model"):
        return str(shuttle["model"])
    try:
        import orchestrator

        return orchestrator.model_for(ticket, data.get("config") or {})[0]
    except Exception:
        return ""


def note(data: dict, ticket: dict, text: str, model: str = "", now=None) -> list:
    """A model failure on this ticket. Marks the model down, points the ticket at the backup, and says what happens."""
    import beam as beam_mod

    now_dt, now_s = beam_mod.coerce_now(now)
    import datetime as dt

    cfg = data.get("config") or {}
    model = str(model or _model_of(data, ticket) or "").strip()
    spare = backup(cfg)
    tid = ticket.get("id")
    reason = re.sub(r"\s+", " ", str(text or "")).strip()[:160] or "model failure"
    log = ticket.get("modelFailures") if isinstance(ticket.get("modelFailures"), list) else []
    log.append({"at": now_s, "model": model, "reason": reason})
    ticket["modelFailures"] = log[-20:]
    if not spare:
        return ["model: %s failed for %s (%s); backupModel is off, so this is an ordinary failure" % (model or "?", tid, reason)]
    if model and model == spare:
        ticket["status"] = "alarm"
        ticket["alarm"] = "model-failure"
        return [
            "model: backup %s failed too for %s (%s)" % (spare, tid, reason),
            "herald: ALARM %s: the backup model %s failed as well (%s). Nothing left to fall back to: set `model` or "
            "`backupModel` in .warp/config.yaml to a model that works, then `warp:retry %s`." % (tid, spare, reason, tid),
        ]
    until = (now_dt + dt.timedelta(minutes=window_minutes(cfg))).strftime("%Y-%m-%dT%H:%M:%SZ")
    down = data.get("modelDown") if isinstance(data.get("modelDown"), dict) else {}
    prior = down.get(model) if isinstance(down.get(model), dict) else {}
    down[model or "?"] = {"since": prior.get("since") if _live(prior, now_dt) else now_s, "until": until, "reason": reason,
                          "count": int(prior.get("count") or 0) + 1, "lastTicket": tid}
    data["modelDown"] = down
    ticket["modelFallback"] = {"from": model, "to": spare, "at": now_s, "until": until, "reason": reason}
    lines = ["model: %s failed for %s (%s); starting it again on backupModel %s, and every Shuttle uses %s instead of %s "
             "until %s" % (model or "?", tid, reason, spare, spare, model or "?", until[11:16] + "Z")]
    if int(down[model or "?"]["count"]) == 1:
        lines.append("herald: %s is failing (%s). Warp switched to the backup model %s for %d minutes." % (
            model or "the model", reason, spare, int(window_minutes(cfg))))
    return lines


def retry(data: dict, ticket: dict) -> None:
    """Start the same step again on the backup model: no recovery counted, never parked."""
    if ticket.get("status") == "alarm" and ticket.get("alarm") == "model-failure":
        return
    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    ticket["modelRetries"] = int(ticket.get("modelRetries") or 0) + 1
    ticket["modelRetryStep"] = str(shuttle.get("step") or "")
    # The same path as an agent-cap hit (agentcap.py): queued, picked up by the next refill, which restarts a ticket
    # with an open pull request from its branch. No recovery counted; never parked.
    ticket["status"] = "queued"
    ticket.pop("phase", None)
    ticket["alarm"] = None
    ticket["agent"] = None
    for key in ("pending", "step", "startedAt", "launching", "launchLines", "failed", "returned", "endedBy"):
        shuttle.pop(key, None)
