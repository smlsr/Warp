#!/usr/bin/env python3
"""Match Warp tickets to Jira issues by summary, and write the plan id back.

Warp has no Jira credentials. This script builds the JQL, matches a saved
candidate list locally, and records a confirmed pair the same way resolve
does. editJiraIssue is only asked for when --write-external-id --yes is set.
Scan and claim do not write that field unless jiraWriteExternalId is true.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import jira_sync
from mcp_tools import TOOL_EDIT, TOOL_EDITMETA, TOOL_FIELDS, TOOL_ISSUE, TOOL_PROJECTS, TOOL_SEARCH

MATCH_NAME = "jira-match.json"
REPORT_NAME = "jira-match-report.json"
WRITE_NAME = "jira-external-id.json"
PAGE_SIZE = 50
MAX_PAGES = 2
MAX_TICKETS = 25
DEFAULT_CHARS = 60
DEFAULT_SCORE = 0.9
DONE_NAMES = {"done", "closed", "resolved"}
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")

HELP = """
examples:
  python3 scripts/jira_match.py ?
  python3 scripts/jira_match.py
  python3 scripts/jira_match.py --results candidates.json
  python3 scripts/jira_match.py --results candidates.json --apply
  python3 scripts/jira_match.py --results candidates.json --apply --yes
  python3 scripts/jira_match.py --all --chars 60 --min-score 0.9 --include-done
  python3 scripts/jira_match.py --write-external-id
  python3 scripts/jira_match.py --write-external-id --yes --results edits.json
  python3 scripts/jira_match.py --set-external-id WV-01=WAR-1 --yes
  python3 scripts/jira_sync.py external-id --ticket WV-01 --key WAR-1 --yes

Dry-run is the default. --apply stores one exact or prefix match on the beam
and in .warp/jira-map.json (source summary). A fuzzy score is a proposal until
--yes. Two issues, or one issue claimed by two tickets, are listed and not
stored. A manual key is left as is. Done and closed issues are skipped unless
--include-done. Issues already mapped to another ticket are skipped.

summary ~ in Jira is fuzzy, so the agent fetches a bounded candidate list
(50 issues, 2 pages) and this script matches locally. A truncated search is
named in the report.

--write-external-id and --set-external-id WV-01=WAR-1 update the External ID
field through editJiraIssue. That is a write and needs the connector permission
from /warp-allow-notify --with-jira. The field is discovered from
getJiraProjectIssueTypesMetadata and getJiraIssueEditmeta. A missing or
read-only field is skipped. A different non-empty value is left alone unless
--force-external-id. --yes confirms the edit. Nothing posts a Jira comment.
jiraWriteExternalId defaults to false, so scan and claim do not write the field.
The explicit flags work even when that config key is false.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
A bare help after an option that takes a value stays that value.
"""


def normalize_summary(text: str) -> str:
    """Case-fold, turn punctuation into spaces, and collapse whitespace."""
    raw = _PUNCT.sub(" ", str(text or "").casefold())
    return _SPACE.sub(" ", raw).strip()


def similarity(left: str, right: str) -> float:
    """Conservative token/ratio score. Exact normalized text is 1."""
    a = normalize_summary(left)
    b = normalize_summary(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    token = (len(ta & tb) / len(ta | tb)) if ta and tb else 0.0
    return round(min(ratio, token), 4)


def classify_pair(warp_summary: str, jira_summary: str, chars: int, min_score: float) -> tuple[str | None, float]:
    """exact, then a shared prefix of `chars`, then fuzzy at or above min_score."""
    left = normalize_summary(warp_summary)
    right = normalize_summary(jira_summary)
    if not left or not right:
        return None, 0.0
    if left == right:
        return "exact", 1.0
    width = max(1, int(chars))
    if len(left) >= width and len(right) >= width and left[:width] == right[:width]:
        return "prefix", similarity(warp_summary, jira_summary)
    score = similarity(warp_summary, jira_summary)
    if score >= float(min_score):
        return "fuzzy", score
    return None, score


def _issue_summary(issue: dict) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    return str(issue.get("summary") or fields.get("summary") or "").strip()


def _status(issue: dict) -> tuple[str, str]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raw = issue.get("status") if issue.get("status") is not None else fields.get("status")
    if isinstance(raw, str):
        return raw, str(issue.get("statusCategory") or "")
    if isinstance(raw, dict):
        cat = raw.get("statusCategory") or {}
        key = cat.get("key") if isinstance(cat, dict) else ""
        return str(raw.get("name") or ""), str(key or issue.get("statusCategory") or "")
    return "", str(issue.get("statusCategory") or "")


def is_done(issue: dict) -> bool:
    name, cat = _status(issue)
    return cat.casefold() == "done" or name.casefold() in DONE_NAMES


def _issue_key(issue: dict) -> str | None:
    return jira_sync.normalize_key(issue.get("key") or issue.get("jiraKey"))


def _external_on_issue(issue: dict, field: str) -> str:
    values = jira_sync._external_values(issue, field)
    return values[0] if values else ""


def summary_jql(summary: str, project: str | None, chars: int) -> str:
    text = normalize_summary(summary)[: max(1, int(chars))]
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    body = f'summary ~ "{safe}"'
    if project:
        return f"project = {project} AND {body}"
    return body


def _load_beam(beam_path: Path) -> dict:
    if not beam_path.is_file():
        return {"tickets": {}}
    try:
        data = json.loads(beam_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"tickets": {}}
    if not isinstance(data, dict):
        return {"tickets": {}}
    data.setdefault("tickets", {})
    return data


def _taken_keys(beam: dict, cfg: dict) -> dict[str, str]:
    """Issue key to the plan id that already owns it."""
    prefixes = jira_sync.prefixes_from(cfg)
    owned: dict[str, str] = {}
    for tid, ticket in (beam.get("tickets") or {}).items():
        key = jira_sync.jira_key(ticket, prefixes)
        if key and key not in owned:
            owned[key] = str(tid)
    return owned


def candidate_issues(data: dict) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()

    def add(issue) -> None:
        if not isinstance(issue, dict):
            return
        key = _issue_key(issue)
        if key and key in seen:
            return
        if key:
            seen.add(key)
        found.append(issue)

    if isinstance(data.get("issues"), list):
        for issue in data["issues"]:
            add(issue)
    elif isinstance(data.get("issues"), dict):
        for issue in data["issues"].values():
            add(issue)
    for row in data.get("searches") or []:
        if isinstance(row, dict):
            for issue in row.get("issues") or []:
                add(issue)
    return found


def search_truncated(data: dict) -> bool:
    if data.get("truncated"):
        return True
    for row in data.get("searches") or []:
        if not isinstance(row, dict):
            continue
        if row.get("isLast") is False or row.get("nextPageToken"):
            return True
        total = row.get("total")
        issues = row.get("issues") or []
        if isinstance(total, int) and total > len(issues):
            return True
    return False


def _confidence(kind: str) -> str:
    if kind == "exact":
        return "high"
    if kind == "prefix":
        return "medium"
    return "low"


def rank_ticket(
    tid: str,
    summary: str,
    issues: list[dict],
    *,
    chars: int,
    min_score: float,
    include_done: bool,
    taken: dict[str, str],
    project: str | None,
) -> dict:
    """One ticket against a candidate list. Does not write."""
    ranked = []
    notes = []
    for issue in issues:
        key = _issue_key(issue)
        if not key:
            continue
        if project and key.split("-", 1)[0] != project:
            continue
        owner = taken.get(key)
        if owner and owner != tid:
            notes.append(f"{key} already mapped to {owner}")
            continue
        if is_done(issue) and not include_done:
            notes.append(f"{key} is Done")
            continue
        kind, score = classify_pair(summary, _issue_summary(issue), chars, min_score)
        if not kind:
            continue
        ranked.append(
            {
                "key": key,
                "summary": _issue_summary(issue),
                "type": kind,
                "score": score,
            }
        )
    exact = [row for row in ranked if row["type"] == "exact"]
    prefix = [row for row in ranked if row["type"] == "prefix"]
    fuzzy = [row for row in ranked if row["type"] == "fuzzy"]

    def pack(outcome: str, chosen: dict | None, pool: list[dict]) -> dict:
        return {
            "id": tid,
            "summary": summary,
            "outcome": outcome,
            "type": (chosen or {}).get("type"),
            "key": (chosen or {}).get("key"),
            "score": (chosen or {}).get("score"),
            "confidence": _confidence((chosen or {}).get("type") or "") if chosen else None,
            "candidates": pool,
            "notes": notes,
        }

    if len(exact) == 1:
        return pack("confirmed", exact[0], exact)
    if len(exact) > 1:
        return pack("ambiguous", None, exact)
    if len(prefix) == 1:
        return pack("confirmed", prefix[0], prefix)
    if len(prefix) > 1:
        return pack("ambiguous", None, prefix)
    if len(fuzzy) == 1:
        return pack("proposal", fuzzy[0], fuzzy)
    if len(fuzzy) > 1:
        return pack("ambiguous", None, fuzzy)
    return pack("miss", None, [])


def _mark_duplicates(rows: list[dict]) -> None:
    claimed: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("outcome") in {"confirmed", "proposal"} and row.get("key"):
            claimed.setdefault(row["key"], []).append(row)
    for key, group in claimed.items():
        if len(group) < 2:
            continue
        ids = ", ".join(row["id"] for row in group)
        for row in group:
            row["outcome"] = "ambiguous"
            row["notes"] = list(row.get("notes") or []) + [f"{key} claimed by {ids}"]
            row["key"] = None
            row["type"] = None
            row["score"] = None
            row["confidence"] = None


def rank_all(tickets: list[dict], issues: list[dict], **opts) -> list[dict]:
    rows = []
    for ticket in tickets:
        if ticket.get("skip"):
            rows.append(
                {
                    "id": ticket["id"],
                    "summary": ticket.get("summary") or "",
                    "outcome": "skip",
                    "type": None,
                    "key": ticket.get("kept"),
                    "score": None,
                    "confidence": None,
                    "candidates": [],
                    "notes": [ticket["skip"]],
                }
            )
            continue
        rows.append(rank_ticket(ticket["id"], ticket.get("summary") or "", issues, **opts))
    _mark_duplicates(rows)
    return rows


def _selectable(beam: dict, cfg: dict, only_id: str | None, include_all: bool) -> list[dict]:
    prefixes = jira_sync.prefixes_from(cfg)
    chosen = []
    for tid, ticket in (beam.get("tickets") or {}).items():
        if only_id and str(tid) != only_id:
            continue
        summary = str(ticket.get("summary") or "")
        manual = ticket.get("jiraKeySource") == "manual" or ticket.get("jiraKeyForced")
        kept = jira_sync.jira_key(ticket, prefixes)
        if manual:
            chosen.append({"id": str(tid), "summary": summary, "skip": "manual key left as is", "kept": kept})
            continue
        if kept and not include_all:
            continue
        if kept and include_all:
            chosen.append({"id": str(tid), "summary": summary, "skip": f"already {kept}", "kept": kept})
            continue
        chosen.append({"id": str(tid), "summary": summary, "skip": None, "kept": None})
    return chosen


def _fmt_score(score) -> str:
    if score is None:
        return ""
    return f"{float(score):.2f}"


def render_rows(rows: list[dict], truncated: bool) -> list[str]:
    lines = []
    if truncated:
        lines.append(
            f"jira: search truncated. summary ~ is fuzzy in Jira; local matching stopped after {MAX_PAGES} pages of {PAGE_SIZE}."
        )
    if not rows:
        lines.append("jira: no unmapped tickets to match")
        return lines
    for row in rows:
        lines.append(f"{row['id']}")
        lines.append(f"  summary: {row.get('summary') or ''}")
        for cand in row.get("candidates") or []:
            lines.append(
                f"  {cand['key']}  {cand['type']}  {_fmt_score(cand['score'])}  {cand.get('summary') or ''}"
            )
        for note in row.get("notes") or []:
            lines.append(f"  note: {note}")
        outcome = row.get("outcome")
        if outcome == "confirmed":
            lines.append(f"  confirmed: {row['key']} ({row['type']}, {_fmt_score(row['score'])}, {row['confidence']})")
        elif outcome == "proposal":
            lines.append(
                f"  proposal: {row['key']} ({row['type']}, {_fmt_score(row['score'])}). Not stored until --apply --yes."
            )
        elif outcome == "ambiguous":
            shown = ", ".join(cand["key"] for cand in row.get("candidates") or []) or "several"
            lines.append(f"  ambiguous: {shown}. Not stored.")
        elif outcome == "skip":
            lines.append(f"  skip: {'; '.join(row.get('notes') or [])}")
        else:
            lines.append("  no match")
    return lines


def _store_report(beam_path: Path, rows: list[dict]) -> None:
    path = beam_path.parent / REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tickets": rows}, indent=2) + "\n")


def apply_rows(beam_path: Path, rows: list[dict], *, do_apply: bool, yes: bool) -> list[str]:
    lines = []
    for row in rows:
        tid = row["id"]
        kind = row.get("type")
        key = row.get("key")
        store = False
        if row.get("outcome") == "confirmed" and key and do_apply:
            store = True
        elif row.get("outcome") == "proposal" and key and do_apply and yes:
            store = True
        if row.get("outcome") == "confirmed" and key and not do_apply:
            lines.append(f"jira: dry-run {tid} -> {key} ({kind}, {_fmt_score(row.get('score'))}). Not written.")
            continue
        if row.get("outcome") == "proposal" and key and not (do_apply and yes):
            lines.append(
                f"jira: proposal {tid} -> {key} (fuzzy, {_fmt_score(row.get('score'))}). Not written. Pass --apply --yes to store it."
            )
            continue
        if not store:
            continue
        text = jira_sync.record_resolved(
            beam_path,
            tid,
            key,
            source="summary",
            confidence=row.get("confidence") or _confidence(kind or ""),
        )
        lines.append(text.split("\n", 1)[0])
    if do_apply and not any(row.get("outcome") in {"confirmed", "proposal"} for row in rows):
        lines.append("jira: nothing to store")
    elif not do_apply:
        lines.append("jira: nothing written. Pass --apply to store an exact or prefix match.")
    return lines


def match_results(beam_path: Path, data: dict, *, only_id: str | None, include_all: bool, chars: int, min_score: float, include_done: bool, do_apply: bool, yes: bool) -> str:
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    project = str(cfg.get("jiraProject") or "").strip().upper() or None
    tickets = _selectable(beam, cfg, only_id, include_all)
    taken = _taken_keys(beam, cfg)
    rows = rank_all(
        tickets,
        candidate_issues(data),
        chars=chars,
        min_score=min_score,
        include_done=include_done,
        taken=taken,
        project=project,
    )
    _store_report(beam_path, rows)
    lines = render_rows(rows, search_truncated(data))
    lines.extend(apply_rows(beam_path, rows, do_apply=do_apply, yes=yes))
    return "\n".join(lines)


def prepare_match(beam_path: Path, *, only_id: str | None, include_all: bool, chars: int, min_score: float, include_done: bool) -> str:
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    project = str(cfg.get("jiraProject") or "").strip().upper() or None
    tickets = [row for row in _selectable(beam, cfg, only_id, include_all) if not row.get("skip")]
    shown = tickets[:MAX_TICKETS]
    queries = []
    for row in shown:
        if not str(row.get("summary") or "").strip():
            continue
        scopes = [project] if project else [None]
        for scope in scopes:
            queries.append(
                {
                    "id": row["id"],
                    "summary": row["summary"],
                    "project": scope,
                    "jql": summary_jql(row["summary"], scope, chars),
                    "maxResults": PAGE_SIZE,
                    "maxPages": MAX_PAGES,
                }
            )
    payload = {
        "server": cfg.get("jiraMcp") or "atlassian",
        "tool": TOOL_SEARCH,
        "projectsTool": None if project else TOOL_PROJECTS,
        "project": project,
        "chars": chars,
        "minScore": min_score,
        "includeDone": include_done,
        "pageSize": PAGE_SIZE,
        "maxPages": MAX_PAGES,
        "note": "summary ~ is fuzzy in Jira. Fetch candidates and match locally. Stop after 2 pages of 50.",
        "queries": queries,
    }
    path = beam_path.parent / MATCH_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    me = Path(__file__).resolve()
    lines = [
        f"jira: match {len(shown)} ticket(s) by summary. Dry-run until --apply.",
        f"jira: server {payload['server']}. This script does not call Jira.",
    ]
    if not project:
        lines.append(f"jira: jiraProject is empty. Call {TOOL_PROJECTS} and run each jql in every visible project.")
    if len(tickets) > MAX_TICKETS:
        lines.append(f"jira: {len(tickets) - MAX_TICKETS} more tickets were not queried. Pass --id to narrow.")
    lines.append(
        f"jira: call {TOOL_SEARCH} for each jql in .warp/{MATCH_NAME}. "
        f"Request summary, status, and the external-id field. maxResults {PAGE_SIZE}. "
        f"Follow the next page at most once ({MAX_PAGES} pages). "
        "If isLast is false after that, set truncated true."
    )
    lines.append(
        'jira: save {"issues":[{"key":"WAR-1","summary":"...","status":"To Do"}],'
        '"searches":[{"jql":"...","issues":[],"isLast":true,"total":1}]}.'
    )
    tail = f" --chars {chars} --min-score {min_score}"
    if include_all:
        tail += " --all"
    if include_done:
        tail += " --include-done"
    if only_id:
        tail += f" --id {only_id}"
    lines.append(f"jira: then python3 {me} --results <file>{tail}")
    lines.append("jira: --apply stores one exact or prefix match. --apply --yes also stores one fuzzy proposal. Ambiguous matches are not stored.")
    return "\n".join(lines)


def hint_line(ticket: dict, beam_path: Path) -> str:
    """Extra check line when this unmapped ticket has a summary or a saved candidate."""
    summary = str(ticket.get("summary") or "").strip()
    tid = str(ticket.get("id") or "")
    me = Path(__file__).resolve()
    report = beam_path.parent / REPORT_NAME
    if report.is_file():
        try:
            data = json.loads(report.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        for row in data.get("tickets") or []:
            if str(row.get("id")) != tid:
                continue
            cands = row.get("candidates") or []
            if cands:
                best = cands[0]
                return (
                    f"summary candidate: {best.get('key')} ({best.get('type')} {_fmt_score(best.get('score'))}). "
                    f"python3 {me} --apply --id {tid}"
                )
    if summary:
        return f"summary match: python3 {me} --id {tid}"
    return ""


def writes_on_scan(cfg: dict) -> bool:
    return bool(cfg.get("jiraWriteExternalId"))


def _pairs_from_beam(beam: dict, cfg: dict, only_id: str | None, explicit: list[tuple[str, str]]) -> list[dict]:
    if explicit:
        return [{"id": tid, "key": key} for tid, key in explicit]
    prefixes = jira_sync.prefixes_from(cfg)
    pairs = []
    for tid, ticket in (beam.get("tickets") or {}).items():
        if only_id and str(tid) != only_id:
            continue
        key = jira_sync.jira_key(ticket, prefixes)
        if key:
            pairs.append({"id": str(tid), "key": key, "ticket": ticket})
    return pairs


def _parse_pair(raw: str) -> tuple[str, str] | None:
    if "=" not in str(raw):
        return None
    tid, key = str(raw).split("=", 1)
    tid = tid.strip()
    key = jira_sync.normalize_key(key)
    if not tid or not key:
        return None
    return tid, key


def _field_catalog(data: dict) -> list[dict]:
    raw = data.get("fields") or data.get("fieldCatalog") or []
    if isinstance(raw, dict):
        rows = []
        for fid, meta in raw.items():
            if isinstance(meta, dict):
                rows.append({"id": meta.get("id") or fid, "name": meta.get("name") or fid})
            elif isinstance(meta, str):
                rows.append({"id": fid, "name": meta})
        return rows
    return [row for row in raw if isinstance(row, dict)]


def discover_editable_field(data: dict, configured: str) -> dict:
    """Find the external-id field and whether editmeta says it can be edited."""
    import jira_lookup

    catalog = _field_catalog(data)
    names = {jira_lookup._norm_name(row.get("name") or ""): row for row in catalog}
    ids = {str(row.get("id") or "").casefold(): row for row in catalog}
    chosen = None
    wanted = jira_lookup._norm_name(configured)
    if wanted in names:
        chosen = names[wanted]
    elif configured.casefold() in ids:
        chosen = ids[configured.casefold()]
    else:
        for name in jira_lookup.EXTERNAL_FIELD_NAMES:
            row = names.get(jira_lookup._norm_name(name))
            if row:
                chosen = row
                break
    editmeta = data.get("editmeta") if isinstance(data.get("editmeta"), dict) else None
    editable_ids = set()
    if isinstance(editmeta, dict):
        fields = editmeta.get("fields") if isinstance(editmeta.get("fields"), dict) else editmeta
        if isinstance(fields, dict):
            editable_ids = {str(fid) for fid in fields}
            for fid, meta in fields.items():
                if isinstance(meta, dict) and meta.get("name"):
                    names.setdefault(jira_lookup._norm_name(meta["name"]), {"id": fid, "name": meta["name"]})
    if chosen is None and editable_ids:
        for fid in editable_ids:
            row = ids.get(fid.casefold()) or names.get(jira_lookup._norm_name(fid))
            if row and jira_lookup._norm_name(row.get("name") or "") in {jira_lookup._norm_name(n) for n in jira_lookup.EXTERNAL_FIELD_NAMES}:
                chosen = row
                break
    if chosen is None:
        return {"found": False, "editable": False, "reason": "field missing"}
    fid = str(chosen.get("id") or "")
    if editmeta is None:
        return {"found": True, "editable": None, "id": fid, "name": chosen.get("name") or configured, "reason": "editmeta missing"}
    editable = fid in editable_ids or any(fid.casefold() == item.casefold() for item in editable_ids)
    if not editable:
        return {"found": True, "editable": False, "id": fid, "name": chosen.get("name") or configured, "reason": "read-only"}
    return {"found": True, "editable": True, "id": fid, "name": chosen.get("name") or configured, "reason": ""}


def _edit_rows(data: dict) -> list[dict]:
    raw = data.get("edits")
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def _before_value(row: dict, issue: dict | None, field_id: str) -> str:
    if "before" in row and row.get("before") is not None:
        return str(row.get("before") or "")
    if isinstance(issue, dict):
        return _external_on_issue(issue, field_id)
    return ""


def prepare_write(beam_path: Path, pairs: list[tuple[str, str]], *, yes: bool, force: bool) -> str:
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    me = Path(__file__).resolve()
    lines = []
    payload_pairs = []
    if not yes:
        lines.append("jira: dry-run. External ID is not written until --yes.")
    lines.append(
        f"jira: this writes the External ID field with {TOOL_EDIT}. "
        "It needs the Jira connector permission (/warp-allow-notify --with-jira). No comment is posted."
    )
    if not writes_on_scan(cfg):
        lines.append("jira: jiraWriteExternalId is false. Scan and claim will not write this field. This command still can.")
    for tid, key in pairs:
        ticket = (beam.get("tickets") or {}).get(tid) or {}
        written = (ticket.get("jira") or {}).get("externalIdWritten") or {}
        if str(written.get("value") or "") == tid and not force:
            lines.append(f"jira: {tid} already written ({tid} at {written.get('at')}). Not sent again.")
            continue
        before = str((ticket.get("jira") or {}).get("externalId") or "")
        if before and before != tid and not force:
            lines.append(f"jira: {tid} {key} before {before} after {tid}. Not written. Pass --force-external-id to replace it.")
            continue
        payload_pairs.append({"id": tid, "key": key, "after": tid})
        lines.append(f"jira: {tid} {key} before {before or '(empty or not loaded)'} after {tid}.")
    path = beam_path.parent / WRITE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "server": cfg.get("jiraMcp") or "atlassian",
                "field": jira_sync.external_field(cfg),
                "editTool": TOOL_EDIT,
                "editmetaTool": TOOL_EDITMETA,
                "fieldsTool": TOOL_FIELDS,
                "issueTool": TOOL_ISSUE,
                "force": force,
                "pairs": payload_pairs,
            },
            indent=2,
        )
        + "\n"
    )
    if not payload_pairs:
        lines.append("jira: nothing to write")
        return "\n".join(lines)
    if not yes:
        lines.append(f"jira: pass --yes to confirm, then call {TOOL_EDITMETA} and {TOOL_EDIT}. Re-run with --results.")
        return "\n".join(lines)
    lines.append(
        f"jira: call {TOOL_FIELDS} and {TOOL_EDITMETA} for each issue. "
        f"If the External ID field is missing or not editable, skip it. "
        f"Otherwise {TOOL_EDIT} sets that field to the plan id. Do not add a comment."
    )
    lines.append(f"jira: then python3 {me} --write-external-id --yes --results <file>")
    return "\n".join(lines)


def record_write(beam_path: Path, data: dict, *, yes: bool, force: bool) -> str:
    """Record a finished edit. A missing or read-only field is skipped and is not a failure."""
    if not yes:
        return "jira: dry-run. Pass --yes to record an External ID write."
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    field = discover_editable_field(data, jira_sync.external_field(cfg))
    lines = []
    if not field.get("found"):
        lines.append("jira: External ID field is missing. Skipped. Nothing was written.")
        return "\n".join(lines)
    if field.get("editable") is False:
        lines.append(f"jira: External ID ({field.get('name')} {field.get('id')}) is read-only. Skipped. Nothing was written.")
        return "\n".join(lines)
    if field.get("editable") is None:
        lines.append("jira: editmeta was not in the transcript. Call getJiraIssueEditmeta. Nothing was written.")
        return "\n".join(lines)
    changed = False
    issues = data.get("issues") if isinstance(data.get("issues"), dict) else {}
    for row in _edit_rows(data):
        tid = str(row.get("id") or "")
        key = jira_sync.normalize_key(row.get("key") or "")
        ticket = (beam.get("tickets") or {}).get(tid)
        if not ticket:
            lines.append(f"jira: unknown ticket {tid}; skipped")
            continue
        issue = issues.get(key or "") if key else None
        if not isinstance(issue, dict):
            for item in candidate_issues(data):
                if _issue_key(item) == key:
                    issue = item
                    break
        before = _before_value(row, issue if isinstance(issue, dict) else None, field.get("id") or "")
        written = (ticket.get("jira") or {}).get("externalIdWritten") or {}
        if str(written.get("value") or "") == tid and not force:
            lines.append(f"jira: {tid} already written ({tid} at {written.get('at')}). Not sent again.")
            continue
        if row.get("editable") is False or row.get("error") in {"field missing", "read-only"}:
            lines.append(f"jira: {tid} {key} skipped. {row.get('error') or 'field is not editable'}. Nothing was written.")
            continue
        if before and before != tid and not force:
            lines.append(f"jira: {tid} {key} before {before} after {tid}. Not written. Pass --force-external-id to replace it.")
            continue
        if before == tid or row.get("result") == "already":
            jira = ticket.setdefault("jira", {"status": None, "lastCommentAt": None})
            wrote = not (jira.get("externalIdWritten") or {}).get("value")
            if wrote:
                jira["externalId"] = tid
                jira["externalIdWritten"] = {"value": tid, "at": jira_sync.now()}
                changed = True
            lines.append(f"jira: {tid} already {tid}. Recorded." if wrote else f"jira: {tid} already written ({tid}). Not sent again.")
            continue
        if row.get("result") not in {"updated", "already"}:
            lines.append(f"jira: {tid} {key} before {before or '(empty)'} after {tid}. Not recorded until editJiraIssue returns.")
            continue
        jira = ticket.setdefault("jira", {"status": None, "lastCommentAt": None})
        if str((jira.get("externalIdWritten") or {}).get("value") or "") == tid and row.get("result") == "already":
            lines.append(f"jira: {tid} already written ({tid} at {jira['externalIdWritten'].get('at')}). Not sent again.")
            continue
        jira["externalId"] = tid
        jira["externalIdWritten"] = {"value": tid, "at": jira_sync.now()}
        changed = True
        lines.append(f"jira: {tid} {key} before {before or '(empty)'} after {tid}. Recorded.")
    if changed:
        jira_sync._save(beam_path, beam)
    if not lines:
        lines.append("jira: nothing to write")
    return "\n".join(lines)


def hint_for_resolve(cfg: dict) -> str:
    if not writes_on_scan(cfg):
        return ""
    return (
        "jira: jiraWriteExternalId is on. After a key is stored, "
        "jira_sync.py external-id may write the plan id with editJiraIssue. It does not post a comment."
    )


def main(argv: list[str] | None = None) -> int:
    import usage

    parser = argparse.ArgumentParser(
        description="Match Warp tickets to Jira issues by summary",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--beam", default=".warp/beam.json")
    parser.add_argument("--id", help="one plan id")
    parser.add_argument("--all", dest="include_all", action="store_true", help="include tickets that already have a key")
    parser.add_argument("--chars", type=int, default=DEFAULT_CHARS, help="prefix length (default 60)")
    parser.add_argument("--min-score", type=float, default=DEFAULT_SCORE, help="fuzzy threshold (default 0.9)")
    parser.add_argument("--include-done", action="store_true", help="match Done and closed issues too")
    parser.add_argument("--results", help="JSON candidate list or edit transcript")
    parser.add_argument("--apply", action="store_true", help="store one exact or prefix match")
    parser.add_argument("--yes", action="store_true", help="also store one fuzzy proposal, and confirm an External ID write")
    parser.add_argument("--dry-run", action="store_true", help="print matches and write nothing")
    parser.add_argument("--write-external-id", action="store_true", help="write the plan id into the External ID field")
    parser.add_argument("--set-external-id", help="WV-01=WAR-1, write that one pair")
    parser.add_argument("--force-external-id", action="store_true", help="replace a different non-empty External ID")
    args = parser.parse_args(usage.normalize_argv(argv, {"--apply"}))
    beam = Path(args.beam)
    if not beam.is_absolute():
        beam = Path.cwd() / beam
    explicit: list[tuple[str, str]] = []
    if args.set_external_id:
        parsed = _parse_pair(args.set_external_id)
        if not parsed:
            print(f"jira: --set-external-id {args.set_external_id!r} must be ID=KEY")
            return 2
        explicit.append(parsed)
    do_apply = bool(args.apply) and not args.dry_run
    if args.write_external_id or explicit:
        if args.results and args.yes and not args.dry_run:
            try:
                data = json.loads(Path(args.results).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                print(f"jira: could not read results: {exc}")
                return 2
            print(record_write(beam, data if isinstance(data, dict) else {}, yes=True, force=bool(args.force_external_id)))
            return 0
        beam_data = _load_beam(beam)
        cfg = jira_sync.settings(beam, beam_data)
        pairs = explicit or [(row["id"], row["key"]) for row in _pairs_from_beam(beam_data, cfg, args.id, [])]
        if args.id and not explicit:
            pairs = [pair for pair in pairs if pair[0] == args.id]
        print(prepare_write(beam, pairs, yes=bool(args.yes) and not args.dry_run, force=bool(args.force_external_id)))
        return 0
    if args.results:
        try:
            data = json.loads(Path(args.results).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"jira: could not read results: {exc}")
            return 2
        if not isinstance(data, dict):
            print("jira: the transcript must be a JSON object")
            return 2
        print(
            match_results(
                beam,
                data,
                only_id=args.id,
                include_all=bool(args.include_all),
                chars=args.chars,
                min_score=args.min_score,
                include_done=bool(args.include_done),
                do_apply=do_apply,
                yes=bool(args.yes),
            )
        )
        return 0
    print(
        prepare_match(
            beam,
            only_id=args.id,
            include_all=bool(args.include_all),
            chars=args.chars,
            min_score=args.min_score,
            include_done=bool(args.include_done),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
