#!/usr/bin/env python3
"""Write the Warp completion report, .warp/warp-complete.html.

Waves are concurrency-run segments. A ticket is active from the status event
that enters the working set (claimed, recovering, planning, coding, review,
bugbot_running, fix, awaiting_approval, merging) until a status event leaves
that set. The
interval is half-open: start inclusive, end exclusive. Boundaries are every
start and end. Each open stretch between two boundaries has one set of active
tickets. An empty set is a gap: it is drawn on the concurrency chart and is
not a wave, and it splits waves so the same tickets resuming later are a new
wave. Adjacent stretches with the same set are one wave. A handoff that keeps
the count the same but changes who is running is two waves, because the set
changed. Dependency levels are not used.

Numbers the beam never recorded are n/a. This script does not invent them.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam  # noqa: E402

ALGO = (
    "Waves are concurrency-run segments. A ticket is active from the status "
    "event that enters the working set (claimed, recovering, planning, coding, "
    "review, bugbot_running, fix, awaiting_approval, merging) until a status event "
    "leaves that set. The interval is half-open: start inclusive, end exclusive. "
    "Boundaries are every start and end. Each stretch between two boundaries has "
    "one set of active tickets. An empty set is a gap: it is drawn on the "
    "concurrency chart and is not a wave, and it splits waves so the same tickets "
    "resuming later are a new wave. Adjacent stretches with the same set are one "
    "wave. A handoff that keeps the count the same but changes who is running is "
    "two waves, because the set changed. Dependency levels are not used."
)

WORKING = {"claimed", "recovering", "planning", "coding", "fix", "merging"}
BUGBOT = {"review", "bugbot_running"}
HUMAN = {"awaiting_approval"}
OPEN = {"queued"} | set(beam.ACTIVE)
FINISHED = {"merged", "done", "skipped"}
KIND = {
    "claimed": "bar",
    "recovering": "bar",
    "planning": "bar",
    "coding": "bar",
    "fix": "bar",
    "merging": "bar",
    "review": "wait",
    "bugbot_running": "wait",
    "awaiting_approval": "human",
}

HELP = """
examples:
  python3 scripts/report.py ?
  python3 scripts/report.py --beam .warp/beam.json
  python3 scripts/report.py --beam .warp/beam.json --partial
  python3 scripts/report.py --out .warp/warp-complete.html --open

Writes .warp/warp-complete.html when every ticket is merged, done, skipped,
blocked, or alarmed, or when the run has been stopped after it started.
--partial writes a snapshot while tickets are still queued or active.
--out PATH also writes that path. --open prints a file URL.
The file is gitignored because it lives under .warp.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def parse_time(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_ts(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not value:
        return "n/a"
    parsed = parse_time(value)
    return fmt_ts(parsed) if parsed else str(value)


def fmt_dur(seconds) -> str:
    if seconds is None:
        return "n/a"
    total = int(round(float(seconds)))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}h {}m".format(hours, minutes)
    if minutes:
        return "{}m {}s".format(minutes, secs)
    return "{}s".format(secs)


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def repo_root(beam_path: Path) -> Path:
    parent = Path(beam_path).parent
    if parent.name == ".warp":
        return parent.parent
    return parent


def canonical_path(beam_path: Path) -> Path:
    return Path(beam_path).parent / "warp-complete.html"


def _yaml_raw(text: str, key: str) -> Optional[str]:
    match = re.search(r"^{}:[ \t]*(.*?)[ \t]*(#.*)?$".format(re.escape(key)), text, re.M)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def _as_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    low = raw.lower()
    if low in {"true", "yes", "1"}:
        return True
    if low in {"false", "no", "0"}:
        return False
    return None


def report_settings(beam_path: Path, data: Optional[dict] = None) -> dict:
    """Defaults, then the beam config, then .warp/config.yaml."""
    cfg = {"reportOnComplete": True, "reportPath": ".warp/warp-complete.html"}
    beam_cfg = (data or {}).get("config") if isinstance((data or {}).get("config"), dict) else {}
    if "reportOnComplete" in beam_cfg:
        cfg["reportOnComplete"] = bool(beam_cfg.get("reportOnComplete"))
    if beam_cfg.get("reportPath"):
        cfg["reportPath"] = str(beam_cfg.get("reportPath"))
    path = repo_root(beam_path) / ".warp" / "config.yaml"
    if path.is_file():
        text = path.read_text()
        flag = _as_bool(_yaml_raw(text, "reportOnComplete"))
        if flag is not None:
            cfg["reportOnComplete"] = flag
        raw_path = _yaml_raw(text, "reportPath")
        if raw_path:
            cfg["reportPath"] = raw_path
    return cfg


def configured_path(beam_path: Path, data: dict) -> Path:
    rel = report_settings(beam_path, data).get("reportPath") or ".warp/warp-complete.html"
    dest = Path(rel)
    if dest.is_absolute():
        return dest
    return repo_root(beam_path) / dest


def execution_context(root: Path, data: dict) -> dict:
    ctx = data.get("execution") if isinstance(data.get("execution"), dict) else {}
    mode = ctx.get("mode")
    provider_name = ctx.get("provider")
    if not mode:
        try:
            import provider

            resolved = provider.resolve(root)
            mode = resolved.get("mode") or "local"
            if not provider_name:
                provider_name = resolved.get("provider")
        except Exception:
            mode = "local"
    return {"mode": mode or "local", "provider": provider_name}


def status_events(ticket: dict) -> list:
    rows = []
    for event in ticket.get("events") or []:
        if not isinstance(event, dict) or event.get("type") != "status":
            continue
        at = parse_time(event.get("at"))
        if not at:
            continue
        rows.append((at, str(event.get("from") or ""), str(event.get("to") or "")))
    rows.sort(key=lambda row: row[0])
    return rows


def active_intervals(ticket: dict, now: datetime) -> list:
    """Half-open [start, end) stretches inside the working set. End is `now` if still open."""
    intervals = []
    open_at = None
    for at, _frm, to in status_events(ticket):
        if to in beam.ACTIVE:
            if open_at is None:
                open_at = at
        elif open_at is not None:
            if at > open_at:
                intervals.append((open_at, at))
            open_at = None
    if open_at is not None:
        end = now if now > open_at else open_at
        if end > open_at:
            intervals.append((open_at, end))
    return intervals


def segments(spans: list) -> list:
    """Contiguous stretches, including count 0. `spans` are (id, start, end), end exclusive."""
    clean = []
    for item in spans:
        tid, start, end = item[0], item[1], item[2]
        if start is None or end is None or end <= start:
            continue
        clean.append((str(tid), start, end))
    bounds = sorted({moment for _tid, start, end in clean for moment in (start, end)})
    out = []
    for index in range(len(bounds) - 1):
        left, right = bounds[index], bounds[index + 1]
        if right <= left:
            continue
        ids = sorted({tid for tid, start, end in clean if start <= left and end > left})
        out.append({"start": left, "end": right, "ids": ids, "count": len(ids)})
    return out


def waves(spans: list) -> list:
    """Non-empty segments. Same set merges only when no empty gap sits between them."""
    merged = []
    prev = None
    for seg in segments(spans):
        if seg["count"] == 0:
            prev = None
            continue
        if prev is not None and prev["ids"] == seg["ids"]:
            prev["end"] = seg["end"]
            continue
        prev = {
            "start": seg["start"],
            "end": seg["end"],
            "ids": list(seg["ids"]),
            "count": seg["count"],
        }
        merged.append(prev)
    for number, wave in enumerate(merged, 1):
        wave["n"] = number
    return merged


def _settled(data: dict) -> bool:
    tickets = data.get("tickets") or {}
    if not tickets:
        return False
    return all((t or {}).get("status") not in OPEN for t in tickets.values())


def reportable(data: dict, because_stopped: bool = False) -> bool:
    """True when every ticket has left the queue, or scan.py stop just ran.

    A beam that ingest left at runState stopped is not finished. Stop sets
    stoppedAt. A later set can see that and refresh the report.
    """
    if because_stopped:
        return True
    if _settled(data):
        return True
    if (data.get("runState") or "") == "stopped" and data.get("stoppedAt"):
        return True
    return False


def is_partial(data: dict) -> bool:
    tickets = list((data.get("tickets") or {}).values())
    if not tickets:
        return True
    return any((t or {}).get("status") not in FINISHED for t in tickets)


def _num(ticket: dict, key: str, cast):
    usage = ticket.get("usage")
    if not isinstance(usage, dict) or key not in usage or usage[key] is None:
        return None
    try:
        return cast(usage[key])
    except (TypeError, ValueError):
        return None


def _sum_reported(tickets: list, key: str, cast):
    vals = [v for v in (_num(t, key, cast) for t in tickets) if v is not None]
    if not vals:
        return None
    return sum(vals)


def _first_status(ticket: dict, target: str) -> Optional[datetime]:
    for at, _frm, to in status_events(ticket):
        if to == target:
            return at
    return None


def _status_spans(ticket: dict, now: datetime) -> list:
    events = status_events(ticket)
    spans = []
    current = ticket.get("status")
    for index, (at, _frm, to) in enumerate(events):
        if index + 1 < len(events):
            end = events[index + 1][0]
        elif to == current and to in beam.ACTIVE:
            end = now
        else:
            continue
        if end > at:
            spans.append((at, end, to))
    return spans


def _durations(ticket: dict, data: dict, now: datetime) -> dict:
    spans = _status_spans(ticket, now)
    notes = []
    if not status_events(ticket):
        notes.append("no status events; timeline is empty")
        empty = {key: None for key in ("queued", "deps", "working", "bugbot", "human", "mergeWindow")}
        return {"durations": empty, "notes": notes, "pieces": []}
    statuses = {status for _a, _b, status in spans}
    outer = statuses <= {"claimed", "merging"}
    if outer:
        notes.append("only the outer interval was recorded")
    working = bugbot = human = 0.0
    pieces = []
    for start, end, status in spans:
        delta = (end - start).total_seconds()
        kind = KIND.get(status)
        if kind:
            pieces.append((start, end, kind))
        if status in WORKING or (outer and status == "claimed"):
            working += delta
        elif status in BUGBOT:
            bugbot += delta
        elif status in HUMAN:
            human += delta
    run_start = parse_time(data.get("runStartedAt") or data.get("generatedAt"))
    claim = _first_status(ticket, "claimed")
    if claim is None:
        for at, _frm, to in status_events(ticket):
            if to in beam.ACTIVE:
                claim = at
                break
    queued = deps = None
    if run_start and claim and claim > run_start:
        gap = (claim - run_start).total_seconds()
        pieces.insert(0, (run_start, claim, "dep"))
        if ticket.get("deps"):
            deps = gap
        else:
            queued = gap
    merge_window = None
    for event in ticket.get("events") or []:
        if isinstance(event, dict) and event.get("type") == "merge-window":
            merge_window = 0.0
    return {
        "durations": {
            "queued": queued,
            "deps": deps,
            "working": working,
            "bugbot": None if outer else bugbot,
            "human": None if outer else human,
            "mergeWindow": merge_window,
        },
        "notes": notes,
        "pieces": pieces,
    }


def _show_count(value) -> str:
    return "n/a" if value is None else str(value)


def _show_hours(value) -> str:
    if value is None or value == "":
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    text = "{:.1f}".format(number).rstrip("0").rstrip(".")
    return text + "h"


def _show_cost(value) -> str:
    if value is None:
        return "n/a"
    return "${:.2f}".format(float(value))


def summarize(data: dict, ctx: dict, now: datetime, partial: bool) -> dict:
    tickets = [t for t in (data.get("tickets") or {}).values() if isinstance(t, dict)]
    tickets.sort(key=lambda t: str(t.get("id") or ""))
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    program = data.get("program") if isinstance(data.get("program"), dict) else {}
    estimate = program.get("estimate") if isinstance(program.get("estimate"), dict) else {}
    notes = [
        "Merge-window wait is not recorded. respectMergeWindows does not stamp a wait.",
        "Lock wait is included in the pre-claim span and is not split out.",
    ]
    rows = []
    spans = []
    human_actual = None
    human_known = False
    any_ci = False
    ci_retries = 0
    bug_found_vals = []
    bug_fixed_vals = []
    attempts = 0
    alarms = 0
    stuck = 0
    prs_opened = 0
    prs_merged = 0
    jira_transitions = 0
    jira_comments = 0
    spend = 0
    any_spend = False
    usage_tickets = 0
    done = blocked = failed = skipped = 0
    auto_merged = manual_merged = 0
    for ticket in tickets:
        status = ticket.get("status") or ""
        if status in {"merged", "done"}:
            done += 1
            if ticket.get("autoMerge"):
                auto_merged += 1
            else:
                manual_merged += 1
        elif status == "blocked":
            blocked += 1
        elif status == "alarm":
            failed += 1
        elif status == "skipped":
            skipped += 1
        if status == "alarm" or ticket.get("alarm"):
            alarms += 1
        if "stuck" in str(ticket.get("alarm") or "").casefold():
            stuck += 1
        attempts += int(ticket.get("attempts") or 0)
        pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
        jira = ticket.get("jira") if isinstance(ticket.get("jira"), dict) else {}
        if pr.get("url"):
            prs_opened += 1
        if status in {"merged", "done"} and (pr.get("url") or pr.get("sha") or pr.get("via")):
            prs_merged += 1
        if "bugbotFindings" in pr and pr.get("bugbotFindings") is not None:
            bug_found_vals.append(int(pr.get("bugbotFindings") or 0))
        if "bugbotFixed" in pr and pr.get("bugbotFixed") is not None:
            bug_fixed_vals.append(int(pr.get("bugbotFixed") or 0))
        events = [e for e in (ticket.get("events") or []) if isinstance(e, dict)]
        if pr.get("ci") or pr.get("ciRetries") is not None or any(e.get("type") == "ci" for e in events):
            any_ci = True
            ci_retries += int(pr.get("ciRetries") or 0)
        for field in ("startedAt", "qaReadyAt", "doneAt"):
            if jira.get(field):
                jira_transitions += 1
        comments = jira.get("comments")
        if isinstance(comments, dict):
            jira_comments += len(comments)
        tokens = int(ticket.get("tokens") or 0)
        if tokens:
            any_spend = True
            spend += tokens
        if any(_num(ticket, key, float) is not None for key in ("tokensIn", "tokensOut", "tokensCached", "cost")):
            usage_tickets += 1
        detail = _durations(ticket, data, now)
        for note in detail["notes"]:
            notes.append("{}: {}".format(ticket.get("id"), note))
        if detail["durations"]["human"] is not None:
            human_known = True
            human_actual = (human_actual or 0) + detail["durations"]["human"]
        for start, end in active_intervals(ticket, now):
            spans.append((ticket.get("id"), start, end))
        claimed = _first_status(ticket, "claimed")
        bug_clean = None
        for event in events:
            if event.get("type") == "bugbot" and str(event.get("value") or "").strip().casefold() == "pass":
                bug_clean = parse_time(event.get("at"))
                break
        if bug_clean is None and str(pr.get("bugbot") or "").strip().casefold() == "pass":
            bug_clean = parse_time(pr.get("reviewedAt"))
        rows.append(
            {
                "id": ticket.get("id"),
                "jiraKey": ticket.get("jiraKey"),
                "summary": ticket.get("summary") or "",
                "size": ticket.get("size") or "",
                "status": status,
                "branch": ticket.get("branch"),
                "pr": pr.get("url"),
                "agent": ticket.get("agent"),
                "deps": list(ticket.get("deps") or []),
                "locks": list(ticket.get("locks") or []),
                "stamps": {
                    "claimed": claimed,
                    "started": parse_time(jira.get("startedAt")) or claimed,
                    "prOpened": parse_time(pr.get("openedAt")),
                    "bugbotClean": bug_clean,
                    "qaReady": parse_time(jira.get("qaReadyAt")) or _first_status(ticket, "awaiting_approval"),
                    "approved": parse_time(pr.get("approvedAt")),
                    "merged": parse_time(pr.get("mergedAt")) or _first_status(ticket, "merged"),
                    "done": parse_time(jira.get("doneAt")) or _first_status(ticket, "done"),
                },
                "durations": detail["durations"],
                "pieces": detail["pieces"],
                "tokensIn": _num(ticket, "tokensIn", int),
                "tokensOut": _num(ticket, "tokensOut", int),
                "tokensCached": _num(ticket, "tokensCached", int),
                "cost": _num(ticket, "cost", float),
                "spend": tokens if tokens else None,
                "bugbotFixed": int(pr.get("bugbotFixed")) if pr.get("bugbotFixed") is not None else None,
                "attempts": int(ticket.get("attempts") or 0),
            }
        )
    wave_rows = waves(spans)
    step_rows = segments(spans)
    if usage_tickets == 0:
        notes.append("Tokens and cost were not reported. They are n/a, not zero.")
    elif usage_tickets < len(tickets):
        notes.append(
            "Usage was reported for {} of {} tickets. Missing tickets are n/a and are not counted as zero.".format(
                usage_tickets, len(tickets)
            )
        )
    if any_spend and usage_tickets == 0:
        notes.append("Legacy spend tokens are a single additive total from beam.py spend. They are not tokens in, out, or cached.")
    if not any_ci:
        notes.append("CI retries were not recorded.")
    if not bug_found_vals and not bug_fixed_vals:
        notes.append("Bugbot finding counts were not recorded.")
    start = parse_time(data.get("runStartedAt") or data.get("generatedAt"))
    end = parse_time(data.get("runEndedAt"))
    if start and end:
        end_label = fmt_ts(end)
        elapsed = fmt_dur((end - start).total_seconds())
    elif partial and start:
        end_label = "in progress"
        elapsed = fmt_dur((now - start).total_seconds())
    else:
        end_label = "in progress" if partial else "n/a"
        elapsed = "n/a"
    try:
        import version

        ver = version.label(version.version_of(version.PLUGIN_ROOT))
    except Exception:
        ver = "Warp"
    totals = {
        "total": str(len(tickets)),
        "done": str(done),
        "blocked": str(blocked),
        "failed": str(failed),
        "skipped": str(skipped),
        "autoMerged": str(auto_merged),
        "manualMerged": str(manual_merged),
        "agentHours": _show_hours(estimate.get("agentHours") if estimate.get("agentHours") is not None else program.get("agentHours")),
        "elapsed": elapsed,
        "humanEstimate": _show_hours(estimate.get("humanHours")),
        "humanActual": fmt_dur(human_actual) if human_known else "n/a",
        "tokensIn": _show_count(_sum_reported(tickets, "tokensIn", int)),
        "tokensOut": _show_count(_sum_reported(tickets, "tokensOut", int)),
        "tokensCached": _show_count(_sum_reported(tickets, "tokensCached", int)),
        "cost": _show_cost(_sum_reported(tickets, "cost", float)),
        "spendTokens": str(spend) if any_spend else "n/a",
        "bugbotFound": _show_count(sum(bug_found_vals) if bug_found_vals else None),
        "bugbotFixed": _show_count(sum(bug_fixed_vals) if bug_fixed_vals else None),
        "fixAttempts": str(attempts),
        "ciRetries": str(ci_retries) if any_ci else "n/a",
        "alarms": str(alarms),
        "stuck": str(stuck),
        "prsOpened": str(prs_opened),
        "prsMerged": str(prs_merged),
        "jiraTransitions": str(jira_transitions),
        "jiraComments": str(jira_comments),
        "waves": str(len(wave_rows)),
    }
    return {
        "version": ver,
        "mode": ctx.get("mode") or "local",
        "provider": ctx.get("provider") or "n/a",
        "model": cfg.get("model") or "n/a",
        "maxAgents": cfg.get("maxAgents") if cfg.get("maxAgents") is not None else "n/a",
        "project": cfg.get("projectName") or cfg.get("jiraProject") or "",
        "jiraSite": (cfg.get("jiraSite") or "").strip().rstrip("/"),
        "start": fmt_ts(start) if start else "n/a",
        "end": end_label,
        "partial": partial,
        "totals": totals,
        "rows": rows,
        "waves": wave_rows,
        "steps": step_rows,
        "notes": notes,
    }


def totals_plain(summary: dict) -> str:
    t = summary["totals"]
    lines = [
        "Tickets: {} total, {} done, {} blocked, {} failed, {} skipped".format(
            t["total"], t["done"], t["blocked"], t["failed"], t["skipped"]
        ),
        "Auto-merged (sizes in autoMergeSizes): {}".format(t["autoMerged"]),
        "Manual merged (sizes not in autoMergeSizes): {}".format(t["manualMerged"]),
        "Agent hours (estimate): {}".format(t["agentHours"]),
        "Elapsed (wall): {}".format(t["elapsed"]),
        "Human time (estimate): {}".format(t["humanEstimate"]),
        "Human time (actual): {}".format(t["humanActual"]),
        "Tokens in: {}".format(t["tokensIn"]),
        "Tokens out: {}".format(t["tokensOut"]),
        "Tokens cached: {}".format(t["tokensCached"]),
        "Cost: {}".format(t["cost"]),
        "Spend tokens (legacy): {}".format(t["spendTokens"]),
        "Bugbot findings found: {}".format(t["bugbotFound"]),
        "Bugbot findings fixed: {}".format(t["bugbotFixed"]),
        "Fix attempts: {}".format(t["fixAttempts"]),
        "CI retries: {}".format(t["ciRetries"]),
        "Alarms: {}".format(t["alarms"]),
        "Stuck: {}".format(t["stuck"]),
        "PRs opened: {}".format(t["prsOpened"]),
        "PRs merged: {}".format(t["prsMerged"]),
        "Jira transitions: {}".format(t["jiraTransitions"]),
        "Jira comments: {}".format(t["jiraComments"]),
        "Waves: {}".format(t["waves"]),
    ]
    return "\n".join(lines)


def _href(url: Optional[str]) -> str:
    if not url or not isinstance(url, str):
        return ""
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return ""


def _link(url: str, label: str) -> str:
    href = _href(url)
    if not href:
        return esc(label)
    return '<a href="{}">{}</a>'.format(esc(href), esc(label))


def _dur_cell(durs: dict) -> str:
    labels = (
        ("queued", "queued"),
        ("deps", "deps"),
        ("working", "working"),
        ("bugbot", "bugbot/ci"),
        ("human", "human"),
        ("mergeWindow", "merge window"),
    )
    parts = []
    for key, label in labels:
        parts.append("{} {}".format(label, fmt_dur(durs.get(key))))
    return esc(" · ".join(parts))


def _stamp_cell(stamps: dict) -> str:
    labels = (
        ("claimed", "claimed"),
        ("started", "started"),
        ("prOpened", "PR opened"),
        ("bugbotClean", "Bugbot clean"),
        ("qaReady", "QA ready"),
        ("approved", "approved"),
        ("merged", "merged"),
        ("done", "done"),
    )
    parts = ["{} {}".format(label, fmt_ts(stamps.get(key))) for key, label in labels]
    return esc(" · ".join(parts))


def _domain(summary: dict):
    moments = []
    for row in summary["rows"]:
        for start, end, _kind in row["pieces"]:
            moments.append(start)
            moments.append(end)
    for seg in summary["steps"]:
        moments.append(seg["start"])
        moments.append(seg["end"])
    if not moments:
        return None
    return min(moments), max(moments)


def _scale(moment, left, right, x0, width):
    span = (right - left).total_seconds() or 1
    return x0 + (moment - left).total_seconds() / span * width


def _svg_gantt(summary: dict) -> str:
    domain = _domain(summary)
    rows = [row for row in summary["rows"] if row["pieces"]]
    if not domain or not rows:
        return (
            '<svg class="gantt" viewBox="0 0 800 48" role="img">'
            '<text x="8" y="28" fill="var(--muted)">No timed intervals were recorded.</text>'
            "</svg>"
        )
    left, right = domain
    width = 800
    label_w = 88
    row_h = 28
    height = 16 + row_h * len(rows)
    parts = [
        '<svg class="gantt" viewBox="0 0 {} {}" role="img">'.format(width, height),
        "<title>Per-ticket timeline</title>",
    ]
    plot_w = width - label_w - 8
    for index, row in enumerate(rows):
        y = 8 + index * row_h
        parts.append('<text x="4" y="{}" fill="var(--fg)">{}</text>'.format(y + 16, esc(row["id"])))
        for start, end, kind in row["pieces"]:
            x = _scale(start, left, right, label_w, plot_w)
            w = max(1.5, _scale(end, left, right, label_w, plot_w) - x)
            fill = {"bar": "var(--bar)", "wait": "var(--wait)", "human": "var(--human)", "dep": "var(--dep)"}.get(kind, "var(--bar)")
            parts.append(
                '<rect x="{:.1f}" y="{}" width="{:.1f}" height="16" fill="{}"></rect>'.format(x, y + 4, w, fill)
            )
    parts.append("</svg>")
    return "".join(parts)


def _svg_steps(summary: dict) -> str:
    segs = summary["steps"]
    domain = _domain(summary)
    if not segs or not domain:
        return (
            '<svg class="steps" viewBox="0 0 800 48" role="img">'
            '<text x="8" y="28" fill="var(--muted)">No timed intervals were recorded.</text>'
            "</svg>"
        )
    left, right = domain
    width = 800
    height = 120
    pad = 16
    peak = max(seg["count"] for seg in segs) or 1
    points = []
    for seg in segs:
        for moment in (seg["start"], seg["end"]):
            x = _scale(moment, left, right, pad, width - 2 * pad)
            y = height - pad - (seg["count"] / peak) * (height - 2 * pad)
            points.append("{:.1f},{:.1f}".format(x, y))
    return (
        '<svg class="steps" viewBox="0 0 {} {}" role="img">'.format(width, height)
        + "<title>Concurrency over time</title>"
        + '<polyline points="{}" fill="none" stroke="var(--bar)" stroke-width="2"></polyline>'.format(" ".join(points))
        + "</svg>"
    )


def _wave_text(wave: dict) -> str:
    ids = ", ".join(wave["ids"])
    if wave["count"] == 1:
        body = "1 ticket ({})".format(ids)
    else:
        body = "{} tickets in parallel ({})".format(wave["count"], ids)
    return "Wave {}: {}, {} → {}".format(wave["n"], body, fmt_ts(wave["start"]), fmt_ts(wave["end"]))


def render_html(data: dict, ctx: dict, partial: bool, now: Optional[datetime] = None, repo: str = "") -> str:
    now = now or datetime.now(timezone.utc)
    summary = summarize(data, ctx, now, partial)
    try:
        import herald_fmt

        root = Path(repo) if repo else Path(".")
        repo_label = herald_fmt.repo_name(root)
    except Exception:
        repo_label = repo or "repo"
    project = summary["project"] or repo_label
    banner = ""
    if partial:
        banner = '<p class="banner">Partial snapshot. The run is still in progress.</p>'
    cards = []
    for label, key in (
        ("Tickets", "total"),
        ("Done", "done"),
        ("Blocked", "blocked"),
        ("Failed", "failed"),
        ("Auto-merged", "autoMerged"),
        ("Manual merged", "manualMerged"),
        ("Elapsed", "elapsed"),
        ("Cost", "cost"),
        ("Tokens in", "tokensIn"),
        ("Bugbot fixed", "bugbotFixed"),
        ("PRs merged", "prsMerged"),
        ("Waves", "waves"),
    ):
        cards.append("<div><span>{}</span><strong>{}</strong></div>".format(esc(label), esc(summary["totals"][key])))
    body_rows = []
    site = summary["jiraSite"]
    for row in summary["rows"]:
        key = row.get("jiraKey") or ""
        if key and site:
            key_cell = _link(site + "/browse/" + key, key)
        else:
            key_cell = esc(key or "n/a")
        pr_cell = _link(row.get("pr"), row.get("pr")) if row.get("pr") else "n/a"
        usage = "in {} · out {} · cached {} · {}".format(
            _show_count(row["tokensIn"]),
            _show_count(row["tokensOut"]),
            _show_count(row["tokensCached"]),
            _show_cost(row["cost"]),
        )
        if row["spend"] is not None:
            usage += " · spend {}".format(row["spend"])
        deps = ", ".join(str(d) for d in row["deps"]) or "—"
        locks = ", ".join(str(d) for d in row["locks"]) or "—"
        body_rows.append(
            "<tr>"
            "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "</tr>".format(
                esc(row["id"]),
                key_cell,
                esc(row["summary"]),
                esc(row["size"] or "n/a"),
                esc(row["status"] or "n/a"),
                esc(row["branch"] or "n/a"),
                pr_cell,
                _stamp_cell(row["stamps"]),
                _dur_cell(row["durations"]),
                esc(row["agent"] or "n/a"),
                esc(usage),
                esc(_show_count(row["bugbotFixed"])),
                esc(row["attempts"]),
                esc(deps + " / " + locks),
            )
        )
    wave_items = ["<li>{}</li>".format(esc(_wave_text(wave))) for wave in summary["waves"]] or ["<li>No waves. No timed intervals were recorded.</li>"]
    note_items = ["<li>{}</li>".format(esc(note)) for note in summary["notes"]]
    plain = totals_plain(summary)
    css = """
:root { color-scheme: light dark; --bg: #f7f7f5; --fg: #1c1917; --card: #ffffff; --muted: #57534e; --bar: #2563eb; --wait: #d97706; --human: #7c3aed; --dep: #a8a29e; --line: #e7e5e4; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1917; --fg: #fafaf9; --card: #292524; --muted: #a8a29e; --bar: #60a5fa; --wait: #fbbf24; --human: #c4b5fd; --dep: #78716c; --line: #44403c; }
}
@media print {
  :root { --bg: #ffffff; --fg: #111111; --card: #ffffff; --muted: #444444; --bar: #1d4ed8; --wait: #b45309; --human: #6d28d9; --dep: #888888; --line: #dddddd; }
  .strip { break-inside: avoid; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--fg); font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; }
h1, h2 { font-weight: 650; }
h1 { margin: 0 0 8px; font-size: 1.6rem; }
h2 { margin: 28px 0 8px; font-size: 1.15rem; }
.meta, .muted { color: var(--muted); }
.banner { background: var(--wait); color: #111; padding: 8px 12px; border-radius: 8px; }
.strip { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.strip div { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; min-width: 7rem; }
.strip span { display: block; color: var(--muted); font-size: 12px; }
table { width: 100%; border-collapse: collapse; background: var(--card); }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: var(--card); }
.wrap { overflow-x: auto; }
pre.totals { background: var(--card); border: 1px solid var(--line); padding: 12px; white-space: pre-wrap; }
svg { width: 100%; height: auto; background: var(--card); border: 1px solid var(--line); }
.swatch { display: inline-block; width: 0.8em; height: 0.8em; margin-right: 0.3em; vertical-align: -0.05em; }
.swatch.bar { background: var(--bar); }
.swatch.wait { background: var(--wait); }
.swatch.human { background: var(--human); }
.swatch.dep { background: var(--dep); }
ol, ul { padding-left: 1.2rem; }
a { color: var(--bar); }
"""
    header = (
        "<header>"
        "<h1>Warp completion report</h1>"
        "<p class=\"meta\">{} / {} · {} · mode {} · provider {} · model {} · maxAgents {}</p>"
        "<p class=\"meta\">Run start {} · end {} · elapsed {}</p>"
        "</header>"
    ).format(
        esc(repo_label),
        esc(project),
        esc(summary["version"]),
        esc(summary["mode"]),
        esc(summary["provider"]),
        esc(summary["model"]),
        esc(summary["maxAgents"]),
        esc(summary["start"]),
        esc(summary["end"]),
        esc(summary["totals"]["elapsed"]),
    )
    table = (
        "<div class=\"wrap\"><table>"
        "<thead><tr>"
        "<th>id</th><th>Jira</th><th>summary</th><th>size</th><th>status</th>"
        "<th>branch</th><th>PR</th><th>timestamps</th><th>durations</th>"
        "<th>agent</th><th>cost / tokens</th><th>Bugbot fixed</th><th>fix attempts</th><th>blocked-by / locks</th>"
        "</tr></thead><tbody>{}</tbody></table></div>"
    ).format("".join(body_rows) or "<tr><td colspan=\"14\">No tickets.</td></tr>")
    legend = (
        "<p class=\"muted\"><span class=\"swatch bar\"></span>working "
        "<span class=\"swatch wait\"></span>Bugbot / CI "
        "<span class=\"swatch human\"></span>waiting for a person "
        "<span class=\"swatch dep\"></span>queued or waiting on dependencies</p>"
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Warp completion report</title><style>{}</style></head><body>"
        "{}{}<h2>Totals</h2><div class=\"strip\">{}</div>"
        "<h2>Tickets</h2>{}<h2>Waves</h2><p>{}</p>{}<ol class=\"waves\">{}</ol>"
        "<h2>Timeline</h2>{}<h2>Concurrency</h2>{}"
        "<h2>Final counts</h2><pre class=\"totals\">{}</pre>"
        "<h2>Data quality</h2><ul class=\"notes\">{}</ul>"
        "</body></html>"
    ).format(
        css,
        banner,
        header,
        "".join(cards),
        table,
        esc(ALGO),
        legend,
        "".join(wave_items),
        _svg_gantt(summary),
        _svg_steps(summary),
        esc(plain),
        "".join(note_items),
    )


def _write_copies(beam_path: Path, data: dict, text: str, out: Optional[str]) -> list:
    canonical = canonical_path(beam_path)
    beam.atomic_write(canonical, text)
    written = [canonical]
    extras = []
    if out:
        dest = Path(out)
        if not dest.is_absolute():
            dest = Path.cwd() / dest
        extras.append(dest)
    extras.append(configured_path(beam_path, data))
    seen = {canonical.resolve()}
    for dest in extras:
        try:
            key = dest.resolve()
        except OSError:
            key = dest
        if key in seen:
            continue
        seen.add(key)
        beam.atomic_write(dest, text)
        written.append(dest)
    return written


def _herald(root: Path, summary_text: str, path: Path) -> None:
    try:
        import herald_fmt
        import notify

        web, _branch, _kind = herald_fmt.repo_web(root)
        intro = summary_text + "\nReport: {} (gitignored, local).".format(path)
        links = [("Repository", web)] if web else []
        msg = herald_fmt.message(
            root,
            "Run complete",
            intro=intro,
            links=links,
            footer="The completion report stays on this machine. .warp is gitignored, so the file is not on the remote.",
        )
        payload = {"kind": "report", **msg, "header": herald_fmt.header(root)}
        cfg = herald_fmt.read_config(root)
        targets, notes = notify.targets(cfg)
        payload["targets"] = targets
        payload["notes"] = notes
        if targets:
            payload["action"] = "post"
        else:
            payload["action"] = "outbox"
            notify.outbox(root, payload["text"])
            payload["notes"].append("no channel configured; message saved to .warp/outbox.md")
        payload["createdAt"] = beam.utcnow()
        dest = root / ".warp" / notify.PAYLOAD
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2) + "\n")
        notify.report(payload)
    except Exception as exc:
        print("herald: not posted ({})".format(exc))


def _maybe_open(path: Path) -> None:
    print(path.resolve().as_uri())
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return
    exe = shutil.which("xdg-open")
    if not exe:
        return
    try:
        subprocess.Popen(
            [exe, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


def generate(
    beam_path: Path,
    out: Optional[str] = None,
    partial: bool = False,
    open_it: bool = False,
    announce: bool = False,
    because_stopped: bool = False,
) -> dict:
    beam_path = Path(beam_path)
    data = beam.load_json(beam_path)
    finished = reportable(data, because_stopped)
    if not finished and not partial:
        raise SystemExit(2)
    was_complete = bool(data.get("runComplete"))
    now_text = beam.utcnow()
    data["reportGeneratedAt"] = now_text
    mark = finished and not partial
    if mark:
        data["runComplete"] = True
        data["runEndedAt"] = now_text
        if not data.get("runStartedAt"):
            data["runStartedAt"] = data.get("generatedAt") or now_text
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    show_partial = partial or is_partial(data)
    root = repo_root(beam_path)
    ctx = execution_context(root, data)
    text = render_html(data, ctx, show_partial, repo=str(root))
    written = _write_copies(beam_path, data, text, out)
    beam.journal(beam_path, {"type": "report", "path": str(written[0]), "partial": show_partial})
    if announce and mark and not was_complete:
        plain = totals_plain(summarize(data, ctx, datetime.now(timezone.utc), show_partial))
        _herald(root, plain, written[0])
    if open_it:
        target = written[0]
        if out:
            wanted = Path(out)
            if not wanted.is_absolute():
                wanted = Path.cwd() / wanted
            target = wanted
        _maybe_open(target)
    return {"canonical": written[0], "paths": written, "partial": show_partial}


def maybe_complete(beam_path: Path, announce: bool = True, because_stopped: bool = False) -> Optional[dict]:
    """Write the report when the beam is finished. A failure does not undo the status change."""
    try:
        beam_path = Path(beam_path)
        data = beam.load_json(beam_path)
        if not reportable(data, because_stopped):
            if data.get("runComplete"):
                data["runComplete"] = False
                beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
            return None
        if not report_settings(beam_path, data).get("reportOnComplete", True):
            return None
        result = generate(
            beam_path,
            announce=announce,
            because_stopped=because_stopped,
        )
        print("report: {}".format(result["canonical"]))
        return result
    except SystemExit:
        return None
    except Exception as exc:
        print("report: skipped ({})".format(exc))
        return None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the Warp completion report",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--out", help="also write the report to PATH")
    parser.add_argument("--open", action="store_true", help="print a file URL and try to open the report")
    parser.add_argument("--partial", action="store_true", help="write a snapshot while the run is still in progress")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    if not path.is_file():
        print("no beam at {}".format(path))
        return 1
    data = beam.load_json(path)
    if not reportable(data, False) and not args.partial:
        print("not complete; pass --partial")
        return 2
    result = generate(path, out=args.out, partial=bool(args.partial), open_it=bool(args.open), announce=not args.partial)
    print("report: {}".format(result["canonical"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
