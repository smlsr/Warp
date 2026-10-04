#!/usr/bin/env python3
"""Move a Jira issue to In Progress when a ticket is claimed. Fail-soft.

Warp has no Jira credentials. Like Herald's Slack posts, the Jira call is made
by an agent through the connected Jira MCP server. This script decides whether
a Jira move is wanted, helps pick the right transition from the ones Jira
offers (by name or status category, never a hardcoded id), and records what
happened. It never blocks a claim.

  plan    --beam B --id ID --event claim|release   what the agent should do
  pick    --target NAME --transitions JSON ...     choose a transition
  record  --beam B --id ID --event E --result R    store the outcome

Settings (.warp/config.yaml, else the beam copy, else defaults):
  jiraTransition          true   move to In Progress on claim
  jiraInProgressStatus    "In Progress"
  jiraRestoreOnRelease    false  on release (back to queued), move back to the
                                 status the issue had before the claim

Only tickets with a Jira key are touched. A markdown plan has none, so it is
skipped. Pause and stop never move an issue.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
ACTIVE = {"claimed", "planning", "coding", "review", "fix", "awaiting_approval", "merging"}
DEFAULTS = {"jiraTransition": True, "jiraInProgressStatus": "In Progress", "jiraRestoreOnRelease": False}
FALSE = {"false", "no", "off", "0"}
RESULTS = {"moved", "already", "skipped", "unavailable", "no-transition", "failed"}
OK_RESULTS = {"moved", "already", "skipped"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(beam_path: Path, beam: dict | None = None) -> dict:
    """Defaults, then the beam's config copy, then .warp/config.yaml."""
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
            if key not in src:
                continue
            v = src[key]
            if isinstance(default, bool):
                out[key] = v if isinstance(v, bool) else str(v).lower() not in FALSE
            elif str(v).strip():
                out[key] = str(v).strip()
    return out


def jira_key(ticket: dict) -> str | None:
    key = ticket.get("jiraKey")
    return key if isinstance(key, str) and KEY_RE.match(key) else None


def plan(ticket: dict, event: str, cfg: dict) -> dict:
    """What should happen to the Jira issue for this event."""
    out = {"id": ticket.get("id"), "jiraKey": jira_key(ticket), "event": event, "action": "skip", "reason": ""}
    jira = ticket.get("jira") or {}
    if not out["jiraKey"]:
        out["reason"] = "no Jira key on this ticket"
    elif not cfg["jiraTransition"]:
        out["reason"] = "jiraTransition is false"
    elif event == "claim":
        if jira.get("startedAt"):
            out["reason"] = "already moved by Warp"
        else:
            out.update(action="transition", target=cfg["jiraInProgressStatus"], allowCategory=True)
    elif event == "release":
        prev = jira.get("previousStatus")
        if not cfg["jiraRestoreOnRelease"]:
            out["reason"] = "jiraRestoreOnRelease is false; issue left as is"
        elif not jira.get("startedAt") or not prev:
            out["reason"] = "Warp did not move this issue, nothing to restore"
        else:
            out.update(action="transition", target=prev, allowCategory=False)
    return out


def pick(transitions: list[dict], target: str, current: dict | None = None, allow_category: bool = True) -> dict:
    """Choose one of the transitions Jira offers. Names are matched case-insensitively.

    Accepts Jira's shape ({id, name, to: {name, statusCategory: {key}}}) or a flat
    one ({id, name, toName, category}). `current` is {name, category}.
    """
    want = target.casefold()
    if current:
        cname = (current.get("name") or "").casefold()
        ccat = (current.get("category") or "").casefold()
        if cname == want:
            return {"transition": None, "result": "already", "reason": f"already {target}"}
        if allow_category and ccat == "indeterminate":
            return {"transition": None, "result": "already", "reason": f"already in progress ({current.get('name')})"}
        if allow_category and ccat == "done":
            return {"transition": None, "result": "skipped", "reason": f"issue is {current.get('name')}; not reopened"}

    def to_name(t):
        return ((t.get("to") or {}).get("name") or t.get("toName") or "").casefold()

    def to_cat(t):
        return (((t.get("to") or {}).get("statusCategory") or {}).get("key") or t.get("category") or "").casefold()

    for how, ok in (("name", lambda t: (t.get("name") or "").casefold() == want), ("status", lambda t: to_name(t) == want)):
        hits = [t for t in transitions if ok(t)]
        if hits:
            return {"transition": hits[0], "result": "pick", "how": how}
    if allow_category:
        cats = [t for t in transitions if to_cat(t) == "indeterminate"]
        if len(cats) == 1:
            return {"transition": cats[0], "result": "pick", "how": "category"}
        if cats:
            prog = [t for t in cats if "progress" in to_name(t) or "progress" in (t.get("name") or "").casefold()]
            if prog:
                return {"transition": prog[0], "result": "pick", "how": "category"}
            return {"transition": None, "result": "no-transition", "reason": f"several in-progress-like transitions; none is named {target}"}
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


def instruction(p: dict) -> str:
    me = Path(__file__).resolve()
    if p["action"] == "skip":
        return f"jira: {p['id']}: no Jira move ({p['reason']})."
    verb = "move to" if p["event"] == "claim" else "move back to"
    cat = "" if p["allowCategory"] else " --no-category"
    return (
        f"jira: {p['id']} ({p['jiraKey']}): {verb} \"{p['target']}\" through the connected Jira MCP server. "
        f"Read the issue status and its available transitions, then run: python3 {me} pick --target \"{p['target']}\"{cat} "
        f"--current '<{{\"name\":..., \"category\":...}}>' --transitions '<json>'. Run the transition it picks. "
        f"Then record: python3 {me} record --beam <beam> --id {p['id']} --event {p['event']} "
        f"--result moved|already|skipped|unavailable|no-transition|failed --from '<old status>' --to '<new status>'. "
        "If Jira is not connected or nothing fits, record that and carry on. The claim stands."
    )


def on_set(beam_path: Path, beam: dict, ticket: dict, prev: str, new: str) -> None:
    """Called by beam.py set. Prints what the agent should do. Never raises."""
    try:
        if new == prev:
            return
        if new == "claimed" and prev not in ACTIVE:
            event = "claim"
        elif new == "queued" and prev in ACTIVE:
            event = "release"
        else:
            return
        p = plan(ticket, event, settings(beam_path, beam))
        if p["action"] == "skip" and event == "release" and p["reason"].startswith("jiraRestoreOnRelease"):
            return
        print(instruction(p))
        _journal(beam_path, {"type": "jira-intent", "id": p["id"], "event": event, "action": p["action"], "reason": p["reason"]})
    except Exception as e:  # a Jira problem must not break a claim
        print(f"jira: skipped ({e})")


def record(beam_path: Path, tid: str, event: str, result: str, frm: str | None, to: str | None, detail: str | None) -> str:
    beam = _load(beam_path)
    t = beam["tickets"].get(tid)
    if not t:
        return f"jira: unknown ticket {tid}; nothing recorded"
    jira = t.setdefault("jira", {"status": None, "lastCommentAt": None})
    jira["lastSync"] = {"event": event, "result": result, "at": now(), "detail": detail}
    if result == "moved":
        if event == "claim":
            jira["startedAt"] = now()
            jira["previousStatus"] = frm
        else:
            jira.pop("startedAt", None)
            jira.pop("previousStatus", None)
        jira["status"] = to
    elif result in {"already", "skipped"} and to:
        jira["status"] = to
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
    if event == "claim":
        note += f" Move {key} to \"{to or 'In Progress'}\" by hand if you want it to match."
    _outbox(beam_path, note)
    return f"jira: {note} Saved to .warp/outbox.md. The claim was not affected."


def main() -> None:
    p = argparse.ArgumentParser(description="Jira status sync for claims")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("--beam", default=".warp/beam.json")
    pp.add_argument("--id", required=True)
    pp.add_argument("--event", choices=["claim", "release"], required=True)
    pk = sub.add_parser("pick")
    pk.add_argument("--target", required=True)
    pk.add_argument("--transitions", help='JSON list, or {"transitions": [...]}')
    pk.add_argument("--transitions-file")
    pk.add_argument("--current", help='JSON {"name":..., "category":...}')
    pk.add_argument("--no-category", action="store_true")
    pr = sub.add_parser("record")
    pr.add_argument("--beam", default=".warp/beam.json")
    pr.add_argument("--id", required=True)
    pr.add_argument("--event", choices=["claim", "release"], required=True)
    pr.add_argument("--result", choices=sorted(RESULTS), required=True)
    pr.add_argument("--from", dest="frm")
    pr.add_argument("--to")
    pr.add_argument("--detail")
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
            print(json.dumps(pick(transitions, args.target, current, not args.no_category), indent=2))
        else:
            print(record(Path(args.beam), args.id, args.event, args.result, args.frm, args.to, args.detail))
    except Exception as e:  # fail-soft: report, do not fail the caller
        print(f"jira: skipped ({e})")


if __name__ == "__main__":
    main()
