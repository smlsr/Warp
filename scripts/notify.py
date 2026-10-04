#!/usr/bin/env python3
"""Build the Herald message for /warp-init and /warp-scan.

Like status_post.py, this does not call Slack or Teams. Warp has no webhook or
token. Herald (an agent) posts the payload through the connected Slack or
Teams MCP server. This script only decides whether to post and what to say,
and it never fails the command that called it.

  notify.py init|scan --root .   write .warp/notify-post.json and print it
  notify.py outbox --root .      append that payload's text to .warp/outbox.md
                                 (use when the connector is missing or errors)

action in the payload:
  post    at least one target channel is configured; Herald posts `text`
  outbox  no channel is configured for the chosen messenger; text was appended
          to .warp/outbox.md already
  skip    notify is quiet; init and scan messages are verbose-only
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PAYLOAD = "notify-post.json"
OUTBOX = "outbox.md"
MAX_LINKS = 12


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def read_config(root: Path) -> dict:
    """Top-level scalar keys from .warp/config.yaml. Missing file gives defaults."""
    cfg = {"messenger": "both", "notify": "verbose", "slackChannel": "", "teamsChannel": "", "slackMcp": "slack", "teamsMcp": "teams"}
    f = root / ".warp" / "config.yaml"
    if f.is_file():
        for key in list(cfg):
            m = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*(#.*)?$", f.read_text(), re.M)
            if m:
                cfg[key] = m.group(1).strip().strip("\"'")
    cfg["messenger"] = cfg["messenger"].lower() or "both"
    cfg["notify"] = cfg["notify"].lower() or "verbose"
    return cfg


def repo_web(root: Path) -> tuple[str | None, str | None, str | None]:
    """(web url, branch, host kind) from the origin remote, or Nones."""
    remote = _git(root, "remote", "get-url", "origin")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch in {"", "HEAD"}:
        branch = None
    m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote) if remote else None
    if remote.startswith(("http://", "https://")):
        m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", remote)
    if not m:
        return None, branch, None
    host, path = m.group(1), m.group(2)
    if "." not in host:
        return None, branch, None
    kind = "github" if "github" in host else "gitlab" if "gitlab" in host else "bitbucket" if host == "bitbucket.org" else None
    return f"https://{host}/{path}", branch, kind


def file_ref(rel: str, web: str | None, branch: str | None, kind: str | None) -> str:
    """A browsable link when the host layout is known, else the relative path."""
    if not (web and branch and kind):
        return rel
    p, b = quote(rel), quote(branch)
    layout = {"github": "blob", "gitlab": "-/blob", "bitbucket": "src"}[kind]
    return f"{rel} — {web}/{layout}/{b}/{p}"


def repo_label(root: Path) -> str:
    web, _, _ = repo_web(root)
    return web.rstrip("/").rsplit("/", 1)[-1] if web else root.name


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


def init_text(root: Path, cfg: dict) -> str:
    web, branch, _ = repo_web(root)
    names = [cfg[k] for k in ("slackChannel", "teamsChannel") if cfg[k]]
    channel = ", ".join(dict.fromkeys(names)) or "none set"
    lines = [f"Cursor repo {repo_label(root)} was initialized with Warp.", f"Channel: {channel}"]
    if web:
        lines.append(f"Repo: {web}" + (f" (branch {branch})" if branch else ""))
    lines.append("Config: .warp/config.yaml. Next: /warp-scan, then /warp-start.")
    return "\n".join(lines)


def scan_text(root: Path, info: dict) -> str:
    web, branch, kind = repo_web(root)
    found = info.get("found") or {}
    lines = [f"Warp scan finished for {repo_label(root)}" + (f" (folder {info['folder']})" if info.get("folder") else "") + "."]
    lines.append(
        f"Format: {info.get('format')}. Tickets: {info.get('tickets')}. Gates: {info.get('gates', 0)}. Run: stopped."
    )
    est = info.get("estimate") or {}
    if est:
        lines.append(f"Estimate: agent {est.get('agentHours')}h, human {est.get('humanHours')}h, elapsed {est.get('elapsedHours')}h.")
    used = info.get("source")
    refs = []
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
    if refs:
        lines.append("Files:")
        for label, rel in refs[:MAX_LINKS]:
            lines.append(f"- {label}: {file_ref(rel, web, branch, kind)}")
        if len(refs) > MAX_LINKS:
            lines.append(f"- ... {len(refs) - MAX_LINKS} more in .warp/scan.json")
        if web and branch and kind:
            lines.append(f"Links point at branch {branch}. They work once that branch is pushed.")
    lines.append("Next: /warp-start. Status files are in .warp/ (not committed).")
    return "\n".join(lines)


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
        cfg = read_config(root)
        payload["text"] = init_text(root, cfg) if kind == "init" else scan_text(root, info or {})
        tg, notes = targets(cfg)
        payload["targets"], payload["notes"] = tg, notes
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
        print(f"herald: post .warp/{PAYLOAD} to {where} through the connected MCP server.")
        print(f"herald: if the connector is missing or errors, run: python3 {Path(__file__).resolve()} outbox --root .")
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
