#!/usr/bin/env python3
"""Build the Herald message for /warp-init and /warp-scan.

Like status_post.py, this does not call Slack or Teams. Warp has no webhook or
token. Herald (an agent) posts the payload through the connected Slack or
Teams MCP server. This script decides whether to post and what to say, using
the shared formatter in herald_fmt.py, and it never fails the command that
called it. Warp does not create channels: the channel in the config must
already exist. A Slack channel with uppercase letters is lowercased (Slack
channel names are lowercase) and noted.

  notify.py init|scan --root .   write .warp/notify-post.json and print it. `scan` on its own
                                 rebuilds the scan message from .warp/beam.json, with the Jira
                                 counts as they are now (run it after resolving keys)
  notify.py outbox --root .      append that payload's plain text to
                                 .warp/outbox.md (connector missing or erroring)
  notify.py outbox --pass --root .   the same for the supervise pass payload
  notify.py posted --root .      the parent posted the pass payload: stamp it, so the next pass
                                 does not carry its lines over

Each `orchestrator.py supervise` pass with `herald:` lines writes them as one `pass` payload
(title "Run update") to .warp/notify-pass.json, so it never overwrites a start or scan
message in notify-post.json. A pass payload nobody marked posted is carried into the next one.

action in the payload:
  post    at least one target channel is configured; Herald posts it
  outbox  no channel is configured for the chosen messenger; the text was
          appended to .warp/outbox.md already
  skip    notify is quiet; init and scan messages are verbose-only

Each target is posted its own view: `slack` (mrkdwn + blocks) to Slack,
`teams` (markdown) to Teams. `text` is the plain version.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import herald_fmt as fmt  # noqa: E402

PAYLOAD = "notify-post.json"
# A supervise pass writes its own file, so it never overwrites a start or scan message not posted yet.
PASS_PAYLOAD = "notify-pass.json"
OUTBOX = "outbox.md"
MAX_LINKS = 12


def file_url(rel: str, web: Optional[str], branch: Optional[str], kind: Optional[str]) -> Optional[str]:
    """A browsable link when the host layout is known, else None (use the path)."""
    if not (web and branch and kind):
        return None
    layout = {"github": "blob", "gitlab": "-/blob", "bitbucket": "src"}[kind]
    return f"{web}/{layout}/{quote(branch)}/{quote(rel)}"


def targets(cfg: dict) -> tuple[list[dict], list[str]]:
    want = ["slack", "teams"] if cfg["messenger"] == "both" else [cfg["messenger"]]
    out, notes = [], []
    for m in want:
        if m not in {"slack", "teams"}:
            notes.append(f"messenger {m!r} is not slack, teams, or both")
            continue
        ch = cfg[f"{m}Channel"]
        if m == "slack" and ch and ch != ch.lower():
            notes.append(f"slackChannel {ch!r} has uppercase; Slack channel names are lowercase, using {ch.lower()!r}")
            ch = ch.lower()
        if ch:
            out.append({"messenger": m, "channel": ch, "mcp": cfg[f"{m}Mcp"]})
        else:
            notes.append(f"{m}Channel is empty in .warp/config.yaml")
    return out, notes


def init_message(root: Path, cfg: dict) -> dict:
    web, branch, _ = fmt.repo_web(root)
    names = list(dict.fromkeys(t["channel"] for t in targets(cfg)[0]))
    facts = [("Channel", ", ".join(names) or "none set")]
    if web:
        facts.append(("Repo", (fmt.repo_name(root), web)))
    if branch:
        facts.append(("Branch", branch))
    facts.append(("Config", ".warp/config.yaml"))
    return fmt.message(
        root,
        "Initialized",
        intro=f"Cursor repo {fmt.repo_name(root)} was initialized with Warp.",
        facts=facts,
        footer=fmt.version_label() + ". Next: /warp-scan, then /warp-start.",
    )


def scan_info(root: Path, beam_path: Optional[Path] = None) -> Optional[dict]:
    """The scan message's details from the beam the scan wrote. None when there is no beam.

    The Jira counts are read now, so a message rebuilt after keys were resolved shows them.
    """
    path = Path(beam_path) if beam_path else root / ".warp" / "beam.json"
    if not path.is_file():
        return None
    beam = json.loads(path.read_text())
    tickets = beam.get("tickets") or {}
    rows = list(tickets.values()) if isinstance(tickets, dict) else list(tickets)
    if not rows:
        return None
    src = beam.get("source") if isinstance(beam.get("source"), dict) else {}
    found = src.get("scan") if isinstance(src.get("scan"), dict) else {}
    if not found:
        scan_file = path.parent / "scan.json"
        if scan_file.is_file():
            try:
                found = json.loads(scan_file.read_text())
            except json.JSONDecodeError:
                found = {}
    try:
        import jira_sync

        cfg = jira_sync.settings(path, beam)
        prefixes = jira_sync.prefixes_from(cfg)
        unmapped = [str(t.get("id")) for t in rows if isinstance(t, dict) and not jira_sync.jira_key(t, prefixes)]
    except Exception:
        unmapped = [str(t.get("id")) for t in rows if isinstance(t, dict) and t.get("jiraMapping") == "needs mapping"]
    return {
        "format": src.get("format"),
        "tickets": len(rows),
        "gates": len(beam.get("gates") or []),
        "estimate": (beam.get("program") or {}).get("estimate") or {},
        "source": src.get("plan") or src.get("schedule"),
        "found": found,
        "folder": src.get("folder") or found.get("folder"),
        "unmapped": unmapped,
        "keyed": len(rows) - len(unmapped),
    }


def scan_message(root: Path, info: dict) -> dict:
    web, branch, kind = fmt.repo_web(root)
    found = info.get("found") or {}
    facts = []
    if info.get("folder"):
        facts.append(("Folder", info["folder"]))
    facts += [
        ("Format", str(info.get("format"))),
        ("Tickets", str(info.get("tickets"))),
        ("Gates", str(info.get("gates", 0))),
        ("Run", "stopped"),
    ]
    est = info.get("estimate") or {}
    if est:
        facts.append(("Estimate", f"agent {est.get('agentHours')}h, human {est.get('humanHours')}h, elapsed {est.get('elapsedHours')}h"))
    bullets: list[str] = []
    if "unmapped" in info:
        unmapped = info.get("unmapped") or []
        total = int(info.get("tickets") or 0)
        keyed = info.get("keyed")
        if keyed is None:
            keyed = total - len(unmapped)
        facts.append(("Jira", f"{keyed} keyed, {len(unmapped)} need mapping"))
        if unmapped:
            shown = ", ".join(unmapped[:12])
            more = "" if len(unmapped) <= 12 else f" (+{len(unmapped) - 12} more)"
            bullets.append(f"Needs a Jira key: {shown}{more}")
    refs: list[tuple[str, str]] = []
    used = info.get("source")
    if used:
        rel = used
        try:
            rel = str(Path(used).resolve().relative_to(root))
        except (ValueError, OSError):
            pass
        refs.append(("Plan used", rel))
    for label, key in (("Warp plan", "warp"), ("Schedule", "schedules"), ("Plan file", "plans"), ("Jira export", "jira")):
        for rel in found.get(key) or []:
            if not refs or rel != refs[0][1]:
                refs.append((label, rel))
    links = [(f"{label}: {rel}", file_url(rel, web, branch, kind)) for label, rel in refs[:MAX_LINKS]]
    if len(refs) > MAX_LINKS:
        bullets.append(f"... {len(refs) - MAX_LINKS} more in .warp/scan.json")
    footer = (
        fmt.version_label()
        + ". Next: /warp-start. The beam, journal, and board are committed. .warp/config.yaml stays gitignored; /warp-start publishes its copy."
    )
    if any(u for _, u in links):
        footer = f"Links point at branch {branch}; they work once it is pushed. " + footer
    return fmt.message(root, "Scan finished", facts=facts, bullets=bullets, links=links, footer=footer)


def outbox(root: Path, text: str) -> Path:
    path = root / ".warp" / OUTBOX
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a") as f:
        f.write(f"\n## {stamp}\n\n{text}\n")
    return path


def build(kind: str, root: Path, info: Optional[dict] = None, msg: Optional[dict] = None, extra: Optional[dict] = None) -> dict:
    """Write the payload and return it. Never raises. `msg` is a prebuilt herald_fmt message."""
    root = root.resolve()
    payload = {"kind": kind, "text": "", "action": "skip", "targets": [], "notes": []}
    try:
        if kind == "scan" and msg is None and not (info or {}).get("format") and not (info or {}).get("tickets"):
            # No details passed (notify.py scan on its own): read them from the beam.
            info = scan_info(root)
            if not info:
                payload["notes"].append("no scan on the beam: .warp/beam.json is missing or has no tickets. Run /warp-scan; .warp/notify-post.json was left as it was")
                return payload
        cfg = fmt.read_config(root)
        if msg is None:
            msg = init_message(root, cfg) if kind == "init" else scan_message(root, info or {})
        payload.update(msg)
        payload["header"] = fmt.header(root, cfg)
        tg, notes = targets(cfg)
        payload["targets"], payload["notes"] = tg, notes
        payload.update(extra or {})
        if cfg["notify"] == "quiet" and kind != PASS_KIND:
            payload["action"] = "skip"
            payload["notes"] = ["notify is quiet; init and scan messages are not posted"]
        elif tg:
            payload["action"] = "post"
        else:
            payload["action"] = "outbox"
            path = outbox(root, payload["text"])
            payload["notes"].append(f"no channel configured; message saved to {path.relative_to(root)}")
        payload["createdAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = root / ".warp" / (PASS_PAYLOAD if kind == PASS_KIND else PAYLOAD)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception as e:  # fail-soft: a notification problem must not break init or scan
        payload["action"] = "skip"
        payload["notes"].append(f"could not build message: {e}")
    return payload


def report_lines(payload: dict) -> list:
    """What Herald should do, as lines."""
    act = payload["action"]
    out = []
    if act == "post":
        where = ", ".join(f"{t['messenger']} {t['channel']}" for t in payload["targets"])
        passed = payload.get("kind") == PASS_KIND
        name = PASS_PAYLOAD if passed else PAYLOAD
        out.append(f"herald: post .warp/{name} to {where} through the connected MCP server (slack view to Slack, teams view to Teams).")
        if passed:
            out.append(f"herald: then run: python3 {Path(__file__).resolve()} posted --root .")
        out.append(f"herald: if the connector is missing or errors, run: python3 {Path(__file__).resolve()} outbox --root .{' --pass' if passed else ''}")
        out.extend(f"herald: note: {n}" for n in payload["notes"])
    else:
        out.append(f"herald: not posted ({act}).")
        out.extend(f"herald: {n}" for n in payload["notes"])
    return out


def report(payload: dict) -> None:
    """Print what Herald should do. Safe to call from any command."""
    for line in report_lines(payload):
        print(line)


PASS_KIND = "pass"
MAX_PASS_LINES = 30
# The instruction lines this module prints are not news to post.
_OWN = ("post .warp/", "then run:", "if the connector", "note:", "not posted", "saved to", "nothing to save", "failed softly")
# notify: quiet posts only these from a pass: alarms, parks, approval waits, red results, and work done by hand.
_QUIET = re.compile(r"worker died|parked|alarm|could not merge|approval|given up|gave up|by hand|stopped|findings? after the merge|red\b", re.I)


def news(lines: list) -> list:
    """The `herald:` lines of a pass, without the posting instructions."""
    out = []
    for line in lines:
        if not str(line).startswith("herald: "):
            continue
        text = str(line)[len("herald: "):].strip()
        if text and not text.startswith(_OWN) and text not in out:
            out.append(text)
    return out


def pass_post(root: Path, lines: list, number: Optional[int] = None) -> list:
    """Write a pass's `herald:` lines as one payload and return the lines that tell the parent to post it.

    Before 1.6.18 supervise only printed its `herald:` lines and the parent was trusted to post each one. In the
    edit run it posted the start message (a payload) and none of WE-01's claim, code done, pull request or Bugbot
    lines. A payload no one marked posted (`notify.py posted`) is carried into the next pass's payload.
    """
    try:
        root = Path(root).resolve()
        new = news(lines)
        if not new:
            return []
        path = root / ".warp" / PASS_PAYLOAD
        carried = []
        try:
            prior = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, ValueError):
            prior = {}
        if prior.get("kind") == PASS_KIND and prior.get("action") == "post" and not prior.get("postedAt"):
            carried = [text for text in prior.get("lines") or [] if text not in new]
        items = (carried + new)[-MAX_PASS_LINES:]
        cfg = fmt.read_config(root)
        if cfg["notify"] == "quiet":
            # The status digest is a verbose post ("Alarms: …" in it is not an alarm).
            items = [text for text in items if _QUIET.search(text) and not text.startswith("Status: ")]
            if not items:
                return []
        title = "Run update" + (f" (pass {number})" if number else "")
        msg = fmt.message(root, title, bullets=items)
        payload = build(PASS_KIND, root, msg=msg, extra={"lines": items, "pass": number})
        return report_lines(payload)
    except Exception as e:  # a post problem never breaks a pass
        return [f"herald: not posted ({e})"]


def mark_posted(root: Path) -> str:
    path = Path(root).resolve() / ".warp" / PASS_PAYLOAD
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return "herald: nothing to mark; no %s" % PASS_PAYLOAD
    payload["postedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return "herald: marked posted (%s)" % (payload.get("kind") or "payload")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Herald message for init and scan",
        epilog="examples:\n  python3 scripts/notify.py ?\n  python3 scripts/notify.py init --root .\n"
        "  python3 scripts/notify.py scan --root .\n  python3 scripts/notify.py outbox --root .\n\n"
        "init and scan write .warp/notify-post.json. outbox appends that text to .warp/outbox.md.\n"
        "scan on its own rebuilds the scan message from .warp/beam.json (format, tickets, gates, estimate,\n"
        "plan files) with the Jira counts as they are now: run it after resolving keys. With no beam it\n"
        "writes nothing.\n"
        "notify: quiet skips init and scan. ?, help, -h, and --help print this text. Quote ? if the shell expands it.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("kind", choices=["init", "scan", "outbox", "posted"])
    p.add_argument("--root", default=".")
    p.add_argument("--pass", dest="pass_payload", action="store_true", help="outbox: the supervise pass payload (.warp/notify-pass.json)")
    p.add_argument("--beam", help=argparse.SUPPRESS)
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    root = Path(args.root).resolve()
    if args.kind == "posted":
        print(mark_posted(root))
        return
    if args.kind == "outbox":
        f = root / ".warp" / (PASS_PAYLOAD if args.pass_payload else PAYLOAD)
        if not f.is_file():
            print("herald: nothing to save; no %s" % f.name)
            return
        text = json.loads(f.read_text()).get("text", "")
        print(f"herald: saved to {outbox(root, text).relative_to(root)}")
        return
    if args.kind == "scan" and args.beam:
        info = scan_info(root, Path(args.beam))
        if not info:
            print("herald: not posted (skip).")
            print("herald: no scan on the beam. Run /warp-scan; .warp/notify-post.json was left as it was")
            return
        report(build("scan", root, info))
        return
    report(build(args.kind, root))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"herald: failed softly: {e}")
        sys.exit(0)
