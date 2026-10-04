#!/usr/bin/env python3
"""Build the Herald message for /warp-init and /warp-scan.

Like status_post.py, this does not call Slack or Teams. Warp has no webhook or
token. Herald (an agent) posts the payload through the connected Slack or
Teams MCP server. This script decides whether to post and what to say, using
the shared formatter in herald_fmt.py, and it never fails the command that
called it. Warp does not create channels: the channel in the config must
already exist.

  notify.py init|scan --root .   write .warp/notify-post.json and print it
  notify.py outbox --root .      append that payload's plain text to
                                 .warp/outbox.md (connector missing or erroring)

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
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import herald_fmt as fmt  # noqa: E402

PAYLOAD = "notify-post.json"
OUTBOX = "outbox.md"
MAX_LINKS = 12
BOT_EMAILS = ("noreply", "no-reply", "cursoragent@cursor.com")


def file_url(rel: str, web: str | None, branch: str | None, kind: str | None) -> str | None:
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
        if ch:
            out.append({"messenger": m, "channel": ch, "mcp": cfg[f"{m}Mcp"]})
        else:
            notes.append(f"{m}Channel is empty in .warp/config.yaml")
    return out, notes


def invite_email(root: Path) -> tuple[str | None, str | None]:
    """(email, note) from git user.email. Bot and noreply addresses are not users."""
    mail = fmt.git(root, "config", "user.email")
    if not mail:
        return None, "no git user.email, so nobody to add to the channel"
    if "@" not in mail or any(b in mail.lower() for b in BOT_EMAILS):
        return None, f"git user.email ({mail}) is not a Slack or Teams user, so nobody to add to the channel"
    return mail, None


def init_message(root: Path, cfg: dict) -> dict:
    web, branch, _ = fmt.repo_web(root)
    names = list(dict.fromkeys(cfg[k] for k in ("slackChannel", "teamsChannel") if cfg[k]))
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
        footer="Next: /warp-scan, then /warp-start.",
    )


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
    bullets = [f"... {len(refs) - MAX_LINKS} more in .warp/scan.json"] if len(refs) > MAX_LINKS else []
    footer = "Next: /warp-start. Status files are in .warp/ (not committed)."
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


def build(kind: str, root: Path, info: dict | None = None) -> dict:
    """Write the payload and return it. Never raises."""
    root = root.resolve()
    payload = {"kind": kind, "text": "", "action": "skip", "targets": [], "notes": []}
    try:
        cfg = fmt.read_config(root)
        msg = init_message(root, cfg) if kind == "init" else scan_message(root, info or {})
        payload.update(msg)
        payload["header"] = fmt.header(root, cfg)
        tg, notes = targets(cfg)
        payload["targets"], payload["notes"] = tg, notes
        if kind == "init" and tg:
            email, note = invite_email(root)
            payload["invite"] = email
            if note:
                payload["notes"].append(note)
        if cfg["notify"] == "quiet":
            payload["action"] = "skip"
            payload["notes"] = ["notify is quiet; init and scan messages are not posted"]
        elif tg:
            payload["action"] = "post"
        else:
            payload["action"] = "outbox"
            path = outbox(root, payload["text"])
            payload["notes"].append(f"no channel configured; message saved to {path.relative_to(root)}")
        payload["createdAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = root / ".warp" / PAYLOAD
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception as e:  # fail-soft: a notification problem must not break init or scan
        payload["action"] = "skip"
        payload["notes"].append(f"could not build message: {e}")
    return payload


def report(payload: dict) -> None:
    """Print what Herald should do. Safe to call from any command."""
    act = payload["action"]
    if act == "post":
        where = ", ".join(f"{t['messenger']} {t['channel']}" for t in payload["targets"])
        print(f"herald: post .warp/{PAYLOAD} to {where} through the connected MCP server (slack view to Slack, teams view to Teams).")
        print(f"herald: if the connector is missing or errors, run: python3 {Path(__file__).resolve()} outbox --root .")
        if payload.get("invite"):
            print(
                f"herald: optional, once: if your tools can add a user by email, add {payload['invite']} to the channel. "
                "If they cannot, or it fails, skip and say so in one line. Do not create the channel."
            )
        for n in payload["notes"]:
            print(f"herald: note: {n}")
    else:
        print(f"herald: not posted ({act}).")
        for n in payload["notes"]:
            print(f"herald: {n}")


def main() -> None:
    p = argparse.ArgumentParser(description="Herald message for init and scan")
    p.add_argument("kind", choices=["init", "scan", "outbox"])
    p.add_argument("--root", default=".")
    args = p.parse_args()
    root = Path(args.root).resolve()
    if args.kind == "outbox":
        f = root / ".warp" / PAYLOAD
        if not f.is_file():
            print("herald: nothing to save; no notify-post.json")
            return
        text = json.loads(f.read_text()).get("text", "")
        print(f"herald: saved to {outbox(root, text).relative_to(root)}")
        return
    report(build(args.kind, root))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"herald: failed softly: {e}")
        sys.exit(0)
