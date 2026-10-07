#!/usr/bin/env python3
"""Parse, acknowledge, and apply one inbound warp: command.

Warp has no Slack webhook and does not run plugin hooks on cloud runners.
The one channel listener (skills/warp-listen) reads Slack and Teams, then
runs this script. This script does not call Slack. It writes the ack to
.warp/inbound-ack.json before it changes the beam. Herald posts that ack,
then the listener does the action.

One listener for the beam is tracked at beam["listener"]:
  state    running or stopped. The flag. running means one listener owns
           the channel. stopped means it must not keep reading.
  agentId  the one Agent id for the beam. A second claim does not replace it while
           state is running.
  pid      optional process id. Cloud agents often have none.
  startedAt, stoppedAt, lastSeenAt
  recoveredAt, recoveries   set when the watchdog replaces a dead listener

  python3 scripts/inbound.py claim --beam .warp/beam.json --agent-id <id>
  python3 scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id <id>
  python3 scripts/inbound.py release --beam .warp/beam.json
  python3 scripts/inbound.py handle --beam .warp/beam.json --text "warp:proceed XV-01"
  python3 scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:status" --message-id 1
  python3 scripts/inbound.py drain --beam .warp/beam.json
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
import proceed  # noqa: E402

ACCEPTED_FORMS = (
    "warp:pause, warp:resume, warp:stop, warp:start, "
    "warp:proceed <id>, warp:retry <id>, warp:status"
)
VERB_RE = re.compile(r"^warp:\s*(pause|resume|stop|start|proceed|retry|status)\b\s*(.*?)\s*$", re.I)
ACK_NAME = "inbound-ack.json"
PENDING_NAME = "pending-commands.jsonl"
NEEDS_ID = {"proceed", "retry"}


def _root(beam_path: Path) -> Path:
    beam_path = Path(beam_path)
    return beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent


def listener_of(data: dict) -> dict:
    """The one listener record. Missing or incomplete means stopped."""
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    state = raw.get("state") if raw.get("state") in {"running", "stopped"} else "stopped"
    agent_id = raw.get("agentId") or None
    if state == "running" and not agent_id:
        state = "stopped"
    pid = raw.get("pid")
    if pid in ("", None):
        pid = None
    return {
        "state": state,
        "agentId": agent_id,
        "pid": pid,
        "startedAt": raw.get("startedAt"),
        "stoppedAt": raw.get("stoppedAt"),
    }


def is_running(data: dict) -> bool:
    cur = listener_of(data)
    return cur["state"] == "running" and bool(cur["agentId"])


def status_line(data: dict) -> str:
    cur = listener_of(data)
    if cur["state"] == "running" and cur["agentId"]:
        pid = " pid=%s" % cur["pid"] if cur.get("pid") else ""
        return "listener: running %s%s" % (cur["agentId"], pid)
    return "listener: stopped"


def claim(beam_path: Path, agent_id: str, pid: Optional[str] = None, turn: Optional[str] = None) -> str:
    """Take the listener slot for this parent turn.

    The same `--turn` while the slot is running prints `already running` and
    does not start a second subagent. A new turn, or a new id with no turn,
    takes the slot even when the previous turn left `state` running. That
    flag must not block the next tick. The same id with no turn is idempotent.
    """
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return "listener: no beam at %s" % beam_path
    if not agent_id or not str(agent_id).strip():
        return "listener: claim needs --agent-id"
    agent_id = str(agent_id).strip()
    turn_id = str(turn).strip() if turn else ""
    data = beam.load_json(beam_path)
    cur = listener_of(data)
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    stored_turn = str(raw.get("turn") or "")
    if cur["state"] == "running" and cur["agentId"]:
        same_turn = bool(turn_id) and stored_turn == turn_id
        same_id = (not turn_id) and cur["agentId"] == agent_id
        if same_turn or same_id:
            if pid and cur["agentId"] == agent_id and str(cur.get("pid") or "") != str(pid):
                raw["pid"] = str(pid)
                data["listener"] = raw
                beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
            return "listener: already running %s" % cur["agentId"]
        beam.journal(
            beam_path,
            {"type": "listener-stop", "agentId": cur["agentId"], "reason": "turn-ended"},
        )
    now = beam.utcnow()
    record = {
        "state": "running",
        "agentId": agent_id,
        "pid": str(pid) if pid else None,
        "startedAt": now,
        "stoppedAt": None,
        "lastSeenAt": now,
    }
    if turn_id:
        record["turn"] = turn_id
    data["listener"] = record
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    beam.journal(beam_path, {"type": "listener-start", "agentId": agent_id, "pid": record["pid"], "turn": turn_id or None})
    return "listener: started %s" % agent_id


def heartbeat(beam_path: Path, agent_id: str) -> str:
    """Record lastSeenAt for the running listener. The agent id must own the slot."""
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return "listener: no beam at %s" % beam_path
    if not agent_id or not str(agent_id).strip():
        return "listener: heartbeat needs --agent-id"
    agent_id = str(agent_id).strip()
    data = beam.load_json(beam_path)
    cur = listener_of(data)
    if cur["state"] != "running" or not cur.get("agentId"):
        return "listener: heartbeat refused (stopped)"
    if cur["agentId"] != agent_id:
        return "listener: heartbeat refused %s" % cur["agentId"]
    raw = data.get("listener") if isinstance(data.get("listener"), dict) else {}
    raw["lastSeenAt"] = beam.utcnow()
    raw["agentId"] = agent_id
    raw["state"] = "running"
    data["listener"] = raw
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    return "listener: heartbeat %s" % agent_id


def release(beam_path: Path) -> str:
    """Stop the one listener. Idempotent. Does not launch another."""
    beam_path = Path(beam_path)
    if not beam_path.is_file():
        return "listener: no beam at %s" % beam_path
    data = beam.load_json(beam_path)
    cur = listener_of(data)
    if cur["state"] != "running":
        return "listener: already stopped"
    data["listener"] = {
        "state": "stopped",
        "agentId": cur.get("agentId"),
        "pid": cur.get("pid"),
        "startedAt": cur.get("startedAt"),
        "stoppedAt": beam.utcnow(),
    }
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    beam.journal(beam_path, {"type": "listener-stop", "agentId": cur.get("agentId")})
    who = cur.get("agentId") or ""
    return ("listener: stopped %s" % who).rstrip()


def first_command_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("warp:"):
            return stripped
    return (text or "").strip()


def parse(text: str) -> Optional[dict]:
    """A warp: command, or None when the text is not one.

    ok is false for a warp: line whose verb is unknown or whose id is missing.
    """
    line = first_command_line(text)
    if not line.lower().startswith("warp:"):
        return None
    match = VERB_RE.match(line)
    if not match:
        shown = " ".join(line.split())
        return {"ok": False, "verb": None, "token": "", "rest": "", "raw": line, "command": shown}
    verb = match.group(1).lower()
    rest = match.group(2).strip().strip("'\"")
    if verb in NEEDS_ID:
        command = "warp:%s%s" % (verb, (" " + rest) if rest else "")
        if not rest:
            return {"ok": False, "verb": verb, "token": "", "rest": "", "raw": line, "command": "warp:%s" % verb}
        return {"ok": True, "verb": verb, "token": rest, "rest": rest, "raw": line, "command": command}
    return {"ok": True, "verb": verb, "token": "", "rest": rest, "raw": line, "command": "warp:%s" % verb}


def not_understood(shown: str) -> str:
    text = " ".join((shown or "").split()) or "warp:"
    if len(text) > 120:
        text = text[:117] + "..."
    return "Not understood: %s. Accepted forms: %s." % (text, ACCEPTED_FORMS)


def _token_shown(parsed: dict) -> str:
    return proceed.normalize_token(parsed.get("raw") or "") or parsed.get("token") or parsed.get("command") or "warp:"


def plan_command(data: dict, parsed: dict, cfg: Optional[dict] = None) -> dict:
    """Decide the ack and whether to apply. Does not write."""
    cfg = cfg or {}
    if not parsed.get("ok"):
        return {
            "ack": not_understood(parsed.get("command") or parsed.get("raw")),
            "verb": parsed.get("verb"),
            "ticket": None,
            "action": "none",
            "ok": False,
        }
    verb = parsed["verb"]
    if verb == "pause":
        return {
            "ack": "Received warp:pause. Pausing the run and stopping the listener.",
            "verb": verb,
            "ticket": None,
            "action": "pause",
            "ok": True,
            "reason": parsed.get("rest") or None,
        }
    if verb == "resume":
        return {
            "ack": "Received warp:resume. Resuming the run.",
            "verb": verb,
            "ticket": None,
            "action": "resume",
            "ok": True,
            "reason": parsed.get("rest") or None,
        }
    if verb == "stop":
        return {
            "ack": "Received warp:stop. Stopping the run and the listener.",
            "verb": verb,
            "ticket": None,
            "action": "stop",
            "ok": True,
            "reason": parsed.get("rest") or None,
        }
    if verb == "start":
        return {
            "ack": "Received warp:start. Starting the run.",
            "verb": verb,
            "ticket": None,
            "action": "start",
            "ok": True,
            "reason": parsed.get("rest") or None,
        }
    if verb == "status":
        return {
            "ack": "Received warp:status. Posting the digest.",
            "verb": verb,
            "ticket": None,
            "action": "status",
            "ok": True,
        }
    hits = proceed.resolve_token(data, parsed.get("token") or parsed.get("raw") or "")
    if verb == "retry":
        shown = _token_shown(parsed)
        if len(hits) > 1:
            return {
                "ack": "Received warp:retry %s. That id matches more than one ticket. Nothing was requeued." % shown,
                "verb": verb,
                "ticket": None,
                "action": "none",
                "ok": False,
            }
        if len(hits) != 1 or hits[0][1].get("status") != "alarm":
            label = hits[0][0] if len(hits) == 1 else shown
            return {
                "ack": "Received warp:retry %s. No alarmed ticket matches that id. Nothing was requeued." % label,
                "verb": verb,
                "ticket": hits[0][0] if len(hits) == 1 else None,
                "action": "none",
                "ok": False,
            }
        tid = hits[0][0]
        return {
            "ack": "Received warp:retry %s. Requeueing %s." % (tid, tid),
            "verb": verb,
            "ticket": tid,
            "action": "retry",
            "ok": True,
        }
    shown = _token_shown(parsed)
    if len(hits) != 1:
        if len(hits) > 1:
            ack = "Received warp:proceed %s. That id matches more than one ticket. Nothing was merged." % shown
        else:
            ack = "Received warp:proceed %s. No ticket is awaiting approval with that id. Nothing was merged." % shown
        return {"ack": ack, "verb": verb, "ticket": None, "action": "none", "ok": False}
    tid, ticket, how = hits[0]
    if ticket.get("status") != "awaiting_approval":
        return {
            "ack": "Received warp:proceed %s. No ticket is awaiting approval with that id. Nothing was merged." % tid,
            "verb": verb,
            "ticket": tid,
            "action": "none",
            "ok": False,
            "matched": how,
        }
    block = jira_sync.review_block(ticket, cfg)
    if block:
        return {
            "ack": "Received warp:proceed %s. %s is awaiting approval but the gate is closed. Nothing was merged." % (tid, tid),
            "verb": verb,
            "ticket": tid,
            "action": "none",
            "ok": False,
            "matched": how,
        }
    if jira_sync.moves_to_done(ticket, cfg):
        target = cfg.get("jiraDoneStatus") or "Done"
        ack = "Received warp:proceed %s. Merging and moving Jira to %s." % (tid, target)
    else:
        qa = cfg.get("jiraQaReadyStatus") or "QA Ready"
        ack = "Received warp:proceed %s. Merging and leaving Jira at %s." % (tid, qa)
    return {"ack": ack, "verb": verb, "ticket": tid, "action": "proceed", "ok": True, "matched": how}


def write_ack(root: Path, text: str) -> dict:
    """Write the ack payload before any beam change. Quiet notify still posts it."""
    cfg = herald_fmt.read_config(root)
    msg = herald_fmt.message(root, "Ack", intro=text, footer="Posted before the command runs. " + ACCEPTED_FORMS)
    payload = {"kind": "ack", **msg, "header": herald_fmt.header(root, cfg), "ack": text}
    targets, notes = notify.targets(cfg)
    payload["targets"] = targets
    payload["notes"] = notes
    if targets:
        payload["action"] = "post"
    else:
        payload["action"] = "outbox"
        notify.outbox(root, payload["text"])
        payload["notes"].append("no channel configured; message saved to .warp/outbox.md")
    payload["createdAt"] = beam.utcnow()
    warp = root / ".warp"
    warp.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2) + "\n"
    (warp / ACK_NAME).write_text(body)
    (warp / notify.PAYLOAD).write_text(body)
    return payload


def _requeue(beam_path: Path, tid: str) -> str:
    data = beam.load_json(beam_path)
    ticket = data["tickets"][tid]
    prev = ticket.get("status")
    attempts = ticket.get("attempts")
    ticket["status"] = "queued"
    ticket["alarm"] = None
    ticket["updatedAt"] = beam.utcnow()
    data["metrics"] = beam.metrics(data)
    beam.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
    beam.journal(beam_path, {"type": "retry", "id": tid, "from": prev, "to": "queued"})
    return "%s requeued (attempts still %s). Status is queued and alarm is clear." % (tid, attempts)


def _apply(beam_path: Path, decision: dict, by: Optional[str]) -> str:
    action = decision.get("action")
    if action == "proceed":
        code, text = proceed.describe(beam_path, decision["ticket"], by or "warp:proceed")
        return "action: proceed %s\n%s\nproceed-code: %s" % (decision["ticket"], text, code)
    if action in {"pause", "resume", "stop", "start"}:
        import scan

        state = {"pause": "paused", "resume": "running", "stop": "stopped", "start": "running"}[action]
        scan.set_run(beam_path, state, decision.get("reason"))
        extra = ""
        if action in {"pause", "stop"}:
            extra = "\nlistener: do not keep reading while paused or stopped"
        if action in {"resume", "start"}:
            extra = "\nlistener: the next parent tick starts one listener Subagent. Do not start a second in this turn."
        return "action: %s%s" % (action, extra)
    if action == "retry":
        return "action: retry %s\n%s" % (decision["ticket"], _requeue(beam_path, decision["ticket"]))
    if action == "status":
        import status_post

        body = status_post.payload(beam_path)
        out = beam_path.parent / "status-post.json"
        out.write_text(json.dumps(body, indent=2) + "\n")
        return "action: status\npost the digest from %s after the ack" % out
    return "action: none"


def _decide(beam_path: Path, text: str) -> dict:
    parsed = parse(text)
    if parsed is None:
        return {"ignored": True, "code": 0, "ack": None, "action": "none"}
    if not beam_path.is_file():
        ack = "Not understood: %s. No beam at %s." % (parsed.get("command") or text, beam_path)
        return {"ignored": False, "code": 1, "ack": ack, "action": "none", "verb": parsed.get("verb"), "ticket": None}
    data = beam.load_json(beam_path)
    cfg = jira_sync.settings(beam_path, data)
    decision = plan_command(data, parsed, cfg)
    return {"ignored": False, "code": 0, "parsed": parsed, "decision": decision}


def _pack(result: dict, decision: dict, detail: str, source: str) -> dict:
    return {
        "ignored": False,
        "code": result.get("code") or 0,
        "ack": decision.get("ack"),
        "verb": decision.get("verb"),
        "ticket": decision.get("ticket"),
        "action": decision.get("action"),
        "ok": decision.get("ok"),
        "detail": detail,
        "source": source,
    }


def accept(beam_path: Path, text: str, by: Optional[str] = None, source: str = "slack") -> dict:
    """Write the ack and return the command. Do not apply it and do not merge."""
    beam_path = Path(beam_path)
    decided = _decide(beam_path, text)
    if decided.get("ignored") or decided.get("code"):
        return decided
    parsed = decided["parsed"]
    decision = decided["decision"]
    write_ack(_root(beam_path), decision["ack"])
    beam.journal(
        beam_path,
        {
            "type": "ack",
            "command": parsed.get("command"),
            "verb": parsed.get("verb"),
            "ack": decision["ack"],
            "by": by,
            "source": source,
            "ticket": decision.get("ticket"),
        },
    )
    detail = "return: %s\napply: parent" % (parsed.get("command") or "")
    return _pack(decided, decision, detail, source)


def apply_command(beam_path: Path, text: str, by: Optional[str] = None, source: str = "parent") -> dict:
    """Apply a command the listener returned. Do not write another ack."""
    beam_path = Path(beam_path)
    decided = _decide(beam_path, text)
    if decided.get("ignored") or decided.get("code"):
        return decided
    decision = decided["decision"]
    detail = _apply(beam_path, decision, by) if decision.get("action") != "none" else "action: none"
    return _pack(decided, decision, detail, source)


def handle(beam_path: Path, text: str, by: Optional[str] = None, source: str = "slack") -> dict:
    """Ack first, then apply. A bad id acks and does not merge any other ticket."""
    beam_path = Path(beam_path)
    decided = _decide(beam_path, text)
    if decided.get("ignored") or decided.get("code"):
        return decided
    parsed = decided["parsed"]
    decision = decided["decision"]
    write_ack(_root(beam_path), decision["ack"])
    beam.journal(
        beam_path,
        {
            "type": "ack",
            "command": parsed.get("command"),
            "verb": parsed.get("verb"),
            "ack": decision["ack"],
            "by": by,
            "source": source,
            "ticket": decision.get("ticket"),
        },
    )
    detail = _apply(beam_path, decision, by) if decision.get("action") != "none" else "action: none"
    return _pack(decided, decision, detail, source)


def _pending(beam_path: Path) -> Path:
    return Path(beam_path).parent / PENDING_NAME


def _read_pending(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def enqueue(beam_path: Path, text: str, by: Optional[str], message_id: Optional[str], source: str) -> str:
    path = _pending(beam_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _read_pending(path)
    if message_id and any(str(row.get("id")) == str(message_id) for row in rows):
        return "enqueue: duplicate %s" % message_id
    rows.append(
        {
            "id": message_id,
            "text": text,
            "by": by,
            "source": source or "slack",
            "at": beam.utcnow(),
            "applied": False,
        }
    )
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    return "enqueue: queued"


def drain(beam_path: Path) -> list:
    path = _pending(beam_path)
    rows = _read_pending(path)
    results = []
    changed = False
    for row in rows:
        if row.get("applied"):
            continue
        result = handle(beam_path, row.get("text") or "", by=row.get("by"), source=row.get("source") or "pending")
        row["applied"] = True
        row["ack"] = result.get("ack")
        changed = True
        results.append(result)
    if changed:
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    return results


def _print_result(result: dict) -> None:
    if result.get("ignored"):
        print("inbound: ignored")
        return
    print("ack: %s" % result.get("ack"))
    print("ack-first: post this sentence in the channel before the action")
    if result.get("detail"):
        print(result["detail"])


HELP = """
examples:
  python3 scripts/inbound.py ?
  python3 scripts/inbound.py claim --beam .warp/beam.json --agent-id listener-1 --pid 4242
  python3 scripts/inbound.py heartbeat --beam .warp/beam.json --agent-id listener-1
  python3 scripts/inbound.py release --beam .warp/beam.json
  python3 scripts/inbound.py status --beam .warp/beam.json
  python3 scripts/inbound.py parse --text "warp:proceed XV-01"
  python3 scripts/inbound.py handle --beam .warp/beam.json --by shawn --text "warp:proceed XV-01"
  python3 scripts/inbound.py enqueue --beam .warp/beam.json --text "warp:status" --by shawn --message-id 1 --source slack
  python3 scripts/inbound.py drain --beam .warp/beam.json

claim takes the slot for this parent turn. Pass `--turn` once per parent
turn. `listener: already running <id>` means this turn already started its
one listener Subagent: do not start a second. A new `--turn` takes the slot
even when the previous turn left state running. That flag must not block the
next tick. release sets listener.state to stopped. The listener must not keep
reading while paused or stopped. One listener Subagent for the beam, not one
per ticket, and not a separate Agent.
accept writes the ack and prints `return:` for the parent. It does not apply
the command. apply runs proceed, pause, stop, retry, and the other verbs.
The parent applies. The listener does not merge.

handle writes .warp/inbound-ack.json before it changes the beam. Herald posts
that ack, then the action runs. A proceed id that is not awaiting approval
acks and merges nothing else.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ack and apply inbound warp: commands. One listener slot on the beam.",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("claim")
    pc.add_argument("--beam", default=".warp/beam.json")
    pc.add_argument("--agent-id", required=True)
    pc.add_argument("--pid", default=None)
    pc.add_argument("--turn", default=None, help="parent turn id; a new id takes a stale running slot")

    pr = sub.add_parser("release")
    pr.add_argument("--beam", default=".warp/beam.json")

    pb = sub.add_parser("heartbeat")
    pb.add_argument("--beam", default=".warp/beam.json")
    pb.add_argument("--agent-id", required=True)

    ps = sub.add_parser("status")
    ps.add_argument("--beam", default=".warp/beam.json")

    pp = sub.add_parser("parse")
    pp.add_argument("--text", required=True)

    ph = sub.add_parser("handle")
    ph.add_argument("--beam", default=".warp/beam.json")
    ph.add_argument("--text", required=True)
    ph.add_argument("--by", default=None)
    ph.add_argument("--source", default="slack")

    pa = sub.add_parser("accept")
    pa.add_argument("--beam", default=".warp/beam.json")
    pa.add_argument("--text", required=True)
    pa.add_argument("--by", default=None)
    pa.add_argument("--source", default="slack")

    py = sub.add_parser("apply")
    py.add_argument("--beam", default=".warp/beam.json")
    py.add_argument("--text", required=True)
    py.add_argument("--by", default=None)
    py.add_argument("--source", default="parent")

    pe = sub.add_parser("enqueue")
    pe.add_argument("--beam", default=".warp/beam.json")
    pe.add_argument("--text", required=True)
    pe.add_argument("--by", default=None)
    pe.add_argument("--message-id", default=None)
    pe.add_argument("--source", default="slack")

    pd = sub.add_parser("drain")
    pd.add_argument("--beam", default=".warp/beam.json")

    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    beam_path = Path(getattr(args, "beam", ".warp/beam.json"))
    if args.cmd == "claim":
        line = claim(beam_path, args.agent_id, args.pid, turn=args.turn)
        print(line)
        return 1 if line.startswith("listener: no beam") or line.startswith("listener: claim needs") else 0
    if args.cmd == "release":
        line = release(beam_path)
        print(line)
        return 1 if line.startswith("listener: no beam") else 0
    if args.cmd == "heartbeat":
        line = heartbeat(beam_path, args.agent_id)
        print(line)
        return 0 if line.startswith("listener: heartbeat ") and "refused" not in line and "needs" not in line else 1
    if args.cmd == "status":
        if not beam_path.is_file():
            print("listener: no beam at %s" % beam_path)
            return 1
        print(status_line(beam.load_json(beam_path)))
        return 0
    if args.cmd == "parse":
        parsed = parse(args.text)
        print(json.dumps(parsed, indent=2))
        return 0 if parsed is not None else 0
    if args.cmd == "handle":
        result = handle(beam_path, args.text, by=args.by, source=args.source)
        _print_result(result)
        return result.get("code") or 0
    if args.cmd == "accept":
        result = accept(beam_path, args.text, by=args.by, source=args.source)
        _print_result(result)
        return result.get("code") or 0
    if args.cmd == "apply":
        result = apply_command(beam_path, args.text, by=args.by, source=args.source)
        _print_result(result)
        return result.get("code") or 0
    if args.cmd == "enqueue":
        print(enqueue(beam_path, args.text, args.by, args.message_id, args.source))
        return 0
    results = drain(beam_path)
    if not results:
        print("inbound: nothing pending")
        return 0
    for result in results:
        _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
