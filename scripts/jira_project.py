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


def _prefix(raw: str) -> str | None:
    match = KEY_RE.fullmatch(str(raw or "").strip().upper())
    if not match:
        found = KEY_RE.findall(str(raw or "").upper())
        if len(found) != 1:
            return None
        return found[0][0]
    return match.group(1)


def _add(bucket: list, prefix: str | None, source: str) -> None:
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
        tid = row.get("id") or row.get("tempId") or fields.get("tempId")
        key = row.get("jiraKey") or row.get("jira") or row.get("key") or fields.get("key")
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


def plan_files(root: Path, folder: Path | None = None) -> list[Path]:
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


def gather(root: Path, folder: Path | None = None, branches: list[str] | None = None, commits: list[str] | None = None) -> dict:
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


def ensure(root: Path, *, write: bool = True, folder: Path | None = None, report_set: bool = False, branches: list[str] | None = None, commits: list[str] | None = None) -> str:
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
        if write and len(show) > 1:
            _outbox(root, "\n".join([note, *_commands(sorted(show))]))
        return "\n".join(lines)
    if write:
        lines.append(write_project(root, chosen, how, force=False))
    else:
        lines.append(f"jira: would set jiraProject to {chosen} ({how})")
    return "\n".join(lines)


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
