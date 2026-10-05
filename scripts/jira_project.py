#!/usr/bin/env python3
"""Find the Jira project key without asking the user to edit config by hand.

Scripts have no Jira credentials. This module does the offline half: read plan,
spec, and export files, branch names, and recent commit subjects, and write
jiraProject only when it is empty and one prefix is clearly the Jira project.
Plan-id prefixes such as WV are ignored when they never appear as a Jira key.

The online half is a todo for the agent. It calls getAccessibleAtlassianResources
and getVisibleJiraProjects, then `jira_sync.py project --apply`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
JIRA_FIELD_RE = re.compile(r"\bJira(?:\s*Key)?\s*[:=]\s*([A-Z][A-Z0-9]+-\d+)\b", re.I)
BRACKET_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d+)\]")
SKIP = {".git", "node_modules", "vendor", "dist", ".warp", "coverage", "__pycache__"}
TODO_NAME = "jira-project.json"
SET_COMMAND = "python3 scripts/jira_sync.py project --set"


def warning(candidates: list[str]) -> str:
    shown = ", ".join(candidates) if candidates else "none"
    return f"jiraProject not set: Jira moves are disabled until you set it (candidates: {shown})"


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _blank(value) -> bool:
    if value is None:
        return True
    return str(value).strip().strip("\"'") == ""


def config_text(root: Path) -> str:
    path = root / ".warp" / "config.yaml"
    if not path.is_file():
        return ""
    try:
        return path.read_text()
    except OSError:
        return ""


def config_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*(#.*)?$", text or "", re.M)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def _write_line(text: str, key: str, value: str, comment: str) -> str:
    line = f'{key}: "{value}"  # {comment}'
    pat = re.compile(rf"^{key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _: line, text, count=1)
    base = text.rstrip("\n")
    return (base + "\n" if base else "") + line + "\n"


def repo_name(root: Path) -> str:
    remote = _git(root, "remote", "get-url", "origin").strip()
    if remote:
        name = remote.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return root.name


def _prefix(raw: str) -> Optional[str]:
    match = KEY_RE.fullmatch(str(raw or "").strip().upper())
    if not match:
        found = KEY_RE.findall(str(raw or "").upper())
        if len(found) != 1:
            return None
        return found[0][0]
    return match.group(1)


def _add(bucket: list, prefix: Optional[str], source: str) -> None:
    if prefix and PREFIX_RE.fullmatch(prefix):
        bucket.append((prefix, source))


def _keys_in(text: str) -> list[str]:
    return [m.group(1) for m in KEY_RE.finditer(text or "")]


def _from_json(path: Path, source: str, plan: list, link: list) -> bool:
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return False
    rows = []
    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        rows = data["tickets"]
    elif isinstance(data, dict) and isinstance(data.get("issues"), list):
        rows = data["issues"]
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("projects"), list):
        for proj in data["projects"]:
            if isinstance(proj, dict):
                rows.extend(proj.get("issues") or [])
    if not rows:
        return False
    used = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        external = row.get("externalId") or fields.get("externalId")
        tid = external or row.get("id") or row.get("tempId") or fields.get("tempId")
        key = row.get("jiraKey") or row.get("jira") or row.get("key") or fields.get("key")
        if external and key and str(key).strip().upper() == str(external).strip().upper():
            key = None
        _add(plan, _prefix(str(tid or "")), source)
        key_prefix = _prefix(str(key or ""))
        if key_prefix and (not tid or str(key).strip().upper() != str(tid).strip().upper() or row.get("key") or row.get("jiraKey") or row.get("jira")):
            _add(link, key_prefix, source)
        used = True
    return used


def _from_markdown(path: Path, source: str, plan: list, link: list, mention: list) -> None:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return
    header = None
    for line in lines:
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) or c == "" for c in cells):
                continue
            low = [c.lower() for c in cells]
            if header is None and any(h in {"id", "ticket", "key"} for h in low):
                header = low
                continue
            if header and len(cells) == len(header):
                row = dict(zip(header, cells))
                _add(plan, _prefix(row.get("id") or row.get("ticket") or ""), source)
                jraw = row.get("jira key") or row.get("jirakey") or row.get("jira") or ""
                _add(link, _prefix(jraw), source)
                continue
        field = JIRA_FIELD_RE.search(line)
        linked = set()
        if field:
            pref = _prefix(field.group(1))
            _add(link, pref, source)
            if pref:
                linked.add(field.group(1).upper())
        for raw in BRACKET_RE.findall(line):
            pref = _prefix(raw)
            _add(link, pref, source)
            linked.add(raw.upper())
        ids = [m.group(0) for m in KEY_RE.finditer(line) if m.group(0).upper() not in linked]
        if not ids:
            continue
        if re.match(r"^\s*(?:[-*]|\d+\.|#{1,6})\s+", line) or re.search(r"\bafter\s+", line, re.I):
            _add(plan, ids[0].split("-", 1)[0], source)
            for extra in ids[1:]:
                if extra.upper() not in linked:
                    _add(mention, extra.split("-", 1)[0], source)
        else:
            for extra in ids:
                _add(mention, extra.split("-", 1)[0], source)


def plan_files(root: Path, folder: Optional[Path] = None) -> list[Path]:
    base = folder or root
    found = []
    if not base.is_dir():
        return found
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in SKIP for part in rel.parts):
            continue
        if path.stat().st_size > 1_000_000:
            continue
        name = path.name.lower()
        parent = path.parent.name.lower()
        if name in {"schedule.json", "cursor_plan.md", "cursor_plan_fast.md", "warp_plan.json", "warp-plan.json"}:
            found.append(path)
        elif name.endswith((".md", ".txt")) and any(bit in name for bit in ("plan", "spec", "ticket")):
            found.append(path)
        elif name.endswith(".json") and ("ticket" in name or "jira" in name or parent == "jira"):
            found.append(path)
    return found


def gather(root: Path, folder: Optional[Path] = None, branches: Optional[list[str]] = None, commits: Optional[list[str]] = None) -> dict:
    """Collect prefix hits. Pass branches/commits to skip git (tests)."""
    plan: list = []
    link: list = []
    mention: list = []
    for path in plan_files(root, folder):
        source = path.name
        if path.suffix.lower() == ".json" and _from_json(path, source, plan, link):
            continue
        if path.suffix.lower() in {".md", ".txt"}:
            _from_markdown(path, source, plan, link, mention)
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pref in _keys_in(text):
            _add(mention, pref, source)
    if branches is None:
        raw = _git(root, "branch", "-a", "--format=%(refname:short)")
        head = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        branches = [line.strip() for line in raw.splitlines() if line.strip()]
        if head and head not in branches and head != "HEAD":
            branches.append(head)
    if commits is None:
        raw = _git(root, "log", "-30", "--format=%s")
        commits = [line.strip() for line in raw.splitlines() if line.strip()]
    for name in branches or []:
        for pref in _keys_in(name.upper()):
            _add(mention, pref, "git branch")
    for subject in commits or []:
        for pref in _keys_in(subject.upper()):
            _add(mention, pref, "git commit")
    return {"plan": plan, "link": link, "mention": mention}


def choose(hits: dict) -> dict:
    """Pick one prefix, or none when the evidence is mixed or only plan ids."""
    plan_prefixes = {p for p, _ in hits.get("plan") or []}
    link = hits.get("link") or []
    mention = hits.get("mention") or []
    link_count: Counter = Counter(p for p, _ in link)
    mention_count: Counter = Counter(p for p, _ in mention)
    denied = {p for p in plan_prefixes if link_count[p] == 0}
    scores = {}
    for prefix in set(link_count) | set(mention_count):
        if prefix in denied:
            continue
        scores[prefix] = link_count[prefix] * 3 + mention_count[prefix]
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    sources: dict[str, Counter] = defaultdict(Counter)
    for prefix, source in link + mention:
        if prefix not in denied:
            sources[prefix][source] += 1
    chosen = None
    if len(ranked) == 1:
        chosen = ranked[0]
    elif len(ranked) >= 2 and scores[ranked[0]] >= 3 and scores[ranked[0]] >= 3 * scores[ranked[1]]:
        chosen = ranked[0]
    how = ""
    if chosen:
        common = sources[chosen].most_common(1)
        where = common[0][0] if common else "local files"
        kind = "Jira fields" if link_count[chosen] else "mentions"
        how = f"detected from {kind} in {where}"
    return {
        "prefix": chosen,
        "candidates": ranked,
        "how": how,
        "scores": scores,
    }


def _save_config(root: Path, text: str) -> None:
    path = root / ".warp" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")


def write_project(root: Path, prefix: str, comment: str, *, force: bool = False) -> str:
    """Set jiraProject. Refuses a non-empty value unless force. Also fills empty jiraKeyPrefixes."""
    prefix = prefix.strip().upper()
    if not PREFIX_RE.fullmatch(prefix):
        return f"jira: {prefix!r} is not a Jira project key"
    text = config_text(root)
    current = config_value(text, "jiraProject")
    if current and not force:
        return f"jira: jiraProject is {current}; left as is"
    text = _write_line(text, "jiraProject", prefix, comment)
    prefixes = config_value(text, "jiraKeyPrefixes")
    if _blank(prefixes):
        text = _write_line(text, "jiraKeyPrefixes", prefix, "detected jiraProject")
    _save_config(root, text)
    if current and force and current != prefix:
        return f"jira: jiraProject was {current}; now {prefix}"
    return f"jira: set jiraProject to {prefix} ({comment})"


def write_site(root: Path, url: str) -> str:
    url = url.strip().rstrip("/")
    text = config_text(root)
    current = config_value(text, "jiraSite")
    if current:
        return f"jira: jiraSite is {current}; left as is"
    text = _write_line(text, "jiraSite", url, "only Atlassian site")
    _save_config(root, text)
    return f"jira: set jiraSite to {url} (only Atlassian site)"


def _todo(root: Path, candidates: list[str], server: str) -> None:
    path = root / ".warp" / TODO_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "server": server or "atlassian",
        "tools": ["getAccessibleAtlassianResources", "getVisibleJiraProjects"],
        "candidates": candidates,
        "repo": repo_name(root),
        "projectName": config_value(config_text(root), "projectName"),
        "setCommand": SET_COMMAND,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _commands(candidates: list[str]) -> list[str]:
    if not candidates:
        return [f"jira: {SET_COMMAND} WAR"]
    return [f"jira: {SET_COMMAND} {key}" for key in candidates]


def project_lines(root: Path, decision: dict, server: str = "atlassian") -> str:
    """Text for an empty jiraProject: the warning, the set commands, and the agent todo."""
    candidates = list(decision.get("candidates") or [])
    _todo(root, candidates, server)
    lines = [
        warning(candidates),
        * _commands(candidates),
        "jira: PROJECT. Call getAccessibleAtlassianResources and getVisibleJiraProjects "
        f"on server {server or 'atlassian'}. Save "
        '{"resources":[{"url":"https://yoursite.atlassian.net","id":"cloud-id"}],'
        '"projects":[{"key":"WAR","name":"Warp"}]} '
        "and run python3 scripts/jira_sync.py project --apply results.json. "
        "One project, or one match to the repo or these candidates, is stored. Several are listed and not guessed.",
    ]
    return "\n".join(lines)


def _outbox(root: Path, text: str) -> None:
    path = root / ".warp" / "outbox.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(f"\n## jiraProject\n\n{text}\n")


def ensure(root: Path, *, write: bool = True, folder: Optional[Path] = None, report_set: bool = False, branches: Optional[list[str]] = None, commits: Optional[list[str]] = None) -> str:
    """Offline detection. Writes jiraProject only when it is empty and one prefix wins."""
    root = root.resolve()
    text = config_text(root)
    current = config_value(text, "jiraProject")
    if current:
        return f"jira: jiraProject is {current}; left as is" if report_set else ""
    decision = choose(gather(root, folder, branches, commits))
    server = config_value(text, "jiraMcp") or "atlassian"
    if decision["prefix"]:
        comment = decision["how"] or "detected from local files"
        if not write:
            return f"jira: would set jiraProject to {decision['prefix']} ({comment})"
        line = write_project(root, decision["prefix"], comment, force=False)
        site = config_value(config_text(root), "jiraSite")
        if _blank(site):
            _todo(root, [decision["prefix"]], server)
            line += (
                "\njira: PROJECT. jiraProject is set. If jiraSite is empty, call "
                "getAccessibleAtlassianResources and run python3 scripts/jira_sync.py project --apply results.json."
            )
        return line
    if not write:
        return warning(decision["candidates"])
    note = project_lines(root, decision, server)
    if len(decision["candidates"]) > 1:
        _outbox(root, note)
    return note


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _project_rows(data) -> list[dict]:
    rows = []
    raw = data
    if isinstance(data, dict):
        raw = data.get("projects") or data.get("values") or data.get("visible") or []
    if isinstance(raw, dict):
        raw = raw.get("values") or raw.get("projects") or []
    for row in raw or []:
        if isinstance(row, str):
            pref = row.strip().upper()
            if PREFIX_RE.fullmatch(pref):
                rows.append({"key": pref, "name": ""})
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("projectKey") or "").strip().upper()
        if PREFIX_RE.fullmatch(key):
            rows.append({"key": key, "name": str(row.get("name") or row.get("projectName") or "")})
    return rows


def _resource_urls(data) -> list[str]:
    raw = []
    if isinstance(data, dict):
        raw = data.get("resources") or data.get("sites") or data.get("values") or []
        if data.get("url") or data.get("cloudId"):
            raw = [data]
    urls = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("site") or "").strip()
        if url and url not in urls:
            urls.append(url.rstrip("/"))
    return urls


class ReplayClient:
    """Project list and sites an agent already saved. No network."""

    def __init__(self, data: dict):
        self.data = data if isinstance(data, dict) else {}

    def projects(self) -> list[dict]:
        return _project_rows(self.data)

    def sites(self) -> list[str]:
        return _resource_urls(self.data)

    def candidates(self) -> list[str]:
        raw = self.data.get("candidates") or []
        out = []
        for item in raw:
            pref = str(item).strip().upper()
            if PREFIX_RE.fullmatch(pref) and pref not in out:
                out.append(pref)
        return out


def apply_remote(root: Path, client: ReplayClient, *, write: bool = True) -> str:
    """Store one visible project and, when there is a single site, jiraSite. Never replaces a set project."""
    root = root.resolve()
    lines = []
    urls = client.sites()
    if len(urls) == 1:
        if write:
            lines.append(write_site(root, urls[0]))
        else:
            lines.append(f"jira: would set jiraSite to {urls[0]}")
    elif len(urls) > 1:
        lines.append("jira: several Atlassian sites; jiraSite left empty")
    current = config_value(config_text(root), "jiraProject")
    if current:
        lines.append(f"jira: jiraProject is {current}; left as is")
        return "\n".join(lines)
    projects = client.projects()
    keys = []
    for row in projects:
        if row["key"] not in keys:
            keys.append(row["key"])
    repo = _norm(repo_name(root))
    pname = _norm(config_value(config_text(root), "projectName"))
    saved = []
    todo = root / ".warp" / TODO_NAME
    if todo.is_file():
        try:
            saved = ReplayClient(json.loads(todo.read_text())).candidates()
        except (OSError, json.JSONDecodeError):
            saved = []
    hints = set(client.candidates() or saved)
    matches = []
    for row in projects:
        key = row["key"]
        name = _norm(row.get("name"))
        key_norm = _norm(key)
        matched = key in hints
        if repo and (key_norm == repo or name == repo):
            matched = True
        if pname and (key_norm == pname or name == pname):
            matched = True
        if matched and key not in matches:
            matches.append(key)
    chosen = None
    how = ""
    if len(keys) == 1:
        chosen, how = keys[0], "only visible Jira project"
    elif len(matches) == 1:
        chosen, how = matches[0], "matched the repo, project name, or an offline candidate"
    show = matches or keys
    if not chosen:
        note = warning(sorted(show))
        lines.append(note)
        lines.extend(_commands(sorted(show)))
        if len(show) > 1:
            lines.append(prepare_probe(root, sorted(show), write=write))
        if write and len(show) > 1:
            _outbox(root, "\n".join([note, *_commands(sorted(show))]))
        return "\n".join(lines)
    if write:
        lines.append(write_project(root, chosen, how, force=False))
    else:
        lines.append(f"jira: would set jiraProject to {chosen} ({how})")
    return "\n".join(lines)


PROBE_NAME = "jira-project-probe.json"


def collect_ids(root: Path, limit: int = 3) -> list[str]:
    """Plan ids to probe, beam tickets first, then externalId values in plan JSON."""
    ids: list[str] = []

    def add(raw) -> None:
        text = str(raw or "").strip()
        if text and text not in ids:
            ids.append(text)

    beam = root / ".warp" / "beam.json"
    if beam.is_file():
        try:
            data = json.loads(beam.read_text())
            for tid in data.get("tickets") or {}:
                add(tid)
                if len(ids) >= limit:
                    return ids
        except (OSError, json.JSONDecodeError):
            pass
    for path in plan_files(root):
        if path.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(path.read_text(errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = data if isinstance(data, list) else []
        if isinstance(data, dict):
            rows = data.get("issues") or data.get("tickets") or []
        for row in rows or []:
            if isinstance(row, dict):
                add(row.get("externalId") or row.get("id"))
            if len(ids) >= limit:
                return ids
    return ids[:limit]


def probe_queries(projects: list[str], ids: list[str], field: str = "externalId") -> list[dict]:
    """JQL for each project and the first few plan ids. External id, then the named field, then a label."""
    import jira_lookup

    rows = []
    seen = set()
    names = []
    for label in (field or "externalId", "External ID", "externalId"):
        if label and label not in names:
            names.append(label)
    for project in projects:
        for tid in ids:
            for label in names:
                clause = jira_lookup.clause_for(label)
                jql = jira_lookup.jql_equals(clause, tid, project)
                if jql in seen:
                    continue
                seen.add(jql)
                rows.append({"project": project, "id": tid, "field": label, "kind": "external", "jql": jql})
            label_jql = jira_lookup.jql_equals("labels", f"warp:{tid}", project)
            if label_jql not in seen:
                seen.add(label_jql)
                rows.append({"project": project, "id": tid, "field": "labels", "kind": "label", "jql": label_jql})
    return rows


def _project_of(row: dict) -> Optional[str]:
    raw = row.get("project")
    if raw and PREFIX_RE.fullmatch(str(raw).strip().upper()):
        return str(raw).strip().upper()
    match = re.search(r"project\s*=\s*([A-Z][A-Z0-9]+)", str(row.get("jql") or ""), re.I)
    return match.group(1).upper() if match else None


def _issue_keys(issues) -> list[str]:
    found = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get("key") or issue.get("jiraKey") or "").strip().upper()
        if KEY_RE.fullmatch(key) and key not in found:
            found.append(key)
    return found


def decide_probe(projects: list[str], searches: list) -> dict:
    """One project with a hit is chosen. Several or none are not."""
    hits: dict[str, list[dict]] = {p: [] for p in projects}
    for row in searches or []:
        if not isinstance(row, dict) or row.get("error"):
            continue
        project = _project_of(row)
        if project not in hits:
            if project:
                hits.setdefault(project, [])
            else:
                continue
        keys = _issue_keys(row.get("issues"))
        if not keys:
            continue
        tid = str(row.get("id") or "")
        shown = keys[0] if len(keys) == 1 else ", ".join(keys)
        if any(item["key"] == shown and item["id"] == tid for item in hits[project]):
            continue
        hits[project].append({"id": tid, "key": shown, "field": row.get("field")})
    matched = [p for p, rows in hits.items() if rows]
    chosen = matched[0] if len(matched) == 1 else None
    how = ""
    if chosen:
        first = hits[chosen][0]
        how = f"matched {first['field'] or 'external id'} {first['id']} on {first['key']}"
    return {"chosen": chosen, "how": how, "hits": {p: rows for p, rows in hits.items() if rows}, "matched": matched}


class ProbeClient:
    """Saved Jira searches. A row with error is a missing field. No network."""

    def __init__(self, data: dict):
        self._rows = {}
        for row in (data or {}).get("searches") or []:
            if isinstance(row, dict) and row.get("jql"):
                self._rows[row["jql"]] = row

    def search(self, jql: str) -> list:
        row = self._rows.get(jql)
        if not row:
            return []
        if row.get("error"):
            raise LookupError(str(row.get("error")))
        return row.get("issues") or []


def prepare_probe(root: Path, projects: list[str], *, write: bool = True) -> str:
    """Write the JQL an agent should run. Does not set jiraProject and does not call Jira."""
    projects = [p.strip().upper() for p in projects if PREFIX_RE.fullmatch(str(p).strip().upper())]
    ids = collect_ids(root, 3)
    field = config_value(config_text(root), "jiraExternalIdField") or "externalId"
    queries = probe_queries(projects, ids, field) if projects and ids else []
    payload = {
        "projects": projects,
        "ids": ids,
        "field": field,
        "tool": "searchJiraIssuesUsingJql",
        "queries": queries,
        "recordCommand": "python3 scripts/jira_sync.py project --record",
    }
    if write:
        path = root / ".warp" / PROBE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        todo = root / ".warp" / TODO_NAME
        try:
            current = json.loads(todo.read_text()) if todo.is_file() else {}
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current["projects"] = projects
        current["probe"] = PROBE_NAME
        todo.write_text(json.dumps(current, indent=2) + "\n")
    if not projects:
        return "jira: PROBE no projects were given."
    if not ids:
        return (
            "jira: PROBE no plan ids to match. "
            + " ".join(_commands(projects))
        )
    shown = ", ".join(f"{row['project']} {row['id']} ({row['field']})" for row in queries[:12])
    extra = f" (+{len(queries) - 12} more)" if len(queries) > 12 else ""
    return (
        f"jira: PROBE {', '.join(projects)} for {', '.join(ids)}. "
        f"Call searchJiraIssuesUsingJql for each jql in .warp/{PROBE_NAME} ({shown}{extra}). "
        "Save {\"projects\":[...],\"searches\":[{\"jql\":\"...\",\"project\":\"WAR\",\"id\":\"WV-01\",\"issues\":[{\"key\":\"WAR-1\"}]}]}. "
        f"Then python3 scripts/jira_sync.py project --record probe.json. "
        "One project with a hit is stored. Several are listed with their issue keys and a project --set command. None lists every project."
    )


def record_probe(root: Path, data: dict, *, write: bool = True) -> str:
    """Store jiraProject when exactly one probed project contains a plan id."""
    projects = []
    for raw in data.get("projects") or []:
        if isinstance(raw, dict):
            raw = raw.get("key")
        pref = str(raw or "").strip().upper()
        if PREFIX_RE.fullmatch(pref) and pref not in projects:
            projects.append(pref)
    searches = data.get("searches") or []
    for row in searches:
        if isinstance(row, dict):
            project = _project_of(row)
            if project and project not in projects:
                projects.append(project)
    decision = decide_probe(projects, searches)
    if decision["chosen"]:
        if not write:
            return f"jira: would set jiraProject to {decision['chosen']} ({decision['how']})"
        return write_project(root, decision["chosen"], decision["how"], force=False)
    lines = []
    if len(decision["matched"]) > 1:
        lines.append("jira: several projects matched an external id. Not set.")
        for project in decision["matched"]:
            pairs = ", ".join(f"{row['id']}={row['key']}" for row in decision["hits"][project])
            lines.append(f"jira: {project} matched {pairs}")
            lines.append(f"jira: {SET_COMMAND} {project}")
    else:
        lines.append("jira: no project matched an external id.")
        lines.append(warning(sorted(projects)))
        lines.extend(_commands(sorted(projects)))
    if write and len(decision["matched"]) > 1:
        _outbox(root, "\n".join(lines))
    return "\n".join(lines)


def run_probe(root: Path, client: ProbeClient, projects: list[str], ids: Optional[list[str]] = None, *, write: bool = True) -> str:
    """Ask a fake or saved client, then record. Field errors are skipped, not fatal."""
    ids = ids if ids is not None else collect_ids(root, 3)
    field = config_value(config_text(root), "jiraExternalIdField") or "externalId"
    searches = []
    for row in probe_queries(projects, ids, field):
        try:
            issues = client.search(row["jql"])
        except LookupError as exc:
            searches.append({**row, "issues": [], "error": str(exc)})
            continue
        searches.append({**row, "issues": issues})
    return record_probe(root, {"projects": projects, "searches": searches}, write=write)


def init_steps(root: Path, dry: bool) -> list[tuple[str, str]]:
    """One /warp-init step. Offline detection now; the online call is printed for the agent."""
    note = ensure(root, write=not dry, report_set=True)
    first = note.split("\n", 1)[0]
    if first.startswith("jira: jiraProject is"):
        return [("skip", first.removeprefix("jira: "))]
    if first.startswith("jira: set") or first.startswith("jira: would set"):
        status = "info" if dry or first.startswith("jira: would") else "done"
        return [(status, note.removeprefix("jira: ") if "\n" not in note else note)]
    return [("warn", note)]
