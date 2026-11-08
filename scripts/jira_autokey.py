#!/usr/bin/env python3
"""Resolve the Jira key of a merged ticket that has none, and move it to Done in the same pass (1.7.15).

Before 1.7.15 a merged ticket with no Jira key was asked about three times ("Jira still not Done … catchup"), then
raised an ALARM on every wake. On Forge-QA run core 712 of 1512 tickets had no key. Now:

  1. One batched search per pass for up to BATCH keyless merged tickets (`.warp/jira-autokey.json` holds the batch):
       JQL  project = <P> AND labels in ("warp:<ID>", …)                 the label every Warp issue carries
       JQL  project = <P> AND (summary ~ "\\"[<ID>]\\"" OR …)               the summary prefix [<ID>]
  2. `jira_autokey.py apply --results <file>` matches each id: label warp:<ID>, then a summary that starts with
     `[<ID>]`, then the warp-jira-match exact or prefix match on the ticket's summary. One match maps the key and
     prints the Done move in the same output. No match records `jira.noIssue` once; it is never asked or alarmed again.

  python3 scripts/jira_autokey.py ask   --beam .warp/beam.json           the MUST DO search lines (what a pass prints)
  python3 scripts/jira_autokey.py apply --beam .warp/beam.json --results results.json
  python3 scripts/jira_autokey.py apply --beam .warp/beam.json --results-json '<searchJiraIssuesUsingJql output>'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

BATCH = 50
REASK_MINUTES = 30
FILE = "jira-autokey.json"


def _scripts() -> str:
    return str(Path(__file__).resolve().parent)


def needs_key(ticket: dict, prefixes: list) -> bool:
    import jira_sync

    if not isinstance(ticket, dict) or ticket.get("status") != "merged" or ticket.get("followUpOf"):
        return False
    jira = ticket.get("jira") if isinstance(ticket.get("jira"), dict) else {}
    if jira.get("noIssue") or jira.get("doneAt"):
        return False
    return not jira_sync.jira_key(ticket, prefixes)


def _state(beam_path: Path) -> dict:
    try:
        doc = json.loads((Path(beam_path).parent / FILE).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _project(cfg: dict) -> str:
    import jira_sync

    prefixes = jira_sync.prefixes_from(cfg)
    return str(cfg.get("jiraProject") or (prefixes[0] if prefixes else "") or "")


def jql(ids: list, project: str) -> tuple:
    head = ("project = %s AND " % project) if project else ""
    labels = head + "labels in (%s)" % ", ".join('"warp:%s"' % tid for tid in ids)
    summary = head + "(%s)" % " OR ".join('summary ~ "\\"[%s]\\""' % tid for tid in ids)
    return labels, summary


def ask_lines(beam_path: Path, data: dict, now=None, force: bool = False) -> list:
    """The batched search for keyless merged tickets, at most once every REASK_MINUTES. Writes the batch file."""
    import beam as beam_mod
    import jira_sync
    from mcp_tools import TOOL_SEARCH

    beam_path = Path(beam_path)
    cfg = jira_sync.settings(beam_path, data)
    if not cfg.get("jiraTransition", True):
        return []
    prefixes = jira_sync.prefixes_from(cfg)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    ids = [tid for tid, t in sorted(tickets.items()) if needs_key(t, prefixes)]
    if not ids:
        return []
    now_dt, now_s = beam_mod.coerce_now(now)
    state = _state(beam_path)
    asked = beam_mod.parse_ts(str(state.get("askedAt") or ""))
    if not force and asked is not None and (now_dt - asked).total_seconds() < REASK_MINUTES * 60 and set(state.get("ids") or []) & set(ids):
        return []
    batch = ids[:BATCH]
    project = _project(cfg)
    labels, summary = jql(batch, project)
    doc = {"askedAt": now_s, "ids": batch, "project": project, "jql": [labels, summary], "left": len(ids) - len(batch)}
    (beam_path.parent / FILE).write_text(json.dumps(doc, indent=2) + "\n")
    server = cfg.get("jiraMcp") or "atlassian"
    me = Path(__file__).resolve()
    return [
        "jira: %d merged ticket%s with no Jira key (%s%s). MUST DO resolve them (no alarm; nothing waits on this): "
        "on server \"%s\" call %s twice with fields [\"summary\",\"labels\",\"status\"] and maxResults 100: "
        "JQL 1: %s  JQL 2: %s. Save both results as one JSON list to .warp/jira-autokey-results.json, then run: "
        "python3 %s apply --beam %s --results .warp/jira-autokey-results.json. It maps each key and prints the "
        "Done moves in the same output; an id with no issue is recorded no-issue once." % (
            len(ids), "" if len(ids) == 1 else "s", ", ".join(batch[:8]), " …" if len(batch) > 8 else "",
            server, TOOL_SEARCH, labels, summary, me, beam_path.resolve()),
    ]


def _issues(raw) -> list:
    out = []
    if isinstance(raw, dict):
        for key in ("issues", "results", "values"):
            if isinstance(raw.get(key), list):
                return _issues(raw[key])
        if raw.get("key"):
            return [raw]
        return []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                out.append(item)
            elif isinstance(item, (dict, list)):
                out.extend(_issues(item))
    return out


def _labels(issue: dict) -> list:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raw = issue.get("labels") if issue.get("labels") is not None else fields.get("labels")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def match(tid: str, summary: str, issues: list, chars: int = 60) -> tuple:
    """(key, how) for one ticket, or ("", why). Label, then [ID] summary prefix, then exact or prefix summary."""
    import jira_match

    label = "warp:%s" % tid
    by_label = sorted({str(i["key"]) for i in issues if label in _labels(i)})
    if len(by_label) == 1:
        return by_label[0], "label"
    if len(by_label) > 1:
        return "", "several issues carry %s: %s" % (label, ", ".join(by_label))
    prefix = ("[%s]" % tid).casefold()
    by_prefix = sorted({str(i["key"]) for i in issues if jira_match._issue_summary(i).casefold().startswith(prefix)})
    if len(by_prefix) == 1:
        return by_prefix[0], "summary"
    if len(by_prefix) > 1:
        return "", "several issues start with [%s]: %s" % (tid, ", ".join(by_prefix))
    if summary:
        close = []
        for issue in issues:
            kind, _score = jira_match.classify_pair(summary, jira_match._issue_summary(issue), chars, 1.01)
            if kind in {"exact", "prefix"}:
                close.append((0 if kind == "exact" else 1, str(issue["key"])))
        close.sort()
        if close and (len(close) == 1 or close[0][0] < close[1][0]):
            return close[0][1], "summary"
        if close:
            return "", "several summary matches: %s" % ", ".join(k for _r, k in close)
    return "", "none"


def apply(beam_path: Path, raw, now=None) -> list:
    """Map what the batch found, print the Done moves for those, and record no-issue for the rest (once)."""
    import beam as beam_mod
    import beamlock
    import jira_sync

    beam_path = Path(beam_path)
    issues = _issues(raw)
    state = _state(beam_path)
    lines: list = []
    found: list = []
    with beamlock.run_lock(beam_path):
        data = beam_mod.load_json(beam_path)
        cfg = jira_sync.settings(beam_path, data)
        prefixes = jira_sync.prefixes_from(cfg)
        tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
        ids = [tid for tid in (state.get("ids") or []) if tid in tickets] or \
              [tid for tid, t in sorted(tickets.items()) if needs_key(t, prefixes)][:BATCH]
        taken = {str(t.get("jiraKey")) for t in tickets.values() if isinstance(t, dict) and t.get("jiraKey")}
        now_s = beam_mod.coerce_now(now)[1]
        for tid in ids:
            ticket = tickets[tid]
            if not needs_key(ticket, prefixes):
                continue
            key, how = match(tid, str(ticket.get("summary") or ""), issues)
            if key and key in taken:
                key, how = "", "%s is already mapped to another ticket" % key
            if key:
                found.append((tid, key, how))
                taken.add(key)
                continue
            jira = ticket.setdefault("jira", {})
            jira["noIssue"] = now_s
            jira["noIssueWhy"] = how
            lines.append("jira: %s no-issue: %s; recorded, and it is not asked or alarmed again" % (
                tid, "no Jira issue has label warp:%s or a summary starting [%s]" % (tid, tid) if how == "none" else how))
        beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n", data=data)
    for tid, key, how in found:
        lines.append(jira_sync.record_resolved(beam_path, tid, key, source=how, confidence="high" if how == "label" else "medium"))
    data = beam_mod.load_json(beam_path)
    for tid, _key, _how in found:
        ticket = (data.get("tickets") or {}).get(tid)
        if isinstance(ticket, dict):
            text = jira_sync.queue_done(beam_path, data, ticket)
            if text:
                lines.extend(text.splitlines())
    path = beam_path.parent / FILE
    if path.is_file():
        state["appliedAt"] = beam_mod.coerce_now(now)[1]
        state["mapped"] = [tid for tid, _k, _h in found]
        path.write_text(json.dumps(state, indent=2) + "\n")
    lines.insert(0, "jira: autokey mapped %d, no issue for %d" % (len(found), sum(1 for l in lines if " no-issue: " in l)))
    return lines


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve missing Jira keys", epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("--beam", default=".warp/beam.json")
    ap = sub.add_parser("apply")
    ap.add_argument("--beam", default=".warp/beam.json")
    ap.add_argument("--results", default="")
    ap.add_argument("--results-json", dest="results_json", default="")
    args = parser.parse_args(argv)
    import beam as beam_mod

    path = Path(args.beam)
    if not path.is_file():
        print("jira: no beam at %s" % path)
        return 2
    if args.cmd == "ask":
        for line in ask_lines(path, beam_mod.load_json(path), force=True) or ["jira: every merged ticket has a key or a no-issue note"]:
            print(line)
        return 0
    text = Path(args.results).read_text() if args.results else (args.results_json or "[]")
    for line in apply(path, json.loads(text)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
