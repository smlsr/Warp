#!/usr/bin/env python3
"""One formatter for every message Herald posts.

The channel is shared across repos, so each message opens with a header that
names its source: `Warp | <repo> / <project>`. The repo comes from the origin
remote (else the folder name). The project is `projectName` in
.warp/config.yaml, else `jiraProject`, else the workspace folder name. If the
project is the same word as the repo, only the repo is shown.

`render` returns three views of one message:
  text    plain text, for .warp/outbox.md and connectors that take text only
  slack   {"text": mrkdwn fallback, "blocks": Block Kit} (header, section, context)
  teams   {"markdown": ...} for a Teams channel message

Herald posts `slack` to Slack channels and `teams` to Teams channels. Warp has
no webhook or token; this only builds the content.

CLI, for messages the agent composes (claim, PR opened, alarm, gate, ...):
  herald_fmt.py --title "Claim API-01" --fact size=M --fact mode=auto \\
      --line "locks: db" --link "PR=https://host/pr/1" --footer "warp:retry API-01"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

GENERIC_FOLDERS = {"workspace"}
SLACK_SECTION_MAX = 2900
SLACK_HEADER_MAX = 150

CONFIG_DEFAULTS = {
    "messenger": "both",
    "notify": "verbose",
    "slackChannel": "",
    "teamsChannel": "",
    "slackMcp": "slack",
    "teamsMcp": "teams",
    "projectName": "",
    "jiraProject": "",
    "gitProvider": "auto",
    "githubMcp": "github",
    "bitbucketMcp": "bitbucket",
    "ghCli": "true",
    "pushMerge": "true",
    "baseBranch": "",
}


def git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def read_config(root: Path) -> dict:
    """Top-level scalar keys from .warp/config.yaml. A missing file gives defaults."""
    cfg = dict(CONFIG_DEFAULTS)
    f = root / ".warp" / "config.yaml"
    if f.is_file():
        text = f.read_text()
        for key in cfg:
            m = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*(#.*)?$", text, re.M)
            if m:
                cfg[key] = m.group(1).strip().strip("\"'")
    cfg["messenger"] = cfg["messenger"].lower() or "both"
    cfg["notify"] = cfg["notify"].lower() or "verbose"
    return cfg


def repo_web(root: Path) -> tuple[str | None, str | None, str | None]:
    """(web url, branch, host kind) from the origin remote, or Nones."""
    remote = git(root, "remote", "get-url", "origin")
    branch = git(root, "symbolic-ref", "--short", "-q", "HEAD") or None
    m = None
    if remote.startswith(("http://", "https://")):
        m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", remote)
    elif remote:
        m = re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote)
    if not m or "." not in m.group(1):
        return None, branch, None
    host, path = m.group(1), m.group(2)
    kind = "github" if "github" in host else "gitlab" if "gitlab" in host else "bitbucket" if host == "bitbucket.org" else None
    return f"https://{host}/{path}", branch, kind


def repo_name(root: Path) -> str:
    web, _, _ = repo_web(root)
    return web.rstrip("/").rsplit("/", 1)[-1] if web else root.name


def project_name(root: Path, cfg: dict) -> str | None:
    """projectName, else jiraProject, else the workspace folder. None if it adds nothing."""
    repo = repo_name(root)
    for cand in (cfg.get("projectName"), cfg.get("jiraProject"), root.name):
        if cand and cand.casefold() not in GENERIC_FOLDERS:
            return None if cand.casefold() == repo.casefold() else cand
    return None


def header(root: Path, cfg: dict | None = None) -> str:
    root = root.resolve()
    cfg = cfg or read_config(root)
    proj = project_name(root, cfg)
    return f"Warp | {repo_name(root)}" + (f" / {proj}" if proj else "")


def _link(v):
    return v if isinstance(v, tuple) else None


def _slack_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_esc(s: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", s)


def _plain(v) -> str:
    return f"{v[0]} — {v[1]}" if _link(v) else str(v)


def _slack(v) -> str:
    return f"<{v[1]}|{_slack_esc(v[0])}>" if _link(v) else _slack_esc(str(v))


def _teams(v) -> str:
    return f"[{_md_esc(v[0])}]({v[1]})" if _link(v) else _md_esc(str(v))


def render(head: str, title: str, intro: str | None = None, facts=(), bullets=(), links=(), footer: str | None = None) -> dict:
    """facts: (key, value); a value may be (label, url). links: (label, url|None)."""
    link_vals = [(l, u) if u else l for l, u in links]
    plain = [head, title]
    if intro:
        plain.append(intro)
    plain += [f"{k}: {_plain(v)}" for k, v in facts]
    plain += [f"- {b}" for b in bullets]
    plain += [f"- {_plain(v)}" for v in link_vals]
    if footer:
        plain.append(footer)

    body = [f"*{_slack_esc(title)}*"]
    if intro:
        body.append(_slack_esc(intro))
    body += [f"*{_slack_esc(k)}:* {_slack(v)}" for k, v in facts]
    body += [f"• {_slack_esc(b)}" for b in bullets]
    body += [f"• {_slack(v)}" for v in link_vals]
    section = "\n".join(body)
    if len(section) > SLACK_SECTION_MAX:
        section = section[: SLACK_SECTION_MAX - 20].rsplit("\n", 1)[0] + "\n• … (truncated)"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": head[:SLACK_HEADER_MAX]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": section}},
    ]
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _slack_esc(footer)}]})

    md = [f"**{_md_esc(head)}**", f"**{_md_esc(title)}**"]
    if intro:
        md.append(_md_esc(intro))
    items = [f"**{_md_esc(k)}:** {_teams(v)}" for k, v in facts]
    items += [_md_esc(b) for b in bullets]
    items += [_teams(v) for v in link_vals]
    if items:
        md.append("\n".join(f"- {i}" for i in items))
    if footer:
        md.append(f"_{_md_esc(footer)}_")

    return {
        "text": "\n".join(plain),
        "slack": {"text": f"*{_slack_esc(head)}* — {_slack_esc(title)}", "blocks": blocks},
        "teams": {"markdown": "\n\n".join(md)},
    }


def message(root: Path, title: str, **kw) -> dict:
    root = root.resolve()
    return render(header(root), title, **kw)


def _kv(items: list[str], what: str) -> list[tuple[str, str]]:
    out = []
    for i in items:
        if "=" not in i:
            sys.exit(f"{what} needs key=value: {i!r}")
        k, v = i.split("=", 1)
        out.append((k, v))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Format a Herald message")
    p.add_argument("--root", default=".")
    p.add_argument("--title", required=True)
    p.add_argument("--intro")
    p.add_argument("--fact", action="append", default=[], help="key=value")
    p.add_argument("--line", action="append", default=[], help="a bullet")
    p.add_argument("--link", action="append", default=[], help="label=url, or a plain path")
    p.add_argument("--footer")
    args = p.parse_args()
    links = [tuple(l.split("=", 1)) if "=" in l else (l, None) for l in args.link]
    out = message(
        Path(args.root),
        args.title,
        intro=args.intro,
        facts=_kv(args.fact, "--fact"),
        bullets=args.line,
        links=links,
        footer=args.footer,
    )
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
