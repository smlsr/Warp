#!/usr/bin/env python3
"""Match Warp tickets to Jira issues by summary, and write the plan id back.

Warp has no Jira credentials. This script builds the JQL, matches a saved
candidate list locally, and records a confirmed pair the same way resolve
does. editJiraIssue is asked for by --write-external-id --yes, by
/warp-jira-external-id --apply --yes, and by /warp-scan and /warp-jira-check
when jiraWriteExternalId is true. That config flag is the consent, so scan
does not also require --yes.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import jira_sync
from mcp_tools import (
    CREATE_FIELD_TOOLS,
    TOOL_EDIT,
    TOOL_EDITMETA,
    TOOL_FIELDS,
    TOOL_ISSUE,
    TOOL_PROJECTS,
    TOOL_REMOTE_CREATE,
    TOOL_SCREEN,
    TOOL_SCREEN_UPDATE,
    TOOL_SEARCH,
)

MATCH_NAME = "jira-match.json"
REPORT_NAME = "jira-match-report.json"
WRITE_NAME = "jira-external-id.json"
FIELD_STATE_NAME = "jira-field.json"
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
jiraWriteExternalId defaults to false. When it is true, /warp-scan and
/warp-jira-check queue a write for every mapped ticket whose External ID is
not already the plan id, and --yes is not required. A key found by the
external-id search is already equal and is skipped. These explicit flags
still need --yes, and they work when that config key is false.
/warp-jira-external-id is the bulk command for mappings that already exist.

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


def _on_issue(ticket: dict) -> str | None:
    """Last value seen on the Jira issue. None means this beam has not loaded it."""
    jira = ticket.get("jira") or {}
    if "externalIdOnIssue" not in jira:
        return None
    return str(jira.get("externalIdOnIssue") or "")


def _current_label(value: str | None, attempt: str = "") -> str:
    if attempt == "field missing" and value in (None, ""):
        return "field missing"
    if value is None:
        return "unknown"
    if value == "":
        return "empty"
    return value


def warp_label(tid: str) -> str:
    return f"warp:{tid}"


def field_display_name(cfg: dict) -> str:
    raw = str(cfg.get("jiraExternalIdFieldName") or "External ID").strip().strip("\"'")
    return raw or "External ID"


def fallback_mode(cfg: dict) -> str:
    raw = str(cfg.get("jiraExternalIdFallback") or "label").strip().lower()
    if raw in {"remote-link", "remotelink", "remote_link", "link"}:
        return "remote-link"
    if raw == "none":
        return "none"
    return "label"


def project_key(cfg: dict) -> str:
    prefixes = jira_sync.prefixes_from(cfg)
    return prefixes[0] if prefixes else ""


def field_fingerprint(cfg: dict) -> str:
    return "|".join(
        [
            project_key(cfg),
            jira_sync.external_field(cfg),
            field_display_name(cfg),
            "1" if cfg.get("jiraCreateExternalIdField") else "0",
            fallback_mode(cfg),
        ]
    )


def load_field_state(beam_path: Path) -> dict:
    path = beam_path.parent / FIELD_STATE_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_field_state(beam_path: Path, state: dict) -> None:
    path = beam_path.parent / FIELD_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _attempts_say_missing(beam: dict) -> bool:
    for ticket in (beam.get("tickets") or {}).values():
        err = ((ticket.get("jira") or {}).get("externalIdAttempt") or {}).get("error")
        if err == "field missing":
            return True
    return False


def remembered_missing(beam_path: Path, beam: dict, cfg: dict, *, recheck: bool) -> bool:
    """True when a previous catalog check showed the field is missing. --recheck ignores that."""
    if recheck:
        return False
    state = load_field_state(beam_path)
    if state.get("status") == "missing" and state.get("fingerprint") == field_fingerprint(cfg):
        return True
    return _attempts_say_missing(beam)


def remember_missing(beam_path: Path, beam: dict, cfg: dict, *, save: bool, refresh: bool = False) -> dict:
    state = load_field_state(beam_path)
    fresh = not (state.get("status") == "missing" and state.get("fingerprint") == field_fingerprint(cfg) and state.get("checkedAt"))
    if fresh or refresh:
        state = {
            "project": project_key(cfg),
            "status": "missing",
            "checkedAt": jira_sync.now(),
            "fingerprint": field_fingerprint(cfg),
            "fieldName": field_display_name(cfg),
            "configuredField": jira_sync.external_field(cfg),
            "fallback": fallback_mode(cfg),
        }
    changed = False
    if save:
        for ticket in (beam.get("tickets") or {}).values():
            jira = ticket.get("jira") or {}
            attempt = jira.get("externalIdAttempt") or {}
            if attempt.get("error") == "field missing":
                jira.pop("externalIdAttempt", None)
                changed = True
    if save:
        save_field_state(beam_path, state)
        if changed:
            jira_sync._save(beam_path, beam)
    return state


def remember_present(beam_path: Path, cfg: dict, field_id: str, field_name: str, *, save: bool) -> None:
    state = {
        "project": project_key(cfg),
        "status": "present",
        "id": field_id,
        "name": field_name,
        "checkedAt": jira_sync.now(),
        "fingerprint": field_fingerprint(cfg),
        "configuredField": jira_sync.external_field(cfg),
    }
    if save:
        save_field_state(beam_path, state)


def report_discovered_id(beam_path: Path, beam: dict, field_id: str, *, save: bool) -> str:
    """Remember the id on the beam and in jira-field.json. Write yaml only when the key is empty."""
    import jira_project

    cfg_copy = beam.setdefault("config", {})
    if isinstance(cfg_copy, dict):
        cfg_copy["jiraExternalIdField"] = field_id
    root = beam_path.parent.parent
    text = jira_project.config_text(root)
    current = jira_project.config_value(text, "jiraExternalIdField")
    line = f"jiraExternalIdField: {field_id}"
    if jira_project._blank(current):
        if save:
            updated = jira_project._write_line(text, "jiraExternalIdField", field_id, "discovered External ID field")
            jira_project._save_config(root, updated)
            jira_sync._save(beam_path, beam)
        return f"jira: set {line}"
    if save:
        jira_sync._save(beam_path, beam)
    return (
        f"jira: jiraExternalIdField is {current}; left as is. "
        f"Add this line when you want scans to use the discovered id:\n{line}"
    )


def _admin_lines(cfg: dict) -> list[str]:
    project = project_key(cfg) or "the project"
    name = field_display_name(cfg)
    return [
        "jira: Creating a field is a site-wide Jira admin change and is off.",
        "jira: To allow creation: /warp-jira-external-id --create-field --yes",
        (
            f'jira: Or in Jira admin, create a Short text custom field named "{name}", '
            f"add it to the {project} screens and field context, then /warp-jira-external-id --recheck."
        ),
    ]


def _ensure_lines(cfg: dict) -> list[str]:
    project = project_key(cfg) or "the project"
    name = field_display_name(cfg)
    tools = ", ".join(CREATE_FIELD_TOOLS)
    return [
        "jira: MUST DO ensure field. Creating a field is a site-wide Jira admin change.",
        f"jira: Probe the connected server for {tools}. If discover exists, ask it to create a short text custom field. Do not invent a tool.",
        (
            "jira: The Atlassian Rovo MCP catalog (30 Sep 2026) has no create-field tool. "
            "It does not expose POST /rest/api/3/field or field contexts. "
            f"{TOOL_SCREEN} and {TOOL_SCREEN_UPDATE} only add a field that already exists."
        ),
        (
            f'jira: If a create tool exists, create a single-line text field named "{name}". '
            f"Then {TOOL_SCREEN} and {TOOL_SCREEN_UPDATE} (commit) so {project} edit screens include it. "
            f"Verify with {TOOL_EDITMETA} on one issue."
        ),
        "jira: If the tool is missing or permission is denied, do not fail. Record createField.result as unavailable or denied and use the fallback.",
        "jira: Do not overwrite jiraExternalIdField in .warp/config.yaml when it already has a value. Report the line jiraExternalIdField: <id>.",
    ]


def _label_command(row: dict) -> str:
    return f"jira: editJiraIssue {row['key']} update.labels add {warp_label(row['id'])}"


def _remote_command(row: dict) -> str:
    tid = row["id"]
    return (
        f"jira: {TOOL_REMOTE_CREATE} {row['key']} globalId {warp_label(tid)} "
        f"title {tid} url https://warp.local/tickets/{tid}"
    )


def _field_command(row: dict, field: str) -> str:
    return f"jira: editJiraIssue {row['key']} field {field} = {row['value']}"


def _backfill_rows(beam: dict, cfg: dict, *, only_ids: set[str] | None, force: bool, force_value: bool, method: str = "field"):
    """Mapped tickets that still need the plan id written."""
    prefixes = jira_sync.prefixes_from(cfg)
    queue = []
    equal = []
    skipped = []
    for tid, ticket in (beam.get("tickets") or {}).items():
        if only_ids is not None and str(tid) not in only_ids:
            continue
        key = jira_sync.jira_key(ticket, prefixes)
        if not key:
            skipped.append({"id": str(tid), "key": "", "reason": "no key"})
            continue
        source = ticket.get("jiraKeySource") or ""
        jira = ticket.get("jira") or {}
        written = jira.get("externalIdWritten") or {}
        current = _on_issue(ticket)
        attempt = str((jira.get("externalIdAttempt") or {}).get("error") or "")
        if source == "external" and not force:
            equal.append({"id": str(tid), "key": key, "reason": "external id match", "current": str(tid)})
            continue
        if str(written.get("value") or "") == str(tid) and not force:
            equal.append({"id": str(tid), "key": key, "reason": "already written", "current": str(tid)})
            continue
        if method == "none":
            skipped.append({"id": str(tid), "key": key, "reason": "field missing"})
            continue
        if method == "field" and current not in (None, "") and current != str(tid) and not force_value:
            skipped.append({"id": str(tid), "key": key, "reason": "different value", "current": current})
            continue
        queue.append({"id": str(tid), "key": key, "value": str(tid), "current": current, "attempt": attempt, "method": method})
    return queue, equal, skipped


def _summary_line(verb: str, n_write: int, n_equal: int, skipped: list[dict]) -> str:
    detail = []
    missing_n = 0
    for row in skipped:
        if row["reason"] == "field missing":
            missing_n += 1
        else:
            detail.append(f"{row['id']} {row['reason']}")
    if missing_n:
        detail.append("field missing" if missing_n == 1 else f"field missing x{missing_n}")
    tail = f" ({', '.join(detail)})" if detail else ""
    return f"jira: external id: {verb} {n_write}, already equal {n_equal}, skipped {len(skipped)}{tail}"


def _write_backfill_file(
    beam_path: Path,
    cfg: dict,
    queue: list[dict],
    *,
    must: bool,
    force: bool,
    force_value: bool,
    method: str,
    create: bool,
) -> None:
    path = beam_path.parent / WRITE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "server": cfg.get("jiraMcp") or "atlassian",
                "field": jira_sync.external_field(cfg),
                "fieldName": field_display_name(cfg),
                "method": method,
                "fallback": fallback_mode(cfg),
                "createField": create,
                "createTools": list(CREATE_FIELD_TOOLS),
                "editTool": TOOL_EDIT,
                "editmetaTool": TOOL_EDITMETA,
                "fieldsTool": TOOL_FIELDS,
                "issueTool": TOOL_ISSUE,
                "screenTool": TOOL_SCREEN,
                "screenUpdateTool": TOOL_SCREEN_UPDATE,
                "remoteTool": TOOL_REMOTE_CREATE,
                "mustDo": must,
                "force": force,
                "forceExternalId": force_value,
                "note": "One catalog call. If the External ID field is missing, do not edit it per ticket. Use method. Labels are update.labels add, never fields.labels. No comment. Then record-external-id.",
                "pairs": [
                    {
                        "id": row["id"],
                        "key": row["key"],
                        "value": row["value"],
                        "method": row.get("method") or method,
                        "label": warp_label(row["id"]),
                    }
                    for row in queue
                ],
            },
            indent=2,
        )
        + "\n"
    )


def _dry_line(row: dict, field: str) -> str:
    method = row.get("method") or "field"
    if method == "label":
        return f"jira: {row['id']} -> {row['key']}: method label: would add label {warp_label(row['id'])}"
    if method == "remote-link":
        return f"jira: {row['id']} -> {row['key']}: method remote-link: would add remote link {warp_label(row['id'])}"
    current = _current_label(row.get("current"), row.get("attempt") or "")
    return (
        f"jira: {row['id']} -> {row['key']}: External ID currently {current} -> would set {row['value']} (method field)"
    )


def _command_for(row: dict, field: str, method: str) -> str:
    if method == "label":
        return _label_command(row)
    if method == "remote-link":
        return _remote_command(row)
    return _field_command(row, field)


def external_id_backfill(
    beam_path: Path,
    *,
    mode: str = "auto",
    only_ids: set[str] | None = None,
    force: bool = False,
    force_value: bool = False,
    create: bool = False,
    recheck: bool = False,
) -> str:
    """List or queue External ID writes for mapped tickets.

    mode auto: queue a MUST DO block when jiraWriteExternalId is true, else say nothing.
    mode dry: list the same tickets and write nothing onto the beam. The config flag is not required.
    mode must: MUST DO block. Used by --apply --yes. The config flag is not required.
    create: --create-field --yes, or jiraCreateExternalIdField. Field creation is admin and off by default.
    recheck: ignore .warp/jira-field.json and look the field up again.
    """
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    if mode == "auto" and not writes_on_scan(cfg):
        return ""
    must = mode in {"auto", "must"}
    missing = remembered_missing(beam_path, beam, cfg, recheck=recheck)
    if missing and must:
        remember_missing(beam_path, beam, cfg, save=True)
        beam = _load_beam(beam_path)
    method = fallback_mode(cfg) if missing else "field"
    create_ok = bool(create or cfg.get("jiraCreateExternalIdField"))
    queue, equal, skipped = _backfill_rows(
        beam, cfg, only_ids=only_ids, force=force, force_value=force_value, method=method
    )
    todo = beam_path.parent / WRITE_NAME
    if must and queue:
        _write_backfill_file(
            beam_path, cfg, queue, must=must, force=force, force_value=force_value, method=method, create=create_ok
        )
    elif must and todo.is_file():
        todo.unlink()
    me = Path(jira_sync.__file__).resolve()
    field = jira_sync.external_field(cfg)
    lines = []
    if missing:
        state = load_field_state(beam_path)
        checked = state.get("checkedAt") or "a previous check"
        project = project_key(cfg) or "the project"
        lines.append(
            f"jira: External ID field is missing on {project} (checked {checked}). "
            "One summary. Not one skip per ticket."
        )
        if create_ok:
            lines.extend(_ensure_lines(cfg))
        else:
            lines.extend(_admin_lines(cfg))
        if method == "label":
            lines.append(
                "jira: Falling back to label. editJiraIssue update.labels add warp:<id>. "
                "Do not set fields.labels. Do not remove other labels."
            )
        elif method == "remote-link":
            lines.append(
                f"jira: Falling back to remote link via {TOOL_REMOTE_CREATE}. globalId is warp:<id>."
            )
        else:
            lines.append("jira: jiraExternalIdFallback is none. No label or remote link is queued.")
    for row in queue:
        lines.append(_dry_line(row, field))
    for row in equal:
        lines.append(
            f"jira: {row['id']} -> {row['key']}: External ID currently {row['current']} -> already equal ({row['reason']})"
        )
    for row in skipped:
        if row["reason"] in {"no key", "field missing"}:
            continue
        if row["reason"] == "different value":
            lines.append(
                f"jira: {row['id']} -> {row['key']}: External ID currently {row['current']} -> would set {row['id']}. "
                "Not written. Pass --force-external-id to replace it."
            )
        else:
            lines.append(f"jira: {row['id']}: skipped ({row['reason']})")
    lines.append(_summary_line("would write", len(queue), len(equal), skipped))
    if not queue:
        return "\n".join(lines)
    if not must:
        lines.append("jira: dry-run. External ID is not written. Pass --apply --yes to queue the edit.")
        return "\n".join(lines)
    server = cfg.get("jiraMcp") or "atlassian"
    lines.append(
        f'jira: MUST DO write External ID. {len(queue)} ticket(s). Server "{server}". Method {method}. '
        f"Field {field}. Call {TOOL_FIELDS} once, then {TOOL_ISSUE} and {TOOL_EDITMETA} for each key in .warp/{WRITE_NAME}. "
        "No comment is posted."
    )
    if mode == "auto":
        lines.append("jira: jiraWriteExternalId is on. This config flag is the consent. --yes is not required.")
    if not missing:
        lines.append(
            "jira: If that one catalog call shows the External ID field, use the FIELD LINES. "
            "If it is missing, do not edit the field on each ticket. Use the FALLBACK LINES in this same turn."
        )
        if create_ok:
            lines.extend(_ensure_lines(cfg))
        else:
            lines.append("jira: If the field is missing, do not create it. To allow creation: /warp-jira-external-id --create-field --yes")
        lines.append("jira: FIELD LINES, only if the field exists. Skip a value that already equals the plan id. Leave a different non-empty value unless --force-external-id.")
        for row in queue:
            lines.append(_field_command(row, field))
        alt = fallback_mode(cfg)
        if alt == "none":
            lines.append("jira: FALLBACK LINES: jiraExternalIdFallback is none. If the field is missing, stop. Do not add a label.")
        else:
            lines.append(
                "jira: FALLBACK LINES, only if the field is missing. Do not run these if you ran the field lines. "
                "Labels use update.labels add, not fields.labels."
            )
            for row in queue:
                lines.append(_command_for(row, field, alt))
    else:
        lines.append("jira: Skip a value that already equals the plan id. A missing or read-only field is not a failure.")
        for row in queue:
            lines.append(_command_for(row, field, method))
    lines.append(f"jira: then python3 {me} record-external-id --beam {beam_path} --results <file>")
    lines.append("jira: It does not post a comment.")
    return "\n".join(lines)


def _editmeta_for(data: dict, key: str) -> dict | None:
    raw = data.get("editmeta")
    if isinstance(raw, dict) and isinstance(raw.get(key), dict):
        return raw[key]
    per = data.get("editmetas")
    if isinstance(per, dict) and isinstance(per.get(key), dict):
        return per[key]
    if isinstance(raw, dict) and isinstance(raw.get("fields"), dict):
        return raw
    return None


def _issue_for(data: dict, key: str) -> dict | None:
    issues = data.get("issues")
    if isinstance(issues, dict):
        found = issues.get(key)
        if isinstance(found, dict):
            return found
    for item in candidate_issues(data):
        if _issue_key(item) == key:
            return item
    return None


def _live_value(issue: dict | None, field_id: str) -> str | None:
    if not isinstance(issue, dict):
        return None
    values = jira_sync._external_values(issue, field_id)
    if values:
        return values[0]
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else None
    if isinstance(fields, dict) and field_id and field_id not in fields:
        return ""
    if "fields" in issue:
        return ""
    return None


def _issue_labels(issue: dict | None) -> list[str]:
    if not isinstance(issue, dict):
        return []
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raw = issue.get("labels")
    if raw is None and isinstance(fields, dict):
        raw = fields.get("labels")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _mark_written(jira: dict, tid: str, method: str) -> None:
    jira["externalId"] = tid
    jira["externalIdWritten"] = {"value": tid, "at": jira_sync.now(), "method": method}
    jira.pop("externalIdAttempt", None)
    if method == "field":
        jira["externalIdOnIssue"] = tid


def record_external_id(beam_path: Path, data: dict, *, force: bool = False, force_value: bool = False, save: bool = True) -> str:
    """Record editJiraIssue results. A missing or read-only field is skipped and is not a failure."""
    beam_path = beam_path.resolve()
    beam = _load_beam(beam_path)
    cfg = jira_sync.settings(beam_path, beam)
    field = discover_editable_field(data, jira_sync.external_field(cfg))
    created = data.get("createField") if isinstance(data.get("createField"), dict) else {}
    was_missing = load_field_state(beam_path).get("status") == "missing" or _attempts_say_missing(beam)
    lines = []
    if field.get("found") and field.get("id"):
        remember_present(beam_path, cfg, str(field["id"]), str(field.get("name") or field_display_name(cfg)), save=save)
        if created.get("result") == "created" or was_missing:
            lines.append(report_discovered_id(beam_path, beam, str(field["id"]), save=save))
            beam = _load_beam(beam_path)
    elif created.get("result"):
        lines.append(
            "jira: Could not create the External ID field "
            f"({created.get('error') or created.get('result')}). This is not a failure."
        )
        lines.append(
            f'jira: In Jira admin, create a Short text custom field named "{field_display_name(cfg)}", '
            f"add it to the {project_key(cfg) or 'project'} screens and field context, then /warp-jira-external-id --recheck."
        )
    if not field.get("found"):
        remember_missing(beam_path, beam, cfg, save=save, refresh=True)
        beam = _load_beam(beam_path) if save else beam
        method = fallback_mode(cfg)
        if not created.get("result"):
            if cfg.get("jiraCreateExternalIdField"):
                lines.append("jira: External ID field is missing. The transcript did not create one.")
            else:
                lines.extend(_admin_lines(cfg))
        lines.append("jira: External ID field is missing. Nothing was written to that field.")
        queue, equal, skipped = _backfill_rows(
            beam, cfg, only_ids=None, force=force, force_value=force_value, method=method if method != "none" else "none"
        )
        edits = {str(row.get("id") or ""): row for row in _edit_rows(data)}
        written_n = 0
        equal_n = len(equal)
        changed = False
        if method == "none":
            lines.append("jira: jiraExternalIdFallback is none. No label or remote link was written.")
        else:
            for tid, row in {item["id"]: item for item in queue}.items():
                ticket = (beam.get("tickets") or {}).get(tid)
                if not ticket:
                    skipped.append({"id": tid, "reason": "unknown ticket"})
                    continue
                edit = edits.get(tid) or {}
                issue = _issue_for(data, row["key"])
                edit_method = str(edit.get("method") or method)
                has_label = warp_label(tid) in _issue_labels(issue)
                result = edit.get("result")
                jira = ticket.setdefault("jira", {"status": None, "lastCommentAt": None})
                if edit_method == "label" and (result in {"updated", "already"} or has_label):
                    _mark_written(jira, tid, "label")
                    changed = True
                    if result == "updated":
                        written_n += 1
                        lines.append(f"jira: {tid} {row['key']} label {warp_label(tid)} added. Recorded.")
                    else:
                        equal_n += 1
                        lines.append(f"jira: {tid} {row['key']} label {warp_label(tid)} already equal. Recorded.")
                    continue
                if edit_method == "remote-link" and result in {"updated", "already"}:
                    _mark_written(jira, tid, "remote-link")
                    changed = True
                    if result == "updated":
                        written_n += 1
                        lines.append(f"jira: {tid} {row['key']} remote link {warp_label(tid)} added. Recorded.")
                    else:
                        equal_n += 1
                        lines.append(f"jira: {tid} {row['key']} remote link already equal. Recorded.")
                    continue
                if result in {"failed", "denied", "unavailable"}:
                    jira["externalIdAttempt"] = {
                        "at": jira_sync.now(),
                        "result": "skipped",
                        "error": edit.get("error") or result,
                        "event": "external-id",
                    }
                    skipped.append({"id": tid, "reason": result})
                    changed = True
                    lines.append(f"jira: {tid} {row['key']} skipped. {result}. Nothing was written.")
        if changed and save:
            jira_sync._save(beam_path, beam)
        lines.append(_summary_line("written" if save else "would write", written_n, equal_n, skipped))
        return "\n".join(lines)
    queue, equal, skipped = _backfill_rows(beam, cfg, only_ids=None, force=force, force_value=force_value, method="field")
    wanted = {row["id"]: row for row in queue}
    written_n = 0
    equal_n = len(equal)
    changed = False
    edits = {str(row.get("id") or ""): row for row in _edit_rows(data)}
    for tid, row in wanted.items():
        ticket = (beam.get("tickets") or {}).get(tid)
        if not ticket:
            skipped.append({"id": tid, "reason": "unknown ticket"})
            continue
        key = row["key"]
        meta = _editmeta_for(data, key)
        issue = _issue_for(data, key)
        editable = field.get("editable")
        if meta is not None:
            one = discover_editable_field({"fields": data.get("fields") or data.get("fieldCatalog"), "editmeta": meta}, jira_sync.external_field(cfg))
            editable = one.get("editable")
            if not one.get("found"):
                editable = False
        edit = edits.get(tid) or {}
        if edit.get("editable") is False or edit.get("error") in {"field missing", "read-only"}:
            editable = False
        before = _before_value(edit, issue, field.get("id") or "") if edit or issue else ""
        if not edit and issue is None and "before" not in edit:
            before = ""
        if issue is not None or "before" in edit:
            live = str(edit.get("before")) if "before" in edit and edit.get("before") is not None else _live_value(issue, field.get("id") or "")
            if live is None:
                live = before
        else:
            live = None
        jira = ticket.setdefault("jira", {"status": None, "lastCommentAt": None})
        if editable is False or (meta is None and field.get("editable") is not True and not edit):
            reason = "not editable" if field.get("found") else "field missing"
            if meta is None and not edit:
                reason = "editmeta missing"
            if edit.get("error") in {"field missing", "read-only"}:
                reason = "field missing" if edit.get("error") == "field missing" else "not editable"
            jira["externalIdAttempt"] = {"at": jira_sync.now(), "result": "skipped", "error": reason, "event": "external-id"}
            skipped.append({"id": tid, "reason": reason})
            changed = True
            lines.append(f"jira: {tid} {key} skipped. {reason}. Nothing was written.")
            continue
        shown = live if live is not None else before
        if shown and shown != tid and not force_value:
            jira["externalIdOnIssue"] = shown
            jira["externalIdAttempt"] = {"at": jira_sync.now(), "result": "skipped", "error": "different value", "event": "external-id"}
            skipped.append({"id": tid, "reason": "different value"})
            changed = True
            lines.append(f"jira: {tid} {key} before {shown} after {tid}. Not written. Pass --force-external-id to replace it.")
            continue
        result = edit.get("result")
        if shown == tid or result == "already":
            _mark_written(jira, tid, str(edit.get("method") or "field"))
            equal_n += 1
            changed = True
            lines.append(f"jira: {tid} {key} already equal. Recorded.")
            continue
        if result not in {"updated", "already"}:
            jira["externalIdAttempt"] = {
                "at": jira_sync.now(),
                "result": result or "pending",
                "error": edit.get("error") or "editJiraIssue did not return",
                "event": "external-id",
            }
            skipped.append({"id": tid, "reason": result or "not recorded"})
            changed = True
            lines.append(f"jira: {tid} {key} before {_current_label(shown)} after {tid}. Not recorded until editJiraIssue returns.")
            continue
        _mark_written(jira, tid, str(edit.get("method") or "field"))
        written_n += 1
        changed = True
        lines.append(f"jira: {tid} {key} before {_current_label(shown)} after {tid}. Recorded.")
    if changed and save:
        jira_sync._save(beam_path, beam)
    lines.append(_summary_line("written" if save else "would write", written_n, equal_n, skipped))
    return "\n".join(lines)


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
