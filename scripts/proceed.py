#!/usr/bin/env python3
"""Resolve warp:proceed and say whether a manual ticket may merge.

Warp does not call GitHub, Bitbucket, or Jira. This script matches the token
the person wrote (plan id, Jira key, or a #number), refuses anything that is
not awaiting approval, and prints the merge plus the post-merge steps as one
turn. There is no --force.

  python3 scripts/proceed.py --beam .warp/beam.json WV-01
  python3 scripts/proceed.py --beam .warp/beam.json "warp:proceed WAR-1"
  python3 scripts/proceed.py --beam .warp/beam.json --by alex "#01"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beam  # noqa: E402
import herald_fmt  # noqa: E402
import jira_sync  # noqa: E402
import notify  # noqa: E402

PROCEED_RE = re.compile(r"^warp:proceed\s+", re.I)
NUMBER_RE = re.compile(r"^#*(\d+)$")


def _root(beam_path: Path) -> Path:
    beam_path = Path(beam_path)
    return beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent


def normalize_token(raw: str) -> str:
    text = raw.strip().strip("'\"")
    text = PROCEED_RE.sub("", text).strip().strip("'\"")
    return text


def _forms(token: str) -> list[str]:
    text = normalize_token(token)
    forms = [text] if text else []
    if text.startswith("#") and not NUMBER_RE.fullmatch(text):
        stripped = text.lstrip("#").strip()
        if stripped and stripped not in forms:
            forms.append(stripped)
    return forms


def _tail(value: str) -> str:
    return str(value).rsplit("-", 1)[-1]


def resolve_token(data: dict, token: str) -> list[tuple[str, dict, str]]:
    """Match a plan id, a Jira key, or a #number. Exact id wins over a number."""
    tickets = data.get("tickets") or {}
    forms = _forms(token)
    if not forms:
        return []
    hits: list[tuple[str, dict, str]] = []
    seen = set()

    def add(tid: str, how: str) -> None:
        if tid in seen or tid not in tickets:
            return
        seen.add(tid)
        hits.append((tid, tickets[tid], how))

    for form in forms:
        low = form.casefold()
        for tid in tickets:
            if str(tid).casefold() == low:
                add(str(tid), "id")
    if hits:
        return hits
    for form in forms:
        key = form.strip().upper()
        if not jira_sync.KEY_RE.fullmatch(key):
            continue
        for tid, t in tickets.items():
            stored = str(t.get("jiraKey") or "").strip().upper()
            if stored and stored == key:
                add(str(tid), "jira")
    if hits:
        return hits
    number = ""
    for form in forms:
        m = NUMBER_RE.fullmatch(form.strip())
        if m:
            number = m.group(1)
            break
    if not number:
        return []
    exact: list[tuple[str, dict, str]] = []
    loose: list[tuple[str, dict, str]] = []
    want_int = str(int(number))
    for tid, t in tickets.items():
        tails = [_tail(str(tid))]
        if t.get("jiraKey"):
            tails.append(_tail(str(t.get("jiraKey"))))
        if any(tail == number for tail in tails):
            exact.append((str(tid), t, "number"))
        elif any(tail.isdigit() and str(int(tail)) == want_int for tail in tails):
            loose.append((str(tid), t, "number"))
    return exact or loose


def _state_line(ticket: dict) -> str:
    pr = ticket.get("pr") or {}
    jira = ticket.get("jira") or {}
    return (
        f"status {ticket.get('status')} bugbot={pr.get('bugbot') or 'none'} "
        f"ci={pr.get('ci') or 'none'} jira={jira.get('status') or 'none'} "
        f"qaReadyAt={jira.get('qaReadyAt') or 'none'} doneAt={jira.get('doneAt') or 'none'}"
    )


def _reply(root: Path, text: str) -> None:
    """Write the Slack/Teams payload. A person asked, so quiet notify still posts."""
    try:
        cfg = herald_fmt.read_config(root)
        msg = herald_fmt.message(
            root,
            "Proceed",
            intro=text,
            footer="warp:proceed merges only a ticket that is awaiting approval.",
        )
        payload = {"kind": "proceed", **msg, "header": herald_fmt.header(root, cfg)}
        targets, notes = notify.targets(cfg)
        payload["targets"] = targets
        payload["notes"] = notes
        if targets:
            payload["action"] = "post"
        else:
            payload["action"] = "outbox"
            notify.outbox(root, payload["text"])
            payload["notes"].append("no channel configured; message saved to .warp/outbox.md")
        from datetime import datetime, timezone

        payload["createdAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = root / ".warp" / notify.PAYLOAD
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
        notify.report(payload)
    except Exception as e:
        print(f"herald: not posted ({e})")


def _mode(root: Path) -> str:
    try:
        import provider

        return provider.resolve(root).get("mode") or "local"
    except Exception:
        return "local"


def describe(beam_path: Path, token: str, by: Optional[str]) -> tuple[int, str]:
    """Return a process status and the text to print. Does not merge."""
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        reply = f"warp:proceed refused. No beam at {beam_path}."
        return 1, reply
    data = json.loads(beam_path.read_text())
    hits = resolve_token(data, token)
    root = _root(beam_path)
    if not hits:
        reply = f"warp:proceed refused. No ticket matches {normalize_token(token) or token!r}."
        _reply(root, reply)
        return 1, "proceed: refused\n" + reply + f"\nreply: {reply}"
    if len(hits) > 1:
        listed = ", ".join(f"{tid} ({how})" for tid, _, how in hits)
        reply = f"warp:proceed refused. {normalize_token(token)} matches more than one ticket: {listed}."
        _reply(root, reply)
        return 1, "proceed: refused\n" + reply + f"\nreply: {reply}"
    tid, ticket, how = hits[0]
    pr = ticket.setdefault("pr", {})
    state = _state_line(ticket)
    if ticket.get("status") != "awaiting_approval":
        reply = (
            f"{tid} is {ticket.get('status')}, not awaiting approval. Not merged. {state}."
        )
        _reply(root, reply)
        return 1, f"proceed: {tid} matched by {how}\nproceed: refused\n{state}\nreply: {reply}"
    cfg = jira_sync.settings(beam_path, data)
    block = jira_sync.review_block(ticket, cfg)
    if block:
        reply = f"{tid} is awaiting_approval but the gate is closed: {block}. Not merged."
        _reply(root, reply)
        return 1, f"proceed: {tid} matched by {how}\nproceed: refused\n{reply}\nreply: {reply}"
    who = by or "warp:proceed"
    pr["proceededBy"] = who
    if not pr.get("approvedBy"):
        pr["approvedBy"] = who
    if not pr.get("approvedAt"):
        pr["approvedAt"] = jira_sync.now()
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    beam.journal(beam_path, {"type": "proceed", "id": tid, "by": who, "matched": how})
    mode = _mode(root)
    target = cfg.get("jiraDoneStatus") or "Done"
    branch = ticket.get("branch") or "<branch>"
    beam_py = Path(beam.__file__).resolve()
    provider_py = Path(__file__).resolve().parent / "provider.py"
    lines = [
        f"proceed: {tid} matched by {how}",
        f"proceed: awaiting_approval {state}",
        "proceed: merge in this turn. Do not stop after the provider merge.",
    ]
    if mode == "local":
        lines.append(
            "proceed: local mode. Run this. It squash-merges and sets status merged, "
            "which prints the post-merge MUST DO:"
        )
        lines.append(
            f"python3 {provider_py} merge-local --root {root} --beam {beam_path} --id {tid} --branch {branch}"
        )
    else:
        lines.append(
            "proceed: connected mode. Squash-merge the pull request, then run this in the same turn. "
            "Do not stop after the GitHub or Bitbucket merge:"
        )
        lines.append(
            f"python3 {beam_py} set --beam {beam_path} --id {tid} --status merged "
            f"--sha <sha> --via connected --proceeded-by {who} --approved-by {who} --merge-method squash"
        )
    if jira_sync.moves_to_done(ticket, cfg):
        lines.append(
            f"proceed: the set prints one post-merge MUST DO. Run every line: move Jira to {target}, "
            "post the merged Jira and pull-request comments, record them, and post Slack. "
            "Locks release when the status leaves the active set. Dependents whose deps are terminal become ready."
        )
        reply = (
            f"{tid}: warp:proceed accepted. Merge now. "
            f"Jira moves to {target} after the merge. Reply with the merge sha and Jira status {target}."
        )
    else:
        qa = cfg.get("jiraQaReadyStatus") or "QA Ready"
        lines.append(
            f"proceed: jiraDoneOnManualMerge is false. Post the merged comment and leave Jira at {qa}."
        )
        reply = f"{tid}: warp:proceed accepted. Merge now. Jira stays at {qa}."
    lines.append(f"reply: {reply}")
    lines.append(f"proceed: recorded by {who}. Status is still awaiting_approval until the merge is set.")
    _reply(root, reply)
    return 0, "\n".join(lines)


HELP = """
examples:
  python3 scripts/proceed.py ?
  python3 scripts/proceed.py --beam .warp/beam.json WV-01
  python3 scripts/proceed.py --beam .warp/beam.json "warp:proceed WAR-1"
  python3 scripts/proceed.py --beam .warp/beam.json --by alex "#01"

The token is a plan id (WV-01), a Jira key (WAR-1), or a #number such as #01.
A ticket that is not awaiting_approval is refused. The current status is the
reply. Nothing is merged. There is no --force.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Resolve warp:proceed. Refuse unless the ticket is awaiting approval.",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--beam", default=".warp/beam.json")
    p.add_argument("--by", help="who said warp:proceed; stored on pr.proceededBy")
    p.add_argument("token", nargs="+", help="plan id, Jira key, #number, or the whole warp:proceed line")
    import usage

    args = p.parse_args(usage.normalize_argv(argv))
    code, text = describe(Path(args.beam), " ".join(args.token), args.by)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
