#!/usr/bin/env python3
"""Let Warp's MCP writes run without a Cursor approval prompt.

Default scope is this repo. `--user` edits the home-directory files instead,
and it refuses to write unless `--yes` is also set.

What each file actually does (Cursor docs, not a guess):

  IDE Run prompt
    `.cursor/permissions.json` or `~/.cursor/permissions.json`, key
    `mcpAllowlist`, entries `server:tool`. This is the list that skips the
    prompt when Run Mode is Auto-review, Allowlist, or Run Everything.
    Ask Every Time (removed for new users in Cursor 3.5) does not consult it.
    Setting the key replaces the in-app MCP allowlist. Per-user and per-repo
    files are concatenated. An empty array does not fall back to Cursor
    Settings, so a revoke that empties the list deletes the key.

  CLI
    `.cursor/cli.json` or `~/.cursor/cli-config.json`, `permissions.allow`
    entries `Mcp(server:tool)`. Separate from the IDE Run button.

  Hook
    `.cursor/hooks.json` or `~/.cursor/hooks.json`, `beforeMCPExecution`.
    The script returns allow only for the same pairs, and ask otherwise.
    Cursor staff have said a hook allow does not skip the MCP prompt (only
    deny is enforced). The hook is still installed so the project has an
    explicit allowlist check. `failClosed` is not set.

  Cloud agents
    They do not use Run Modes and do not ask for approval. `beforeMCPExecution`
    does not run there. This command does not change cloud-agent prompting.

No entry uses a wildcard. Slack, Teams, Jira, and GitHub tool names come from
`scripts/mcp_tools.py`. Extra `server:tool` pairs come from `notifyAllow` in
`.warp/config.yaml`.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import herald_fmt  # noqa: E402
import mcp_tools  # noqa: E402

ADDED_BY = "warp-allow-notify"
MANIFEST_VERSION = 1
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK_SOURCE = PLUGIN_ROOT / "hooks" / "warp-mcp-allow.py"

PROJECT_REL = {
    "permissions": ".cursor/permissions.json",
    "cli": ".cursor/cli.json",
    "hooks": ".cursor/hooks.json",
    "hook_script": ".cursor/hooks/warp-mcp-allow.py",
    "pairs": ".cursor/hooks/warp-allow.json",
    "manifest": ".cursor/warp-allow.json",
    "hook_command": ".cursor/hooks/warp-mcp-allow.py",
}
USER_REL = {
    "permissions": "permissions.json",
    "cli": "cli-config.json",
    "hooks": "hooks.json",
    "hook_script": "hooks/warp-mcp-allow.py",
    "pairs": "hooks/warp-allow.json",
    "manifest": "warp-allow.json",
    "hook_command": "./hooks/warp-mcp-allow.py",
}


class ConfigError(Exception):
    pass


def find_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


def cursor_home(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return (Path.home() / ".cursor").resolve()


def dump(doc: object) -> str:
    return json.dumps(doc, indent=2) + "\n"


def strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads(text: str, path: Path) -> tuple[object, bool]:
    raw = text.strip()
    if not raw:
        return {}, False
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass
    stripped = strip_jsonc(text)
    try:
        return json.loads(stripped), stripped != text
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not JSON ({e}). Nothing written.") from e


def check_token(kind: str, value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ConfigError(f"{kind} {value!r} is empty or has surrounding space")
    if any(ch in value for ch in "*?\n\t "):
        raise ConfigError(f"{kind} {value!r} has a wildcard or space; Warp will not write one")
    if ":" in value:
        raise ConfigError(f"{kind} {value!r} contains ':'")
    return value


def split_entry(entry: str) -> tuple[str, str]:
    if entry.count(":") != 1:
        raise ConfigError(f"notifyAllow entry {entry!r} must be server:tool, with no wildcard")
    server, tool = entry.split(":", 1)
    return check_token("server", server), check_token("tool", tool)


def parse_notify_allow(text: str) -> list[str]:
    """`notifyAllow` list from config.yaml. Missing key means no extras."""
    lines = text.splitlines()
    start = None
    inline = ""
    for i, line in enumerate(lines):
        m = re.match(r"^notifyAllow:[ \t]*(.*?)\s*$", line)
        if m:
            start = i
            inline = m.group(1).split("#", 1)[0].strip()
            break
    if start is None:
        return []
    items: list[str] = []
    if inline:
        if inline == "[]":
            return []
        if not (inline.startswith("[") and inline.endswith("]")):
            raise ConfigError(f"notifyAllow value {inline!r} is not a list of server:tool")
        inner = inline[1:-1].strip()
        if inner:
            items.extend(part.strip().strip("\"'") for part in inner.split(",") if part.strip())
    else:
        for line in lines[start + 1 :]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = re.match(r"^[ \t]+-[ \t]*(.*)$", line)
            if not m:
                break
            val = m.group(1).split("#", 1)[0].strip().strip("\"'")
            if val:
                items.append(val)
    for item in items:
        split_entry(item)
    return items


def server_name(cfg: dict, key: str) -> str:
    value = (cfg.get(key) or "").strip()
    if not value:
        value = herald_fmt.CONFIG_DEFAULTS[key]
    return check_token(key, value)


def build_pairs(cfg: dict, extras: list[str], with_jira: bool, with_git: bool) -> list[tuple[str, str]]:
    groups: list[tuple[str, tuple[str, ...]]] = [
        (server_name(cfg, "slackMcp"), mcp_tools.SLACK_TOOLS),
        (server_name(cfg, "teamsMcp"), mcp_tools.TEAMS_TOOLS),
    ]
    if with_jira:
        groups.append((server_name(cfg, "jiraMcp"), mcp_tools.JIRA_TOOLS))
    if with_git:
        groups.append((server_name(cfg, "githubMcp"), mcp_tools.GITHUB_TOOLS))
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(server: str, tool: str) -> None:
        check_token("tool", tool)
        key = (server.casefold(), tool.casefold())
        if key in seen:
            return
        seen.add(key)
        pairs.append((server, tool))

    for server, tools in groups:
        for tool in tools:
            add(server, tool)
    for extra in extras:
        server, tool = split_entry(extra)
        add(server, tool)
    return pairs


def perm_entry(server: str, tool: str) -> str:
    return f"{server}:{tool}"


def cli_entry(server: str, tool: str) -> str:
    return f"Mcp({server}:{tool})"


def fold(value: str) -> str:
    return value.casefold()


def merge_strings(current: list, wanted: list[str]) -> tuple[list, list[str]]:
    out = list(current)
    have = {fold(item) for item in out if isinstance(item, str)}
    added: list[str] = []
    for item in wanted:
        if fold(item) in have:
            continue
        out.append(item)
        have.add(fold(item))
        added.append(item)
    return out, added


def remove_strings(current: list, unwanted: list[str]) -> list:
    drop = {fold(item) for item in unwanted}
    return [item for item in current if not (isinstance(item, str) and fold(item) in drop)]


def as_dict(doc: object, path: Path) -> dict:
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must be a JSON object. Nothing written.")
    return doc


def union_ci(old: list, new: list[str]) -> list:
    out = [item for item in old if isinstance(item, str)]
    have = {fold(item) for item in out}
    for item in new:
        if fold(item) not in have:
            out.append(item)
            have.add(fold(item))
    return out


class Scope:
    def __init__(self, name: str, base: Path, rel: dict[str, str]):
        self.name = name
        self.base = base
        self.rel = rel

    def path(self, key: str) -> Path:
        return self.base / self.rel[key]


def project_scope(root: Path) -> Scope:
    return Scope("project", root, PROJECT_REL)


def user_scope(home: Path) -> Scope:
    return Scope("user", home, USER_REL)


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text()


def load_doc(path: Path) -> tuple[dict, str | None, bool]:
    text = read_text(path)
    if text is None:
        return {}, None, False
    doc, comments = loads(text, path)
    return as_dict(doc, path), text, comments


def load_manifest(scope: Scope) -> dict | None:
    path = scope.path("manifest")
    text = read_text(path)
    if text is None:
        return None
    doc, _ = loads(text, path)
    if not isinstance(doc, dict) or doc.get("addedBy") != ADDED_BY:
        raise ConfigError(f"{path} is not a {ADDED_BY} manifest. Nothing written.")
    return doc


def hook_present(doc: dict, command: str) -> bool:
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return False
    arr = hooks.get("beforeMCPExecution")
    if not isinstance(arr, list):
        return False
    return any(isinstance(item, dict) and item.get("command") == command for item in arr)


def add_hook(doc: dict, command: str, created: bool) -> bool:
    if created and "version" not in doc:
        doc["version"] = 1
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
        doc["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ConfigError("hooks.json 'hooks' must be an object")
    arr = hooks.get("beforeMCPExecution")
    if arr is None:
        arr = []
        hooks["beforeMCPExecution"] = arr
    if not isinstance(arr, list):
        raise ConfigError("beforeMCPExecution must be a list")
    if hook_present(doc, command):
        return False
    arr.append({"command": command})
    return True


def remove_hook(doc: dict, command: str) -> None:
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return
    arr = hooks.get("beforeMCPExecution")
    if not isinstance(arr, list):
        return
    kept = [item for item in arr if not (isinstance(item, dict) and item.get("command") == command)]
    if kept:
        hooks["beforeMCPExecution"] = kept
    else:
        hooks.pop("beforeMCPExecution", None)
    if not hooks:
        doc.pop("hooks", None)


def empty_doc(doc: dict) -> bool:
    return not doc or set(doc) <= {"version"}


def diff_text(path: Path, old: str | None, new: str | None) -> str:
    old_lines = [] if old is None else old.splitlines(True)
    new_lines = [] if new is None else new.splitlines(True)
    fromfile = "/dev/null" if old is None else str(path)
    tofile = "/dev/null" if new is None else str(path)
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile))


def backup(path: Path, text: str) -> None:
    path.with_name(path.name + ".bak").write_text(text)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def rmdir_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


class Change:
    def __init__(self, path: Path, old: str | None, new: str | None, executable: bool = False, keep_backup: bool = True):
        self.path = path
        self.old = old
        self.new = new
        self.executable = executable
        self.keep_backup = keep_backup

    @property
    def same(self) -> bool:
        return self.old == self.new


def apply_changes(changes: list[Change], dry: bool) -> list[str]:
    notes: list[str] = []
    for change in changes:
        if change.same:
            continue
        if change.old is None and change.new is not None:
            action = "create"
        elif change.new is None:
            action = "delete"
        else:
            action = "update"
        notes.append(f"{action} {change.path}")
        if dry:
            continue
        if change.keep_backup and change.old is not None and change.path.is_file():
            backup(change.path, change.old)
        if change.new is None:
            if change.path.is_file() or change.path.is_symlink():
                change.path.unlink()
            continue
        write_text(change.path, change.new)
        if change.executable:
            os.chmod(change.path, 0o755)
    return notes


def print_diff(change: Change) -> None:
    text = diff_text(change.path, change.old, change.new)
    if text:
        print(text, end="" if text.endswith("\n") else "\n")


def surfaces(scope: Scope) -> str:
    if scope.name == "project":
        perm = ".cursor/permissions.json"
        cli = ".cursor/cli.json"
        hooks = ".cursor/hooks.json"
        where = "this repo"
    else:
        perm = "~/.cursor/permissions.json"
        cli = "~/.cursor/cli-config.json"
        hooks = "~/.cursor/hooks.json"
        where = "your user Cursor config"
    return "\n".join([
        f"Scope: {where}.",
        f"IDE Run prompt: {perm} mcpAllowlist (server:tool). This is what skips the prompt when Run Mode is Auto-review, Allowlist, or Run Everything. Ask Every Time does not consult it. Setting mcpAllowlist replaces the in-app MCP allowlist; entries that exist only in Cursor Settings prompt again until they are in this file. Per-user and per-repo files are concatenated. A team admin Run Mode override ignores this file.",
        f"CLI: {cli} permissions.allow as Mcp(server:tool). This does not change the IDE Run button.",
        f"Hook: {hooks} beforeMCPExecution returns allow only for this list and ask for every other tool. It is not a blanket allow, and failClosed is off. Current Cursor behavior: a hook allow does not skip the MCP Run prompt (deny is what the hook enforces). The permissions file is what skips the IDE prompt.",
        "Cloud agents do not use Run Modes and do not ask for approval. beforeMCPExecution does not run on cloud agents. This command does not change cloud-agent prompting.",
        "Reload Cursor (Developer: Reload Window) or start a new agent chat. Cursor re-reads permissions.json when it changes; reload if the Run prompt is still there.",
    ])


def remind(scope: Scope, with_git: bool) -> None:
    print()
    print(surfaces(scope))
    if with_git:
        print("Git: GitHub add_issue_comment only. Warp does not name a Bitbucket comment tool; add server:tool under notifyAllow for that.")
    print("If the Run prompt names a different tool, add server:tool to notifyAllow in .warp/config.yaml and run this again.")


def pairs_doc(pairs: list[tuple[str, str]]) -> dict:
    return {
        "addedBy": ADDED_BY,
        "pairs": [{"server": server, "tool": tool} for server, tool in pairs],
    }


def manifest_doc(
    scope: Scope,
    prev: dict | None,
    added_perm: list[str],
    added_cli: list[str],
    hook_added: bool,
    created: list[str],
    originals: dict,
) -> dict:
    prev = prev or {}
    doc = {
        "addedBy": ADDED_BY,
        "version": MANIFEST_VERSION,
        "scope": scope.name,
        "hookCommand": scope.rel["hook_command"],
        "hookCommandAdded": bool(prev.get("hookCommandAdded")) or hook_added,
        "mcpAllowlistAdded": union_ci(prev.get("mcpAllowlistAdded") or [], added_perm),
        "cliAllowAdded": union_ci(prev.get("cliAllowAdded") or [], added_cli),
        "createdFiles": union_ci(prev.get("createdFiles") or [], created),
    }
    if originals:
        doc["originals"] = originals
    return doc


def remember_original(originals: dict, rel: str, old: str | None, new: str) -> None:
    """Keep the first pre-existing body so revoke can put that file back."""
    if old is not None and rel not in originals and old != new:
        originals[rel] = old


def require_list(doc: dict, key: str, path: Path) -> list:
    value = doc.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{path} key {key} must be a list. Nothing written.")
    return value


def allow_changes(scope: Scope, pairs: list[tuple[str, str]]) -> tuple[list[Change], dict]:
    perm_wanted = [perm_entry(s, t) for s, t in pairs]
    cli_wanted = [cli_entry(s, t) for s, t in pairs]
    command = scope.rel["hook_command"]
    script = HOOK_SOURCE.read_text()
    prev = load_manifest(scope)

    perm_path = scope.path("permissions")
    cli_path = scope.path("cli")
    hooks_path = scope.path("hooks")
    script_path = scope.path("hook_script")
    pairs_path = scope.path("pairs")
    manifest_path = scope.path("manifest")

    perm_doc, perm_old, perm_comments = load_doc(perm_path)
    cli_doc, cli_old, cli_comments = load_doc(cli_path)
    hooks_doc, hooks_old, hooks_comments = load_doc(hooks_path)
    script_old = read_text(script_path)
    pairs_old = read_text(pairs_path)
    manifest_old = read_text(manifest_path)

    if perm_comments or cli_comments or hooks_comments:
        print("Note: comments in a JSON file are not kept when that file is rewritten. A .bak holds the previous text.")

    perm_list = require_list(perm_doc, "mcpAllowlist", perm_path)
    merged_perm, added_perm = merge_strings(perm_list, perm_wanted)
    if merged_perm:
        perm_doc["mcpAllowlist"] = merged_perm
    elif "mcpAllowlist" in perm_doc:
        perm_doc.pop("mcpAllowlist", None)

    perms = cli_doc.get("permissions")
    if perms is None:
        perms = {}
    if not isinstance(perms, dict):
        raise ConfigError(f"{cli_path} permissions must be an object. Nothing written.")
    allow = perms.get("allow")
    if allow is None:
        allow = []
    if not isinstance(allow, list):
        raise ConfigError(f"{cli_path} permissions.allow must be a list. Nothing written.")
    merged_allow, added_cli = merge_strings(allow, cli_wanted)
    if merged_allow:
        perms["allow"] = merged_allow
        cli_doc["permissions"] = perms
    elif "allow" in perms:
        perms.pop("allow", None)
        if perms:
            cli_doc["permissions"] = perms
        else:
            cli_doc.pop("permissions", None)

    hook_added = add_hook(hooks_doc, command, hooks_old is None)
    pairs_new = dump(pairs_doc(pairs))
    script_new = script if script.endswith("\n") else script + "\n"
    created: list[str] = []
    for key, old in (
        ("permissions", perm_old),
        ("cli", cli_old),
        ("hooks", hooks_old),
        ("hook_script", script_old),
        ("pairs", pairs_old),
        ("manifest", manifest_old),
    ):
        if old is None:
            created.append(scope.rel[key])

    originals = dict((prev or {}).get("originals") or {})
    remember_original(originals, scope.rel["hook_script"], script_old, script_new)
    remember_original(originals, scope.rel["pairs"], pairs_old, pairs_new)
    manifest = manifest_doc(scope, prev, added_perm, added_cli, hook_added, created, originals)

    def json_change(path: Path, doc: dict, old: str | None) -> Change | None:
        if not doc and old is None:
            return None
        new = dump(doc) if doc else None
        if new is None:
            return None
        return Change(path, old, new)

    changes = [
        c
        for c in (
            json_change(perm_path, perm_doc, perm_old),
            json_change(cli_path, cli_doc, cli_old),
            json_change(hooks_path, hooks_doc, hooks_old),
        )
        if c is not None
    ]
    changes.append(Change(script_path, script_old, script_new, executable=True))
    changes.append(Change(pairs_path, pairs_old, pairs_new))
    changes.append(Change(manifest_path, manifest_old, dump(manifest)))
    return changes, manifest


def revoke_changes(scope: Scope) -> list[Change]:
    manifest = load_manifest(scope)
    if manifest is None:
        return []
    if manifest.get("scope") not in (None, scope.name):
        raise ConfigError(f"Manifest scope is {manifest.get('scope')!r}, not {scope.name}. Nothing written.")

    perm_path = scope.path("permissions")
    cli_path = scope.path("cli")
    hooks_path = scope.path("hooks")
    script_path = scope.path("hook_script")
    pairs_path = scope.path("pairs")
    manifest_path = scope.path("manifest")

    perm_doc, perm_old, _ = load_doc(perm_path)
    cli_doc, cli_old, _ = load_doc(cli_path)
    hooks_doc, hooks_old, _ = load_doc(hooks_path)
    script_old = read_text(script_path)
    pairs_old = read_text(pairs_path)
    manifest_old = read_text(manifest_path)
    created = {fold(item) for item in (manifest.get("createdFiles") or []) if isinstance(item, str)}

    listed = manifest.get("mcpAllowlistAdded") or []
    if not isinstance(listed, list):
        listed = []
    current = require_list(perm_doc, "mcpAllowlist", perm_path)
    kept = remove_strings(current, [item for item in listed if isinstance(item, str)])
    if kept:
        perm_doc["mcpAllowlist"] = kept
    else:
        perm_doc.pop("mcpAllowlist", None)

    perms = cli_doc.get("permissions")
    if isinstance(perms, dict):
        allow = perms.get("allow")
        cli_listed = manifest.get("cliAllowAdded") or []
        if isinstance(allow, list) and isinstance(cli_listed, list):
            kept_allow = remove_strings(allow, [item for item in cli_listed if isinstance(item, str)])
            if kept_allow:
                perms["allow"] = kept_allow
            else:
                perms.pop("allow", None)
        if perms:
            cli_doc["permissions"] = perms
        else:
            cli_doc.pop("permissions", None)

    if manifest.get("hookCommandAdded") and isinstance(manifest.get("hookCommand"), str):
        remove_hook(hooks_doc, manifest["hookCommand"])

    def file_change(key: str, doc: dict, old: str | None) -> Change | None:
        rel = scope.rel[key]
        if old is None:
            return None
        ours = fold(rel) in created
        if ours and empty_doc(doc):
            return Change(scope.path(key), old, None, keep_backup=False)
        if not doc:
            new = "{}\n"
        else:
            new = dump(doc)
        return Change(scope.path(key), old, new)

    changes: list[Change] = []
    for key, doc, old in (
        ("permissions", perm_doc, perm_old),
        ("cli", cli_doc, cli_old),
        ("hooks", hooks_doc, hooks_old),
    ):
        change = file_change(key, doc, old)
        if change is not None:
            changes.append(change)

    originals = manifest.get("originals") if isinstance(manifest.get("originals"), dict) else {}

    def owned(key: str, old: str | None) -> None:
        if old is None:
            return
        rel = scope.rel[key]
        if rel in originals:
            changes.append(Change(scope.path(key), old, originals[rel], executable=(key == "hook_script")))
        elif fold(rel) in created:
            changes.append(Change(scope.path(key), old, None, executable=(key == "hook_script"), keep_backup=False))

    owned("hook_script", script_old)
    owned("pairs", pairs_old)
    if manifest_old is not None:
        changes.append(Change(manifest_path, manifest_old, None, keep_backup=False))
    return changes


def revoke_project(root: Path) -> int:
    """Remove project allow-notify entries. Used by /warp-uninstall after --yes."""
    try:
        return run(project_scope(root), [], dry=False, revoke=True, with_git=False)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1


def uninstall_notes(root: Path) -> list[str]:
    scope = project_scope(root)
    path = scope.path("manifest")
    if not path.is_file():
        return []
    try:
        manifest = load_manifest(scope)
    except ConfigError as e:
        return [f"note: {e}"]
    if not manifest:
        return []
    lines = [f"{PROJECT_REL['manifest']}  [allow-notify manifest, project only]"]
    if manifest.get("hookCommandAdded"):
        lines.append(f"hook {manifest.get('hookCommand')} in {PROJECT_REL['hooks']}")
    for item in manifest.get("mcpAllowlistAdded") or []:
        lines.append(f"mcpAllowlist {item} in {PROJECT_REL['permissions']}")
    for item in manifest.get("cliAllowAdded") or []:
        lines.append(f"cli allow {item} in {PROJECT_REL['cli']}")
    for item in manifest.get("createdFiles") or []:
        lines.append(f"file created by allow-notify: {item}")
    return lines


def cleanup_dirs(scope: Scope) -> None:
    rmdir_empty(scope.path("hook_script").parent)
    if scope.name == "project":
        rmdir_empty(scope.base / ".cursor")


def run(scope: Scope, pairs: list[tuple[str, str]], dry: bool, revoke: bool, with_git: bool) -> int:
    if revoke:
        changes = revoke_changes(scope)
        if not changes:
            print("No warp-allow-notify manifest. Nothing removed.")
            return 0
    else:
        changes, _manifest = allow_changes(scope, pairs)
    real = [change for change in changes if not change.same]
    if not real:
        print("Already set. Nothing changed.")
        remind(scope, with_git and not revoke)
        return 0
    if dry:
        print("Dry run. Nothing written.")
        print()
    for change in real:
        if dry:
            print_diff(change)
    notes = apply_changes(real, dry)
    if not dry:
        for note in notes:
            print(note)
        if revoke:
            cleanup_dirs(scope)
            print("Removed the allow-notify entries recorded in the manifest. Other hooks and allow entries were left in place.")
        else:
            print("Changed the files above.")
    remind(scope, with_git and not revoke)
    return 0


def config_text(root: Path) -> str:
    path = root / ".warp" / "config.yaml"
    if path.is_file():
        return path.read_text()
    return ""


HELP = """
examples:
  python3 scripts/allow_notify.py ?
  python3 scripts/allow_notify.py --dry-run
  python3 scripts/allow_notify.py
  python3 scripts/allow_notify.py --with-jira --with-git
  python3 scripts/allow_notify.py --user --dry-run
  python3 scripts/allow_notify.py --user --yes
  python3 scripts/allow_notify.py --revoke

What it writes (project is the default; --user writes the home-directory files):
  IDE    .cursor/permissions.json   mcpAllowlist server:tool
         ~/.cursor/permissions.json with --user
         Skips the Run prompt when Run Mode is Auto-review, Allowlist, or
         Run Everything. Ask Every Time does not consult it. Setting the key
         replaces the in-app MCP allowlist. Per-user and per-repo files are
         concatenated. A team admin Run Mode override ignores the file.
  CLI    .cursor/cli.json permissions.allow Mcp(server:tool)
         ~/.cursor/cli-config.json with --user
         Does not change the IDE Run button.
  Hook   .cursor/hooks.json beforeMCPExecution (.cursor/hooks/warp-mcp-allow.py)
         ~/.cursor/hooks.json and ./hooks/warp-mcp-allow.py with --user
         Returns allow for this list and ask for every other tool. A hook
         allow does not currently skip the Run prompt. failClosed is off.
  Cloud  nothing. Cloud agents do not ask for approval, and beforeMCPExecution
         does not run there.

Default tools, from scripts/mcp_tools.py, on slackMcp and teamsMcp:
  slack_post_message, slack_send_message
  send_channel_message, teams_send_message
--with-jira adds the Atlassian tools Warp calls (getAccessibleAtlassianResources,
  getJiraIssue, getTransitionsForJiraIssue, listJiraIssueTransitions,
  transitionJiraIssue, addOrEditJiraIssueComment, addCommentToJiraIssue,
  searchJiraIssuesUsingJql).
--with-git adds GitHub add_issue_comment only. No Bitbucket tool is named;
  put that in notifyAllow.

notifyAllow in .warp/config.yaml is a list of extra server:tool pairs. No
wildcards. If the Run prompt shows a different tool, add that server:tool and
run this again.

Merges into existing JSON. Other hooks, other allow entries, the terminal
allowlist, and autoRun stay. A file that already exists is copied to a .bak
before it changes. A second run with the same flags writes nothing. --dry-run
prints a unified diff and writes nothing. --revoke removes only the entries
recorded in .cursor/warp-allow.json (or ~/.cursor/warp-allow.json). An emptied
mcpAllowlist key is deleted so Cursor can use the in-app list again.
/warp-uninstall removes the project entries and does not touch ~/.cursor.
--user refuses to write unless --yes is also set. --cursor-home stands in for
~/.cursor (used by tests).

?, help, -h, and --help print this text. Quote ? if the shell expands it.
A bare help after an option that takes a value stays that value.
"""


def main(argv: list[str] | None = None) -> int:
    import usage

    p = argparse.ArgumentParser(
        description="Allow Warp Slack/Teams (and optional Jira/GitHub) MCP writes without a prompt",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", help="repo root (default: git toplevel or cwd)")
    p.add_argument("--user", action="store_true", help="edit ~/.cursor instead of the repo")
    p.add_argument("--yes", action="store_true", help="required with --user before anything is written")
    p.add_argument("--with-jira", action="store_true", help="also allow the Jira tools Warp calls")
    p.add_argument("--with-git", action="store_true", help="also allow GitHub add_issue_comment")
    p.add_argument("--dry-run", action="store_true", help="print diffs and write nothing")
    p.add_argument("--revoke", action="store_true", help="remove only the entries this command recorded")
    p.add_argument("--cursor-home", help="directory standing in for ~/.cursor (tests)")
    args = p.parse_args(usage.normalize_argv(argv))

    if args.user and not args.yes and not args.dry_run:
        print("Refusing to edit user-level Cursor files without --yes.")
        print("Those files are ~/.cursor/permissions.json, ~/.cursor/cli-config.json, and ~/.cursor/hooks.json.")
        print("Get an explicit yes from the user, then re-run with --user --yes.")
        return 2

    root = find_root(args.root)
    scope = user_scope(cursor_home(args.cursor_home)) if args.user else project_scope(root)
    try:
        if args.revoke:
            pairs: list[tuple[str, str]] = []
        else:
            cfg = herald_fmt.read_config(root)
            extras = parse_notify_allow(config_text(root))
            pairs = build_pairs(cfg, extras, args.with_jira, args.with_git)
            print("Allowing these tools only:")
            for server, tool in pairs:
                print(f"  {server}:{tool}")
        return run(scope, pairs, args.dry_run, args.revoke, args.with_git)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
