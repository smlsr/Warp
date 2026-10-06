#!/usr/bin/env python3
"""Install or remove Warp in a repo. Both subcommands are idempotent.

  init       copy the plugin to .cursor/plugins/warp, create .warp/config.yaml,
             set the channel names to warp, and append the
             gitignore snippet. Each step runs only if it is not already done.
  uninstall  list what would be removed; delete only with --yes.

Nothing here touches product code, git history, or any file outside the repo.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_REL = Path(".cursor/plugins/warp")
STATE_REL = Path(".warp")
COPY_IGNORE = {".git", ".cursor", ".warp", "__pycache__", "node_modules"}
CHANNEL_KEYS = ("slackChannel", "teamsChannel")
DEFAULT_CHANNEL = "warp"
OLD_DEFAULT_CHANNEL = "Warp"  # shipped briefly; Slack channel names are lowercase
CHANNEL_RE = re.compile(r"[a-z0-9_-]{1,80}")
IGNORE_ENTRIES = {".warp", ".warp/", "/.warp", "/.warp/"}


def git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def find_root(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).resolve()
    top = git(Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else Path.cwd().resolve()


def read_snippet(root: Path) -> Optional[str]:
    for base in (root / PLUGIN_REL, PLUGIN_ROOT):
        f = base / "assets" / "gitignore-snippet.txt"
        if f.is_file():
            return f.read_text().rstrip("\n") + "\n"
    return None


def example_config(root: Path) -> Optional[Path]:
    for base in (root / PLUGIN_REL, PLUGIN_ROOT):
        f = base / "assets" / "config.example.yaml"
        if f.is_file():
            return f
    return None


def copy_missing(src: Path, dst: Path, dry: bool) -> int:
    """Copy files that do not exist in dst. Never overwrite. Returns count."""
    n = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(part in COPY_IGNORE for part in rel.parts) or not p.is_file():
            continue
        target = dst / rel
        if target.exists() or target.is_symlink():
            continue
        n += 1
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    return n


def channel_value(text: str, key: str) -> tuple[bool, str]:
    m = re.search(rf"^{key}:[ \t]*(.*?)[ \t]*(#.*)?$", text, re.M)
    if not m:
        return False, ""
    return True, m.group(1).strip().strip("\"'")


def set_channel(text: str, key: str, value: str) -> str:
    line = f'{key}: "{value}"'
    pat = re.compile(rf"^{key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"


def apply_channels(text: str, channel: str) -> tuple[str, list[str]]:
    changed = []
    for key in CHANNEL_KEYS:
        present, val = channel_value(text, key)
        if val in ("", OLD_DEFAULT_CHANNEL):
            text = set_channel(text, key, channel)
            changed.append(key)
    return text, changed


# An active key is a line that starts with the name. A commented line such as
# `# pushMerge: false` is a hint, not a key, so it never counts as "already set"
# and never blocks adding the real line.
_KEY_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):")


def active_keys(text: str) -> list[str]:
    return [m.group(1) for line in text.splitlines() if (m := _KEY_LINE.match(line))]


def key_blocks(example: str) -> list[tuple[str, str]]:
    """Each active key plus the comments that introduce it and the comment lines right after it.

    `# key: value` stays inside a block as a comment. It is not its own key.
    """
    lines = example.splitlines()
    blocks: list[tuple[str, str]] = []
    pending: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _KEY_LINE.match(line)
        if not m:
            if line.strip() or pending:
                pending.append(line)
            i += 1
            continue
        block = pending + [line]
        pending = []
        i += 1
        while i < len(lines) and lines[i].lstrip().startswith("#"):
            block.append(lines[i])
            i += 1
        while block and not block[0].strip():
            block.pop(0)
        blocks.append((m.group(1), "\n".join(block).rstrip() + "\n"))
    return blocks


def add_missing_keys(text: str, example: str) -> tuple[str, list[str]]:
    """Append example blocks whose active key is absent. Does not edit existing lines."""
    have = set(active_keys(text))
    added: list[str] = []
    extra: list[str] = []
    for key, block in key_blocks(example):
        if key in have:
            continue
        extra.append(block.rstrip("\n"))
        added.append(key)
        have.add(key)
    if not extra:
        return text, []
    return text.rstrip("\n") + "\n\n" + "\n\n".join(extra) + "\n", added


def has_ignore_entry(text: str) -> bool:
    return any(l.strip() in IGNORE_ENTRIES for l in text.splitlines())


def apply_gitignore(text: str, snippet: str) -> tuple[str, str]:
    """Append the snippet, or replace an old ignore-all `.warp/` line.

    Unrelated gitignore lines stay. Returns (text, skip|append|replace).
    """
    norm = text.replace("\r\n", "\n")
    snip = snippet if snippet.endswith("\n") else snippet + "\n"
    lines = norm.splitlines()
    drop = set()
    for index, line in enumerate(lines):
        if line.strip() not in IGNORE_ENTRIES:
            continue
        drop.add(index)
        cursor = index - 1
        while cursor >= 0 and cursor not in drop:
            raw = lines[cursor]
            if not raw.strip() or (raw.lstrip().startswith("#") and "warp" in raw.lower()):
                drop.add(cursor)
                cursor -= 1
                continue
            break
    present = snip.strip() in norm
    if not drop and present:
        return norm if norm.endswith("\n") or norm == "" else norm + "\n", "skip"
    if not drop:
        sep = ""
        if norm and not norm.endswith("\n"):
            sep = "\n"
        if norm and not norm.endswith("\n\n"):
            sep = ("" if norm.endswith("\n") else "\n") + "\n"
        body = norm + sep + snip
        if not body.endswith("\n"):
            body += "\n"
        return body, "append"
    rebuilt = []
    inserted = present
    for index, line in enumerate(lines):
        if index in drop:
            if not inserted:
                rebuilt.extend(snip.rstrip("\n").split("\n"))
                inserted = True
            continue
        rebuilt.append(line)
    if not inserted:
        if rebuilt and rebuilt[-1] != "":
            rebuilt.append("")
        rebuilt.extend(snip.rstrip("\n").split("\n"))
    body = "\n".join(rebuilt).rstrip("\n") + "\n"
    return body, "replace"


def init(root: Path, channel: Optional[str], dry: bool, allow: bool = True) -> list[tuple[str, str]]:
    steps: list[tuple[str, str]] = []
    channel = channel or DEFAULT_CHANNEL
    plugin = root / PLUGIN_REL
    state = root / STATE_REL

    for d in (root / ".cursor" / "plugins", state):
        rel = d.relative_to(root)
        if d.is_dir():
            steps.append(("skip", f"{rel}/ already exists"))
        else:
            if not dry:
                d.mkdir(parents=True, exist_ok=True)
            steps.append(("done", f"created {rel}/"))

    if plugin.resolve() == PLUGIN_ROOT:
        steps.append(("skip", f"{PLUGIN_REL}/ is the running plugin"))
    else:
        n = copy_missing(PLUGIN_ROOT, plugin, dry)
        if n:
            steps.append(("done", f"copied {n} plugin file(s) to {PLUGIN_REL}/ (existing files kept)"))
        else:
            steps.append(("skip", f"{PLUGIN_REL}/ already has every plugin file"))

    cfg = state / "config.yaml"
    if cfg.exists():
        text = cfg.read_text()
        new, changed = apply_channels(text, channel)
        src = example_config(root)
        added: list[str] = []
        if src:
            new, added = add_missing_keys(new, src.read_text())
        elif not active_keys(new):
            steps.append(("warn", "assets/config.example.yaml not found; could not add missing config keys"))
        if changed or added:
            if not dry:
                cfg.write_text(new)
        if changed:
            steps.append(("done", f"set {', '.join(changed)} to {channel!r} in existing config (was empty or the old default)"))
        if added:
            steps.append(("done", "added missing config keys: " + ", ".join(added) + " (existing values and comments kept)"))
        if not changed and not added:
            steps.append(("skip", ".warp/config.yaml exists, channels already set; left as is"))
        _, slack = channel_value(new, "slackChannel")
        if slack != slack.lower():
            steps.append(("warn", f"slackChannel {slack!r} has uppercase; Slack channel names are lowercase, so Herald will use {slack.lower()!r}. Change it in .warp/config.yaml"))
    else:
        src = example_config(root)
        if not src:
            steps.append(("fail", "assets/config.example.yaml not found"))
        else:
            new = src.read_text()
            for key in CHANNEL_KEYS:
                new = set_channel(new, key, channel)
            if not dry:
                cfg.write_text(new)
            steps.append(("done", f"created .warp/config.yaml from example with channel {channel!r}"))

    try:
        import provider

        steps += provider.init_steps(root, dry)
    except Exception as e:  # provider detection must never fail init
        steps.append(("warn", f"could not check the git provider: {e}"))

    try:
        import jira_project

        steps += jira_project.init_steps(root, dry)
    except Exception as e:  # project detection must never fail init
        steps.append(("warn", f"could not detect jiraProject: {e}"))

    snippet = read_snippet(root)
    gi = root / ".gitignore"
    if snippet is None:
        steps.append(("fail", "assets/gitignore-snippet.txt not found"))
    else:
        text = gi.read_text() if gi.exists() else ""
        updated, action = apply_gitignore(text, snippet)
        if action == "skip":
            steps.append(("skip", ".gitignore already keeps Warp state and ignores secrets"))
        else:
            if not dry:
                gi.write_text(updated)
            if action == "replace":
                steps.append(("done", "updated .gitignore so the beam, journal, and board are committed and secrets stay ignored"))
            else:
                steps.append(("done", "appended Warp snippet to .gitignore"))

    steps.extend(allow_steps(root, dry, allow))
    steps.append(record_version(root, dry))
    return steps


def allow_steps(root: Path, dry: bool, allow: bool) -> list[tuple[str, str]]:
    """Write the project MCP allowlist. User-level files stay opt-in."""
    if not allow:
        return [("skip", "MCP allowlist skipped (--no-allow). /warp-allow-notify writes it. Undo: /warp-allow-notify --revoke")]
    cfg = root / ".warp" / "config.yaml"
    text = cfg.read_text() if cfg.is_file() else ""
    import allow_notify

    if not allow_notify.auto_allow_enabled(text):
        return [("skip", "autoAllowTools is false. MCP allowlist not written. /warp-allow-notify writes it.")]
    try:
        with_git, note = allow_notify.git_comment_wanted(root)
        steps = allow_notify.apply_project(root, dry=dry, with_git=with_git)
    except Exception as e:  # allowlist must never fail init
        return [("warn", f"could not write the MCP allowlist: {e}")]
    if note:
        steps.append(("warn", note))
    return steps


def record_version(root: Path, dry: bool) -> tuple[str, str]:
    """Write .warp/version from the installed copy, and warn when the source copy is newer."""
    import version

    source = version.version_of(version.PLUGIN_ROOT)
    installed = version.version_of(root / PLUGIN_REL)
    recorded = installed or source
    path = root / ".warp" / "version"
    wrote = False
    if recorded and not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = recorded + "\n"
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
            wrote = True
    if installed and source and installed != source:
        return ("warn", version.upgrade_line(installed, source))
    shown = version.label(source or installed)
    if wrote:
        return ("done", f"recorded {shown} in .warp/version")
    return ("skip", shown)


def uninstall(root: Path, remove_gitignore: bool, yes: bool) -> int:
    plugin, state, gi = root / PLUGIN_REL, root / STATE_REL, root / ".gitignore"
    snippet = read_snippet(root)
    targets: list[tuple[str, Path]] = []
    for label, p in (("plugin copy", plugin), ("Warp state", state)):
        if p.is_symlink() or p.exists():
            targets.append((label, p))

    block_found = False
    gi_note = None
    if remove_gitignore:
        text = gi.read_text() if gi.exists() else ""
        if snippet and snippet in text.replace("\r\n", "\n"):
            block_found = True
        elif has_ignore_entry(text):
            gi_note = ".gitignore has a .warp/ entry that is not the Warp snippet; leaving it alone"
        else:
            gi_note = ".gitignore has no Warp snippet; nothing to remove"

    print(f"Repo: {root}")
    print("Will remove:")
    for label, p in targets:
        extra = ""
        if p.is_dir() and not p.is_symlink():
            extra = f" ({sum(1 for f in p.rglob('*') if f.is_file())} files)"
        print(f"  - {p.relative_to(root)}{'/' if p.is_dir() else ''}  [{label}]{extra}")
    if block_found:
        print("  - Warp snippet in .gitignore  [only that block; other lines kept]")
    if not targets and not block_found:
        print("  (nothing)")
    if gi_note:
        print(f"Note: {gi_note}")

    beam = state / "beam.json"
    if beam.is_file():
        try:
            run = json.loads(beam.read_text()).get("runState")
        except Exception:
            run = None
        if run == "running":
            print("Warning: the beam is still running. Run /warp-stop first.")
        print("Warning: .warp/ holds the beam, journal, and config. They cannot be recovered.")
    cfg = state / "config.yaml"
    if cfg.is_file():
        m = re.search(r"^stateDir:[ \t]*(\S+)", cfg.read_text(), re.M)
        if m and m.group(1).strip("\"'") not in {".warp", ".warp/"}:
            print(f"Note: config stateDir is {m.group(1)}; that location is NOT removed.")
    print("Not touched: product code, warp/<id> branches, pull requests, the user-level plugin (Cursor Settings).")
    print("Not touched: user-level allow-notify files (~/.cursor/permissions.json, cli-config.json, hooks.json).")

    import allow_notify

    allow_notes = allow_notify.uninstall_notes(root)
    if allow_notes:
        print("Will remove project allow-notify entries (not user-level files):")
        for line in allow_notes:
            print(f"  - {line}")

    if not targets and not block_found and not allow_notes:
        print("\nNothing to remove. Run /warp-init to install.")
        return 0

    if not yes:
        print("\nNothing deleted. Show this list to the user, get an explicit yes, then re-run with --yes.")
        return 0

    if allow_notes:
        rc = allow_notify.revoke_project(root)
        if rc != 0:
            return rc

    for _, p in targets:
        if p.is_symlink() or p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
    for d in (root / ".cursor" / "plugins", root / ".cursor"):
        try:
            d.rmdir()
        except OSError:
            break
    if block_found:
        text = gi.read_text().replace("\r\n", "\n")
        i = text.find(snippet)
        before, after = text[:i], text[i + len(snippet):]
        if before.endswith("\n\n"):
            before = before[:-1]
        gi.write_text(before + after)
    print("\nRemoved. Reload Cursor, then run /warp-init for a fresh install.")
    return 0


INSTALL_HELP = """
examples:
  python3 scripts/install.py ?
  python3 scripts/install.py init
  python3 scripts/install.py init --dry-run
  python3 scripts/install.py init --channel eng-builds
  python3 scripts/install.py init --no-allow
  python3 scripts/install.py uninstall
  python3 scripts/install.py uninstall --remove-gitignore --yes

init is idempotent. It copies missing plugin files, appends missing config
keys with their defaults, sets an empty slackChannel or teamsChannel (or the
old Warp default) to warp, detects gitProvider from origin, writes the project
MCP allowlist (the same tools as allow_notify.py, including Jira, and the
GitHub comment tool when the provider is GitHub), and writes .warp/version.
autoAllowTools defaults to true. --no-allow skips the allowlist. --user is
not used here; user-level files stay opt-in. --dry-run writes nothing.
--channel is lowercased (letters, digits, - and _, max 80). --root is the
repo (default: git top level). Undo the allowlist with allow_notify.py --revoke.

uninstall without --yes only lists. --yes deletes .cursor/plugins/warp and
.warp/, and the project allow-notify entries. --remove-gitignore removes only
the snippet init added. ~/.cursor is not touched.

?, help, -h, and --help print this text. Quote ? if the shell expands it.
A bare help after an option that takes a value stays that value
(--channel help is a channel name).
"""


def main() -> None:
    p = argparse.ArgumentParser(
        description="Warp install / uninstall",
        epilog=INSTALL_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init")
    pi.add_argument("--root")
    pi.add_argument("--channel", help="channel to use instead of warp (lowercase letters, digits, - and _)")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--no-allow", action="store_true", help="do not write the project MCP allowlist")
    pu = sub.add_parser("uninstall")
    pu.add_argument("--root")
    pu.add_argument("--remove-gitignore", action="store_true")
    pu.add_argument("--yes", action="store_true", help="delete; without it only list")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
    root = find_root(args.root)
    if args.cmd == "init":
        if args.channel is not None:
            wanted = args.channel.strip().lstrip("#").lower()
            if not CHANNEL_RE.fullmatch(wanted):
                sys.exit(f"invalid --channel {args.channel!r}: use lowercase letters, digits, - and _ (max 80), no spaces or dots")
            if wanted != args.channel:
                print(f"note: --channel {args.channel!r} normalized to {wanted!r}")
            args.channel = wanted
        steps = init(root, args.channel, args.dry_run, allow=not args.no_allow)
        print(f"Repo: {root}" + ("  (dry run, nothing written)" if args.dry_run else ""))
        for status, msg in steps:
            print(f"  [{status}] {msg}")
        if any(s == "fail" for s, _ in steps):
            sys.exit(1)
        if any(s == "done" for s, _ in steps) and not args.dry_run:
            print("Reload Cursor so the commands and rules load. Then connect Jira, your git provider (GitHub or Bitbucket, if you want PRs), and Slack or Teams in Settings.")
            try:
                import notify

                notify.report(notify.build("init", root))
            except Exception as e:
                print(f"herald: not posted ({e})")
        elif not args.dry_run:
            print("Already set up. Nothing changed.")
    else:
        sys.exit(uninstall(root, args.remove_gitignore, args.yes))


if __name__ == "__main__":
    main()
