#!/usr/bin/env python3
"""Keep a Jira issue in step with a ticket, and say what to comment. Fail-soft.

Warp has no Jira credentials. An agent calls the Atlassian MCP server. This
script decides the move, picks a transition from the ones Jira offers, writes
the comment text, and records what happened. It never blocks a claim, a merge,
or a scan.

  plan            what one event should do
  pick            choose a transition id from Jira's list
  record          store a transition result
  record-comment  store a Jira or pull-request comment id
  verify          per ticket: what should have happened, and what is missing
  catchup         print the moves still owed; --write stores an inferred key

Settings (.warp/config.yaml, else the beam copy, else the defaults below):
  jiraTransition          true
  jiraInProgressStatus    "In Progress"
  jiraQaReadyStatus       "QA Ready"
  jiraDoneStatus          "Done"
  jiraRestoreOnRelease    false
  jiraMcp                 "atlassian"   Cursor server name, not a tool name
  jiraSite                ""            site URL used as cloudId when set

Only a ticket with a Jira key is touched. The key is the jiraKey field, or a
key inferred from the id, summary, or branch (two or more letters, then a
number: ABC-123, not T-3). Pause and stop never move an issue.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
# Inference requires a real project key (2+ characters before the hyphen) so
# plan ids like T-3 are not treated as Jira keys. Explicit jiraKey still uses KEY_RE.
INFER_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
ACTIVE = {"claimed", "planning", "coding", "review", "fix", "awaiting_approval", "merging"}
DEFAULTS = {
    "jiraTransition": True,
    "jiraInProgressStatus": "In Progress",
    "jiraQaReadyStatus": "QA Ready",
    "jiraDoneStatus": "Done",
    "jiraRestoreOnRelease": False,
    "jiraMcp": "atlassian",
    "jiraSite": "",
}
EVENTS = ["claim", "release", "qa-ready", "done"]
COMMENT_EVENTS = ["claim", "pr-opened", "qa-ready", "merged", "bugbot", "ci", "alarm", "blocked"]
DONE = {"merged", "done"}
FALSE = {"false", "no", "off", "0"}
RESULTS = {"moved", "already", "skipped", "unavailable", "no-transition", "failed"}
OK_RESULTS = {"moved", "already", "skipped"}
TODO_NAME = "jira-todo.json"

# Tool names on the official Atlassian remote MCP server. `jiraMcp` (default
# "atlassian") is the Cursor server name, not one of these.
TOOL_RESOURCES = "getAccessibleAtlassianResources"
TOOL_ISSUE = "getJiraIssue"
TOOL_TRANSITIONS = "getTransitionsForJiraIssue"
TOOL_TRANSITIONS_ALT = "listJiraIssueTransitions"
TOOL_TRANSITION = "transitionJiraIssue"
TOOL_COMMENT = "addOrEditJiraIssueComment"
TOOL_COMMENT_ALT = "addCommentToJiraIssue"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(beam_path: Path, beam: dict | None = None) -> dict:
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
            if isinstance(default, bool):
                out[key] = v if isinstance(v, bool) else str(v).lower() not in FALSE
            elif str(v).strip():
                out[key] = str(v).strip()
    return out


def jira_key(ticket: dict) -> str | None:
    """The explicit jiraKey field only. Does not look at the id or the summary."""
    key = ticket.get("jiraKey")
    return key if isinstance(key, str) and KEY_RE.match(key) else None


def infer_key(ticket: dict) -> str | None:
    """Explicit jiraKey, else the id, else the first key in the summary or branch."""
    explicit = jira_key(ticket)
    if explicit:
        return explicit
    tid = str(ticket.get("id") or "")
    if re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-\d+", tid):
        return tid
    for field in ("summary", "branch"):
        m = INFER_RE.search(str(ticket.get(field) or ""))
        if m:
            return m.group(1)
    return None


def plan(ticket: dict, event: str, cfg: dict) -> dict:
    """What should happen to the Jira issue for this event. Uses the explicit key only."""
    out = {"id": ticket.get("id"), "jiraKey": jira_key(ticket), "event": event, "action": "skip", "reason": ""}
    jira = ticket.get("jira") or {}
    auto = bool(ticket.get("autoMerge"))
    if not out["jiraKey"]:
        out["reason"] = "no Jira key on this ticket"
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
        if not auto:
            out["reason"] = f"manual-path ticket; it stays at {cfg['jiraQaReadyStatus']} for QA, Warp does not move it to Done"
        elif jira.get("doneAt"):
            out["reason"] = "already moved to Done"
        else:
            out.update(action="transition", target=cfg["jiraDoneStatus"], kind="done")
    return out


def pick(transitions: list[dict], target: str, current: dict | None = None, allow_category: bool = True, kind: str = "start") -> dict:
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
    return (
        f"jira: {p['id']} ({p['jiraKey']}): {VERB[p['event']]} \"{p['target']}\" through the connected Jira MCP server. "
        f"Read the issue with {TOOL_ISSUE} and its transitions with {TOOL_TRANSITIONS} "
        f"(or {TOOL_TRANSITIONS_ALT} if that is the name the server lists), then run: python3 {me} pick --kind {p['kind']} --target \"{p['target']}\" "
        f"--current '<{{\"name\":..., \"category\":...}}>' --transitions '<json>'. "
        f"Call {TOOL_TRANSITION} with the id it picks (argument transition.id, or transitionId if that is the schema's field). "
        f"Then record: python3 {me} record --beam <beam> --id {p['id']} --event {p['event']} "
        f"--result moved|already|skipped|unavailable|no-transition|failed --from '<old status>' --to '<new status>'. "
        "If Jira is not connected or nothing fits, record that and carry on. Nothing else is blocked."
    )


def _bodies(ticket: dict, root: Path, mode: str) -> dict[str, str]:
    key = jira_key(ticket) or infer_key(ticket) or ticket.get("id")
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
    return {
        "claim": f"{head}\nStarted. Shuttle {agent}. Branch {branch}.",
        "pr-opened-jira": f"{head}\nPull request: {url}",
        "pr-opened-pr": f"{head}\nTicket {tid}. Jira {key}.",
        "qa-ready": f"{head}\nWaiting for a person. Review and merge, or reply warp:proceed {tid}.",
        "merged": f"{head}\nMerged ({where}). PR {url}. sha {sha}.",
        "bugbot": f"{head}\n{bug_line}",
        "ci": f"{head}\nCI: {pr.get('ci')}.",
        "alarm": f"{head}\nBlocked ({alarm}). Needs a person. warp:retry {tid}.",
        "blocked": f"{head}\nBlocked ({alarm}). Needs a person. warp:retry {tid}.",
    }


def _comment_action(ticket: dict, where: str, event: str, body: str) -> dict | None:
    if event in _comments(ticket, where):
        return None
    return {"type": "comment", "where": where, "event": event, "body": body}


def event_for(ticket: dict, prev: str, new: str) -> str | None:
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
    if not cfg.get("jiraTransition", True) or not jira_key(ticket):
        return []
    prev = before.get("status")
    new = ticket.get("status")
    event = event_for(ticket, prev, new) if prev != new else None
    actions: list[dict] = []
    if event:
        p = plan(ticket, event, cfg)
        if p["action"] == "transition":
            actions.append({"type": "transition", "plan": p})
    bodies = _bodies(ticket, root, mode)
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
    return {
        "id": ticket.get("id"),
        "jiraKey": jira_key(ticket),
        "server": cfg.get("jiraMcp") or "atlassian",
        "jiraSite": site,
        "mode": mode,
        "cloudId": (
            f"Call {TOOL_RESOURCES} on server {cfg.get('jiraMcp') or 'atlassian'}. "
            + (f"jiraSite is set; you may pass {site} as cloudId. " if site else "jiraSite is empty; do not guess a site. ")
            + "Every Jira call needs cloudId and the issue key."
        ),
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
            "github": "add_issue_comment on the pull request number, or `gh pr comment <url> --body`",
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
    key = jira_key(ticket)
    server = cfg.get("jiraMcp") or "atlassian"
    site = cfg.get("jiraSite") or ""
    site_bit = f" jiraSite is {site!r}; you may pass that URL as cloudId." if site else " jiraSite is empty; do not guess a site."
    me = Path(__file__).resolve()
    lines = [
        f'jira: MUST DO {ticket.get("id")} ({key}). Server "{server}" is the jiraMcp name in config (a Cursor MCP server, not a tool name).',
        f"jira: cloudId: call {TOOL_RESOURCES} on that server.{site_bit} Every Jira call needs cloudId and the issue key.",
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
                    "jira: pull-request comment, connected mode only. GitHub: add_issue_comment on the pull request number, "
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


def on_set(beam_path: Path, beam: dict, ticket: dict, prev, new: str | None = None) -> None:
    """Called by beam.py set. Prints what the agent must do. Never raises.

    `prev` is a snapshot dict from before the edit, or a status string.
    """
    try:
        before = _snapshot(prev, ticket)
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
        if not jira_key(ticket):
            if event or ((ticket.get("pr") or {}).get("url") and (ticket.get("pr") or {}).get("url") != before.get("pr_url")):
                print(instruction(plan(ticket, event or "claim", cfg)))
            _write_todo(beam_path, [])
            return
        if not cfg["jiraTransition"]:
            if watched:
                print(instruction(plan(ticket, event or "claim", cfg)))
            _write_todo(beam_path, [])
            return
        actions = actions_for(ticket, before, cfg, mode, root)
        if not actions:
            _write_todo(beam_path, [])
            return
        payload = _todo_payload(ticket, actions, cfg, mode)
        _write_todo(beam_path, [payload])
        print(render(ticket, actions, cfg))
        _journal(beam_path, {"type": "jira-intent", "id": ticket.get("id"), "actions": [a.get("event") or a.get("plan", {}).get("event") for a in actions]})
    except Exception as e:  # a Jira problem must not break a status change
        print(f"jira: skipped ({e})")


def _loud(beam_path: Path, note: str) -> None:
    """Outbox is already written. Also ask Herald to post the same failure."""
    root = _root(beam_path)
    try:
        import herald_fmt
        import notify

        msg = herald_fmt.message(
            root,
            "Jira not updated",
            intro=note,
            footer="Run /warp-jira-check. The ticket was not stopped.",
        )
        notify.report(notify.build("jira-failed", root, msg=msg))
    except Exception as e:
        print(f"herald: not posted ({e})")


def record(beam_path: Path, tid: str, event: str, result: str, frm: str | None, to: str | None, detail: str | None) -> str:
    beam = _load(beam_path)
    t = beam["tickets"].get(tid)
    if not t:
        return f"jira: unknown ticket {tid}; nothing recorded"
    jira = t.setdefault("jira", {"status": None, "lastCommentAt": None})
    jira["lastSync"] = {"event": event, "result": result, "at": now(), "detail": detail}
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
    key = t.get("jiraKey")
    why = {
        "unavailable": "Jira is not connected",
        "no-transition": "no matching transition was available",
        "failed": "the Jira call failed",
    }.get(result, result)
    note = f"Jira status not updated for {tid} ({key}): {why}." + (f" {detail}" if detail else "")
    if event != "release":
        default = {"claim": "In Progress", "qa-ready": "QA Ready", "done": "Done"}.get(event, "In Progress")
        note += f" Move {key} to \"{to or default}\" by hand if you want it to match."
        if event == "done":
            note += " The merge stands; Warp retries on the next tick."
    _outbox(beam_path, note)
    _loud(beam_path, note)
    return f"jira: {note} Saved to .warp/outbox.md. The claim was not affected."


def record_comment(beam_path: Path, tid: str, where: str, event: str, comment_id: str | None) -> str:
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
    key = jira_key(ticket) or infer_key(ticket)
    if not key:
        return [], "no Jira key, and none found in the id, summary, or branch"
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
        if auto:
            items.append({"type": "transition", "event": "done", "target": cfg["jiraDoneStatus"]})
        else:
            items.append({"type": "transition", "event": "qa-ready", "target": cfg["jiraQaReadyStatus"]})
        items.append({"type": "comment", "where": "jira", "event": "merged"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "merged"})
        return items, note
    items.append({"type": "transition", "event": "claim", "target": cfg["jiraInProgressStatus"]})
    items.append({"type": "comment", "where": "jira", "event": "claim"})
    if url:
        items.append({"type": "comment", "where": "jira", "event": "pr-opened"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "pr-opened"})
    if status == "awaiting_approval" and not auto:
        items.append({"type": "transition", "event": "qa-ready", "target": cfg["jiraQaReadyStatus"]})
        items.append({"type": "comment", "where": "jira", "event": "qa-ready"})
        if _wants_pr(ticket, mode):
            items.append({"type": "comment", "where": "pr", "event": "qa-ready"})
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
    explicit = jira_key(ticket)
    inferred = infer_key(ticket)
    return {
        "id": ticket.get("id"),
        "jiraKey": explicit,
        "inferredKey": None if explicit else inferred,
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
        },
        "should": [_label(i) for i in items],
        "missing": [_label(i) for i in missing],
        "missingItems": missing,
        "note": note,
    }


def _actions_from_missing(ticket: dict, missing: list[dict], cfg: dict, mode: str, root: Path) -> list[dict]:
    """Turn diagnose gaps into the same actions on_set prints, without historical claim comments on a merged ticket."""
    bodies = _bodies(ticket, root, mode)
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


def format_diagnosis(row: dict) -> str:
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
        f"beam: {row['status']}  autoMerge: {row['autoMerge']}  mode: {row['mode']}",
        f"recorded: startedAt={rec['startedAt'] or '(none)'} qaReadyAt={rec['qaReadyAt'] or '(none)'} doneAt={rec['doneAt'] or '(none)'}",
        f"comments jira: {jira_comments}",
        f"comments pr: {pr_comments}",
        "should: " + ("; ".join(row["should"]) or "(nothing)"),
        "missing: " + ("; ".join(row["missing"]) or "(nothing)"),
    ]
    if rec.get("lastSync"):
        lines.append(f"lastSync: {rec['lastSync'].get('event')} {rec['lastSync'].get('result')}")
    if row["note"]:
        lines.append(f"note: {row['note']}")
    return "\n".join(lines)


def verify(beam_path: Path, tid: str | None = None) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    mode = _mode(beam_path)
    ids = [tid] if tid else list(beam["tickets"])
    parts = []
    for i in ids:
        t = beam["tickets"].get(i)
        if not t:
            parts.append(f"jira: unknown ticket {i}")
            continue
        parts.append(format_diagnosis(diagnose(t, cfg, mode)))
    return "\n\n".join(parts)


def catchup(beam_path: Path, tid: str | None, write: bool) -> str:
    beam = _load(beam_path)
    cfg = settings(beam_path, beam)
    mode = _mode(beam_path)
    root = _root(beam_path)
    ids = [tid] if tid else list(beam["tickets"])
    stamped = []
    if write:
        for i in ids:
            t = beam["tickets"].get(i)
            if t and not jira_key(t):
                k = infer_key(t)
                if k:
                    t["jiraKey"] = k
                    stamped.append(i)
        if stamped:
            _save(beam_path, beam)
    lines = []
    payloads = []
    if stamped:
        lines.append("jira: stored inferred keys for " + ", ".join(stamped))
    for i in ids:
        t = beam["tickets"].get(i)
        if not t:
            lines.append(f"jira: unknown ticket {i}")
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
        lines.append(render(t, actions, cfg))
    _write_todo(beam_path, payloads)
    if not lines:
        lines.append("jira: nothing missing")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Jira status sync for claims")
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
    pc = sub.add_parser("record-comment")
    pc.add_argument("--beam", default=".warp/beam.json")
    pc.add_argument("--id", required=True)
    pc.add_argument("--where", choices=["jira", "pr"], required=True)
    pc.add_argument("--event", choices=COMMENT_EVENTS, required=True)
    pc.add_argument("--comment-id")
    pv = sub.add_parser("verify")
    pv.add_argument("--beam", default=".warp/beam.json")
    pv.add_argument("--id")
    pu = sub.add_parser("catchup")
    pu.add_argument("--beam", default=".warp/beam.json")
    pu.add_argument("--id")
    pu.add_argument("--write", action="store_true", help="store an inferred jiraKey on the ticket")
    args = p.parse_args()
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
            print(record(Path(args.beam), args.id, args.event, args.result, args.frm, args.to, args.detail))
        elif args.cmd == "record-comment":
            print(record_comment(Path(args.beam), args.id, args.where, args.event, args.comment_id))
        elif args.cmd == "verify":
            print(verify(Path(args.beam), args.id))
        else:
            print(catchup(Path(args.beam), args.id, args.write))
    except Exception as e:  # fail-soft: report, do not fail the caller
        print(f"jira: skipped ({e})")


if __name__ == "__main__":
    main()
