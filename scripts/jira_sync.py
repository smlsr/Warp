#!/usr/bin/env python3
"""Keep a Jira issue in step with a ticket, and say what to comment. Fail-soft.

Warp has no Jira credentials. An agent calls the Atlassian MCP server. This
script decides the move, picks a transition from the ones Jira offers, writes
the comment text, and records what happened. It never blocks a merge or a scan.
The first claimed ticket of a run that Jira cannot resolve is released and the
run is stopped. A later miss, after one ticket was moved to In Progress, is not.

  plan            what one event should do
  pick            choose a transition id from Jira's list
  record          store a transition result
  record-comment  store a Jira or pull-request comment id
  verify          per ticket: keyed or unmapped, why, and the one command that fixes it
  catchup         print the moves still owed; --write stores an inferred key

Settings (.warp/config.yaml, else the beam copy, else the defaults below):
  jiraTransition          true
  jiraInProgressStatus    "In Progress"
  jiraQaReadyStatus       "QA Ready"
  jiraDoneStatus          "Done"
  jiraDoneOnManualMerge   true     manual merge moves to Done; false leaves QA Ready
  jiraRestoreOnRelease    false
  jiraMcp                 "atlassian"   Cursor server name, not a tool name
  jiraSite                ""            site URL used as cloudId when set

Only a ticket with a confirmed Jira issue key is touched. A plan id such as
WV-01 is not that key. Inference accepts a key only when its project prefix
matches jiraProject or jiraKeyPrefixes. A key on the plan, in the export, or
in the map file is stored on scan. Tickets still open are looked up in Jira
(.warp/jira-resolve.json): external-id field, then label warp:<id>, then a
remote link. One exact match is stored on resolve --apply, with the source
and confidence. A summary match waits for confirmation. An ambiguous match
is reported and not chosen. /warp-jira-map is only for a leftover or an
override. Pause and stop never move an issue. A missing or invalid key is
not sent to Jira.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Tool names live in mcp_tools.py. Re-exported so callers can keep using these.
import jira_lookup
from mcp_tools import (
    GITHUB_COMMENT,
    TOOL_COMMENT,
    TOOL_COMMENT_ALT,
    TOOL_FIELDS,
    TOOL_ISSUE,
    TOOL_REMOTE_LINKS,
    TOOL_RESOURCES,
    TOOL_SEARCH,
    TOOL_TRANSITION,
    TOOL_TRANSITIONS,
    TOOL_TRANSITIONS_ALT,
)

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
# A project prefix is at least two letters, so T-3 is never a Jira key.
INFER_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]+")
ACTIVE = {"claimed", "planning", "coding", "review", "bugbot_running", "fix", "awaiting_approval", "merging"}
MAP_NAME = "jira-map.json"
SEARCH_NAME = "jira-search.json"
RESOLVE_NAME = "jira-resolve.json"
CONFIRMED_SOURCES = {"plan", "export", "map", "manual", "external", "label", "link", "summary"}
# A search hit is sent even when jiraProject does not list its prefix.
LOOKUP_SOURCES = {"external", "label", "link", "summary"}
DEFAULTS = {
    "jiraTransition": True,
    "jiraInProgressStatus": "In Progress",
    "jiraQaReadyStatus": "QA Ready",
    "jiraDoneStatus": "Done",
    "jiraDoneOnManualMerge": True,
    "jiraRestoreOnRelease": False,
    "jiraMcp": "atlassian",
    "jiraSite": "",
    "jiraProject": "",
    "jiraKeyPrefixes": "",
    "jiraKeyMap": {},
    "jiraExternalIdField": "externalId",
    "jiraExternalIdFieldName": "External ID",
    "jiraWriteExternalId": False,
    "jiraCreateExternalIdField": False,
    "jiraExternalIdFallback": "label",
    "bugbotRequired": True,
    "bugbotManual": True,
    "maxFixAttempts": 3,
}
EVENTS = ["claim", "release", "qa-ready", "done"]
COMMENT_EVENTS = ["claim", "pr-opened", "qa-ready", "merged", "bugbot", "bugbot-rerun", "ci", "alarm", "blocked"]
GATED_STATUSES = {"awaiting_approval", "merging", "merged"}
DONE = {"merged", "done"}
FALSE = {"false", "no", "off", "0"}
RESULTS = {"moved", "already", "skipped", "unavailable", "no-transition", "failed", "not-found"}
OK_RESULTS = {"moved", "already", "skipped"}
TODO_NAME = "jira-todo.json"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in FALSE


def bugbot_applies(ticket: dict, cfg: Optional[dict]) -> bool:
    """True when this ticket must have a Bugbot pass before QA Ready or merge.

    bugbotRequired covers both paths. bugbotManual false turns it off for
    autoMerge false tickets only.
    """
    cfg = cfg or {}
    if not _flag(cfg.get("bugbotRequired", True), True):
        return False
    if ticket.get("autoMerge"):
        return True
    return _flag(cfg.get("bugbotManual", True), True)


def _bugbot_pass(ticket: dict) -> bool:
    return str((ticket.get("pr") or {}).get("bugbot") or "").strip().casefold() == "pass"


def _ci_green(ticket: dict) -> bool:
    return str((ticket.get("pr") or {}).get("ci") or "").strip().casefold() == "green"


def review_block(ticket: dict, cfg: Optional[dict]) -> Optional[str]:
    """Why awaiting_approval, merging, or merged must be refused. None when the gates are open."""
    pr = ticket.get("pr") or {}
    reasons = []
    if bugbot_applies(ticket, cfg) and not _bugbot_pass(ticket):
        reasons.append(f"bugbot is {pr.get('bugbot') or 'not run'}; need pass")
    if not _ci_green(ticket):
        reasons.append(f"ci is {pr.get('ci') or 'not run'}; need green")
    return "; ".join(reasons) if reasons else None


def _drop_comments(ticket: dict, events: tuple[str, ...]) -> None:
    for where in ("jira", "pr"):
        bucket = ticket.get(where)
        if not isinstance(bucket, dict):
            continue
        comments = bucket.get("comments")
        if isinstance(comments, dict):
            for event in events:
                comments.pop(event, None)


def invalidate_head(ticket: dict) -> None:
    """New commits after QA Ready. Jira stays where it is. Bugbot and CI must run again."""
    pr = ticket.setdefault("pr", {})
    ticket["status"] = "bugbot_running"
    pr["bugbot"] = None
    pr["ci"] = None
    pr["approvedAt"] = None
    pr["reviewedAt"] = None
    pr["reviewedSha"] = None
    pr["rerun"] = "new-commits"
    _drop_comments(ticket, ("qa-ready", "bugbot", "ci"))


def mark_bugbot_pass(ticket: dict) -> None:
    pr = ticket.setdefault("pr", {})
    pr["bugbotFixed"] = int(pr.get("bugbotFindings") or 0)


def mark_bugbot_fail(ticket: dict, cfg: Optional[dict], count_attempt: bool) -> str:
    cfg = cfg or {}
    cap = int(cfg.get("maxFixAttempts") or 3)
    pr = ticket.setdefault("pr", {})
    if count_attempt:
        ticket["attempts"] = int(ticket.get("attempts") or 0) + 1
    pr["bugbotFindings"] = int(pr.get("bugbotFindings") or 0) + 1
    attempt = int(ticket.get("attempts") or 0)
    if attempt >= cap:
        ticket["status"] = "alarm"
        ticket["alarm"] = "bugbot-failed"
        return f"bugbot fail; attempt {attempt}/{cap}; status alarm"
    ticket["status"] = "fix"
    return f"bugbot fail; attempt {attempt}/{cap}; status fix"


def apply_review(ticket: dict, cfg: Optional[dict], args) -> Optional[str]:
    """Record Bugbot, CI, and a new sha, then accept or refuse a final status.

    A return value that starts with 'refusing ' means the beam must not be written.
    Any other string is a note for the status line. None means a quiet update.
    """
    cfg = cfg or {}
    pr = ticket.setdefault("pr", {})
    prev = ticket.get("status")
    old_sha = pr.get("sha")
    new_sha = getattr(args, "sha", None)
    requested = getattr(args, "status", None)
    bug = getattr(args, "bugbot", None)
    failed = str(bug or "").strip().casefold() == "fail"
    invalidated = bool(
        new_sha
        and prev == "awaiting_approval"
        and new_sha != old_sha
        and requested not in {"merged", "done", "merging"}
    )
    if invalidated:
        invalidate_head(ticket)
    if new_sha:
        pr["sha"] = new_sha
    if getattr(args, "attempts", None) is not None:
        ticket["attempts"] = args.attempts
    note = "new commits, re-running Bugbot" if invalidated else None
    if bug is not None:
        pr["bugbot"] = bug
        if str(bug).strip().casefold() == "pass":
            mark_bugbot_pass(ticket)
        elif failed:
            fail_note = mark_bugbot_fail(ticket, cfg, count_attempt=getattr(args, "attempts", None) is None)
            note = f"{note}; {fail_note}" if note else fail_note
            if requested in GATED_STATUSES:
                note = f"{note}; refusing {requested}"
    if getattr(args, "ci", None) is not None:
        pr["ci"] = args.ci
    if requested and not failed:
        if requested in GATED_STATUSES:
            block = review_block(ticket, cfg)
            if block:
                return f"refusing {requested}: {block}"
        ticket["status"] = requested
    if ticket.get("status") == "awaiting_approval":
        pr.pop("rerun", None)
        pr["reviewedAt"] = pr.get("reviewedAt") or now()
        if pr.get("sha") and not pr.get("reviewedSha"):
            pr["reviewedSha"] = pr.get("sha")
    if getattr(args, "approved_by", None):
        pr["approvedBy"] = args.approved_by
    if getattr(args, "proceeded_by", None):
        pr["proceededBy"] = args.proceeded_by
    if ticket.get("status") == "merged":
        pr["mergedAt"] = pr.get("mergedAt") or now()
        method = getattr(args, "merge_method", None)
        if method:
            pr["mergeMethod"] = method
        elif not pr.get("mergeMethod"):
            pr["mergeMethod"] = "squash"
    return note


def settings(beam_path: Path, beam: Optional[dict] = None) -> dict:
    """Defaults, then the beam's config copy, then .warp/config.yaml. A missing key keeps the default."""
    out = dict(DEFAULTS)
    sources = [(beam or {}).get("config") or {}]
    cfg = beam_path.parent / "config.yaml"
    if cfg.is_file():
        text = cfg.read_text()
        found = {}
        for key in DEFAULTS:
            m = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*(#.*)?$", text, re.M)
            if m:
                found[key] = m.group(1).strip().strip("\"'")
        sources.append(found)
    for src in sources:
        for key, default in DEFAULTS.items():
            if key not in src or src[key] is None:
                continue
            v = src[key]
            if key == "jiraKeyMap":
                out[key] = parse_key_map(v)
            elif key == "jiraKeyPrefixes":
                out[key] = v
            elif key in {"jiraProject", "jiraExternalIdField"}:
                out[key] = str(v).strip().strip("\"'")
            elif isinstance(default, bool):
                out[key] = v if isinstance(v, bool) else str(v).lower() not in FALSE
            elif str(v).strip():
                out[key] = str(v).strip()
    return out


def normalize_key(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key if KEY_RE.match(key) else None


def prefixes_from(cfg: Optional[dict]) -> list[str]:
    """Project prefixes that confirm a Jira key: jiraProject, then jiraKeyPrefixes."""
    cfg = cfg or {}
    found: list[str] = []

    def add(raw) -> None:
        if raw is None or isinstance(raw, bool):
            return
        if isinstance(raw, (list, tuple)):
            for item in raw:
                add(item)
            return
        text = str(raw).strip().strip("\"'")
        if not text or text in {"{}", "[]"}:
            return
        if text.startswith("[") and text.endswith("]"):
            add(text[1:-1])
            return
        for part in re.split(r"[, ]+", text):
            prefix = part.strip().strip("\"'").upper()
            if prefix and PREFIX_RE.fullmatch(prefix) and prefix not in found:
                found.append(prefix)

    add(cfg.get("jiraProject"))
    add(cfg.get("jiraKeyPrefixes"))
    return found


def prefix_ok(key: str, prefixes: list[str]) -> bool:
    if not prefixes:
        return True
    return key.split("-", 1)[0] in prefixes


def jira_key(ticket: dict, prefixes: Optional[list[str]] = None) -> Optional[str]:
    """Issue key safe to send to Jira.

    A value equal to the plan id is not sent unless the ticket id itself came from
    a Jira export or a person forced it. A key stored from an external-id, label,
    link, or summary hit is sent even when jiraProject does not list that prefix.
    """
    key = normalize_key(ticket.get("jiraKey"))
    if not key:
        return None
    if ticket.get("jiraKeyForced"):
        return key
    source = ticket.get("jiraKeySource")
    tid = normalize_key(str(ticket.get("id") or ""))
    if tid and key == tid and source not in {"export", "manual"}:
        # Ticket id ABC-123 is the issue key when that prefix is configured.
        # A lookup result equal to the plan id is never sent.
        if source in {"inferred", "plan", "map"} and prefixes and prefix_ok(key, prefixes):
            return key
        return None
    if source in LOOKUP_SOURCES:
        return key
    if prefixes:
        return key if prefix_ok(key, prefixes) else None
    if source in CONFIRMED_SOURCES:
        return key
    return key


def infer_key(ticket: dict, prefixes: Optional[list[str]] = None) -> Optional[str]:
    """Id, summary, or branch, and only when the project prefix is configured.

    With no jiraProject and no jiraKeyPrefixes this returns nothing. WV-01 is not
    accepted just because it looks like PROJECT-NUMBER.
    """
    if not prefixes:
        return None
    candidates = []
    tid = str(ticket.get("id") or "")
    if tid:
        candidates.append(tid)
    for field in ("summary", "branch"):
        candidates.extend(INFER_RE.findall(str(ticket.get(field) or "")))
    for raw in candidates:
        key = normalize_key(raw)
        if key and prefix_ok(key, prefixes):
            return key
    return None


def parse_key_meta(value) -> dict[str, dict]:
    """Plan id to {key, source, confidence}. A plain string value is a key with no source."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"{}", "[]"}:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("map"), dict):
        value = value["map"]
    out: dict[str, dict] = {}
    for tid, raw in value.items():
        if str(tid) in {"map", "generatedAt", "fields", "searches", "remoteLinks"}:
            continue
        source = None
        confidence = None
        if isinstance(raw, dict):
            source = raw.get("source")
            confidence = raw.get("confidence")
            raw = raw.get("key") or raw.get("jiraKey")
        key = normalize_key(raw if isinstance(raw, str) else (str(raw) if raw else ""))
        if key:
            out[str(tid)] = {"key": key, "source": source, "confidence": confidence}
    return out


def parse_key_map(value) -> dict[str, str]:
    return {tid: row["key"] for tid, row in parse_key_meta(value).items()}


def read_map_file(beam_path: Path) -> dict[str, str]:
    path = beam_path.parent / MAP_NAME
    if not path.is_file():
        return {}
    try:
        return parse_key_map(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return {}


def all_meta(beam_path: Optional[Path], cfg: dict) -> dict[str, dict]:
    """Plan id to key from jiraKeyMap and .warp/jira-map.json, including prefixes that will be rejected."""
    merged = parse_key_meta(cfg.get("jiraKeyMap"))
    if beam_path is not None:
        path = beam_path.parent / MAP_NAME
        if path.is_file():
            try:
                merged.update(parse_key_meta(json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError):
                pass
    return merged


def combined_meta(beam_path: Optional[Path], cfg: dict) -> dict[str, dict]:
    merged = all_meta(beam_path, cfg)
    prefixes = prefixes_from(cfg)
    if not prefixes:
        return merged
    return {
        tid: row
        for tid, row in merged.items()
        if prefix_ok(row["key"], prefixes) or row.get("source") in LOOKUP_SOURCES
    }


def combined_map(beam_path: Optional[Path], cfg: dict) -> dict[str, str]:
    return {tid: row["key"] for tid, row in combined_meta(beam_path, cfg).items()}


def remember_map(beam_path: Path, tid: str, key: str, source: Optional[str] = None, confidence: Optional[str] = None) -> None:
    path = beam_path.parent / MAP_NAME
    current: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                current = dict(data.get("map") if isinstance(data.get("map"), dict) else data)
        except (OSError, json.JSONDecodeError):
            current = {}
    current.pop("map", None)
    current.pop("generatedAt", None)
    if source or confidence:
        current[str(tid)] = {"key": key, "source": source or "map", "confidence": confidence or "high"}
    else:
        current[str(tid)] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n")


def assign_keys(tickets, cfg: dict, beam_path: Optional[Path] = None, previous: Optional[dict] = None) -> list[str]:
    """Set jiraKey, jiraKeySource, and jiraMapping. Manual keys on `previous` survive a rescan."""
    prefixes = prefixes_from(cfg)
    key_map = combined_meta(beam_path, cfg)
    previous = previous or {}
    unmapped: list[str] = []
    for t in tickets:
        tid = str(t.get("id") or "")
        prev = previous.get(tid) or {}
        keep_source = prev.get("jiraKeySource")
        if keep_source == "manual" or prev.get("jiraKeyForced") or keep_source in LOOKUP_SOURCES:
            kept = normalize_key(prev.get("jiraKey"))
            tid_key = normalize_key(tid)
            same_as_plan = bool(kept and tid_key and kept == tid_key and keep_source not in {"export", "manual"})
            prefix_pass = bool(
                kept
                and (
                    prev.get("jiraKeyForced")
                    or not prefixes
                    or prefix_ok(kept, prefixes)
                    or keep_source in LOOKUP_SOURCES
                )
            )
            if kept and not same_as_plan and prefix_pass:
                t["jiraKey"] = kept
                t["jiraKeySource"] = keep_source or "manual"
                t["jiraKeyForced"] = bool(prev.get("jiraKeyForced"))
                t["jiraKeyConfidence"] = prev.get("jiraKeyConfidence")
                t["jiraMapping"] = "mapped"
                continue
        info = key_map.get(tid) or {}
        mapped = info.get("key")
        source_in = t.get("jiraKeySource")
        explicit = normalize_key(t.get("jiraKey")) if source_in in CONFIRMED_SOURCES else None
        chosen = None
        source = None
        confidence = None
        forced = False
        if mapped and (not prefixes or prefix_ok(mapped, prefixes)):
            chosen = mapped
            source = info.get("source") if info.get("source") in CONFIRMED_SOURCES else "map"
            confidence = info.get("confidence")
        elif explicit and (not prefixes or prefix_ok(explicit, prefixes)):
            tid_key = normalize_key(tid)
            copied_plan_id = False
            if tid_key and explicit == tid_key:
                if source_in in LOOKUP_SOURCES or source_in not in {"export", "manual", "plan", "map", "inferred"}:
                    copied_plan_id = True
                elif source_in in {"plan", "map", "inferred"} and (not prefixes or not prefix_ok(explicit, prefixes)):
                    copied_plan_id = True
            if not copied_plan_id:
                chosen, source = explicit, source_in or "plan"
                confidence = t.get("jiraKeyConfidence")
        if not chosen:
            inferred = infer_key(t, prefixes)
            if inferred:
                chosen, source, confidence = inferred, "inferred", "high"
        t.pop("jiraKeyCandidates", None)
        if chosen:
            t["jiraKey"] = chosen
            t["jiraKeySource"] = source
            t["jiraKeyForced"] = forced
            t["jiraKeyConfidence"] = confidence
            t["jiraMapping"] = "mapped"
        else:
            t["jiraKey"] = None
            t["jiraKeySource"] = None
            t["jiraKeyForced"] = False
            t["jiraKeyConfidence"] = None
            t["jiraMapping"] = "needs mapping"
            if tid:
                unmapped.append(tid)
    return unmapped


def mapping_message(ticket: dict, cfg: dict) -> str:
    tid = ticket.get("id")
    prefixes = prefixes_from(cfg)
    shown = ", ".join(prefixes) if prefixes else "not set"
    example = f"{prefixes[0]}-1" if prefixes else "WAR-1"
    stored = ticket.get("jiraKey")
    extra = ""
    if stored and not jira_key(ticket, prefixes):
        extra = f" Stored {stored} does not match and will not be sent to Jira."
    return (
        f"jira: {tid} needs mapping. Refusing to call Jira (no Jira key).{extra} "
        f"The plan id is not the issue key. "
        f"jiraProject/jiraKeyPrefixes: {shown}. "
        f"/warp-jira-map is only for this leftover: python3 {Path(__file__).resolve()} map --set {tid}={example}"
    )


def refuse_unmapped(beam_path: Path, ticket: dict, cfg: dict) -> str:
    """Outbox and Herald. Does not write a Jira todo, so nothing calls the MCP with a bad key."""
    note = mapping_message(ticket, cfg)
    _outbox(beam_path, note)
    _loud(beam_path, note, footer="Run /warp-jira-map, then /warp-jira-check. Nothing was sent to Jira.")
    _write_todo(beam_path, [])
    return note


def moves_to_done(ticket: dict, cfg: Optional[dict]) -> bool:
    """Auto-merge always closes Jira. A manual merge does too unless the flag is false."""
    if ticket.get("autoMerge"):
        return True
    return _flag((cfg or {}).get("jiraDoneOnManualMerge", True), True)


def plan(ticket: dict, event: str, cfg: dict) -> dict:
    """What should happen to the Jira issue for this event. Uses a confirmed key only."""
    prefixes = prefixes_from(cfg)
    out = {"id": ticket.get("id"), "jiraKey": jira_key(ticket, prefixes), "event": event, "action": "skip", "reason": ""}
    jira = ticket.get("jira") or {}
    auto = bool(ticket.get("autoMerge"))
    if not out["jiraKey"]:
        stored = normalize_key(ticket.get("jiraKey"))
        plan_id = normalize_key(str(ticket.get("id") or ""))
        if stored and plan_id and stored == plan_id:
            out["reason"] = f"{stored} is the plan id, not a Jira issue key"
        else:
            out["reason"] = "needs mapping: no Jira key on this ticket"
    elif not cfg["jiraTransition"]:
        out["reason"] = "jiraTransition is false"
    elif event == "claim":
        if jira.get("startedAt"):
            out["reason"] = "already moved by Warp"
        else:
            out.update(action="transition", target=cfg["jiraInProgressStatus"], kind="start")
    elif event == "release":
        prev = jira.get("previousStatus")
        if not cfg["jiraRestoreOnRelease"]:
            out["reason"] = "jiraRestoreOnRelease is false; issue left as is"
        elif not jira.get("startedAt") or not prev:
            out["reason"] = "Warp did not move this issue, nothing to restore"
        else:
            out.update(action="transition", target=prev, kind="restore")
    elif event == "qa-ready":
        if auto:
            out["reason"] = "auto-merge ticket; it goes to Done after the merge, not QA Ready"
        elif jira.get("qaReadyAt"):
            out["reason"] = "already moved to QA Ready"
        else:
            out.update(action="transition", target=cfg["jiraQaReadyStatus"], kind="qa")
    elif event == "done":
        if not moves_to_done(ticket, cfg):
            out["reason"] = (
                f"manual-path ticket; jiraDoneOnManualMerge is false, so it stays at "
                f"{cfg['jiraQaReadyStatus']} for QA"
            )
        elif jira.get("doneAt"):
            out["reason"] = "already moved to Done"
        else:
            out.update(action="transition", target=cfg["jiraDoneStatus"], kind="done")
    return out


def pick(transitions: list[dict], target: str, current: Optional[dict] = None, allow_category: bool = True, kind: str = "start") -> dict:
    """Choose one of the transitions Jira offers. Names are matched case-insensitively.

    kind: start (fall back to an in-progress category), qa (name only; a Done issue
    is never pulled back), done (fall back to the done category), restore (name only).
    Accepts Jira's shape ({id, name, to: {name, statusCategory: {key}}}) or a flat
    one ({id, name, toName, category}). `current` is {name, category}.
    """
    want = target.casefold()
    if not allow_category and kind == "start":
        kind = "restore"
    fallback = {"start": "indeterminate", "done": "done"}.get(kind)
    if current:
        cname = (current.get("name") or "").casefold()
        ccat = (current.get("category") or "").casefold()
        if cname == want:
            return {"transition": None, "result": "already", "reason": f"already {target}"}
        if kind == "start" and ccat == "indeterminate":
            return {"transition": None, "result": "already", "reason": f"already in progress ({current.get('name')})"}
        if kind == "start" and ccat == "done":
            return {"transition": None, "result": "skipped", "reason": f"issue is {current.get('name')}; not reopened"}
        if kind == "done" and ccat == "done":
            return {"transition": None, "result": "already", "reason": f"already {current.get('name')}"}
        if kind == "qa" and ccat == "done":
            return {"transition": None, "result": "skipped", "reason": f"issue is {current.get('name')}; not moved back to {target}"}

    def to_name(t):
        return ((t.get("to") or {}).get("name") or t.get("toName") or "").casefold()

    def to_cat(t):
        return (((t.get("to") or {}).get("statusCategory") or {}).get("key") or t.get("category") or "").casefold()

    for how, ok in (("name", lambda t: (t.get("name") or "").casefold() == want), ("status", lambda t: to_name(t) == want)):
        hits = [t for t in transitions if ok(t)]
        if hits:
            return {"transition": hits[0], "result": "pick", "how": how}
    if fallback:
        cats = [t for t in transitions if to_cat(t) == fallback]
        if len(cats) == 1:
            return {"transition": cats[0], "result": "pick", "how": "category"}
        if cats:
            word = "progress" if kind == "start" else "done"
            named = [t for t in cats if word in to_name(t) or word in (t.get("name") or "").casefold()]
            if named:
                return {"transition": named[0], "result": "pick", "how": "category"}
            return {"transition": None, "result": "no-transition", "reason": f"several {fallback} transitions; none is named {target}"}
    names = ", ".join(sorted({t.get("name") or to_name(t) for t in transitions})) or "none"
    return {"transition": None, "result": "no-transition", "reason": f"no transition to {target} is available (offered: {names})"}


def _load(beam_path: Path) -> dict:
    return json.loads(beam_path.read_text())


def _save(beam_path: Path, beam: dict) -> None:
    tmp = beam_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(beam, indent=2) + "\n")
    tmp.replace(beam_path)


def _journal(beam_path: Path, entry: dict) -> None:
    with (beam_path.parent / "journal.jsonl").open("a") as f:
        f.write(json.dumps({"ts": now(), **entry}) + "\n")


def _outbox(beam_path: Path, text: str) -> None:
    with (beam_path.parent / "outbox.md").open("a") as f:
        f.write(f"\n## {now()}\n\n{text}\n")


def _root(beam_path: Path) -> Path:
    beam_path = Path(beam_path)
    return beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent


def _mode(beam_path: Path) -> str:
    try:
        import provider

        return provider.resolve(_root(beam_path)).get("mode") or "local"
    except Exception:
        return "local"


def _header(root: Path, key: str) -> str:
    try:
        import herald_fmt

        return f"{herald_fmt.header(root)} / {key}"
    except Exception:
        return f"Warp | {root.name} / {key}"


def _wants_pr(ticket: dict, mode: str) -> bool:
    pr = ticket.get("pr") or {}
    if mode != "connected" or not pr.get("url") or pr.get("via") == "local":
        return False
    return True


def _comments(ticket: dict, where: str) -> dict:
    if where == "pr":
        return ((ticket.get("pr") or {}).get("comments")) or {}
    return ((ticket.get("jira") or {}).get("comments")) or {}


VERB = {
    "claim": "move to",
    "release": "move back to",
    "qa-ready": "ready for manual review and merge, so move to",
    "done": "merged, so move to",
}


def instruction(p: dict) -> str:
    me = Path(__file__).resolve()
    if p["action"] == "skip":
        return f"jira: {p['id']}: no Jira move ({p['reason']})."
    tail = "If Jira is not connected or nothing fits, record that and carry on. Nothing else is blocked."
    if p.get("event") == "done":
        tail = (
            "Pick by transition name, then by the target status name, then by one done-category transition. "
            "If none fits, record --result no-transition. That writes .warp/outbox.md and Herald posts Jira not updated. "
            "The merge stands."
        )
    elif p.get("event") == "claim":
        tail += (
            " If getJiraIssue says the issue was not found, record --result not-found. "
            "When no ticket in this run has jira.startedAt, that releases the claim and stops the run."
        )
    return (
        f"jira: {p['id']} ({p['jiraKey']}): {VERB[p['event']]} \"{p['target']}\" through the connected Jira MCP server. "
        f"Read the issue with {TOOL_ISSUE} and its transitions with {TOOL_TRANSITIONS} "
        f"(or {TOOL_TRANSITIONS_ALT} if that is the name the server lists), then run: python3 {me} pick --kind {p['kind']} --target \"{p['target']}\" "
        f"--current '<{{\"name\":..., \"category\":...}}>' --transitions '<json>'. "
        f"Call {TOOL_TRANSITION} with the id it picks (argument transition.id, or transitionId if that is the schema's field). "
        f"Then record: python3 {me} record --beam <beam> --id {p['id']} --event {p['event']} "
        f"--result moved|already|skipped|unavailable|no-transition|failed|not-found --from '<old status>' --to '<new status>'. "
        + tail
    )


def _bodies(ticket: dict, root: Path, mode: str, prefixes: Optional[list[str]] = None, cfg: Optional[dict] = None) -> dict[str, str]:
    key = jira_key(ticket, prefixes) or "unmapped"
    head = _header(root, str(key))
    pr = ticket.get("pr") or {}
    url = pr.get("url") or "none"
    sha = pr.get("sha") or "unknown"
    agent = ticket.get("agent") or "shuttle"
    branch = ticket.get("branch") or "(branch not set yet)"
    via = pr.get("via")
    where = via if via in {"local", "connected"} else ("local" if mode == "local" else "connected")
    bug = pr.get("bugbot") or ""
    evidence = pr.get("bugbotEvidence") or ""
    bug_line = f"Bugbot: {bug}." + (f" {evidence}" if evidence else "")
    alarm = ticket.get("alarm") or ticket.get("status") or "blocked"
    tid = ticket.get("id")
    who = pr.get("proceededBy") or pr.get("approvedBy")
    who_bit = f" Approved by {who}." if who else ""
    fixed = int(pr.get("bugbotFixed") or 0)
    if bugbot_applies(ticket, cfg):
        ready = f"Bugbot clean, ready for manual review. Findings fixed: {fixed}."
    else:
        ready = "Ready for manual review. Bugbot was not required."
    return {
        "claim": f"{head}\nStarted. Shuttle {agent}. Branch {branch}.",
        "pr-opened-jira": f"{head}\nPull request: {url}",
        "pr-opened-pr": f"{head}\nTicket {tid}. Jira {key}.",
        "qa-ready": f"{head}\n{ready}\nReview and merge, or reply warp:proceed {tid}.",
        "merged": f"{head}\nMerged ({where}). PR {url}. sha {sha}.{who_bit}",
        "bugbot": f"{head}\n{bug_line}",
        "bugbot-rerun": f"{head}\nnew commits, re-running Bugbot.",
        "ci": f"{head}\nCI: {pr.get('ci')}.",
        "alarm": f"{head}\nBlocked ({alarm}). Needs a person. warp:retry {tid}.",
        "blocked": f"{head}\nBlocked ({alarm}). Needs a person. warp:retry {tid}.",
    }


def _comment_action(ticket: dict, where: str, event: str, body: str) -> Optional[dict]:
    if event in _comments(ticket, where):
        return None
    return {"type": "comment", "where": where, "event": event, "body": body}


def event_for(ticket: dict, prev: str, new: str) -> Optional[str]:
    if new == prev:
        return None
    if new == "claimed" and prev not in ACTIVE:
        return "claim"
    if new == "queued" and prev in ACTIVE:
        return "release"
    if new == "awaiting_approval" and not ticket.get("autoMerge"):
        return "qa-ready"
    if new in DONE and prev not in DONE:
        return "done"
    return None


def actions_for(ticket: dict, before: dict, cfg: dict, mode: str, root: Path) -> list[dict]:
    """Actions owed by this one beam update. Already-recorded comments are left out."""
    prefixes = prefixes_from(cfg)
    if not cfg.get("jiraTransition", True) or not jira_key(ticket, prefixes):
        return []
    prev = before.get("status")
    new = ticket.get("status")
    event = event_for(ticket, prev, new) if prev != new else None
    actions: list[dict] = []
    if event:
        p = plan(ticket, event, cfg)
        if p["action"] == "transition":
            actions.append({"type": "transition", "plan": p})
    bodies = _bodies(ticket, root, mode, prefixes, cfg)
    pr = ticket.get("pr") or {}

    def add(where: str, ev: str, body: str) -> None:
        if where == "pr" and not _wants_pr(ticket, mode):
            return
        found = _comment_action(ticket, where, ev, body)
        if found:
            actions.append(found)

    if event == "claim":
        add("jira", "claim", bodies["claim"])
    if pr.get("url") and pr.get("url") != before.get("pr_url"):
        add("jira", "pr-opened", bodies["pr-opened-jira"])
        add("pr", "pr-opened", bodies["pr-opened-pr"])
    if event == "qa-ready":
        add("jira", "qa-ready", bodies["qa-ready"])
        add("pr", "qa-ready", bodies["qa-ready"])
    if pr.get("rerun") == "new-commits":
        add("jira", "bugbot-rerun", bodies["bugbot-rerun"])
        add("pr", "bugbot-rerun", bodies["bugbot-rerun"])
    if event == "done":
        add("jira", "merged", bodies["merged"])
        add("pr", "merged", bodies["merged"])
    if pr.get("bugbot") and pr.get("bugbot") != before.get("bugbot"):
        add("jira", "bugbot", bodies["bugbot"])
        add("pr", "bugbot", bodies["bugbot"])
    if pr.get("ci") and pr.get("ci") != before.get("ci"):
        add("jira", "ci", bodies["ci"])
        add("pr", "ci", bodies["ci"])
    if new in {"alarm", "blocked"} and new != prev:
        add("jira", new, bodies[new])
    elif ticket.get("alarm") and ticket.get("alarm") != before.get("alarm"):
        add("jira", "alarm", bodies["alarm"])
    return actions


def _todo_payload(ticket: dict, actions: list[dict], cfg: dict, mode: str) -> dict:
    site = cfg.get("jiraSite") or ""
    jira = ticket.get("jira") or {}
    key = jira_key(ticket, prefixes_from(cfg))
    stored_cloud = str(jira.get("cloudId") or "").strip()
    if stored_cloud:
        cloud = f"Use stored cloudId {stored_cloud}. Every Jira call needs cloudId and issue key {key}."
    else:
        cloud = (
            f"Call {TOOL_RESOURCES} on server {cfg.get('jiraMcp') or 'atlassian'}. "
            + (f"jiraSite is set; you may pass {site} as cloudId. " if site else "jiraSite is empty; do not guess a site. ")
            + f"Every Jira call needs cloudId and issue key {key}. Then record it with resolve --ticket {ticket.get('id')} --key {key} --cloud-id <cloudId>."
        )
    return {
        "id": ticket.get("id"),
        "jiraKey": key,
        "issueId": jira.get("id") or None,
        "server": cfg.get("jiraMcp") or "atlassian",
        "jiraSite": site,
        "mode": mode,
        "cloudId": cloud,
        "tools": {
            "resources": TOOL_RESOURCES,
            "issue": TOOL_ISSUE,
            "transitions": [TOOL_TRANSITIONS, TOOL_TRANSITIONS_ALT],
            "transition": TOOL_TRANSITION,
            "transitionArg": "id from pick, passed as transition.id or as transitionId, matching the tool schema",
            "comment": [TOOL_COMMENT, TOOL_COMMENT_ALT],
            "commentArg": "commentBody",
        },
        "prComment": {
            "github": f"{GITHUB_COMMENT} on the pull request number, or `gh pr comment <url> --body`",
            "bitbucket": "the pull request comment tool on bitbucketMcp",
        },
        "actions": actions,
    }


def _write_todo(beam_path: Path, tickets: list[dict]) -> None:
    path = beam_path.parent / TODO_NAME
    path.write_text(json.dumps({"generatedAt": now(), "tickets": tickets}, indent=2) + "\n")


def render(ticket: dict, actions: list[dict], cfg: dict) -> str:
    if not actions:
        return ""
    key = jira_key(ticket, prefixes_from(cfg))
    if not key:
        stored = ticket.get("jiraKey")
        why = f"Stored {stored} is the plan id. " if stored else "No Jira key is stored. "
        return (
            f"jira: {ticket.get('id')}: no transition. {why}"
            "The claim lookup result was not recorded. "
            f"python3 {Path(__file__).resolve()} resolve --ticket {ticket.get('id')} --key <WAR-1> --issue-id <id> --cloud-id <cloudId>"
        )
    server = cfg.get("jiraMcp") or "atlassian"
    site = cfg.get("jiraSite") or ""
    jira = ticket.get("jira") or {}
    stored_cloud = str(jira.get("cloudId") or "").strip()
    if stored_cloud:
        site_bit = f" Stored cloudId is {stored_cloud}. Pass that value."
    elif site:
        site_bit = f" jiraSite is {site!r}; you may pass that URL as cloudId."
    else:
        site_bit = " jiraSite is empty; do not guess a site."
    me = Path(__file__).resolve()
    lines = [
        f'jira: MUST DO {ticket.get("id")} ({key}). Server "{server}" is the jiraMcp name in config (a Cursor MCP server, not a tool name).',
        f"jira: issue key {key}."
        + (f" issue id {jira.get('id')}." if jira.get("id") else "")
        + f" cloudId: call {TOOL_RESOURCES} on that server.{site_bit} Every Jira call needs cloudId and issue key {key}. Do not pass {ticket.get('id')}.",
        (
            f"jira: tools: {TOOL_ISSUE}; {TOOL_TRANSITIONS} (or {TOOL_TRANSITIONS_ALT} if that is the name listed); "
            f"{TOOL_TRANSITION}; {TOOL_COMMENT} with commentBody (or {TOOL_COMMENT_ALT} if that is the name listed)."
        ),
    ]
    for a in actions:
        if a["type"] == "transition":
            lines.append(instruction(a["plan"]))
        else:
            where = "Jira" if a["where"] == "jira" else "the pull request"
            lines.append(f"jira: comment on {where} event={a['event']} (do not post again if this event is already recorded):")
            lines.append(a["body"])
            if a["where"] == "pr":
                lines.append(
                    f"jira: pull-request comment, connected mode only. GitHub: {GITHUB_COMMENT} on the pull request number, "
                    "or `gh pr comment <url> --body`. Bitbucket: the pull request comment tool on bitbucketMcp. "
                    "Local mode posts no pull-request comment."
                )
            lines.append(
                f"jira: then python3 {me} record-comment --beam <beam> --id {ticket.get('id')} "
                f"--where {a['where']} --event {a['event']} --comment-id <id>"
            )
    if not any(a["type"] == "transition" for a in actions):
        lines.append("Nothing else is blocked.")
    return "\n".join(lines)


def _snapshot(prev, ticket: dict) -> dict:
    if isinstance(prev, dict):
        return prev
    pr = ticket.get("pr") or {}
    return {
        "status": prev,
        "pr_url": pr.get("url"),
        "bugbot": pr.get("bugbot"),
        "ci": pr.get("ci"),
        "alarm": ticket.get("alarm"),
        "branch": ticket.get("branch"),
        "agent": ticket.get("agent"),
        "sha": pr.get("sha"),
    }


def _just_merged(before, ticket: dict) -> bool:
    prev = before.get("status") if isinstance(before, dict) else before
    return ticket.get("status") in DONE and prev not in DONE


def _cleared_dependents(beam: dict, tid: str) -> list[str]:
    """Tickets that listed tid and whose deps are now all terminal."""
    terminal = {"merged", "done", "skipped"}
    tickets = beam.get("tickets") or {}
    out = []
    for other in tickets.values():
        deps = other.get("deps") or []
        if tid not in deps:
            continue
        if other.get("status") not in {"queued", "blocked"}:
            continue
        unmet = [d for d in deps if (tickets.get(d) or {}).get("status") not in terminal]
        if not unmet:
            out.append(str(other.get("id")))
    return sorted(out)


def _ready_ids(beam: dict) -> list[str]:
    try:
        import beam as beam_mod

        return [str(t.get("id")) for t in beam_mod.ready(beam)]
    except Exception:
        return []


def post_merge_text(beam_path: Path, beam: dict, ticket: dict, cfg: dict, mode: str, rendered: str = "") -> str:
    """One block for the turn that recorded the merge. Jira text is included when present."""
    tid = ticket.get("id")
    pr = ticket.get("pr") or {}
    jira = ticket.get("jira") or {}
    locks = ticket.get("locks") or []
    if not isinstance(locks, list):
        locks = [str(locks)]
    if ticket.get("status") in ACTIVE:
        lock_line = "locks: still held because the status is still active."
    else:
        names = ", ".join(str(x) for x in locks) if locks else "none"
        lock_line = f"locks: released ({names})."
    cleared = _cleared_dependents(beam, str(tid))
    ready_ids = [i for i in _ready_ids(beam) if i in set(cleared)]
    target = cfg.get("jiraDoneStatus") or "Done"
    sha = pr.get("sha") or "unknown"
    if moves_to_done(ticket, cfg):
        if jira.get("doneAt"):
            jira_line = f"jira: Done already recorded ({target})."
        else:
            jira_line = f"jira: move to {target}. QA Ready was the wait before approval."
        reply = f"slack reply: Merged {tid} sha {sha}. Jira status {target}."
    else:
        qa = cfg.get("jiraQaReadyStatus") or "QA Ready"
        jira_line = f"jira: left at {qa}. jiraDoneOnManualMerge is false, so QA sets Done."
        reply = f"slack reply: Merged {tid} sha {sha}. Jira stays at {qa}."
    try:
        import beam as beam_mod

        beam_py = Path(beam_mod.__file__).resolve()
    except Exception:
        beam_py = Path(__file__).resolve().parent / "beam.py"
    status_py = Path(__file__).resolve().parent / "status_post.py"
    scan_py = Path(__file__).resolve().parent / "scan.py"
    lines = [
        f"post-merge MUST DO {tid}.",
        (
            f"beam: status {ticket.get('status')} sha {sha} "
            f"mergedAt {pr.get('mergedAt') or 'unknown'} via {pr.get('via') or mode} "
            f"method {pr.get('mergeMethod') or 'squash'} "
            f"approvedBy {pr.get('approvedBy') or 'unknown'} "
            f"proceededBy {pr.get('proceededBy') or 'unknown'}."
        ),
        lock_line,
        "unblocked: " + (", ".join(cleared) if cleared else "(none)") + ".",
        "ready: " + (", ".join(ready_ids) if ready_ids else "(none)") + ".",
        jira_line,
    ]
    if rendered.strip():
        lines.append(rendered.strip())
    lines.append(f"board: python3 {beam_py} board --beam {beam_path}")
    lines.append(f"status: python3 {scan_py} status --beam {beam_path}")
    lines.append(f"slack: python3 {status_py} --beam {beam_path} --out {beam_path.parent / 'status-post.json'}")
    lines.append("slack: post that payload in the Warp channel.")
    lines.append(reply)
    if moves_to_done(ticket, cfg) and not jira.get("doneAt"):
        lines.append(
            f"jira: merged-but-not-done {tid}. "
            f"python3 {Path(__file__).resolve()} catchup --beam {beam_path} --id {tid}"
        )
    return "\n".join(lines)


def _emit_merge(beam_path: Path, beam: dict, ticket: dict, before, cfg: dict, mode: str, text: str) -> None:
    if not _just_merged(before, ticket):
        if text:
            print(text)
        return
    try:
        print(post_merge_text(beam_path, beam, ticket, cfg, mode, text))
    except Exception as e:
        if text:
            print(text)
        print(f"post-merge: state note skipped ({e})")


def on_set(beam_path: Path, beam: dict, ticket: dict, prev, new: Optional[str] = None) -> None:
    """Called by beam.py set. Prints what the agent must do. Never raises.

    `prev` is a snapshot dict from before the edit, or a status string.
    """
    try:
        before = _snapshot(prev, ticket)
        root = _root(beam_path)
        try:
            import jira_project

            if not str((settings(beam_path, beam).get("jiraProject") or "")).strip():
                note = jira_project.ensure(root, write=True)
                if note:
                    print(note)
        except Exception:
            pass
        cfg = settings(beam_path, beam)
        mode = _mode(beam_path)
        root = _root(beam_path)
        prev_status = before.get("status")
        new_status = ticket.get("status")
        event = event_for(ticket, prev_status, new_status) if prev_status != new_status else None
        if event == "release":
            p = plan(ticket, event, cfg)
            if p["action"] == "skip" and str(p["reason"]).startswith("jiraRestoreOnRelease"):
                _write_todo(beam_path, [])
                return
        watched = (
            (ticket.get("pr") or {}).get("url") != before.get("pr_url")
            or (ticket.get("pr") or {}).get("bugbot") != before.get("bugbot")
            or (ticket.get("pr") or {}).get("ci") != before.get("ci")
            or ticket.get("alarm") != before.get("alarm")
            or new_status != prev_status
        )
        prefixes = prefixes_from(cfg)
        if not jira_key(ticket, prefixes):
            printed = ""
            if watched and cfg.get("jiraTransition", True):
                printed = prepare_resolve(beam_path, only_ids=[str(ticket.get("id") or "")])
            _write_todo(beam_path, [])
            _emit_merge(beam_path, beam, ticket, before, cfg, mode, printed)
            return
        if not cfg["jiraTransition"]:
            printed = instruction(plan(ticket, event or "claim", cfg)) if watched else ""
            _write_todo(beam_path, [])
            _emit_merge(beam_path, beam, ticket, before, cfg, mode, printed)
            return
        actions = actions_for(ticket, before, cfg, mode, root)
        if not actions:
            _write_todo(beam_path, [])
            _emit_merge(beam_path, beam, ticket, before, cfg, mode, "")
            return
        payload = _todo_payload(ticket, actions, cfg, mode)
        _write_todo(beam_path, [payload])
        _emit_merge(beam_path, beam, ticket, before, cfg, mode, render(ticket, actions, cfg))
        _journal(beam_path, {"type": "jira-intent", "id": ticket.get("id"), "actions": [a.get("event") or a.get("plan", {}).get("event") for a in actions]})
    except Exception as e:  # a Jira problem must not break a status change
        print(f"jira: skipped ({e})")


def _jira_moves_enabled(cfg: dict) -> bool:
    return _flag(cfg.get("jiraTransition", True), True)


def _moved_to_in_progress(beam: dict) -> bool:
    """True when any ticket in this run was linked and moved to In Progress.

    That marker is jira.startedAt, written when a claim transition is recorded
    as moved. No startedAt means this claim is the first ticket of the run.
    """
    for ticket in (beam.get("tickets") or {}).values():
        if (ticket.get("jira") or {}).get("startedAt"):
            return True
    return False


def _issue_not_found(result: str, error: Optional[str], detail: Optional[str]) -> bool:
    """Jira said the issue is missing. A missing custom field is not that."""
    if result in {"not-found", "not_found"}:
        return True
    text = " ".join(part for part in (error, detail) if part).casefold()
    if not text or "field not found" in text:
        return False
    return "not found" in text or "does not exist" in text


def _should_stop_for_unlinked(beam: dict, cfg: dict, ticket: dict) -> bool:
    """First claimed ticket, Jira enabled, and nothing in this run is In Progress yet."""
    if not _jira_moves_enabled(cfg):
        return False
    import beam as beam_mod

    if (ticket.get("status") or "") not in beam_mod.ACTIVE:
        return False
    return not _moved_to_in_progress(beam)


def _refresh_views(beam_path: Path) -> None:
    import beam as beam_mod

    data = beam_mod.load_json(beam_path)
    data["metrics"] = beam_mod.metrics(data)
    beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    beam_mod.refresh_outputs(beam_path, data)


def _release_unlinked_claim(beam_path: Path, tid: str) -> None:
    """Back to queued. Clear the claim's agent, branch, and In Progress clocks."""
    import beam as beam_mod

    data = _load(beam_path)
    ticket = data["tickets"][tid]
    prev = ticket.get("status")
    ticket["status"] = "queued"
    ticket["agent"] = None
    ticket["branch"] = None
    jira = ticket.setdefault("jira", {})
    jira.pop("startedAt", None)
    jira.pop("previousStatus", None)
    ticket["updatedAt"] = now()
    if prev != "queued":
        beam_mod.append_ticket_event(beam_path, ticket, {"type": "status", "from": prev, "to": "queued"})
    data["metrics"] = beam_mod.metrics(data)
    _save(beam_path, data)
    _journal(beam_path, {"type": "set", "id": tid, "from": prev, "to": "queued", "agent": None, "reason": "jira-unlinked"})
    _write_todo(beam_path, [])


def unlinked_sentence(pair: str) -> str:
    """The one Herald/Slack sentence for a run stopped because Jira is not linked."""
    return (
        f"Run stopped because Jira issues are not linked ({pair}). "
        "Fix jiraProject, /warp-jira-match, or /warp-jira-external-id, then /warp-resume."
    )


def stop_unlinked_run(beam_path: Path, tid: str, key_label: str) -> str:
    """Release tid and stop the same way as /warp-stop. One Herald message."""
    _release_unlinked_claim(beam_path, tid)
    pair = f"{tid} / {key_label}"
    reason = f"tickets are not linked to Jira ({pair})"
    sentence = unlinked_sentence(pair)
    import scan

    scan.set_run(beam_path, "stopped", reason, announce_report=False)
    _refresh_views(beam_path)
    _outbox(beam_path, sentence)
    root = _root(beam_path)
    try:
        import herald_fmt
        import notify

        msg = herald_fmt.message(root, "Run stopped", intro=sentence)
        notify.report(notify.build("jira-unlinked", root, msg=msg))
    except Exception as e:
        print(f"herald: not posted ({e})")
    return (
        f"jira: STOP {reason}. {tid} returned to queued "
        "(agent, branch, and in-progress timestamps cleared). "
        "Run stopped. Do not implement. Do not open a pull request.\n"
        + sentence
    )


def _loud(beam_path: Path, note: str, footer: str = "Run /warp-jira-check. The ticket was not stopped.") -> None:
    """Outbox is already written. Also ask Herald to post the same failure."""
    root = _root(beam_path)
    try:
        import herald_fmt
        import notify

        msg = herald_fmt.message(
            root,
            "Jira not updated",
            intro=note,
            footer=footer,
        )
        notify.report(notify.build("jira-failed", root, msg=msg))
    except Exception as e:
        print(f"herald: not posted ({e})")


def record(
    beam_path: Path,
    tid: str,
    event: str,
    result: str,
    frm: Optional[str],
    to: Optional[str],
    detail: Optional[str],
    error: Optional[str] = None,
) -> str:
    beam = _load(beam_path)
    t = beam["tickets"].get(tid)
    if not t:
        return f"jira: unknown ticket {tid}; nothing recorded"
    jira = t.setdefault("jira", {"status": None, "lastCommentAt": None})
    jira["lastSync"] = {"event": event, "result": result, "at": now(), "detail": detail}
    failure = error or (detail if result not in OK_RESULTS else None)
    jira["lastAttempt"] = {
        "event": event,
        "result": result,
        "at": jira["lastSync"]["at"],
        "from": frm,
        "to": to,
        "error": failure,
        "detail": detail,
    }
    marker = {"qa-ready": "qaReadyAt", "done": "doneAt"}.get(event)
    if result == "moved":
        if event == "claim":
            jira["startedAt"] = now()
            jira["previousStatus"] = frm
        elif event == "release":
            jira.pop("startedAt", None)
            jira.pop("previousStatus", None)
        jira["status"] = to
    elif result in {"already", "skipped"} and to:
        jira["status"] = to
    if marker and result in {"moved", "already", "skipped"}:
        jira[marker] = now()
    _save(beam_path, beam)
    _journal(beam_path, {"type": "jira", "id": tid, "event": event, "result": result, "from": frm, "to": to, "detail": detail})
    if result in OK_RESULTS:
        return f"jira: {tid} {result}" + (f" ({to})" if to else "")
    if (
        event == "claim"
        and _issue_not_found(result, error, detail)
        and _should_stop_for_unlinked(beam, settings(beam_path, beam), t)
    ):
        label = str(t.get("jiraKey") or "unresolved")
        return stop_unlinked_run(beam_path, tid, label)
    key = t.get("jiraKey")
    why = {
        "unavailable": "Jira is not connected",
        "no-transition": "no matching transition was available",
        "failed": "the Jira call failed",
        "not-found": "the Jira issue was not found",
    }.get(result, result)
    note = f"Jira status not updated for {tid} ({key}): {why}." + (f" {error or detail}" if (error or detail) else "")
    if event != "release":
        default = {"claim": "In Progress", "qa-ready": "QA Ready", "done": "Done"}.get(event, "In Progress")
        note += f" Move {key} to \"{to or default}\" by hand if you want it to match."
        if event == "done":
            note += " The merge stands; Warp retries on the next tick."
    _outbox(beam_path, note)
    _loud(beam_path, note)
    return f"jira: {note} Saved to .warp/outbox.md. The claim was not affected."


def record_resolved(
    beam_path: Path,
    tid: str,
    key: str,
    issue_id: Optional[str] = None,
    cloud_id: Optional[str] = None,
    status: Optional[str] = None,
    source: str = "external",
    confidence: Optional[str] = None,
) -> str:
    """Write a lookup hit onto the beam and the map file. Does not call Jira.

    The issue key must be the Jira key (WAR-1), not the plan id (WV-01).
    After it is stored, an active ticket gets the transition todo for that key only.
    """
    beam = _load(beam_path)
    t = beam["tickets"].get(tid)
    if not t:
        return f"jira: unknown ticket {tid}; nothing recorded"
    if t.get("jiraKeySource") == "manual" or t.get("jiraKeyForced"):
        kept = jira_key(t, prefixes_from(settings(beam_path, beam))) or t.get("jiraKey")
        return f"jira: {tid} has a manual key ({kept}); left as is"
    norm = normalize_key(key)
    if not norm:
        return f"jira: {key!r} is not a Jira issue key (PROJECT-123). Nothing was stored."
    plan_id = normalize_key(str(tid))
    if plan_id and norm == plan_id:
        return (
            f"jira: {norm} is the plan id, not a Jira issue key. Nothing was stored. "
            "Pass the issue key from the search (WAR-1), not the external id."
        )
    origin = source if source in CONFIRMED_SOURCES else "external"
    if confidence not in {"high", "medium", "low"}:
        confidence = "high" if origin == "external" else ("medium" if origin in {"label", "link"} else "low")
    t["jiraKey"] = norm
    t["jiraKeySource"] = origin
    t["jiraKeyConfidence"] = confidence
    t["jiraKeyForced"] = False
    t["jiraMapping"] = "mapped"
    t.pop("jiraKeyCandidates", None)
    jira = t.setdefault("jira", {"status": None, "lastCommentAt": None})
    jira["externalId"] = str(tid)
    if issue_id:
        jira["id"] = str(issue_id)
    if cloud_id:
        jira["cloudId"] = str(cloud_id).strip()
    if status:
        jira["status"] = status
    remember_map(beam_path, tid, norm, source=origin, confidence=confidence)
    _save(beam_path, beam)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    lines = [f"jira: {tid} -> {norm} ({origin}, {confidence}). Stored on the beam and in .warp/jira-map.json."]
    if jira.get("id"):
        lines.append(f"jira: issue id {jira['id']}")
    if jira.get("cloudId"):
        lines.append(f"jira: cloudId {jira['cloudId']}")
    if prefixes and not prefix_ok(norm, prefixes):
        lines.append(
            f"jira: {norm} is not in jiraProject/jiraKeyPrefixes ({', '.join(prefixes)}). "
            "The lookup key is stored and will be used for transitions."
        )
    mode = _mode(beam_path)
    root = _root(beam_path)
    if (t.get("status") or "queued") not in {"queued", "skipped"} and jira_key(t, prefixes):
        row = diagnose(t, cfg, mode)
        actions = _actions_from_missing(t, row["missingItems"], cfg, mode, root)
        if actions:
            _write_todo(beam_path, [_todo_payload(t, actions, cfg, mode)])
            lines.append(render(t, actions, cfg))
        else:
            _write_todo(beam_path, [])
            lines.append(f"jira: {tid}: nothing missing")
    else:
        lines.append("jira: key stored. No transition is due while the ticket is queued.")
    lines.append(f"jira: transitionJiraIssue and comments must use {norm}. Do not pass {tid}.")
    import jira_match

    write_hint = jira_match.hint_for_resolve(cfg)
    if write_hint:
        lines.append(write_hint)
    extra = jira_match.external_id_backfill(beam_path, mode="auto", only_ids={tid})
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def record_comment(beam_path: Path, tid: str, where: str, event: str, comment_id: Optional[str]) -> str:
    beam = _load(beam_path)
    t = beam["tickets"].get(tid)
    if not t:
        return f"jira: unknown ticket {tid}; nothing recorded"
    if where == "jira":
        bucket = t.setdefault("jira", {"status": None, "lastCommentAt": None})
    else:
        bucket = t.setdefault("pr", {})
    comments = bucket.setdefault("comments", {})
    if event in comments:
        return f"jira: {tid} comment {where}:{event} already recorded ({comments[event].get('id')})"
    comments[event] = {"at": now(), "id": comment_id or "noted"}
    if where == "jira":
        bucket["lastCommentAt"] = comments[event]["at"]
    _save(beam_path, beam)
    _journal(beam_path, {"type": "jira-comment", "id": tid, "where": where, "event": event, "commentId": comments[event]["id"]})
    return f"jira: {tid} comment {where}:{event} recorded"


def _item_recorded(ticket: dict, item: dict) -> bool:
    jira = ticket.get("jira") or {}
    if item["type"] == "transition":
        field = {"claim": "startedAt", "qa-ready": "qaReadyAt", "done": "doneAt"}.get(item["event"])
        return bool(field and jira.get(field))
    where = item["where"]
    bucket = jira if where == "jira" else (ticket.get("pr") or {})
    return item["event"] in (bucket.get("comments") or {})


def expected_items(ticket: dict, cfg: dict, mode: str) -> tuple[list[dict], str]:
    """What the current beam status should already have recorded. Not a history replay."""
    status = ticket.get("status") or "queued"
    auto = bool(ticket.get("autoMerge"))
    prefixes = prefixes_from(cfg)
    key = jira_key(ticket, prefixes) or infer_key(ticket, prefixes)
    if not key:
        return [], mapping_message(ticket, cfg)
    if not cfg["jiraTransition"]:
        return [], "jiraTransition is false; Warp will not move this issue"
    if status in {"queued", "skipped"}:
        return [], "ticket is not active; nothing is due"
    pr = ticket.get("pr") or {}
    items: list[dict] = []
    note = ""
    if status in DONE:
        note = (
            "Already merged. Catch-up requests the closing move only, not a backwards move to In Progress, "
            "and it does not repeat claim or pull-request-opened comments."
        )
        if moves_to_done(ticket, cfg):
            items.append({"type": "transition", "event": "done", "target": cfg["jiraDoneStatus"]})
            if not auto:
                note += " Manual merge moves Jira to Done. QA Ready was only the wait before approval."
        else:
            items.append({"type": "transition", "event": "qa-ready", "target": cfg["jiraQaReadyStatus"]})
            note += " jiraDoneOnManualMerge is false, so catch-up leaves the issue at QA Ready and still posts the merged comment."
        items.append({"type": "comment", "where": "jira", "event": "merged"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "merged"})
        return items, note
    items.append({"type": "transition", "event": "claim", "target": cfg["jiraInProgressStatus"]})
    items.append({"type": "comment", "where": "jira", "event": "claim"})
    if pr.get("url"):
        items.append({"type": "comment", "where": "jira", "event": "pr-opened"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "pr-opened"})
    if status == "awaiting_approval" and not auto:
        items.append({"type": "transition", "event": "qa-ready", "target": cfg["jiraQaReadyStatus"]})
        items.append({"type": "comment", "where": "jira", "event": "qa-ready"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "qa-ready"})
    if pr.get("rerun") == "new-commits":
        items.append({"type": "comment", "where": "jira", "event": "bugbot-rerun"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "bugbot-rerun"})
    if pr.get("bugbot"):
        items.append({"type": "comment", "where": "jira", "event": "bugbot"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "bugbot"})
    if pr.get("ci"):
        items.append({"type": "comment", "where": "jira", "event": "ci"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "ci"})
    if status in {"alarm", "blocked"} or ticket.get("alarm"):
        ev = status if status in {"alarm", "blocked"} else "alarm"
        items.append({"type": "comment", "where": "jira", "event": ev})
    return items, note


def _label(item: dict) -> str:
    if item["type"] == "transition":
        return f"transition {item['event']} -> {item['target']}"
    return f"{item['where']} comment {item['event']}"


def diagnose(ticket: dict, cfg: dict, mode: str) -> dict:
    jira = ticket.get("jira") or {}
    pr = ticket.get("pr") or {}
    items, note = expected_items(ticket, cfg, mode)
    missing = [item for item in items if not _item_recorded(ticket, item)]
    prefixes = prefixes_from(cfg)
    explicit = jira_key(ticket, prefixes)
    inferred = None if explicit else infer_key(ticket, prefixes)
    return {
        "id": ticket.get("id"),
        "jiraKey": explicit,
        "inferredKey": inferred,
        "needsMapping": not explicit,
        "status": ticket.get("status"),
        "autoMerge": bool(ticket.get("autoMerge")),
        "mode": mode,
        "recorded": {
            "startedAt": jira.get("startedAt"),
            "previousStatus": jira.get("previousStatus"),
            "qaReadyAt": jira.get("qaReadyAt"),
            "doneAt": jira.get("doneAt"),
            "lastSync": jira.get("lastSync"),
            "jiraComments": {k: v.get("id") for k, v in (jira.get("comments") or {}).items()},
            "prComments": {k: v.get("id") for k, v in (pr.get("comments") or {}).items()},
            "bugbot": pr.get("bugbot"),
            "ci": pr.get("ci"),
            "bugbotFixed": pr.get("bugbotFixed") or 0,
        },
        "should": [_label(i) for i in items],
        "missing": [_label(i) for i in missing],
        "missingItems": missing,
        "note": note,
    }


def _actions_from_missing(ticket: dict, missing: list[dict], cfg: dict, mode: str, root: Path) -> list[dict]:
    """Turn diagnose gaps into the same actions on_set prints, without historical claim comments on a merged ticket."""
    bodies = _bodies(ticket, root, mode, prefixes_from(cfg), cfg)
    actions = []
    # plan() needs the explicit key. Catch-up may have just stamped it.
    for item in missing:
        if item["type"] == "transition":
            p = plan(ticket, item["event"], cfg)
            if p["action"] == "transition":
                actions.append({"type": "transition", "plan": p})
            continue
        body_key = item["event"]
        if item["event"] == "pr-opened":
            body_key = "pr-opened-jira" if item["where"] == "jira" else "pr-opened-pr"
        body = bodies.get(body_key) or bodies.get(item["event"]) or ""
        actions.append({"type": "comment", "where": item["where"], "event": item["event"], "body": body})
    return actions


def format_diagnosis(row: dict, beam_path: Optional[Path] = None) -> str:
    rec = row["recorded"]
    jira_comments = ", ".join(f"{k}={v}" for k, v in rec["jiraComments"].items()) or "(none)"
    pr_comments = ", ".join(f"{k}={v}" for k, v in rec["prComments"].items()) or "(none)"
    key = row["jiraKey"] or row["inferredKey"] or "(none)"
    inferred = ""
    if row["inferredKey"] and not row["jiraKey"]:
        inferred = f" (inferred {row['inferredKey']}, not stored yet; catchup --write stores it)"
    lines = [
        f"== {row['id']} ==",
        f"jiraKey: {key}{inferred}",
        "jiraMapping: needs mapping" if row.get("needsMapping") else "jiraMapping: mapped",
        f"beam: {row['status']}  autoMerge: {row['autoMerge']}  mode: {row['mode']}",
        f"bugbot: {rec.get('bugbot') or '(none)'}  ci: {rec.get('ci') or '(none)'}  findings fixed: {rec.get('bugbotFixed') or 0}",
        f"recorded: startedAt={rec['startedAt'] or '(none)'} qaReadyAt={rec['qaReadyAt'] or '(none)'} doneAt={rec['doneAt'] or '(none)'}",
        f"comments jira: {jira_comments}",
        f"comments pr: {pr_comments}",
        "should: " + ("; ".join(row["should"]) or "(nothing)"),
        "missing: " + ("; ".join(row["missing"]) or "(nothing)"),
    ]
    if rec.get("lastSync"):
        lines.append(f"lastSync: {rec['lastSync'].get('event')} {rec['lastSync'].get('result')}")
    wants_done = any(item.startswith("transition done") for item in row["should"])
    if row.get("status") in DONE and not rec.get("doneAt") and wants_done:
        beam_bit = str(beam_path) if beam_path else "<beam>"
        lines.append("merged-but-not-done: beam status is merged and jira.doneAt is empty.")
        lines.append(
            f"command: python3 {Path(__file__).resolve()} catchup --beam {beam_bit} --id {row['id']}"
        )
    elif row.get("status") in DONE and not rec.get("doneAt"):
        lines.append("merged-and-left-at-qa: jiraDoneOnManualMerge is false. Jira stays at QA Ready.")
    if row["note"]:
        lines.append(f"note: {row['note']}")
    return "\n".join(lines)


def _script() -> str:
    return f"python3 {Path(__file__).resolve()}"


def unmapped_reason(ticket: dict, cfg: dict, beam_path: Path) -> tuple[str, str]:
    """Why this ticket has no confirmed key, and the one command that addresses it."""
    me = _script()
    tid = str(ticket.get("id") or "")
    prefixes = prefixes_from(cfg)
    stored = normalize_key(ticket.get("jiraKey"))
    accepted = combined_meta(beam_path, cfg).get(tid) or {}
    raw = all_meta(beam_path, cfg).get(tid) or {}
    mapped = accepted.get("key")
    raw_key = raw.get("key")
    field = external_field(cfg)
    mcp = cfg.get("jiraMcp") or "atlassian"
    candidates = ticket.get("jiraKeyCandidates") or []
    if not isinstance(candidates, list):
        candidates = []
    plan_id = normalize_key(tid)
    stale_plan_id = bool(
        stored
        and plan_id
        and stored == plan_id
        and ticket.get("jiraKeySource") not in CONFIRMED_SOURCES
        and not ticket.get("jiraKeyForced")
    )
    if not prefixes:
        if mapped:
            return (
                f"jiraProject is empty. Map file has {mapped}, and it stays unconfirmed until the project is set",
                f"{me} project --list",
            )
        if stale_plan_id:
            return (
                f"jiraProject is empty. Stored {stored} is the plan id, not a Jira key",
                f"{me} project --list",
            )
        return (
            "jiraProject is empty and jiraKeyPrefixes is empty, so a plan id cannot be confirmed",
            f"{me} project --list",
        )
    if mapped and not jira_key(ticket, prefixes):
        return (
            f"map file has {mapped} but the beam has no confirmed key",
            f"{me} verify --link",
        )
    if raw_key and not prefix_ok(raw_key, prefixes):
        return (
            f"map file has {raw_key} but that prefix is not in jiraProject/jiraKeyPrefixes ({', '.join(prefixes)})",
            f"{me} map --set {tid}={prefixes[0]}-1",
        )
    if ticket.get("jiraMapping") == "ambiguous" or len(candidates) > 1:
        shown = ", ".join(str(c) for c in candidates) or "several issues"
        example = str(candidates[0]) if candidates else f"{prefixes[0]}-1"
        return (
            f"ambiguous matches ({shown}); neither was stored",
            f"{me} map --set {tid}={example}",
        )
    if stored and not ticket.get("jiraKeyForced") and not prefix_ok(stored, prefixes):
        shown = ", ".join(prefixes)
        if stale_plan_id:
            return (
                f"stored {stored} is the plan id, not a Jira key, and its prefix is not in jiraProject/jiraKeyPrefixes ({shown})",
                f"{me} verify --link",
            )
        return (
            f"stored {stored} does not match jiraProject/jiraKeyPrefixes ({shown})",
            f"{me} map --set {tid}={prefixes[0]}-1",
        )
    inferred = infer_key(ticket, prefixes)
    if inferred:
        return (
            f"inferred {inferred} from the id, summary, or branch, and it is not stored",
            f"{me} catchup --write --id {tid}",
        )
    return (
        f"no Jira key in the plan, export, or map file. External-id field is {field}. "
        f"This command does not call Jira (jiraMcp={mcp}). "
        f"After the search, store one exact match with verify --apply <results.json>",
        f"{me} verify --link",
    )


_SOURCE_LABEL = {
    "external": "external id",
    "plan": "plan",
    "export": "export",
    "map": "map file",
    "manual": "manual",
    "label": "label",
    "link": "remote link",
    "summary": "summary",
    "inferred": "inferred",
}


def mapping_stored(ticket: dict) -> str:
    """How this plan id is stored on the Jira issue: field, label, remote link, or none."""
    jira = ticket.get("jira") or {}
    written = jira.get("externalIdWritten") or {}
    method = str(written.get("method") or "")
    value = str(written.get("value") or ticket.get("id") or "")
    if method == "label":
        return f"label warp:{value}"
    if method in {"remote-link", "remote_link", "link"}:
        return f"remote link warp:{value}"
    if method == "field":
        return f"field {value}"
    if written.get("value"):
        return f"field {written.get('value')}"
    on_issue = jira.get("externalIdOnIssue")
    if on_issue and str(on_issue) == str(ticket.get("id") or ""):
        return f"field {on_issue}"
    return "none"


def key_report(ticket: dict, cfg: dict) -> str:
    prefixes = prefixes_from(cfg)
    key = jira_key(ticket, prefixes)
    source = ticket.get("jiraKeySource")
    jira = ticket.get("jira") or {}
    lines = []
    if key:
        label = _SOURCE_LABEL.get(source or "", source or "unknown")
        lines.append(f"key stored: {key} (from {label})")
    else:
        stored = normalize_key(ticket.get("jiraKey"))
        plan_id = normalize_key(str(ticket.get("id") or ""))
        if stored and plan_id and stored == plan_id:
            lines.append(f"key missing: stored {stored} is the plan id; claim lookup result was not recorded")
        else:
            lines.append("key missing: claim lookup result was not recorded")
    lines.append(f"jira.id: {jira.get('id') or '(none)'}")
    if jira.get("cloudId"):
        lines.append(f"cloudId: {jira['cloudId']}")
    if jira.get("status"):
        lines.append(f"jira.status: {jira['status']}")
    attempt = jira.get("lastAttempt") or {}
    if attempt:
        err = attempt.get("error") or ""
        lines.append(
            f"lastAttempt: {attempt.get('event')} {attempt.get('result')}" + (f" — {err}" if err else "")
        )
    written = jira.get("externalIdWritten") or {}
    if written.get("value"):
        lines.append(f"externalIdWritten: {written.get('value')}")
    lines.append(f"jira mapping: {mapping_stored(ticket)}")
    ext = jira.get("externalIdAttempt") or {}
    if ext:
        err = ext.get("error") or ""
        lines.append(
            f"externalIdAttempt: {ext.get('result')}" + (f" — {err}" if err else "")
        )
    return "\n".join(lines)


def link_lines(ticket: dict, cfg: dict, beam_path: Path) -> str:
    prefixes = prefixes_from(cfg)
    key = jira_key(ticket, prefixes)
    source = ticket.get("jiraKeySource") or "(none)"
    report = key_report(ticket, cfg)
    if key:
        return f"status: keyed\nsource: {source}\n{report}"
    reason, fix = unmapped_reason(ticket, cfg, beam_path)
    import jira_match

    extra = jira_match.hint_line(ticket, beam_path)
    hint = f"{extra}\n" if extra else ""
    return f"status: unmapped\nsource: {source}\nreason: {reason}\nfix: {fix}\n{hint}{report}"


def why_nothing_linked(tickets: list[dict], cfg: dict, beam_path: Path) -> Optional[str]:
    """One line when every ticket in this report is unmapped. Omitted when any key is confirmed."""
    if not tickets:
        return None
    prefixes = prefixes_from(cfg)
    unmapped = [t for t in tickets if not jira_key(t, prefixes)]
    if not unmapped or len(unmapped) != len(tickets):
        return None
    mcp = cfg.get("jiraMcp") or "atlassian"
    tail = f"This command does not call Jira (jiraMcp={mcp})."
    if not prefixes:
        return f"why nothing linked: jiraProject is empty. {tail}"

    def kind(reason: str) -> str:
        if reason.startswith("map file has") and "beam has no confirmed" in reason:
            return "map"
        if reason.startswith("map file has"):
            return "map-prefix"
        if reason.startswith("ambiguous"):
            return "ambiguous"
        if reason.startswith("stored ") and "does not match" in reason:
            return "prefix"
        if "is the plan id" in reason:
            return "plan-id"
        if reason.startswith("inferred"):
            return "inferred"
        return "none"

    kinds = {kind(unmapped_reason(t, cfg, beam_path)[0]) for t in unmapped}
    if kinds == {"map"}:
        return f"why nothing linked: the map file has keys the beam never stored. {tail}"
    if kinds == {"map-prefix"}:
        shown = ", ".join(prefixes)
        return f"why nothing linked: map-file keys do not match jiraProject/jiraKeyPrefixes ({shown}). {tail}"
    if kinds == {"ambiguous"}:
        return f"why nothing linked: every open ticket matched more than one Jira issue. {tail}"
    if kinds == {"prefix"}:
        shown = ", ".join(prefixes)
        return f"why nothing linked: stored keys do not match jiraProject/jiraKeyPrefixes ({shown}). {tail}"
    if kinds == {"plan-id"}:
        return f"why nothing linked: every stored key is the plan id, not a Jira key. {tail}"
    if kinds == {"inferred"}:
        return f"why nothing linked: keys can be inferred from the id but are not stored. {tail}"
    if kinds == {"none"}:
        return "why nothing linked: no ticket has a Jira key in the plan, map file, or external id. " + tail
    return f"why nothing linked: see each ticket. {tail}"


def apply_known_map(beam_path: Path, only_ids: Optional[list[str]] = None, dry_run: bool = False) -> list[str]:
    """Copy map-file and jiraKeyMap keys onto tickets with no confirmed key.

    A manual or forced key is left alone. A stored key that is not confirmed is
    replaced only when the map has a prefix-valid key. Other stored keys stay.
    """
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    meta = combined_meta(beam_path, cfg)
    wanted = set(only_ids) if only_ids else None
    lines: list[str] = []
    changed = False
    for tid, t in (beam.get("tickets") or {}).items():
        if wanted is not None and tid not in wanted:
            continue
        if t.get("jiraKeySource") == "manual" or t.get("jiraKeyForced"):
            continue
        if jira_key(t, prefixes):
            continue
        info = meta.get(tid)
        if not info:
            continue
        key = info["key"]
        source = info.get("source") if info.get("source") in CONFIRMED_SOURCES else "map"
        confidence = info.get("confidence")
        if dry_run:
            lines.append(f"jira: dry-run {tid} -> {key} (map). Not written.")
            continue
        t["jiraKey"] = key
        t["jiraKeySource"] = source
        t["jiraKeyConfidence"] = confidence
        t["jiraKeyForced"] = False
        t["jiraMapping"] = "mapped"
        t.pop("jiraKeyCandidates", None)
        changed = True
        lines.append(f"jira: {tid} -> {key} (map)")
    if changed:
        _save(beam_path, beam)
    return lines


def verify(
    beam_path: Path,
    tid: Optional[str] = None,
    link: bool = False,
    results: Optional[Path] = None,
    dry_run: bool = False,
) -> str:
    """Print each ticket. With no flags this writes nothing.

    --link copies keys already in the map file and writes the same JQL as
    map --from-jira. It does not call Jira. --results and --apply store one
    exact match from a saved transcript unless --dry-run.
    """
    parts: list[str] = []
    only = [tid] if tid else None
    if link or results is not None:
        parts.extend(apply_known_map(beam_path, only, dry_run=dry_run))
    if results is not None:
        parts.append(_apply_saved(beam_path, results, dry_run))
    elif link:
        parts.append(prepare_resolve(beam_path, only, write=not dry_run))
        if dry_run:
            parts.append("jira: nothing written. Keys stay as they are until --results or --apply.")
        else:
            parts.append("jira: Jira search keys are not written until --results or --apply.")
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    mode = _mode(beam_path)
    ids = [tid] if tid else list(beam["tickets"])
    if not str(cfg.get("jiraProject") or "").strip():
        try:
            import jira_project

            decision = jira_project.choose(jira_project.gather(_root(beam_path)))
            parts.append(jira_project.warning(decision["candidates"]))
        except Exception:
            parts.append("jiraProject not set: Jira moves are disabled until you set it (candidates: none)")
    unmapped: list[str] = []
    reported: list[dict] = []
    for i in ids:
        t = beam["tickets"].get(i)
        if not t:
            parts.append(f"jira: unknown ticket {i}")
            continue
        row = diagnose(t, cfg, mode)
        if row["needsMapping"]:
            unmapped.append(str(i))
        reported.append(t)
        parts.append(format_diagnosis(row, beam_path) + "\n" + link_lines(t, cfg, beam_path))
    diagnosis = why_nothing_linked(reported, cfg, beam_path)
    if diagnosis:
        parts.append(diagnosis)
    if unmapped:
        parts.append("jira: unmapped: " + ", ".join(unmapped))
    return "\n\n".join(parts)


def catchup(beam_path: Path, tid: Optional[str], write: bool) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    mode = _mode(beam_path)
    root = _root(beam_path)
    ids = [tid] if tid else list(beam["tickets"])
    prefixes = prefixes_from(cfg)
    stamped = []
    if write:
        for i in ids:
            t = beam["tickets"].get(i)
            if t and not jira_key(t, prefixes):
                k = infer_key(t, prefixes)
                if k:
                    t["jiraKey"] = k
                    t["jiraKeySource"] = "inferred"
                    t["jiraMapping"] = "mapped"
                    stamped.append(i)
        if stamped:
            _save(beam_path, beam)
    lines = []
    payloads = []
    refused = []
    if stamped:
        lines.append("jira: stored inferred keys for " + ", ".join(stamped))
    for i in ids:
        t = beam["tickets"].get(i)
        if not t:
            lines.append(f"jira: unknown ticket {i}")
            continue
        if not jira_key(t, prefixes):
            lines.append(mapping_message(t, cfg))
            if (t.get("status") or "queued") not in {"queued", "skipped"}:
                refused.append(t)
            continue
        row = diagnose(t, cfg, mode)
        missing = row["missingItems"]
        if not missing:
            lines.append(f"jira: {i}: nothing missing")
            continue
        actions = _actions_from_missing(t, missing, cfg, mode, root)
        if not actions:
            lines.append(f"jira: {i}: nothing missing")
            continue
        payloads.append(_todo_payload(t, actions, cfg, mode))
        rendered = render(t, actions, cfg)
        if (t.get("status") or "") in DONE:
            rendered = post_merge_text(beam_path, beam, t, cfg, mode, rendered)
        lines.append(rendered)
    if refused:
        note = "jira: needs mapping. Nothing was sent to Jira.\n" + "\n".join(mapping_message(t, cfg) for t in refused)
        _outbox(beam_path, note)
        _loud(beam_path, note, footer="Run /warp-jira-map, then jira_sync.py catchup. Nothing was sent to Jira.")
    _write_todo(beam_path, payloads)
    if not lines:
        lines.append("jira: nothing missing")
    text = "\n".join(lines)
    import jira_match

    extra = jira_match.external_id_backfill(beam_path, mode="auto", only_ids={tid} if tid else None)
    if extra:
        text += "\n" + extra
    return text


def list_mappings(beam: dict, cfg: dict) -> str:
    prefixes = prefixes_from(cfg)
    lines = []
    for tid in beam.get("tickets") or {}:
        t = beam["tickets"][tid]
        key = jira_key(t, prefixes)
        lines.append(f"{tid}\t{key if key else 'unmapped'}")
    if not lines:
        return "jira: no tickets"
    unmapped = [line.split("\t", 1)[0] for line in lines if line.endswith("\tunmapped")]
    if unmapped:
        lines.append(f"jira: {len(unmapped)} unmapped. A plan id is not a Jira key.")
    return "\n".join(lines)


def _parse_set(raw: str) -> Optional[tuple[str, str]]:
    if "=" not in raw:
        return None
    tid, key = raw.split("=", 1)
    tid, key = tid.strip(), key.strip()
    if not tid or not key:
        return None
    return tid, key


def parse_mapping_file(path: Path) -> list[tuple[str, str]]:
    text = path.read_text()
    stripped = text.lstrip()
    if path.suffix.lower() == ".json" or stripped.startswith(("{", "[")):
        data = json.loads(text)
        if isinstance(data, dict):
            body = data.get("map") if isinstance(data.get("map"), dict) else data
            return [(str(k), str(v)) for k, v in body.items() if str(k) not in {"map", "generatedAt"}]
        rows = []
        for row in data:
            if not isinstance(row, dict):
                continue
            tid = row.get("id") or row.get("ticket")
            key = row.get("jiraKey") or row.get("jira") or row.get("key")
            if tid and key:
                rows.append((str(tid), str(key)))
        return rows
    if "|" in text and re.search(r"jira", text, re.I):
        rows = []
        header = None
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                header = None
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set(cells) <= {"", "-", "---"} or all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                continue
            low = [c.lower() for c in cells]
            if header is None and any(h in {"id", "ticket"} for h in low):
                header = low
                continue
            if header and len(cells) == len(header):
                row = dict(zip(header, cells))
                tid = row.get("id") or row.get("ticket")
                key = row.get("jira key") or row.get("jirakey") or row.get("jira") or row.get("key")
                if tid and key:
                    rows.append((tid, key))
        if rows:
            return rows
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,\t]", line)]
        if len(parts) < 2:
            continue
        if parts[0].lower() in {"id", "ticket", "plan id"}:
            continue
        rows.append((parts[0], parts[1]))
    return rows


def apply_pairs(beam_path: Path, pairs: list[tuple[str, str]], force: bool) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    lines = []
    changed = False
    for tid, raw in pairs:
        t = beam["tickets"].get(tid)
        if not t:
            lines.append(f"jira: unknown ticket {tid}")
            continue
        key = normalize_key(raw)
        if not key:
            lines.append(f"jira: {raw!r} is not a Jira issue key (PROJECT-123)")
            continue
        if prefixes and not prefix_ok(key, prefixes) and not force:
            lines.append(
                f"jira: {key} does not match jiraProject/jiraKeyPrefixes ({', '.join(prefixes)}). Pass --force to store it."
            )
            continue
        t["jiraKey"] = key
        t["jiraKeySource"] = "map"
        t["jiraKeyForced"] = bool(force and prefixes and not prefix_ok(key, prefixes))
        t["jiraMapping"] = "mapped"
        remember_map(beam_path, tid, key)
        changed = True
        lines.append(f"jira: {tid} -> {key}")
    if changed:
        _save(beam_path, beam)
        lines.append("jira: saved .warp/jira-map.json. Already-claimed tickets: run catchup to post the pending transition and comments.")
    elif not lines:
        lines.append("jira: nothing to map")
    return "\n".join(lines)


def search_request(beam_path: Path) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    project = prefixes[0] if prefixes else ""
    rows = []
    for tid, t in (beam.get("tickets") or {}).items():
        if jira_key(t, prefixes):
            continue
        summary = (t.get("summary") or "").strip()
        if not summary:
            rows.append({"id": tid, "summary": "", "jql": None, "reason": "no summary to search"})
            continue
        safe = summary.replace("\\", "\\\\").replace('"', '\\"')
        jql = f'summary ~ "{safe}"'
        if project:
            jql = f"project = {project} AND {jql}"
        rows.append({"id": tid, "summary": summary, "jql": jql, "tool": TOOL_SEARCH})
    payload = {"server": cfg.get("jiraMcp") or "atlassian", "tool": TOOL_SEARCH, "confirm": True, "tickets": rows}
    path = beam_path.parent / SEARCH_NAME
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return (
        f"jira: SEARCH. Do not change keys yet. Call {TOOL_SEARCH} on server {payload['server']} "
        f"for each jql in .warp/{SEARCH_NAME}.\n"
        "jira: Save the results, then run map --match results.json and read the proposals.\n"
        "jira: Add --yes only after each pair is confirmed. Nothing is written until --yes."
    )


def _issue_summary(issue: dict) -> str:
    fields = issue.get("fields") or {}
    return str(issue.get("summary") or fields.get("summary") or "").strip()


def _issue_key(issue: dict) -> Optional[str]:
    return normalize_key(str(issue.get("key") or issue.get("jiraKey") or ""))


def _normalize_search_results(data) -> dict[str, list[dict]]:
    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        out = {}
        for row in data["tickets"]:
            if isinstance(row, dict) and row.get("id"):
                issues = row.get("issues") or row.get("results") or []
                out[str(row["id"])] = issues if isinstance(issues, list) else []
        return out
    if isinstance(data, dict):
        out = {}
        for tid, issues in data.items():
            if tid in {"server", "tool", "confirm"}:
                continue
            if isinstance(issues, list):
                out[str(tid)] = issues
            elif isinstance(issues, dict):
                out[str(tid)] = issues.get("issues") or []
        return out
    return {}


def match_search(beam_path: Path, results_path: Path, yes: bool) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    data = json.loads(results_path.read_text())
    found = _normalize_search_results(data)
    proposals = []
    lines = []
    for tid, t in (beam.get("tickets") or {}).items():
        if jira_key(t, prefixes_from(cfg)):
            continue
        summary = (t.get("summary") or "").strip().casefold()
        issues = found.get(tid) or []
        hits = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            key = _issue_key(issue)
            if not key:
                continue
            if summary and _issue_summary(issue).casefold() == summary:
                hits.append(key)
        if len(hits) == 1:
            proposals.append((tid, hits[0]))
            lines.append(f"jira: propose {tid}={hits[0]}")
        elif len(hits) > 1:
            lines.append(f"jira: {tid} ambiguous ({', '.join(hits)}); not applied")
        else:
            lines.append(f"jira: {tid} no summary match")
    if not proposals:
        lines.append("jira: no unique matches")
        return "\n".join(lines)
    if not yes:
        lines.append("jira: not written. Re-run with --yes after you confirm these pairs.")
        return "\n".join(lines)
    lines.append(apply_pairs(beam_path, proposals, force=False))
    return "\n".join(lines)


def key_summary(beam: dict, cfg: Optional[dict] = None) -> str:
    """`12 tickets: 9 keyed, 3 need mapping`."""
    cfg = cfg or {}
    prefixes = prefixes_from(cfg)
    tickets = list((beam.get("tickets") or {}).values())
    keyed = sum(1 for t in tickets if jira_key(t, prefixes))
    need = len(tickets) - keyed
    noun = "ticket" if len(tickets) == 1 else "tickets"
    return f"jira: {len(tickets)} {noun}: {keyed} keyed, {need} need mapping"


def external_field(cfg: dict) -> str:
    raw = str(cfg.get("jiraExternalIdField") or "externalId").strip().strip("\"'")
    return raw or "externalId"


def external_id_jql(tid: str, cfg: dict) -> str:
    field = external_field(cfg)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field):
        shown = field
    else:
        shown = '"' + field.replace("\\", "\\\\").replace('"', '\\"') + '"'
    safe = str(tid).replace("\\", "\\\\").replace('"', '\\"')
    jql = f'{shown} = "{safe}"'
    prefixes = prefixes_from(cfg)
    if prefixes:
        jql = f"project = {prefixes[0]} AND {jql}"
    return jql


def prepare_resolve(beam_path: Path, only_ids: Optional[list[str]] = None, write: bool = True) -> str:
    """Write JQL for an exact external-id lookup. Does not call Jira and does not flag a miss."""
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    project = prefixes[0] if prefixes else None
    configured = external_field(cfg)
    wanted = set(only_ids) if only_ids else None
    rows = []
    for tid, t in (beam.get("tickets") or {}).items():
        if wanted is not None and tid not in wanted:
            continue
        if t.get("jiraKeySource") == "manual" or t.get("jiraKeyForced"):
            continue
        if jira_key(t, prefixes):
            continue
        rows.append(
            {
                "id": tid,
                "summary": t.get("summary") or "",
                "externalId": tid,
                "field": configured,
                "jql": external_id_jql(tid, cfg),
                "tool": TOOL_SEARCH,
                "queries": jira_lookup.plan_queries(tid, t.get("summary") or "", project, configured),
            }
        )
    path = beam_path.parent / RESOLVE_NAME
    summary = key_summary(beam, cfg)
    if not rows:
        if write and wanted is None and path.is_file():
            path.unlink()
        text = summary
    else:
        text = _prepare_resolve_text(beam_path, cfg, rows, path, write, summary)
    if write:
        import jira_match

        extra = jira_match.external_id_backfill(beam_path, mode="auto", only_ids=wanted)
        if extra:
            text += "\n" + extra
    return text


def _prepare_resolve_text(beam_path: Path, cfg: dict, rows: list[dict], path: Path, write: bool, summary: str) -> str:
    payload = {
        "server": cfg.get("jiraMcp") or "atlassian",
        "tool": TOOL_SEARCH,
        "fieldsTool": TOOL_FIELDS,
        "remoteLinksTool": TOOL_REMOTE_LINKS,
        "match": "external-id",
        "confirm": False,
        "order": ["plan or map", "external id", "label", "remote link", "summary"],
        "tickets": rows,
    }
    if write:
        path.write_text(json.dumps(payload, indent=2) + "\n")
    shown = ", ".join(row["id"] for row in rows[:30])
    extra = f" (+{len(rows) - 30} more)" if len(rows) > 30 else ""
    me = Path(__file__).resolve()
    text = (
        f"{summary}\n"
        f"jira: RESOLVE {shown}{extra} by external id (exact single match), then label warp:<id>, then a remote link. "
        f"Call {TOOL_FIELDS} once, then {TOOL_SEARCH} on server {payload['server']} for each jql in .warp/{RESOLVE_NAME}. "
        f"Remote links use {TOOL_REMOTE_LINKS}.\n"
        f"jira: Then python3 {me} verify --apply <results.json> or python3 {me} resolve --apply <results.json>. "
        'Save {"fields":[{"name":"External ID","id":"customfield_10050"}],'
        '"searches":[{"jql":"...","issues":[{"key":"WAR-1"}]}],"remoteLinks":[{"key":"WAR-1","ids":["WV-01"]}]}. '
        "One exact match is stored with its source. Zero or several stay unmapped. A summary match is only a proposal.\n"
        "jira: /warp-jira-map is only for tickets that stay unmapped or ambiguous.\n"
        f"jira: RECORD each hit before any transition: python3 {me} resolve --ticket <id> --key <WAR-1> "
        "--issue-id <id> --cloud-id <cloudId>. That writes the beam and .warp/jira-map.json together. "
        "transitionJiraIssue uses only the stored key. Do not pass the plan id.\n"
        "jira: An unmapped ticket with a summary can be matched by jira_match.py. "
        "An exact or prefix hit is stored with --apply. A fuzzy hit stays a proposal until --yes."
    )
    import jira_match

    write_hint = jira_match.hint_for_resolve(cfg)
    if write_hint:
        text += "\n" + write_hint
    return text


def _external_values(issue: dict, field: str) -> list[str]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    raws = [
        issue.get("externalId"),
        issue.get("external_id"),
        issue.get("externalIssueId"),
        fields.get(field) if fields else None,
        fields.get("externalId") if fields else None,
        fields.get("external_id") if fields else None,
        fields.get("externalIssueId") if fields else None,
        fields.get("External issue ID") if fields else None,
    ]
    out: list[str] = []

    def add(raw) -> None:
        if raw is None or isinstance(raw, bool):
            return
        if isinstance(raw, dict):
            add(raw.get("value") or raw.get("name") or raw.get("id"))
            return
        if isinstance(raw, list):
            for item in raw:
                add(item)
            return
        text = str(raw).strip()
        if text:
            out.append(text)

    for raw in raws:
        add(raw)
    return out


def _same_id(a: str, b: str) -> bool:
    return str(a).strip().casefold() == str(b).strip().casefold()


def match_external(tid: str, issues: list, field: str, prefixes: list[str]) -> tuple[Optional[str], str]:
    """Exact external-id match. One issue with no field is the JQL hit. Several keys are ambiguous."""
    exact: list[str] = []
    bare: list[str] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        key = _issue_key(issue)
        if not key or (prefixes and not prefix_ok(key, prefixes)):
            continue
        values = _external_values(issue, field)
        if values:
            if any(_same_id(v, tid) for v in values):
                exact.append(key)
        else:
            bare.append(key)
    chosen = list(dict.fromkeys(exact))
    if len(chosen) == 1:
        return chosen[0], "matched"
    if len(chosen) > 1:
        return None, "ambiguous:" + ",".join(chosen)
    bare_keys = list(dict.fromkeys(bare))
    if len(issues or []) == 1 and len(bare_keys) == 1:
        return bare_keys[0], "matched"
    if len(bare_keys) > 1 or (exact and len(chosen) > 1):
        return None, "ambiguous:" + ",".join(bare_keys or chosen)
    return None, "none"


def _results_by_ticket(data, requested: list[str]) -> dict[str, list]:
    found = _normalize_search_results(data)
    if found:
        return found
    issues = None
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        issues = data["issues"]
    elif isinstance(data, list):
        issues = data
    if issues is not None and len(requested) == 1:
        return {requested[0]: issues}
    return {}


def _requested_ids(beam_path: Path, results) -> list[str]:
    path = beam_path.parent / RESOLVE_NAME
    if path.is_file():
        try:
            payload = json.loads(path.read_text())
            ids = [str(row["id"]) for row in payload.get("tickets") or [] if isinstance(row, dict) and row.get("id")]
            if ids:
                return ids
        except (OSError, json.JSONDecodeError):
            pass
    if isinstance(results, dict):
        return [str(k) for k in results if k not in {"server", "tool", "confirm", "match", "issues"}]
    return []


def apply_external(beam_path: Path, results_path: Path) -> str:
    """Store an exact single external-id match. Flag an active ticket only after this attempt."""
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    field = external_field(cfg)
    data = json.loads(results_path.read_text())
    requested = _requested_ids(beam_path, data)
    found = _results_by_ticket(data, requested)
    if not requested:
        requested = list(found)
    lines = []
    changed = False
    tried_miss: list[str] = []
    newly: list[str] = []
    for tid in requested:
        t = beam["tickets"].get(tid)
        if not t:
            lines.append(f"jira: unknown ticket {tid}")
            continue
        if jira_key(t, prefixes):
            continue
        key, how = match_external(tid, found.get(tid) or [], field, prefixes)
        if how == "matched" and key:
            t["jiraKey"] = key
            t["jiraKeySource"] = "external"
            t["jiraKeyConfidence"] = "high"
            t["jiraKeyForced"] = False
            t["jiraMapping"] = "mapped"
            remember_map(beam_path, tid, key, source="external", confidence="high")
            changed = True
            newly.append(tid)
            lines.append(f"jira: {tid} -> {key} (external id)")
        elif how.startswith("ambiguous"):
            keys = how.split(":", 1)[1]
            lines.append(f"jira: {tid} ambiguous ({keys}); still needs mapping")
            tried_miss.append(tid)
        else:
            lines.append(f"jira: {tid} no external-id match")
            tried_miss.append(tid)
    return _commit_mapping(
        beam_path,
        lines,
        changed,
        beam,
        newly,
        tried_miss,
        flag_misses=True,
        footer="External-id search did not find one match. /warp-jira-map is only for this leftover.",
    )


def _store_found(beam_path: Path, ticket: dict, tid: str, key: str, source: str, confidence: Optional[str]) -> None:
    ticket["jiraKey"] = key
    ticket["jiraKeySource"] = source
    ticket["jiraKeyConfidence"] = confidence
    ticket["jiraKeyForced"] = False
    ticket["jiraMapping"] = "mapped"
    ticket.pop("jiraKeyCandidates", None)
    remember_map(beam_path, tid, key, source=source, confidence=confidence)


def _commit_mapping(
    beam_path: Path,
    lines: list[str],
    changed: bool,
    beam: dict,
    newly: list[str],
    tried_miss: list[str],
    flag_misses: bool,
    footer: str,
) -> str:
    if changed:
        _save(beam_path, beam)
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    lines.insert(0, key_summary(beam, cfg))
    mode = _mode(beam_path)
    root = _root(beam_path)
    payloads = []
    refused = []
    for tid in newly:
        t = beam["tickets"].get(tid) or {}
        if (t.get("status") or "queued") in {"queued", "skipped"}:
            continue
        row = diagnose(t, cfg, mode)
        actions = _actions_from_missing(t, row["missingItems"], cfg, mode, root)
        if actions:
            payloads.append(_todo_payload(t, actions, cfg, mode))
            lines.append(render(t, actions, cfg))
    if flag_misses:
        for tid in tried_miss:
            t = beam["tickets"].get(tid)
            if not t or jira_key(t, prefixes):
                continue
            if (t.get("status") or "queued") in {"queued", "skipped"}:
                continue
            refused.append(t)
    if payloads or refused:
        _write_todo(beam_path, payloads)
    if refused:
        victim = next((t for t in refused if _should_stop_for_unlinked(beam, cfg, t)), None)
        if victim is not None:
            stored = victim.get("jiraKey")
            label = str(stored) if stored else "unresolved"
            lines.append(stop_unlinked_run(beam_path, str(victim["id"]), label))
            return "\n".join(lines)
        note = "jira: needs mapping. Nothing was sent to Jira.\n" + "\n".join(mapping_message(t, cfg) for t in refused)
        _outbox(beam_path, note)
        _loud(beam_path, note, footer=footer)
        lines.append(note)
    if newly:
        lines.append("jira: saved .warp/jira-map.json. A rescan keeps these keys.")
    return "\n".join(lines)


def _transcript(data) -> bool:
    return isinstance(data, dict) and any(name in data for name in ("searches", "fields", "remoteLinks"))


def apply_jira_lookup(beam_path: Path, client, dry_run: bool = False, confirm_summary: bool = False, flag_active: bool = False) -> str:
    """Store one exact Jira match. A manual key is left alone. Summary waits for confirm_summary."""
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    prefixes = prefixes_from(cfg)
    project = prefixes[0] if prefixes else None
    configured = external_field(cfg)
    lines: list[str] = []
    changed = False
    tried_miss: list[str] = []
    newly: list[str] = []
    for tid, t in (beam.get("tickets") or {}).items():
        if t.get("jiraKeySource") == "manual" or t.get("jiraKeyForced"):
            continue
        if jira_key(t, prefixes):
            continue
        row = jira_lookup.resolve_ticket(tid, t.get("summary") or "", project, configured, client, prefixes)
        outcome = row.get("outcome")
        notes = row.get("notes") or []
        if outcome != "hit" or any(configured in note for note in notes):
            for note in notes:
                lines.append(f"jira: {tid} {note}")
        key = row.get("key")
        source = row.get("source")
        confidence = row.get("confidence")
        if outcome == "hit" and key:
            if dry_run:
                lines.append(f"jira: dry-run {tid} -> {key} ({source}, {confidence}). Not written.")
                continue
            _store_found(beam_path, t, tid, key, source, confidence)
            changed = True
            newly.append(tid)
            lines.append(f"jira: {tid} -> {key} ({source}, {confidence})")
        elif outcome == "proposed" and key:
            if dry_run:
                lines.append(f"jira: dry-run {tid} -> {key} (summary, {confidence or 'low'}). Not written.")
            elif confirm_summary:
                _store_found(beam_path, t, tid, key, source or "summary", confidence or "low")
                changed = True
                newly.append(tid)
                lines.append(f"jira: {tid} -> {key} ({source or 'summary'}, {confidence or 'low'})")
            else:
                lines.append(f"jira: propose {tid}={key} (summary, {confidence or 'low'}). Not written.")
        elif outcome == "ambiguous":
            shown = ", ".join(row.get("matches") or [])
            lines.append(f"jira: {tid} ambiguous ({shown}); still needs mapping")
            if not dry_run:
                t["jiraKey"] = None
                t["jiraKeySource"] = None
                t["jiraKeyConfidence"] = None
                t["jiraMapping"] = "ambiguous"
                t["jiraKeyCandidates"] = list(row.get("matches") or [])
                changed = True
            tried_miss.append(tid)
        else:
            lines.append(f"jira: {tid} no match")
            tried_miss.append(tid)
    return _commit_mapping(
        beam_path,
        lines,
        changed and not dry_run,
        beam,
        newly,
        tried_miss,
        flag_misses=flag_active and not dry_run,
        footer="Jira lookup did not find one match. /warp-jira-map is only for this leftover.",
    )


def _apply_saved(beam_path: Path, results_path: Path, dry_run: bool) -> str:
    """Same write path as map --from-jira --results. A check does not flag active misses."""
    data = json.loads(results_path.read_text())
    if _transcript(data):
        return apply_jira_lookup(
            beam_path,
            jira_lookup.ReplayClient(data),
            dry_run=dry_run,
            confirm_summary=False,
            flag_active=False,
        )
    if dry_run:
        return "jira: dry-run. Not written."
    return apply_external(beam_path, results_path)


def apply_resolve(beam_path: Path, results_path: Path) -> str:
    """Old per-ticket results stay on the external-id path. A transcript uses the full lookup."""
    data = json.loads(results_path.read_text())
    if _transcript(data):
        return apply_jira_lookup(
            beam_path,
            jira_lookup.ReplayClient(data),
            dry_run=False,
            confirm_summary=False,
            flag_active=True,
        )
    return apply_external(beam_path, results_path)


JIRA_HELP = """
examples:
  python3 scripts/jira_sync.py ?
  python3 scripts/jira_sync.py verify --beam .warp/beam.json
  python3 scripts/jira_sync.py verify --link
  python3 scripts/jira_sync.py verify --link --dry-run
  python3 scripts/jira_sync.py verify --apply results.json
  python3 scripts/jira_sync.py verify --link --results results.json
  python3 scripts/jira_sync.py catchup --beam .warp/beam.json
  python3 scripts/jira_sync.py catchup --write --id T-9
  python3 scripts/jira_sync.py map
  python3 scripts/jira_sync.py map --set WV-01=WAR-1
  python3 scripts/jira_sync.py map --import jira-map.csv
  python3 scripts/jira_sync.py map --search
  python3 scripts/jira_sync.py map --match results.json --yes
  python3 scripts/jira_sync.py map --from-jira
  python3 scripts/jira_sync.py map --auto --results results.json --dry-run
  python3 scripts/jira_sync.py map --from-jira --results results.json --yes
  python3 scripts/jira_sync.py resolve --apply results.json
  python3 scripts/jira_sync.py resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id cloud-1
  python3 scripts/jira_sync.py external-id --ticket WV-01 --key WAR-1 --yes
  python3 scripts/jira_external_id.py --apply --yes
  python3 scripts/jira_sync.py record-external-id --results edits.json
  python3 scripts/jira_match.py --results candidates.json --apply
  python3 scripts/jira_sync.py record --id WV-01 --event claim --result failed --error "transition rejected"
  python3 scripts/jira_sync.py project --list
  python3 scripts/jira_sync.py project --probe
  python3 scripts/jira_sync.py project --record probe.json
  python3 scripts/jira_sync.py project --set WAR
  python3 scripts/jira_sync.py project --apply results.json
  python3 scripts/jira_sync.py plan --id T-9 --event claim
  python3 scripts/jira_sync.py pick --target "In Progress" --transitions-file transitions.json
  python3 scripts/jira_sync.py record --id T-9 --event claim --result moved
  python3 scripts/jira_sync.py record-comment --id T-9 --where jira --event claim --comment-id 10001

verify with no flags only prints. For each ticket it prints status: keyed or
unmapped, source of the key, and when unmapped the reason and one fix command.
When every ticket is unmapped it prints why nothing linked. It does not call
Jira, and it does not mean the external-id search already ran. jiraMcp is the
server name printed in that line (default atlassian). --link copies a key
already in the map file or jiraKeyMap onto a ticket that has no confirmed key,
and writes the same JQL as map --from-jira. Jira search keys are not written
until --results or --apply. --dry-run prints the decision and writes nothing.
--results FILE and --apply FILE store one exact match from a saved transcript
(external id, then label, then remote link). A summary match is not stored.
Two matches are reported and neither is stored. A manual key is never
overwritten. catchup writes .warp/jira-todo.json for tickets that have a
confirmed key, and refuses (outbox + Herald, no MCP call) when the key is
missing or the prefix does not match. catchup --write stores a key inferred
from the id, summary, or branch only when that prefix matches jiraProject or
jiraKeyPrefixes. This script does not call Jira.

Scan and claim write .warp/jira-resolve.json. The agent searches, then must
record the hit before any transition:
resolve --ticket WV-01 --key WAR-1 --issue-id 10001 --cloud-id <cloudId>.
That writes the beam and .warp/jira-map.json together (jiraKey, jira.id,
jira.cloudId). A plan id is refused and nothing is stored. transitionJiraIssue
uses only the stored key. resolve --apply still accepts a saved transcript.
The search order is the external-id field (jiraExternalIdField, then External
ID, External Id, ExternalId, External Key, Plan ID, Ticket ID), a label
warp:<id>, then a remote-link id. A summary match is only a proposal.
/warp-jira-map is only for tickets still unmapped or ambiguous, or for a review.

record --result failed --error "text" stores jira.lastAttempt and does not
set startedAt. verify prints key stored or key missing, jira.id, and lastAttempt.

map lists id and key, or unmapped. --set ID=KEY writes the beam and
.warp/jira-map.json. --import reads CSV, JSON, or a markdown table.
--search writes JQL for searchJiraIssuesUsingJql and does not change keys.
--match proposes summary matches; --yes stores them.
--from-jira and --auto are the same. With no --results they print the JQL
and write no keys. --results FILE applies a saved search transcript.
--dry-run prints the decision and writes nothing. --yes stores a unique
summary proposal. A manual key is never overwritten. Two matches are reported
and neither is stored. The map file records the key, source, and confidence.

A plan id is not a Jira key unless its project prefix is configured.

project fills an empty jiraProject. With no flags it uses plan files, branches,
and recent commit subjects, and does not guess when several prefixes fit.
--list prints candidates and writes nothing. --set WAR stores that key
(and jiraKeyPrefixes when that is empty). --apply reads a saved
getVisibleJiraProjects / getAccessibleAtlassianResources transcript. One
visible project, or one that matches the repo or a candidate, is stored.
Several projects are probed. --probe writes JQL for the first plan ids
(external id, then External ID, then label warp:<id>) in each project.
--record stores jiraProject when exactly one project has a hit, and records
how. Several hits are listed with issue keys and project --set. None lists
every project. jiraSite is stored when there is a single site. A value you
already set is left alone. When nothing is chosen the warning is:
jiraProject not set: Jira moves are disabled until you set it (candidates: WAR, ABC)

plan/record --event: claim, release, qa-ready, done.
record --result: moved, already, skipped, unavailable, no-transition, failed, not-found.
not-found on the first claimed ticket (no jira.startedAt anywhere in the run)
releases that claim and stops the run. A later not-found does not. jiraTransition
false does not stop the run.
record-comment --where: jira or pr.
record-comment --event: claim, pr-opened, qa-ready, merged, bugbot, bugbot-rerun, ci, alarm, blocked.
pick --kind: start, qa, done, restore. --no-category skips the status-category match.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main() -> None:
    import usage

    p = argparse.ArgumentParser(
        description="Jira status sync for claims",
        epilog=JIRA_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("--beam", default=".warp/beam.json")
    pp.add_argument("--id", required=True)
    pp.add_argument("--event", choices=EVENTS, required=True)
    pk = sub.add_parser("pick")
    pk.add_argument("--target", required=True)
    pk.add_argument("--transitions", help='JSON list, or {"transitions": [...]}')
    pk.add_argument("--transitions-file")
    pk.add_argument("--current", help='JSON {"name":..., "category":...}')
    pk.add_argument("--kind", choices=["start", "qa", "done", "restore"], default="start")
    pk.add_argument("--no-category", action="store_true")
    pr = sub.add_parser("record")
    pr.add_argument("--beam", default=".warp/beam.json")
    pr.add_argument("--id", required=True)
    pr.add_argument("--event", choices=EVENTS, required=True)
    pr.add_argument("--result", choices=sorted(RESULTS), required=True)
    pr.add_argument("--from", dest="frm")
    pr.add_argument("--to")
    pr.add_argument("--detail")
    pr.add_argument("--error", help="error text stored on jira.lastAttempt when the transition failed")
    pc = sub.add_parser("record-comment")
    pc.add_argument("--beam", default=".warp/beam.json")
    pc.add_argument("--id", required=True)
    pc.add_argument("--where", choices=["jira", "pr"], required=True)
    pc.add_argument("--event", choices=COMMENT_EVENTS, required=True)
    pc.add_argument("--comment-id")
    pv = sub.add_parser("verify")
    pv.add_argument("--beam", default=".warp/beam.json")
    pv.add_argument("--id")
    pv.add_argument("--link", action="store_true", help="copy map-file keys and write JQL; a Jira search is not stored")
    pv.add_argument("--results", help="JSON transcript; one exact match is stored")
    pv.add_argument("--apply", dest="verify_apply", help="same as --results")
    pv.add_argument("--dry-run", action="store_true", help="print matches and write nothing")
    pu = sub.add_parser("catchup")
    pu.add_argument("--beam", default=".warp/beam.json")
    pu.add_argument("--id")
    pu.add_argument("--write", action="store_true", help="store an inferred jiraKey on the ticket")
    pm = sub.add_parser("map", help="list or set plan-id to Jira-key mappings")
    pm.add_argument("--beam", default=".warp/beam.json")
    pm.add_argument("--set", action="append", default=[], help="ID=KEY, repeatable")
    pm.add_argument("--import", dest="import_path", help="CSV, JSON, or markdown table")
    pm.add_argument("--search", action="store_true", help="write JQL for searchJiraIssuesUsingJql; changes nothing")
    pm.add_argument("--match", help="search results JSON; prints proposals unless --yes")
    pm.add_argument("--yes", action="store_true", help="store proposals from --match or a summary hit from --from-jira")
    pm.add_argument("--force", action="store_true", help="store a key whose prefix is not configured")
    pm.add_argument("--from-jira", action="store_true", help="match unmapped tickets from a Jira search transcript")
    pm.add_argument("--auto", action="store_true", help="same as --from-jira")
    pm.add_argument("--results", help="JSON transcript from searchJiraIssuesUsingJql")
    pm.add_argument("--dry-run", action="store_true", help="print Jira matches and write nothing")
    pv2 = sub.add_parser("resolve", help="match plan ids to Jira by external id, label, or remote link")
    pv2.add_argument("--beam", default=".warp/beam.json")
    pv2.add_argument("--apply", help="search results JSON; an exact single match is stored")
    pv2.add_argument("--ticket", help="plan id to record, for example WV-01")
    pv2.add_argument("--key", help="Jira issue key from the lookup, for example WAR-1")
    pv2.add_argument("--issue-id", dest="issue_id", help="Jira issue id")
    pv2.add_argument("--cloud-id", dest="cloud_id", help="cloudId for later transitions")
    pv2.add_argument("--status", help="current Jira status name, for example To Do")
    pj = sub.add_parser("project", help="detect or set jiraProject")
    pj.add_argument("--root", default=".")
    pj.add_argument("--list", action="store_true", help="print candidate project keys and write nothing")
    pj.add_argument("--set", dest="project_key", help="store this project key")
    pj.add_argument("--apply", dest="project_apply", help="JSON from getVisibleJiraProjects and getAccessibleAtlassianResources")
    pj.add_argument("--probe", action="store_true", help="write JQL that checks each visible project for a plan id")
    pj.add_argument("--record", dest="project_record", help="probe transcript; one matching project is stored")
    pe = sub.add_parser("external-id", help="write a plan id into the Jira External ID field")
    pe.add_argument("--beam", default=".warp/beam.json")
    pe.add_argument("--ticket", help="plan id, for example WV-01")
    pe.add_argument("--key", help="Jira issue key, for example WAR-1")
    pe.add_argument("--set", dest="pair", help="WV-01=WAR-1")
    pe.add_argument("--results", help="edit transcript from editJiraIssue")
    pe.add_argument("--yes", action="store_true", help="confirm the write")
    pe.add_argument("--force-external-id", action="store_true", help="replace a different non-empty External ID")
    px = sub.add_parser("record-external-id", help="record editJiraIssue results for External ID")
    px.add_argument("--beam", default=".warp/beam.json")
    px.add_argument("--results", required=True, help="transcript with edits, issues, fields, and editmeta")
    px.add_argument("--force", action="store_true", help="record again when externalIdWritten is already set")
    px.add_argument("--force-external-id", action="store_true", help="replace a different non-empty External ID")
    args = p.parse_args(usage.normalize_argv(None))
    try:
        if args.cmd == "plan":
            beam_path = Path(args.beam)
            beam = _load(beam_path)
            t = beam["tickets"].get(args.id)
            if not t:
                print(f"jira: unknown ticket {args.id}")
                return
            print(instruction(plan(t, args.event, settings(beam_path, beam))))
        elif args.cmd == "pick":
            raw = Path(args.transitions_file).read_text() if args.transitions_file else (args.transitions or "[]")
            data = json.loads(raw)
            transitions = data.get("transitions", []) if isinstance(data, dict) else data
            current = json.loads(args.current) if args.current else None
            print(json.dumps(pick(transitions, args.target, current, not args.no_category, args.kind), indent=2))
        elif args.cmd == "record":
            print(record(Path(args.beam), args.id, args.event, args.result, args.frm, args.to, args.detail, args.error))
        elif args.cmd == "record-comment":
            print(record_comment(Path(args.beam), args.id, args.where, args.event, args.comment_id))
        elif args.cmd == "verify":
            results = args.results or args.verify_apply
            print(
                verify(
                    Path(args.beam),
                    args.id,
                    link=bool(args.link),
                    results=Path(results) if results else None,
                    dry_run=bool(args.dry_run),
                )
            )
        elif args.cmd == "map":
            beam_path = Path(args.beam)
            if args.from_jira or args.auto:
                if args.results:
                    data = json.loads(Path(args.results).read_text())
                    client = jira_lookup.ReplayClient(data if isinstance(data, dict) else {})
                    print(
                        apply_jira_lookup(
                            beam_path,
                            client,
                            dry_run=args.dry_run,
                            confirm_summary=bool(args.yes) and not args.dry_run,
                            flag_active=False,
                        )
                    )
                else:
                    print(prepare_resolve(beam_path))
                    print("jira: nothing written. Keys stay as they are until --results.")
            elif args.search:
                print(search_request(beam_path))
            elif args.match:
                print(match_search(beam_path, Path(args.match), args.yes))
            elif args.set or args.import_path:
                pairs = []
                for raw in args.set:
                    parsed = _parse_set(raw)
                    if not parsed:
                        print(f"jira: --set {raw!r} must be ID=KEY")
                        continue
                    pairs.append(parsed)
                if args.import_path:
                    pairs.extend(parse_mapping_file(Path(args.import_path)))
                print(apply_pairs(beam_path, pairs, args.force))
            else:
                print(list_mappings(_load(beam_path), settings(beam_path, _load(beam_path))))
        elif args.cmd == "project":
            import jira_project

            root = Path(args.root).resolve()
            if args.list:
                decision = jira_project.choose(jira_project.gather(root))
                current = jira_project.config_value(jira_project.config_text(root), "jiraProject")
                if current:
                    print(f"jira: jiraProject is {current}")
                shown = ", ".join(decision["candidates"]) if decision["candidates"] else "none"
                print(f"jira: candidates: {shown}")
                if decision["prefix"]:
                    print(f"jira: would set {decision['prefix']} ({decision['how']})")
            elif args.project_key:
                print(jira_project.write_project(root, args.project_key, "set with project --set", force=True))
            elif args.project_apply:
                data = json.loads(Path(args.project_apply).read_text())
                print(jira_project.apply_remote(root, jira_project.ReplayClient(data if isinstance(data, dict) else {})))
            elif args.probe:
                projects = []
                todo = root / ".warp" / "jira-project.json"
                if todo.is_file():
                    try:
                        saved = json.loads(todo.read_text())
                        projects = [str(p).upper() for p in (saved.get("projects") or saved.get("candidates") or [])]
                    except (OSError, json.JSONDecodeError):
                        projects = []
                print(jira_project.prepare_probe(root, projects))
            elif args.project_record:
                data = json.loads(Path(args.project_record).read_text())
                print(jira_project.record_probe(root, data if isinstance(data, dict) else {}))
            else:
                print(jira_project.ensure(root, write=True, report_set=True))
        elif args.cmd == "record-external-id":
            import jira_match

            data = json.loads(Path(args.results).read_text())
            print(
                jira_match.record_external_id(
                    Path(args.beam),
                    data if isinstance(data, dict) else {},
                    force=bool(args.force),
                    force_value=bool(args.force_external_id),
                )
            )
        elif args.cmd == "external-id":
            import jira_match

            beam_path = Path(args.beam)
            pairs = []
            if args.pair:
                parsed = jira_match._parse_pair(args.pair)
                if not parsed:
                    print(f"jira: --set {args.pair!r} must be ID=KEY")
                    return
                pairs.append(parsed)
            elif args.ticket and args.key:
                key = normalize_key(args.key)
                if not key:
                    print(f"jira: {args.key!r} is not a Jira issue key")
                    return
                pairs.append((args.ticket, key))
            if args.results and args.yes:
                data = json.loads(Path(args.results).read_text())
                print(jira_match.record_write(beam_path, data if isinstance(data, dict) else {}, yes=True, force=bool(args.force_external_id)))
            else:
                if not pairs:
                    loaded = _load(beam_path) if beam_path.is_file() else {"tickets": {}}
                    rows = jira_match._pairs_from_beam(loaded, settings(beam_path, loaded), args.ticket, [])
                    pairs = [(row["id"], row["key"]) for row in rows]
                print(jira_match.prepare_write(beam_path, pairs, yes=bool(args.yes), force=bool(args.force_external_id)))
        elif args.cmd == "resolve":
            beam_path = Path(args.beam)
            if args.ticket and args.key:
                print(
                    record_resolved(
                        beam_path,
                        args.ticket,
                        args.key,
                        issue_id=args.issue_id,
                        cloud_id=args.cloud_id,
                        status=args.status,
                    )
                )
            elif args.apply:
                print(apply_resolve(beam_path, Path(args.apply)))
            else:
                print(prepare_resolve(beam_path))
        else:
            print(catchup(Path(args.beam), args.id, args.write))
    except Exception as e:  # fail-soft: report, do not fail the caller
        print(f"jira: skipped ({e})")


if __name__ == "__main__":
    main()
