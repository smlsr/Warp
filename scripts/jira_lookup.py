"""Match a plan id to a Jira issue key. No network.

Resolution order for an unmapped ticket:
  1. An external-id style field (configured name or customfield id, then known names)
  2. A label warp:<id>
  3. A remote-link id
  4. An exact summary, which is only a proposal until someone confirms it

An ambiguous result is reported and not narrowed. A missing field is skipped.
"""

from __future__ import annotations

import re

EXTERNAL_FIELD_NAMES = (
    "External ID",
    "External Id",
    "ExternalId",
    "External Key",
    "Plan ID",
    "Ticket ID",
)
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
CONFIDENCE = {"external": "high", "label": "medium", "link": "medium", "summary": "low"}


class FieldNotFound(Exception):
    def __init__(self, field: str):
        self.field = field
        super().__init__(field)


def clause_for(token: str) -> str:
    text = str(token or "").strip().strip("\"'")
    custom = re.fullmatch(r"customfield_(\d+)", text, re.I)
    if custom:
        return f"cf[{custom.group(1)}]"
    numbered = re.fullmatch(r"cf\[(\d+)\]", text, re.I)
    if numbered:
        return f"cf[{numbered.group(1)}]"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", text):
        return text
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def jql_equals(clause: str, value: str, project: str | None) -> str:
    safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
    body = f'{clause} = "{safe}"'
    if project:
        return f"project = {project} AND {body}"
    return body


def summary_jql(summary: str, project: str | None) -> str:
    safe = str(summary).replace("\\", "\\\\").replace('"', '\\"')
    body = f'summary ~ "{safe}"'
    if project:
        return f"project = {project} AND {body}"
    return body


def _norm_name(name: str) -> str:
    return re.sub(r"[\s_]+", "", str(name or "")).casefold()


_CANDIDATE_NORMS = {_norm_name(name) for name in EXTERNAL_FIELD_NAMES}


def _issue_key(issue: dict, prefixes: list[str]) -> str | None:
    raw = issue.get("key") or issue.get("jiraKey") or ""
    key = str(raw).strip().upper()
    if not KEY_RE.match(key):
        return None
    if prefixes and key.split("-", 1)[0] not in prefixes:
        return None
    return key


def _same(a: str, b: str) -> bool:
    return str(a).strip().casefold() == str(b).strip().casefold()


def field_clause(field: dict) -> str:
    names = field.get("clauseNames") or []
    if names:
        return clause_for(str(names[0]))
    if field.get("id"):
        return clause_for(str(field["id"]))
    return clause_for(str(field.get("name") or ""))


def external_clauses(configured: str, fields: list[dict] | None) -> tuple[list[tuple[str, str]], list[str]]:
    """Clauses to query, then field-not-found notes. Configured field is first."""
    catalog = list(fields or [])
    clauses: list[tuple[str, str]] = []
    seen: set[str] = set()
    notes: list[str] = []

    def add(label: str, clause: str) -> None:
        if not clause or clause in seen:
            return
        seen.add(clause)
        clauses.append((label, clause))

    configured = str(configured or "").strip().strip("\"'") or "externalId"
    names_present = {_norm_name(field.get("name") or "") for field in catalog}
    ids_present = {str(field.get("id") or "").casefold() for field in catalog}

    def in_catalog(label: str) -> bool:
        if not catalog:
            return True
        token = label.strip().strip("\"'")
        if token.casefold() in ids_present:
            return True
        if _norm_name(token) in names_present:
            return True
        wanted = clause_for(token)
        for field in catalog:
            if field_clause(field) == wanted:
                return True
            if clause_for(str(field.get("id") or "")) == wanted:
                return True
        return False

    if in_catalog(configured):
        add(configured, clause_for(configured))
    else:
        notes.append(f"field not found: {configured}")

    if catalog:
        for field in catalog:
            name = str(field.get("name") or "")
            if _norm_name(name) in _CANDIDATE_NORMS or _norm_name(name) == _norm_name(configured):
                add(name or str(field.get("id") or "field"), field_clause(field))
        for name in EXTERNAL_FIELD_NAMES:
            if not in_catalog(name):
                note = f"field not found: {name}"
                if note not in notes:
                    notes.append(note)
    else:
        for name in EXTERNAL_FIELD_NAMES:
            add(name, clause_for(name))
    return clauses, notes


def plan_queries(tid: str, summary: str, project: str | None, configured: str) -> list[dict]:
    """JQL the agent should run, in resolution order, before field discovery narrows it."""
    rows = []
    clauses, _notes = external_clauses(configured, None)
    for label, clause in clauses:
        rows.append(
            {
                "kind": "external",
                "field": label,
                "jql": jql_equals(clause, tid, project),
                "confirm": False,
            }
        )
    rows.append(
        {
            "kind": "label",
            "field": "labels",
            "jql": jql_equals("labels", f"warp:{tid}", project),
            "confirm": False,
        }
    )
    rows.append(
        {
            "kind": "link",
            "field": "remoteLink",
            "tool": "getJiraIssueRemoteIssueLinks",
            "jql": None,
            "confirm": False,
        }
    )
    if str(summary or "").strip():
        rows.append(
            {
                "kind": "summary",
                "field": "summary",
                "jql": summary_jql(summary, project),
                "confirm": True,
            }
        )
    return rows


def _values(raw) -> list[str]:
    if raw is None or isinstance(raw, bool):
        return []
    if isinstance(raw, dict):
        found = []
        for key in ("value", "name", "id", "key", "globalId"):
            found.extend(_values(raw.get(key)))
        nested = raw.get("object")
        if isinstance(nested, dict):
            found.extend(_values(nested))
        return found
    if isinstance(raw, list):
        found = []
        for item in raw:
            found.extend(_values(item))
        return found
    text = str(raw).strip()
    return [text] if text else []


def _unique_keys(issues: list, prefixes: list[str], accept) -> list[str]:
    found = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        key = _issue_key(issue, prefixes)
        if not key:
            continue
        if accept(issue) and key not in found:
            found.append(key)
    return found


def _external_accept(issue: dict, tid: str, field_label: str):
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raws = [issue.get("externalId"), issue.get("external_id"), issue.get(field_label)]
    if fields:
        raws.extend(
            [
                fields.get(field_label),
                fields.get("externalId"),
                fields.get("External ID"),
                fields.get("externalIssueId"),
            ]
        )
    values = []
    for raw in raws:
        values.extend(_values(raw))
    if not values:
        return True
    return any(_same(value, tid) for value in values)


def _label_accept(issue: dict, tid: str) -> bool:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raw = issue.get("labels")
    if raw is None and fields:
        raw = fields.get("labels")
    if raw is None:
        return True
    wanted = f"warp:{tid}"
    return any(_same(value, wanted) or _same(value, tid) for value in _values(raw))


def _summary_text(issue: dict) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    return str(issue.get("summary") or fields.get("summary") or "").strip()


def _result(tid: str, outcome: str, **extra) -> dict:
    row = {
        "id": tid,
        "outcome": outcome,
        "key": None,
        "source": None,
        "confidence": None,
        "matches": [],
        "notes": [],
    }
    row.update(extra)
    return row


def _search(client, jql: str, notes: list[str], label: str):
    try:
        return client.search(jql) or []
    except FieldNotFound:
        note = f"field not found: {label}"
        if note not in notes:
            notes.append(note)
        return None


def resolve_ticket(tid: str, summary: str, project: str | None, configured: str, client, prefixes: list[str] | None = None) -> dict:
    """One ticket. Does not write. `client.search` raises FieldNotFound when Jira has no such field."""
    prefixes = prefixes or []
    notes: list[str] = []
    clauses, missing = external_clauses(configured, client.fields() if hasattr(client, "fields") else [])
    notes.extend(missing)
    for label, clause in clauses:
        jql = jql_equals(clause, tid, project)
        issues = _search(client, jql, notes, label)
        if issues is None:
            continue
        keys = _unique_keys(issues, prefixes, lambda issue, label=label: _external_accept(issue, tid, label))
        if len(keys) == 1:
            return _result(tid, "hit", key=keys[0], source="external", confidence="high", matches=keys, notes=notes)
        if len(keys) > 1:
            return _result(tid, "ambiguous", source="external", matches=keys, notes=notes)

    label_jql = jql_equals("labels", f"warp:{tid}", project)
    issues = _search(client, label_jql, notes, "labels")
    if issues:
        keys = _unique_keys(issues, prefixes, lambda issue: _label_accept(issue, tid))
        if len(keys) == 1:
            return _result(tid, "hit", key=keys[0], source="label", confidence="medium", matches=keys, notes=notes)
        if len(keys) > 1:
            return _result(tid, "ambiguous", source="label", matches=keys, notes=notes)

    links = client.remote_links() if hasattr(client, "remote_links") else []
    link_keys = []
    for row in links or []:
        if not isinstance(row, dict):
            continue
        key = _issue_key(row, prefixes) or _issue_key({"key": row.get("key")}, prefixes)
        ids = _values(row.get("ids"))
        for bucket in ("globalId", "id", "url"):
            ids.extend(_values(row.get(bucket)))
        if key and any(_same(value, tid) or _same(value, f"warp:{tid}") or value.rstrip("/").split("/")[-1].casefold() == tid.casefold() for value in ids):
            if key not in link_keys:
                link_keys.append(key)
    if len(link_keys) == 1:
        return _result(tid, "hit", key=link_keys[0], source="link", confidence="medium", matches=link_keys, notes=notes)
    if len(link_keys) > 1:
        return _result(tid, "ambiguous", source="link", matches=link_keys, notes=notes)

    text = str(summary or "").strip()
    if text:
        issues = _search(client, summary_jql(text, project), notes, "summary")
        if issues:
            keys = _unique_keys(issues, prefixes, lambda issue: _summary_text(issue).casefold() == text.casefold())
            if len(keys) == 1:
                return _result(tid, "proposed", key=keys[0], source="summary", confidence="low", matches=keys, notes=notes)
            if len(keys) > 1:
                return _result(tid, "ambiguous", source="summary", matches=keys, notes=notes)
    return _result(tid, "miss", notes=notes)


class ReplayClient:
    """Search results an agent already saved. Missing JQL is an empty result."""

    def __init__(self, data: dict):
        self._fields = data.get("fields") or []
        self._searches = {}
        for row in data.get("searches") or []:
            if isinstance(row, dict) and row.get("jql"):
                self._searches[row["jql"]] = row
        self._links = data.get("remoteLinks") or []

    def fields(self) -> list:
        return self._fields

    def search(self, jql: str) -> list:
        row = self._searches.get(jql)
        if not row:
            return []
        if row.get("error"):
            raise FieldNotFound(str(row.get("field") or jql))
        return row.get("issues") or []

    def remote_links(self) -> list:
        return self._links
