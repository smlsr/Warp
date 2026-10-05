#!/usr/bin/env python3
"""Print every field on one Jira issue. Warp has no Jira credentials.

An agent calls the Atlassian MCP server named jiraMcp. This script classifies
the argument, writes .warp/jira-view.json, and renders a saved transcript.

  jira_view.py WAR-1                 issue key: ask for getJiraIssue
  jira_view.py WV-01                 external id: beam and map, then JQL
  jira_view.py WV-01 --results f.json   render the transcript
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import jira_lookup
import jira_sync
from mcp_tools import (
    TOOL_FIELDS,
    TOOL_ISSUE,
    TOOL_PROJECTS,
    TOOL_REMOTE_LINKS,
    TOOL_RESOURCES,
    TOOL_SEARCH,
    TOOL_TRANSITIONS,
    TOOL_TRANSITIONS_ALT,
)

VIEW_NAME = "jira-view.json"
# Two or more leading characters, so T-1 is not a Jira key. No underscore.
VIEW_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
SECRET_RE = re.compile(r"(token|password|secret|api[-_ ]?key|authorization|credential)", re.I)
CLIP = 400
WRAP = 88
SOURCE_LABEL = {
    "key": "issue key",
    "external": "external id",
    "label": "label",
    "link": "remote link",
    "summary": "summary",
    "beam": "beam",
    "map": "map",
    "export": "export",
    "manual": "manual",
    "plan": "plan",
    "inferred": "inferred",
}
PREFERRED = (
    "summary",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "creator",
    "labels",
    "components",
    "fixVersions",
    "versions",
    "description",
    "environment",
    "parent",
    "project",
    "created",
    "updated",
    "resolution",
    "resolutiondate",
    "duedate",
)
SKIP_FIELDS = {"comment", "issuelinks", "worklog"}
STANDARD_NAMES = {
    "summary": "summary",
    "issuetype": "issue type",
    "status": "status",
    "priority": "priority",
    "assignee": "assignee",
    "reporter": "reporter",
    "creator": "creator",
    "labels": "labels",
    "components": "components",
    "fixVersions": "fix versions",
    "versions": "affects versions",
    "description": "description",
    "environment": "environment",
    "parent": "parent",
    "project": "project",
    "created": "created",
    "updated": "updated",
    "resolution": "resolution",
    "resolutiondate": "resolved",
    "duedate": "due date",
    "attachment": "attachment",
    "subtasks": "subtasks",
    "watches": "watches",
    "votes": "votes",
    "timetracking": "time tracking",
}

HELP = """
examples:
  python3 scripts/jira_view.py ?
  python3 scripts/jira_view.py WAR-1
  python3 scripts/jira_view.py WV-01
  python3 scripts/jira_view.py WV-01 --results view.json
  python3 scripts/jira_view.py WAR-1 --results view.json --comments --links --verbose
  python3 scripts/jira_view.py WAR-1 --results view.json --all --full --json

WAR-1 is an issue key when its prefix is jiraProject or jiraKeyPrefixes, or
when those are empty and the value matches PROJECT-123. WV-01 is an external
id when a project is set and WV is not that project. A key is fetched with
getJiraIssue. If that call fails, the same text is looked up as an external id.

An external id is resolved from the beam and .warp/jira-map.json first, then
the same JQL as a claim: jiraExternalIdField, then External ID and the other
external-id names, then label warp:<id>, then a remote link. The search is
limited to jiraProject. When jiraProject is empty, getVisibleJiraProjects
runs and each visible project is probed. Two matches are listed and neither
issue is printed.

The transcript is saved by the agent. This script does not call Jira.
getJiraIssue should request every field (fields ["*all"] and expand names,
or the arguments that tool actually has). customfield_NNNNN is labeled from
the names map or from getJiraProjectIssueTypesMetadata. getTransitionsForJiraIssue
(or listJiraIssueTransitions) supplies the transitions.

--comments adds the comment count and the latest comments. --links adds
issue links and remote links. Empty fields are hidden unless --all. --json
prints the rendered issue as JSON. Long text is truncated unless --full.
--verbose adds the account id on a user field. A field whose name is a token,
password, or secret is shown as <redacted>. Nothing else is masked.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
A bare help after an option that takes a value stays that value.
"""


def classify(query: str, prefixes: list[str]) -> str:
    """'key' when the argument is an issue key for a known project, else 'external'."""
    key = str(query or "").strip().upper()
    if not VIEW_KEY_RE.fullmatch(key):
        return "external"
    if not prefixes:
        return "key"
    if key.split("-", 1)[0] in prefixes:
        return "key"
    return "external"


def _norm_name(name: str) -> str:
    return re.sub(r"[\s_]+", "", str(name or "")).casefold()


_EXTERNAL_NORMS = {_norm_name(name) for name in jira_lookup.EXTERNAL_FIELD_NAMES}


def _same(a, b) -> bool:
    return str(a).strip().casefold() == str(b).strip().casefold()


def _secret(name: str) -> bool:
    return bool(SECRET_RE.search(str(name or "")))


def iso_date(text: str) -> str:
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$",
        text.strip(),
    )
    if not match:
        return text.strip()
    day, clock, tz = match.group(1), match.group(2), match.group(3) or ""
    if not clock:
        return day
    if tz in {"", "Z", "+0000", "+00:00", "-0000", "-00:00"}:
        return f"{day}T{clock}Z"
    if re.fullmatch(r"[+-]\d{4}", tz):
        tz = tz[:3] + ":" + tz[3:]
    return f"{day}T{clock}{tz}"


def flatten_adf(node) -> str:
    """Atlassian document JSON to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_adf(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        attrs = node.get("attrs") or {}
        return "@" + str(attrs.get("text") or attrs.get("id") or "")
    if kind == "emoji":
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    inner = flatten_adf(node.get("content")) if node.get("content") else ""
    if kind == "heading":
        return inner.strip() + "\n"
    if kind in {"paragraph", "blockquote", "codeBlock", "panel"}:
        return inner.strip() + "\n"
    if kind == "listItem":
        body = inner.strip()
        return f"- {body}\n" if body else ""
    return inner


def _is_adf(value: dict) -> bool:
    kind = value.get("type")
    return kind in {"doc", "paragraph", "heading", "bulletList", "orderedList", "blockquote", "codeBlock", "panel"} and (
        "content" in value or kind == "text"
    )


def _is_user(value: dict) -> bool:
    if "accountId" in value or "accountType" in value:
        return True
    return "displayName" in value and "statusCategory" not in value and value.get("type") not in {
        "doc",
        "paragraph",
        "heading",
    }


def format_user(value: dict, verbose: bool) -> str | None:
    name = str(value.get("displayName") or value.get("name") or "").strip()
    account = str(value.get("accountId") or "").strip()
    if not name and not account:
        return None
    if not name:
        return account if verbose else None
    if verbose and account:
        return f"{name} ({account})"
    return name


def format_value(value, verbose: bool):
    """One field value, or None when it is empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if DATE_RE.fullmatch(text):
            return iso_date(text)
        return text
    if isinstance(value, list):
        parts = []
        for item in value:
            part = format_value(item, verbose)
            if part:
                parts.append(part)
        return ", ".join(parts) if parts else None
    if isinstance(value, dict):
        if _is_adf(value):
            text = flatten_adf(value).strip()
            return text or None
        if _is_user(value):
            return format_user(value, verbose)
        if "watchCount" in value:
            return str(value.get("watchCount"))
        if "votes" in value and not isinstance(value.get("votes"), (dict, list)):
            return str(value.get("votes"))
        if value.get("total") is not None and ("worklogs" in value or "comments" in value):
            return str(value.get("total"))
        if value.get("filename"):
            return str(value["filename"])
        if value.get("key"):
            return str(value["key"])
        if value.get("name"):
            return str(value["name"])
        if "value" in value and isinstance(value.get("value"), (str, int, float)):
            return str(value["value"])
        bits = []
        for key in ("originalEstimate", "remainingEstimate", "timeSpent"):
            if value.get(key):
                bits.append(str(value[key]))
        if bits:
            return ", ".join(bits)
        if not value:
            return None
        return json.dumps(value, ensure_ascii=False)
    return None


def clip_text(text: str, full: bool) -> str:
    if full or len(text) <= CLIP:
        return text
    return text[:CLIP].rstrip() + "..."


def wrap_text(text: str) -> str:
    lines = []
    for para in text.split("\n"):
        if para == "":
            lines.append("")
            continue
        words = para.split(" ")
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= WRAP:
                current = current + " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return "\n".join(lines)


def field_line(label: str, value: str, full: bool) -> str:
    text = wrap_text(clip_text(value, full))
    rows = text.split("\n")
    if len(rows) == 1 and len(label) + 2 + len(rows[0]) <= 100:
        return f"{label}: {rows[0]}"
    body = "\n".join(("  " + row) if row else "" for row in rows)
    return f"{label}:\n{body}"


def catalog_names(raw) -> dict[str, str]:
    """Field id to display name from a names map or a metadata payload."""
    found: dict[str, str] = {}

    def add(fid, name) -> None:
        if fid and name and not isinstance(name, (dict, list)):
            found[str(fid)] = str(name)

    def walk(node, parent_key: str | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, parent_key)
            return
        if not isinstance(node, dict):
            return
        fid = node.get("id") or node.get("fieldId") or node.get("key")
        name = node.get("name")
        if parent_key and str(parent_key).startswith("customfield_") and name:
            add(parent_key, name)
        if fid and name:
            add(fid, name)
        for key, val in node.items():
            if key in {"schema", "clauseNames"}:
                continue
            if isinstance(val, (dict, list)):
                walk(val, key if isinstance(key, str) else parent_key)

    if isinstance(raw, dict) and raw and all(isinstance(v, str) for v in raw.values()):
        for fid, name in raw.items():
            add(fid, name)
        return found
    walk(raw)
    return found


def _load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _tickets(beam_path: Path) -> dict:
    data = _load_json(beam_path)
    if isinstance(data, dict) and isinstance(data.get("tickets"), dict):
        return data["tickets"]
    return {}


def _usable_key(tid: str, key, source: str | None, prefixes: list[str]) -> str | None:
    norm = jira_sync.normalize_key(key)
    if not norm:
        return None
    plan = jira_sync.normalize_key(str(tid))
    if plan and norm == plan and source not in {"export", "manual"}:
        return None
    if source in jira_sync.LOOKUP_SOURCES:
        return norm
    if prefixes and not jira_sync.prefix_ok(norm, prefixes):
        return None
    return norm


def local_resolve(query: str, beam_path: Path, cfg: dict) -> dict:
    """Beam and map before any JQL. One key, several keys, or none."""
    prefixes = jira_sync.prefixes_from(cfg)
    qfold = str(query).strip().casefold()
    hits = []

    def add(key, where: str, ticket: str, source: str | None) -> None:
        norm = _usable_key(ticket, key, source, prefixes)
        if not norm:
            return
        row = {"key": norm, "where": where, "ticket": str(ticket), "source": source or where}
        if row not in hits:
            hits.append(row)

    for tid, ticket in _tickets(beam_path).items():
        external = str((ticket.get("jira") or {}).get("externalId") or "").strip()
        if tid.casefold() == qfold or (external and external.casefold() == qfold):
            add(ticket.get("jiraKey"), "beam", tid, ticket.get("jiraKeySource"))
    for tid, row in jira_sync.all_meta(beam_path, cfg).items():
        if tid.casefold() == qfold:
            add(row.get("key"), "map", tid, row.get("source"))
    keys = list(dict.fromkeys(hit["key"] for hit in hits))
    if len(keys) == 1:
        chosen = hits[0]
        wheres = list(dict.fromkeys(hit["where"] for hit in hits))
        sources = list(dict.fromkeys(hit["source"] for hit in hits if hit["source"]))
        return {
            "outcome": "hit",
            "key": keys[0],
            "where": "+".join(wheres),
            "source": sources[0] if len(sources) == 1 else (chosen.get("source") or wheres[0]),
            "ticket": chosen["ticket"],
            "matches": hits,
        }
    if len(keys) > 1:
        return {"outcome": "ambiguous", "matches": hits}
    return {"outcome": "miss", "matches": []}


def _project_of_jql(jql: str) -> str | None:
    match = re.search(r"project\s*=\s*([A-Z][A-Z0-9]+)", str(jql or ""), re.I)
    return match.group(1).upper() if match else None


def _projects_from(data: dict) -> list[str]:
    found = []

    def add(raw) -> None:
        if isinstance(raw, dict):
            raw = raw.get("key") or raw.get("project")
        text = str(raw or "").strip().upper()
        if text and jira_sync.PREFIX_RE.fullmatch(text) and text not in found:
            found.append(text)

    for raw in data.get("projects") or []:
        add(raw)
    for row in data.get("searches") or []:
        if isinstance(row, dict):
            add(row.get("project"))
            add(_project_of_jql(row.get("jql") or ""))
    return found


def _known_projects(warp: Path) -> list[str]:
    data = _load_json(warp / "jira-project.json")
    if not isinstance(data, dict):
        return []
    found = []
    for raw in list(data.get("projects") or []) + list(data.get("candidates") or []):
        if isinstance(raw, dict):
            raw = raw.get("key")
        text = str(raw or "").strip().upper()
        if text and jira_sync.PREFIX_RE.fullmatch(text) and text not in found:
            found.append(text)
    return found


def search_resolve(query: str, cfg: dict, data: dict) -> dict:
    """External id, then label, then remote link. Summary is not chosen."""
    configured = jira_sync.external_field(cfg)
    project = str(cfg.get("jiraProject") or "").strip().upper()
    projects = [project] if project else _projects_from(data)
    client = jira_lookup.ReplayClient(data if isinstance(data, dict) else {})
    scopes = projects or [None]
    hits = []
    ambiguous = []
    notes = []
    proposed = []
    for scope in scopes:
        prefixes = [scope] if scope else []
        result = jira_lookup.resolve_ticket(query, "", scope, configured, client, prefixes)
        notes.extend(result.get("notes") or [])
        if result["outcome"] == "hit":
            hits.append(
                {
                    "key": result["key"],
                    "source": result["source"],
                    "project": scope,
                    "ticket": query,
                    "where": "jira",
                }
            )
        elif result["outcome"] == "ambiguous":
            for key in result.get("matches") or []:
                ambiguous.append({"key": key, "source": result.get("source"), "project": scope, "ticket": query, "where": "jira"})
        elif result["outcome"] == "proposed" and result.get("key"):
            proposed.append({"key": result["key"], "source": "summary", "project": scope})
    unique = list(dict.fromkeys(hit["key"] for hit in hits))
    extra = [row["key"] for row in ambiguous if row["key"] not in unique]
    if len(unique) == 1 and not extra:
        return {"outcome": "hit", "key": unique[0], "source": hits[0]["source"], "where": "jira", "matches": hits, "notes": notes, "proposed": proposed}
    if unique or extra:
        return {"outcome": "ambiguous", "matches": hits + ambiguous, "notes": notes, "proposed": proposed}
    return {"outcome": "miss", "matches": [], "notes": notes, "proposed": proposed}


def _error_text(raw) -> str | None:
    if not raw:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        for key in ("message", "error", "errorMessage"):
            if raw.get(key):
                return str(raw[key])
        return None
    return str(raw)


def _issue_key(issue: dict) -> str | None:
    return jira_sync.normalize_key(issue.get("key") or issue.get("jiraKey"))


def _direct_issue(query: str, data: dict) -> tuple[dict | None, str | None]:
    """Issue returned for this exact key, and an error if that call failed."""
    wanted = str(query).strip().upper()
    if not isinstance(data, dict):
        return None, None
    direct = data.get("direct") if isinstance(data.get("direct"), dict) else {}
    err = _error_text(direct.get("error") or direct.get("message"))
    issue = direct.get("issue") if isinstance(direct.get("issue"), dict) else None
    if issue and _issue_key(issue) == wanted:
        return issue, None
    if isinstance(data.get("key"), str) and isinstance(data.get("fields"), dict) and _issue_key(data) == wanted:
        return data, None
    top = data.get("issue") if isinstance(data.get("issue"), dict) else None
    if top and _issue_key(top) == wanted and not err:
        return top, None
    if err:
        return None, err
    return None, None


def _body_for(key: str, data: dict) -> dict | None:
    wanted = key.upper()
    if not isinstance(data, dict):
        return None
    candidates = []
    for raw in (data.get("issue"), (data.get("direct") or {}).get("issue") if isinstance(data.get("direct"), dict) else None):
        if isinstance(raw, dict):
            candidates.append(raw)
    bucket = data.get("issues")
    if isinstance(bucket, dict) and isinstance(bucket.get(key), dict):
        candidates.append(bucket[key])
    if isinstance(bucket, dict) and isinstance(bucket.get(wanted), dict):
        candidates.append(bucket[wanted])
    for row in data.get("searches") or []:
        if isinstance(row, dict):
            for issue in row.get("issues") or []:
                if isinstance(issue, dict):
                    candidates.append(issue)
    if isinstance(data.get("key"), str) and isinstance(data.get("fields"), dict):
        candidates.append(data)
    best = None
    best_n = -1
    for issue in candidates:
        if _issue_key(issue) != wanted:
            continue
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        score = len(fields)
        if score > best_n:
            best = issue
            best_n = score
    return best


def resolve_query(query: str, beam_path: Path, cfg: dict, data: dict | None) -> dict:
    prefixes = jira_sync.prefixes_from(cfg)
    kind = classify(query, prefixes)
    data = data or {}
    if kind == "key":
        issue, err = _direct_issue(query, data)
        if issue:
            return {
                "outcome": "hit",
                "kind": "key",
                "key": _issue_key(issue),
                "source": "key",
                "where": "jira",
                "issue": issue,
                "error": None,
                "matches": [],
            }
        # getJiraIssue failed, or it was not tried yet. Fall through to external id.
        fallback_note = err
    else:
        fallback_note = None
    local = local_resolve(query, beam_path, cfg)
    if local["outcome"] in {"hit", "ambiguous"}:
        local["kind"] = kind
        local["fallback"] = fallback_note
        if local["outcome"] == "hit" and data:
            local["issue"] = _body_for(local["key"], data)
        return local
    if not data:
        return {"outcome": "miss", "kind": kind, "key": None, "source": None, "matches": [], "fallback": fallback_note, "issue": None}
    found = search_resolve(query, cfg, data)
    found["kind"] = kind
    found["fallback"] = fallback_note
    if found["outcome"] == "hit":
        found["issue"] = _body_for(found["key"], data)
    return found


def _how(found: dict) -> str:
    source = found.get("source") or ""
    where = found.get("where") or ""
    label = SOURCE_LABEL.get(source, source or "jira")
    if where == "beam" and source and source != "beam":
        return f"beam, {label}"
    if where == "map" and source and source != "map":
        return f"map, {label}"
    if where == "beam+map":
        return f"beam and map, {label}"
    return label


def _names_for(issue: dict | None, data: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    names.update(catalog_names(data.get("fields")))
    names.update(catalog_names(data.get("fieldCatalog") or data.get("names")))
    if isinstance(issue, dict):
        names.update(catalog_names(issue.get("names")))
    return names


def _scalar_eq(value, query: str) -> bool:
    if isinstance(value, str):
        return _same(value, query)
    if isinstance(value, dict):
        for key in ("value", "name", "id"):
            if value.get(key) and _same(value.get(key), query):
                return True
        return False
    if isinstance(value, list):
        return any(_scalar_eq(item, query) for item in value)
    return False


def discover_field(fields: dict, names: dict[str, str], query: str, configured: str) -> dict | None:
    ranked = []
    seen = set()
    for fid, name in names.items():
        if fid in seen:
            continue
        value = fields.get(fid)
        name_hit = _norm_name(name) in _EXTERNAL_NORMS or _norm_name(name) == _norm_name(configured) or _same(fid, configured)
        value_hit = _scalar_eq(value, query)
        if name_hit or value_hit:
            seen.add(fid)
            ranked.append((1 if value_hit else 0, 1 if name_hit else 0, fid, name, value))
    for fid, value in fields.items():
        if fid in seen:
            continue
        if _scalar_eq(value, query) and fid not in {"summary", "description", "status"}:
            ranked.append((1, 0, fid, names.get(fid, fid), value))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    _v, _n, fid, name, value = ranked[0]
    return {"id": fid, "name": name, "value": format_value(value, False)}


def stored_lines(query: str, key: str | None, beam_path: Path, cfg: dict) -> list[str]:
    prefixes = jira_sync.prefixes_from(cfg)
    qfold = str(query).strip().casefold()
    beam_bits = []
    for tid, ticket in _tickets(beam_path).items():
        stored = _usable_key(tid, ticket.get("jiraKey"), ticket.get("jiraKeySource"), prefixes)
        external = str((ticket.get("jira") or {}).get("externalId") or "")
        related = tid.casefold() == qfold or external.casefold() == qfold or (key and stored == key)
        if related and stored:
            beam_bits.append(f"{tid} = {stored} ({ticket.get('jiraKeySource') or 'beam'})")
    map_bits = []
    for tid, row in jira_sync.all_meta(beam_path, cfg).items():
        stored = _usable_key(tid, row.get("key"), row.get("source"), prefixes)
        if not stored:
            continue
        if tid.casefold() == qfold or (key and stored == key):
            map_bits.append(f"{tid} = {stored} ({row.get('source') or 'map'})")
    lines = []
    lines.append("beam: " + ("; ".join(beam_bits) if beam_bits else "not stored"))
    lines.append("map: " + ("; ".join(map_bits) if map_bits else "not stored"))
    stored = []
    for tid, ticket in _tickets(beam_path).items():
        stored_key = _usable_key(tid, ticket.get("jiraKey"), ticket.get("jiraKeySource"), prefixes)
        external = str((ticket.get("jira") or {}).get("externalId") or "")
        related = tid.casefold() == qfold or external.casefold() == qfold or (key and stored_key == key)
        if related:
            stored.append(f"{tid}: {jira_sync.mapping_stored(ticket)}")
    lines.append("stored in Jira: " + ("; ".join(stored) if stored else "none"))
    return lines


def redact_issue(issue: dict, names: dict[str, str]) -> dict:
    copied = json.loads(json.dumps(issue))
    fields = copied.get("fields")
    if isinstance(fields, dict):
        for fid, value in list(fields.items()):
            display = names.get(str(fid), str(fid))
            if _secret(str(fid)) or _secret(display):
                fields[fid] = "<redacted>"
            else:
                fields[fid] = _redact(value, str(fid))
    return copied


def _redact(value, name: str | None = None):
    if name and _secret(name):
        return "<redacted>"
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(val, key) for key, val in value.items()}
    return value


def transition_rows(data: dict) -> list | None:
    if not isinstance(data, dict) or "transitions" not in data:
        return None
    raw = data.get("transitions")
    if isinstance(raw, dict):
        raw = raw.get("transitions") or raw.get("values") or []
    return raw if isinstance(raw, list) else []


def transition_lines(rows: list) -> list[str]:
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        dest = row.get("to") if isinstance(row.get("to"), dict) else {}
        to_name = str(dest.get("name") or "").strip()
        ident = str(row.get("id") or "").strip()
        if to_name and to_name != name:
            shown = f"{name} -> {to_name}" if name else to_name
        else:
            shown = name or to_name
        if ident and shown:
            lines.append(f"{ident}: {shown}")
        elif shown:
            lines.append(shown)
        elif ident:
            lines.append(ident)
    return lines


def _comment_rows(issue: dict | None, data: dict) -> tuple[int | None, list]:
    raw = None
    if isinstance(data, dict) and data.get("comments") is not None:
        raw = data.get("comments")
    fields = (issue or {}).get("fields") if isinstance((issue or {}).get("fields"), dict) else {}
    if raw is None:
        raw = fields.get("comment")
    if isinstance(raw, dict):
        total = raw.get("total")
        rows = raw.get("comments") or raw.get("values") or []
        return (int(total) if isinstance(total, int) else None), list(rows)
    if isinstance(raw, list):
        return len(raw), raw
    return None, []


def comment_lines(issue: dict | None, data: dict, verbose: bool, full: bool) -> list[str]:
    total, rows = _comment_rows(issue, data)
    dated = []
    for row in rows:
        if isinstance(row, dict):
            dated.append(row)
    dated.sort(key=lambda row: str(row.get("created") or row.get("updated") or ""))
    last = dated[-3:]
    count = total if total is not None else len(dated)
    lines = [f"count: {count}"]
    if not last:
        return lines
    lines.append("last:")
    for row in last:
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        who = format_user(author, verbose) if author else ""
        when = format_value(row.get("created") or row.get("updated"), verbose) or ""
        body = format_value(row.get("body"), verbose) or ""
        body = clip_text(body, full).replace("\n", " ")
        bits = [bit for bit in (when, who, body) if bit]
        lines.append("  " + " ".join(bits))
    return lines


def _link_rows(issue: dict | None, data: dict) -> tuple[list, list]:
    fields = (issue or {}).get("fields") if isinstance((issue or {}).get("fields"), dict) else {}
    issue_links = fields.get("issuelinks") if isinstance(fields.get("issuelinks"), list) else []
    remote = []
    if isinstance(data, dict):
        extra = data.get("links")
        if isinstance(extra, dict):
            if isinstance(extra.get("issuelinks"), list):
                issue_links = extra["issuelinks"]
            if isinstance(extra.get("remoteLinks"), list):
                remote = extra["remoteLinks"]
        if isinstance(data.get("remoteLinks"), list) and not remote:
            remote = data["remoteLinks"]
    return issue_links, remote


def link_lines(issue: dict | None, data: dict) -> list[str]:
    issue_links, remote = _link_rows(issue, data)
    lines = []
    for row in issue_links:
        if not isinstance(row, dict):
            continue
        kind = row.get("type") if isinstance(row.get("type"), dict) else {}
        if isinstance(row.get("outwardIssue"), dict):
            label = kind.get("outward") or kind.get("name") or "links"
            key = row["outwardIssue"].get("key")
            if key:
                lines.append(f"{label} {key}")
        if isinstance(row.get("inwardIssue"), dict):
            label = kind.get("inward") or kind.get("name") or "links"
            key = row["inwardIssue"].get("key")
            if key:
                lines.append(f"{label} {key}")
    for row in remote:
        if not isinstance(row, dict):
            continue
        # A lookup transcript row is {key, ids}, not a remote-link object.
        if "ids" in row and "object" not in row and "globalId" not in row:
            continue
        obj = row.get("object") if isinstance(row.get("object"), dict) else {}
        title = obj.get("title") or row.get("globalId") or ""
        url = obj.get("url") or ""
        bits = [str(bit) for bit in (title, url) if bit]
        if bits:
            lines.append("remote: " + " ".join(bits))
    return lines


def field_lines(issue: dict, names: dict[str, str], *, show_all: bool, verbose: bool, full: bool) -> list[str]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    order = [fid for fid in PREFERRED if fid in fields]
    rest = sorted((fid for fid in fields if fid not in PREFERRED and fid not in SKIP_FIELDS), key=lambda fid: names.get(fid, STANDARD_NAMES.get(fid, fid)).casefold())
    lines = []
    key = _issue_key(issue)
    if key:
        lines.append(f"key: {key}")
    if issue.get("id"):
        lines.append(f"id: {issue.get('id')}")
    for fid in order + rest:
        if fid in SKIP_FIELDS:
            continue
        display = names.get(fid) or STANDARD_NAMES.get(fid) or fid
        if _secret(display) or _secret(fid):
            empty = fields.get(fid) in (None, "", [], {})
            if empty and not show_all:
                continue
            shown = "null" if empty else "<redacted>"
        else:
            shown = format_value(fields.get(fid), verbose)
            if shown is None:
                if not show_all:
                    continue
                shown = "null"
        label = f"{display} ({fid})" if str(fid).startswith("customfield_") else display
        lines.append(field_line(label, shown, full))
    return lines


def _section(title: str, rows: list[str]) -> list[str]:
    return [f"== {title} ==", *rows]


def render_found(query: str, found: dict, beam_path: Path, cfg: dict, data: dict, args) -> str:
    show_all = bool(getattr(args, "show_all", False))
    verbose = bool(getattr(args, "verbose", False))
    full = bool(getattr(args, "full", False))
    as_json = bool(getattr(args, "json", False))
    want_comments = bool(getattr(args, "comments", False))
    want_links = bool(getattr(args, "links", False))
    lines = [f"query: {query}"]
    if found.get("fallback"):
        lines.append(f"getJiraIssue: {found['fallback']}")
    if found["outcome"] == "ambiguous":
        lines.append("resolved: ambiguous")
        lines.append("matches:")
        seen = set()
        for row in found.get("matches") or []:
            key = row.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            how = SOURCE_LABEL.get(row.get("source") or "", row.get("source") or "")
            project = f", project {row['project']}" if row.get("project") else ""
            where = row.get("where") or ""
            prefix = f"{where} " if where in {"beam", "map"} else ""
            lines.append(f"  {prefix}{key} ({how}{project})")
        me = Path(__file__).resolve()
        word = "two issues match" if len(seen) == 2 else f"{len(seen)} issues match"
        lines.append(f"jira: {word}. Neither is shown.")
        for key in seen:
            lines.append(f"jira: python3 {me} {key}")
        return "\n".join(_section("warp", lines))
    if found["outcome"] != "hit" or not found.get("key"):
        lines.append("resolved: none")
        note = "No issue matched this external id."
        if found.get("notes"):
            note += " " + " ".join(found["notes"][:4])
        lines.append(note)
        if found.get("proposed"):
            for row in found["proposed"]:
                lines.append(f"summary match {row['key']} is not used. Re-run with that key to print it.")
        return "\n".join(_section("warp", lines))
    key = found["key"]
    lines.append(f"resolved: {key} ({_how(found)})")
    issue = found.get("issue") if isinstance(found.get("issue"), dict) else _body_for(key, data)
    names = _names_for(issue, data)
    fields = issue.get("fields") if isinstance(issue, dict) and isinstance(issue.get("fields"), dict) else {}
    status = format_value(fields.get("status"), False) if fields else None
    if status:
        lines.append(f"status: {status}")
    discovered = discover_field(fields, names, query, jira_sync.external_field(cfg)) if fields else None
    if discovered and discovered.get("value"):
        ident = discovered["id"]
        label = discovered["name"]
        if str(ident).startswith("customfield_"):
            lines.append(f"external id field: {label} ({ident}): {discovered['value']}")
        else:
            lines.append(f"external id field: {label}: {discovered['value']}")
    configured = jira_sync.external_field(cfg)
    lines.append(f"jiraExternalIdField: {configured}")
    if discovered:
        ident = discovered["id"]
        shown = f"{discovered['name']} ({ident})" if str(ident).startswith("customfield_") else discovered["name"]
        hint = ""
        configured_known = _same(ident, configured) or _norm_name(discovered["name"]) == _norm_name(configured)
        if not configured_known:
            hint = f" Set jiraExternalIdField to {ident}."
        lines.append(f"discovered field: {shown}.{hint}" if hint else f"discovered field: {shown}")
    lines.extend(stored_lines(query, key, beam_path, cfg))
    if not fields:
        me = Path(__file__).resolve()
        lines.append(
            f"jira: key is {key}. Call {TOOL_ISSUE} with issueIdOrKey {key}, fields [\"*all\"], expand names. "
            f"Then {TOOL_TRANSITIONS}. Re-run python3 {me} {query} --results <file>."
        )
        return "\n".join(_section("warp", lines))
    if as_json:
        payload = {
            "query": query,
            "resolved": {"key": key, "source": found.get("source"), "how": _how(found)},
            "warp": lines,
            "issue": redact_issue(issue, names),
            "names": names,
        }
        rows = transition_rows(data)
        if rows is not None:
            payload["transitions"] = rows
        if want_comments:
            payload["comments"] = _redact(_comment_rows(issue, data)[1])
        if want_links:
            issue_links, remote = _link_rows(issue, data)
            payload["links"] = {"issuelinks": issue_links, "remoteLinks": remote}
        return json.dumps(payload, indent=2)
    out = _section("warp", lines)
    out.extend(_section("fields", field_lines(issue, names, show_all=show_all, verbose=verbose, full=full)))
    rows = transition_rows(data)
    if rows is None:
        out.extend(_section("transitions", [f"not in the transcript. Call {TOOL_TRANSITIONS}."]))
    else:
        shown = transition_lines(rows)
        out.extend(_section("transitions", shown or ["none"]))
    if want_comments:
        out.extend(_section("comments", comment_lines(issue, data, verbose, full)))
    if want_links:
        shown = link_lines(issue, data)
        out.extend(_section("links", shown or ["none"]))
    return "\n".join(out)


def _flag_tail(args) -> str:
    parts = []
    for name, flag in (
        ("comments", "--comments"),
        ("links", "--links"),
        ("show_all", "--all"),
        ("json", "--json"),
        ("full", "--full"),
        ("verbose", "--verbose"),
    ):
        if getattr(args, name, False):
            parts.append(flag)
    return (" " + " ".join(parts)) if parts else ""


def prepare_view(beam_path: Path, query: str, args) -> str:
    beam_path = beam_path.resolve()
    cfg = jira_sync.settings(beam_path, {})
    prefixes = jira_sync.prefixes_from(cfg)
    kind = classify(query, prefixes)
    project = str(cfg.get("jiraProject") or "").strip().upper() or None
    configured = jira_sync.external_field(cfg)
    local = local_resolve(query, beam_path, cfg)
    server = cfg.get("jiraMcp") or "atlassian"
    site = str(cfg.get("jiraSite") or "").strip()
    queries = []
    projects = [project] if project else _known_projects(beam_path.parent)
    if kind == "key" or local["outcome"] != "hit":
        scopes = projects or [None]
        for scope in scopes:
            for row in jira_lookup.plan_queries(query, "", scope, configured):
                if row["kind"] == "summary":
                    continue
                queries.append(row)
    payload = {
        "query": query,
        "classify": kind,
        "server": server,
        "jiraSite": site,
        "project": project,
        "field": configured,
        "local": {k: local.get(k) for k in ("outcome", "key", "source", "where", "ticket")},
        "fetchKey": local.get("key") if local["outcome"] == "hit" and kind != "key" else (query.strip().upper() if kind == "key" else None),
        "tryDirect": kind == "key",
        "issueTool": TOOL_ISSUE,
        "issueArgs": {"fields": ["*all"], "expand": "names"},
        "transitionsTool": TOOL_TRANSITIONS,
        "transitionsAlt": TOOL_TRANSITIONS_ALT,
        "fieldsTool": TOOL_FIELDS,
        "resourcesTool": TOOL_RESOURCES,
        "projectsTool": None if project or local["outcome"] == "hit" else TOOL_PROJECTS,
        "searchTool": TOOL_SEARCH,
        "remoteLinksTool": TOOL_REMOTE_LINKS,
        "comments": bool(getattr(args, "comments", False)),
        "links": bool(getattr(args, "links", False)),
        "queries": queries,
    }
    path = beam_path.parent / VIEW_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    me = Path(__file__).resolve()
    tail = _flag_tail(args)
    lines = []
    if kind == "key":
        lines.append(f"jira: view {query.strip().upper()} as an issue key.")
    else:
        why = f"prefix {query.strip().upper().split('-', 1)[0]} is not in jiraProject/jiraKeyPrefixes" if VIEW_KEY_RE.fullmatch(query.strip().upper()) else "it is not an issue key"
        lines.append(f"jira: view {query} as an external id ({why}).")
    if local["outcome"] == "ambiguous" and kind != "key":
        lines.append("jira: the beam and the map disagree. Neither issue is fetched.")
        for row in local["matches"]:
            lines.append(f"jira: {row['where']} {row['ticket']} = {row['key']} ({row.get('source')})")
        lines.append("jira: re-run with one issue key.")
        return "\n".join(lines)
    if local["outcome"] == "hit" and kind != "key":
        lines.append(f"jira: {query} -> {local['key']} ({_how(local)}).")
    elif kind != "key":
        lines.append("jira: not in the beam or .warp/jira-map.json.")
    cloud = f" jiraSite is {site}; pass that as cloudId." if site else f" Call {TOOL_RESOURCES} on {server} for cloudId."
    lines.append(f"jira: server {server}.{cloud} This script does not call Jira.")
    fetch = payload["fetchKey"]
    if kind == "key":
        lines.append(
            f"jira: call {TOOL_ISSUE} with issueIdOrKey {fetch}, fields [\"*all\"], expand names "
            f"(or the arguments that tool lists). Then {TOOL_TRANSITIONS} (or {TOOL_TRANSITIONS_ALT})."
        )
        lines.append(
            f"jira: if {TOOL_ISSUE} does not find {fetch}, look up {query} as an external id with the queries in .warp/{VIEW_NAME}."
        )
        if not project:
            lines.append(f"jira: jiraProject is empty. Call {TOOL_PROJECTS} and search each visible project.")
    elif fetch:
        lines.append(
            f"jira: call {TOOL_ISSUE} with issueIdOrKey {fetch}, fields [\"*all\"], expand names. "
            f"Then {TOOL_TRANSITIONS} (or {TOOL_TRANSITIONS_ALT})."
        )
    else:
        if not project:
            lines.append(f"jira: jiraProject is empty. Call {TOOL_PROJECTS} and search each visible project.")
        lines.append(f"jira: call {TOOL_FIELDS} once, then {TOOL_SEARCH} for each jql in .warp/{VIEW_NAME}.")
        lines.append(f"jira: one issue only. Then {TOOL_ISSUE} on that key with fields [\"*all\"] and expand names, then {TOOL_TRANSITIONS}.")
        lines.append("jira: two issues: list both and stop. Do not pick one.")
    if payload["comments"]:
        lines.append("jira: include comments (the comment field, or the latest comments the tool returns).")
    if payload["links"]:
        lines.append(f"jira: include issuelinks and call {TOOL_REMOTE_LINKS}.")
    lines.append(
        "jira: save {\"query\": \"...\", \"direct\": {\"issue\": null, \"error\": null}, "
        "\"projects\": [], \"fields\": [], \"searches\": [{\"jql\": \"...\", \"issues\": [{\"key\": \"WAR-1\"}]}], "
        "\"remoteLinks\": [], \"issue\": {\"key\": \"WAR-1\", \"fields\": {}, \"names\": {}}, \"transitions\": []}."
    )
    lines.append(f"jira: then python3 {me} {query} --results <file>{tail}")
    lines.append(
        f"jira: names on the issue, or {TOOL_FIELDS}, label customfield_NNNNN. "
        "The discovered field id is what to set as jiraExternalIdField."
    )
    return "\n".join(lines)


def render_view(beam_path: Path, query: str, data: dict, args) -> str:
    beam_path = beam_path.resolve()
    cfg = jira_sync.settings(beam_path, {})
    found = resolve_query(query, beam_path, cfg, data)
    return render_found(query, found, beam_path, cfg, data if isinstance(data, dict) else {}, args)


def main(argv: list[str] | None = None) -> int:
    import usage

    parser = argparse.ArgumentParser(
        description="Print every field on a Jira issue from an agent transcript",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", help="Jira issue key (WAR-1) or external id / plan id (WV-01)")
    parser.add_argument("--beam", default=".warp/beam.json", help="beam path (default .warp/beam.json)")
    parser.add_argument("--results", help="JSON transcript from getJiraIssue and the lookup tools")
    parser.add_argument("--all", dest="show_all", action="store_true", help="include empty and null fields")
    parser.add_argument("--json", action="store_true", help="print the issue as JSON")
    parser.add_argument("--full", action="store_true", help="do not truncate long text")
    parser.add_argument("--verbose", action="store_true", help="show accountId on user fields")
    parser.add_argument("--comments", action="store_true", help="include the comment count and the latest comments")
    parser.add_argument("--links", action="store_true", help="include issue links and remote links")
    args = parser.parse_args(usage.normalize_argv(argv))
    beam = Path(args.beam)
    if not beam.is_absolute():
        beam = Path.cwd() / beam
    if args.results:
        path = Path(args.results)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"jira: could not read {path}: {exc}")
            return 2
        if not isinstance(data, dict):
            print("jira: the transcript must be a JSON object")
            return 2
        print(render_view(beam, args.query, data, args))
        return 0
    print(prepare_view(beam, args.query, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
