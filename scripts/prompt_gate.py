#!/usr/bin/env python3
"""Refuse /warp-start when a cloud run would prompt for Jira or Slack.

Local runners are not blocked. `runner: auto` blocks only when this session
is a cloud agent (`CURSOR_CLOUD`, `CURSOR_CLOUD_AGENT`, or
`WARP_CLOUD_SESSION`). `--force` on `scan.py start` starts anyway and later
claims skip this check.

  python3 scripts/prompt_gate.py ?
  python3 scripts/prompt_gate.py check --beam .warp/beam.json
  python3 scripts/prompt_gate.py check --beam .warp/beam.json --force

?, help, -h, and --help print this text. Quote ? if the shell expands it.
Missing tools are printed on stdout and handed to Herald.
Jira transitions require transitionJiraIssue and addOrEditJiraIssueComment.
Slack notify requires slack_send_message. --force starts anyway.
When subagentVm is true on a cloud runner, a start with no cloud environment
(.cursor/environment.json or cloudSnapshot) warns and refuses. --force allows
that start and still warns. runner: local forces subagentVm false and launch
worktree, skips that check, and logs one note. Start prints the effective
values after that override. Cloud subagents use the MCP servers configured
at cursor.com/agents, not the local session's.
--cursor-home stands in for ~/.cursor. A local runner does not block.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
CLOUD_ENV = ("CURSOR_CLOUD", "CURSOR_CLOUD_AGENT", "WARP_CLOUD_SESSION")
_TRUE = {"1", "true", "yes", "cloud", "on"}
_FALSE = {"false", "no", "off", "0"}
JIRA_TOOLS = ("transitionJiraIssue", "addOrEditJiraIssueComment")
SLACK_TOOL = "slack_send_message"
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):[ \t]*(.*?)\s*$")


def flag(value, default: bool) -> bool:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE


def session_is_cloud(env: Optional[dict] = None) -> bool:
    import os

    env = os.environ if env is None else env
    for key in CLOUD_ENV:
        if str(env.get(key) or "").strip().lower() in _TRUE:
            return True
    return False


def yaml_scalars(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        match = _KEY.match(line)
        if not match:
            continue
        out[match.group(1)] = match.group(2).strip().strip("\"'")
    return out


def effective_config(beam: dict, root: Path) -> dict:
    cfg = dict((beam or {}).get("config") or {})
    path = root / ".warp" / "config.yaml"
    if path.is_file():
        for key, value in yaml_scalars(path.read_text()).items():
            if value != "":
                cfg[key] = value
    return cfg


def cloud_run(config: dict, env: Optional[dict] = None) -> bool:
    """True when this start would run on a cloud agent."""
    runner = str((config or {}).get("runner") or "cloud").strip().casefold()
    if runner == "local":
        return False
    if runner == "auto":
        return session_is_cloud(env)
    return True


def jira_tools_required(config: dict) -> bool:
    """Transitions and the claim comment use the same switch."""
    return flag((config or {}).get("jiraTransition"), True)


def slack_tool_required(config: dict) -> bool:
    messenger = str((config or {}).get("messenger") or "both").strip().casefold()
    notify = str((config or {}).get("notify") or "verbose").strip().casefold()
    if notify in {"off", "false", "none"}:
        return False
    return messenger in {"slack", "both"}


def required_tools(config: dict) -> list:
    tools = []
    if jira_tools_required(config):
        tools.extend(JIRA_TOOLS)
    if slack_tool_required(config):
        tools.append(SLACK_TOOL)
    return tools


def tool_name(entry: str) -> str:
    text = str(entry or "").strip()
    if not text or text.endswith(":*") or text == "*":
        return ""
    if ":" in text:
        return text.split(":")[-1].strip()
    return text


def listed_tools(entries) -> set:
    names = set()
    for entry in entries or []:
        name = tool_name(entry)
        if name and name != "*":
            names.add(name)
    return names


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def allow_entries(root: Path, home: Optional[Path] = None) -> list:
    home = home or (Path.home() / ".cursor")
    entries = []
    for path in (root / ".cursor" / "permissions.json", home / "permissions.json"):
        doc = _read_json(path)
        for item in doc.get("mcpAllowlist") or []:
            if isinstance(item, str):
                entries.append(item)
    for path in (
        root / ".cursor" / "hooks" / "warp-allow.json",
        home / "hooks" / "warp-allow.json",
    ):
        doc = _read_json(path)
        for pair in doc.get("pairs") or []:
            if isinstance(pair, dict) and pair.get("server") and pair.get("tool"):
                entries.append("%s:%s" % (pair["server"], pair["tool"]))
    return entries


def missing_tools(config: dict, entries, env: Optional[dict] = None, force: bool = False) -> list:
    if force or not cloud_run(config, env):
        return []
    have = listed_tools(entries)
    return [name for name in required_tools(config) if name not in have]


ENV_WARN = (
    "warn: no cloud environment (.cursor/environment.json or a configured snapshot). "
    "Cloud subagents use the MCP servers configured at cursor.com/agents, not the local session's."
)


def has_cloud_environment(root: Path, config: dict) -> bool:
    """A repo environment file, or a snapshot id in config, is enough to start."""
    path = Path(root) / ".cursor" / "environment.json"
    try:
        if path.is_file() and path.stat().st_size > 0:
            return True
    except OSError:
        pass
    return bool(str((config or {}).get("cloudSnapshot") or "").strip())


def environment_blocked(beam: dict, beam_path: Path, env: Optional[dict] = None) -> bool:
    """True when a cloud run asked for VM subagents and has no environment.

    A force-started beam is not blocked. A local runner is not blocked.
    subagentVm false is not blocked.
    """
    if beam.get("promptForced"):
        return False
    if (beam.get("runState") or "") != "running":
        return False
    import orchestrator

    root = beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent
    cfg = effective_config(beam, root)
    if not orchestrator.subagent_vm(cfg, env) or not cloud_run(cfg, env):
        return False
    return not has_cloud_environment(root, cfg)


def warn_environment() -> None:
    print(ENV_WARN)
    print("refuse: no cloud environment")


def announce(root: Path, missing: list) -> None:
    text = "Cloud run would prompt. Missing MCP allow-list tools: %s" % ", ".join(missing)
    print("refuse: " + text)
    print("missing: " + ", ".join(missing))
    try:
        import herald_fmt as fmt
        import notify

        msg = fmt.message(
            root,
            "Start refused",
            intro=text,
            facts=[("Missing", ", ".join(missing))],
            footer="Add the tools with /warp-allow-notify, or start with --force.",
        )
        payload = notify.build("prompt-gate", root, msg=msg)
        if payload.get("action") == "skip":
            payload["action"] = "post" if payload.get("targets") else "outbox"
            if payload["action"] == "outbox":
                notify.outbox(root, payload.get("text") or text)
        notify.report(payload)
    except Exception as e:
        print("herald: not posted (%s)" % e)


def gate_start(beam_path: Path, force: bool = False, env: Optional[dict] = None, home: Optional[Path] = None) -> int:
    """0 to start. 2 when tools are missing. Does not set runState."""
    beam_path = beam_path.resolve()
    if not beam_path.is_file():
        print("refuse: no beam")
        return 2
    beam = json.loads(beam_path.read_text())
    root = beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent
    cfg = effective_config(beam, root)
    gap = False
    import orchestrator

    note = orchestrator.override_note(cfg, env)
    if note:
        print(note)
    print(orchestrator.effective_text(cfg, env))
    if orchestrator.subagent_vm(cfg, env) and cloud_run(cfg, env) and not has_cloud_environment(root, cfg):
        gap = True
    if force:
        beam["promptForced"] = True
        import beam as beam_mod

        beam_mod.atomic_write(beam_path, json.dumps(beam, indent=2) + "\n")
        if gap:
            print(ENV_WARN)
        print("prompt-gate: force")
        return 0
    if beam.get("promptForced"):
        print("prompt-gate: forced earlier")
        return 0
    missing = missing_tools(cfg, allow_entries(root, home), env=env, force=False)
    if missing:
        announce(root, missing)
        return 2
    if gap:
        warn_environment()
        return 2
    print("prompt-gate: ok")
    return 0


def claim_blocked(beam: dict, beam_path: Path, env: Optional[dict] = None, home: Optional[Path] = None) -> list:
    """Missing tools for a later claim. Empty when the run was force-started."""
    if beam.get("promptForced"):
        return []
    if (beam.get("runState") or "") != "running":
        return []
    root = beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent
    cfg = effective_config(beam, root)
    return missing_tools(cfg, allow_entries(root, home), env=env, force=False)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Cloud MCP prompt gate", epilog=HELP)
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--beam", default=".warp/beam.json")
    check.add_argument("--force", action="store_true")
    check.add_argument("--root")
    check.add_argument("--cursor-home", help="directory standing in for ~/.cursor")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    path = Path(args.beam)
    home = Path(args.cursor_home).resolve() if args.cursor_home else None
    if args.root:
        path = Path(args.root).resolve() / args.beam
    return gate_start(path, force=bool(args.force), home=home)


if __name__ == "__main__":
    sys.exit(main())
