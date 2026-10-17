#!/usr/bin/env python3
"""Replace `.cursor/plugins/warp` with the current plugin source.

The source is this command's plugin tree, or `--source`. When that tree is a
git checkout, the default branch is fetched first and that tree is what gets
copied. A failed fetch leaves the installed copy in place. `.warp/config.yaml`
and the beam are not touched.

  python3 scripts/upgrade.py ?
  python3 scripts/upgrade.py --root .

?, help, -h, and --help print this text. Quote ? if the shell expands it.
Prints `Warp vX.Y.Z` and tells you to reload Cursor. Safe to run again.

A live upgrade (1.7.6) is the default while a run is running (`--live` forces it, `--no-live` turns it off). It
holds the run lock, so supervise, launches, and Slack commands wait for it; Shuttles and the listener keep working
and are never reaped. It replaces each plugin file atomically (a Shuttle that runs a script mid-upgrade gets the old
file or the new one, never half of one), runs the new `install.py init --live` (new config keys, rules, the
allowlist, `.warp/version`; the run is kept), syncs `.warp/config.yaml` into the beam, records the upgrade on the
beam, posts it, and releases the lock. Do not reload Cursor while Shuttles are out: a reload ends the subagents.
The parent re-reads the new skill and rules instead, then runs its next pass.
Also writes every `commands/*.md` file to `.cursor/commands/` when the source
has that directory, so a reload lists those slash commands. Cursor's plugin
command index keeps the paths it had when it was published, which left out
`/warp-upgrade` and later `/warp-update-state`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import version  # noqa: E402

HELP = __doc__
PLUGIN_REL = Path(".cursor/plugins/warp")
COPY_IGNORE = {".git", ".cursor", ".warp", "__pycache__", "node_modules"}
KEEP_STATE = ("config.yaml", "beam.json")
COMMANDS_DIR = Path("commands")
PROJECT_COMMANDS = Path(".cursor/commands")


def project_command_sources(source: Path) -> list[Path]:
    folder = source / COMMANDS_DIR
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.md") if path.is_file())


def install_project_commands(root: Path, source: Path, dry: bool = False) -> list[tuple[str, str]]:
    """Copy every commands/*.md file to .cursor/commands/.

    Cursor's plugin command index keeps paths from when that index was
    published and does not admit commands added later. A project command
    at .cursor/commands/ is what a reload lists. Each row is (wrote|kept, filename).
    An empty list means the source has no command files.
    """
    files = project_command_sources(source)
    rows: list[tuple[str, str]] = []
    for src in files:
        dest = root / PROJECT_COMMANDS / src.name
        data = src.read_bytes()
        if dest.is_file() and dest.read_bytes() == data:
            rows.append(("kept", src.name))
            continue
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        rows.append(("wrote", src.name))
    return rows


def install_project_command(root: Path, source: Path, dry: bool = False) -> str:
    """Install every project command. Returns wrote, kept, or missing."""
    rows = install_project_commands(root, source, dry)
    if not rows:
        return "missing"
    if any(status == "wrote" for status, _name in rows):
        return "wrote"
    return "kept"


def installed_project_commands(root: Path) -> list[Path]:
    """Project slash commands this plugin installed, for uninstall."""
    folder = root / PROJECT_COMMANDS
    if not folder.is_dir():
        return []
    known = {path.stem for path in project_command_sources(version.PLUGIN_ROOT)}
    found = []
    for path in sorted(folder.glob("warp*.md")):
        if known and path.stem not in known:
            continue
        text = path.read_text(errors="replace")
        if text.startswith("---\nname: %s\n" % path.stem):
            found.append(path)
    return found


def _report_project_command(root: Path, source: Path) -> None:
    for _status, name in install_project_commands(root, source):
        print("/%s -> .cursor/commands/%s" % (Path(name).stem, name))


def _git(root: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_git(source: Path) -> bool:
    try:
        probe = _git(source, "rev-parse", "--is-inside-work-tree", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def default_branch(source: Path) -> str:
    head = _git(source, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", timeout=10)
    if head.returncode == 0 and head.stdout.strip():
        name = head.stdout.strip()
        return name.split("/", 1)[-1]
    for branch in ("main", "master"):
        have = _git(source, "rev-parse", "--verify", "-q", "refs/remotes/origin/%s" % branch, timeout=10)
        if have.returncode == 0:
            return branch
    return "main"


def fetched_tree(source: Path) -> tuple[Optional[Path], str]:
    """Fetch origin's default branch. (tree, "") or (None, error)."""
    if not _is_git(source):
        return source, ""
    remote = _git(source, "remote", "get-url", "origin", timeout=10)
    if remote.returncode != 0 or not remote.stdout.strip():
        return source, ""
    branch = default_branch(source)
    try:
        fetched = _git(source, "fetch", "--no-tags", "origin", branch, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "fetch failed").strip()
        return None, detail
    dest = Path(tempfile.mkdtemp(prefix="warp-upgrade-"))
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", "origin/%s" % branch],
        capture_output=True,
        timeout=30,
    )
    if archive.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        err = archive.stderr.decode("utf-8", "replace").strip() or "git archive failed"
        return None, err
    unpacked = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True)
    if unpacked.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return None, "could not unpack the fetched tree"
    return dest, ""


def _replace_file(path: Path, target: Path) -> None:
    """Copy path over target in one step: written beside it, then renamed onto it. A process that opens target
    meanwhile reads the old file or the new one, never a half-written one (a Shuttle running a script mid-upgrade)."""
    import os

    temp = target.parent / (".%s.warp-upgrade-%d" % (target.name, os.getpid()))
    shutil.copy2(path, temp)
    os.replace(temp, target)


def copy_tree(src: Path, dst: Path) -> int:
    """Overwrite dst with src, one file at a time and each atomically. Returns the number of files copied."""
    count = 0
    wanted = set()
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in COPY_IGNORE for part in rel.parts):
            continue
        if not path.is_file():
            continue
        wanted.add(rel.as_posix())
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.read_bytes() == path.read_bytes():
            continue  # unchanged: leave it alone
        _replace_file(path, target)
        count += 1
    if dst.is_dir():
        for path in sorted(dst.rglob("*"), reverse=True):
            if not path.is_file():
                continue
            rel = path.relative_to(dst).as_posix()
            if rel not in wanted and not any(part in COPY_IGNORE for part in Path(rel).parts):
                path.unlink()
    return count


def upgrade(root: Path, source: Optional[Path] = None) -> int:
    root = root.resolve()
    source = (source or version.PLUGIN_ROOT).resolve()
    plugin = root / PLUGIN_REL
    state = root / ".warp"
    before = {}
    for name in KEEP_STATE:
        path = state / name
        if path.is_file():
            before[name] = path.read_bytes()
    tree, err = fetched_tree(source)
    if tree is None:
        print("fetch failed: %s" % err)
        print("keeping the installed copy")
        _report_project_command(root, source)
        return 0
    cleanup = tree != source
    try:
        if plugin.resolve() == tree.resolve():
            _report_project_command(root, tree)
            landed = version.version_of(plugin) or version.version_of(source)
            print(version.label(landed))
            print("Reload Cursor (Developer: Reload Window).")
            return 0
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.mkdir(parents=True, exist_ok=True)
        copy_tree(tree, plugin)
        _report_project_command(root, tree)
    finally:
        if cleanup:
            shutil.rmtree(tree, ignore_errors=True)
    for name, blob in before.items():
        path = state / name
        if not path.is_file() or path.read_bytes() != blob:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
    landed = version.version_of(plugin) or version.version_of(source)
    print(version.label(landed))
    print("Reload Cursor (Developer: Reload Window).")
    return 0


def _run_state(root: Path) -> tuple:
    """(beam path or None, the beam or {}) for this checkout's live beam."""
    import json

    path = Path(root) / ".warp" / "beam.json"
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError):
        return None, {}


def _running(data: dict) -> bool:
    return isinstance(data, dict) and (data.get("runState") or "") == "running" and not data.get("paused")


def _out_count(data: dict) -> int:
    count = 0
    for t in (data.get("tickets") or {}).values():
        shuttle = t.get("shuttle") if isinstance(t, dict) and isinstance(t.get("shuttle"), dict) else {}
        if shuttle.get("pending") and not shuttle.get("returned"):
            count += 1
    return count


def live_upgrade(root: Path, source: Optional[Path] = None, init_runner=None) -> int:
    """Upgrade under the run lock, keeping every Shuttle and the listener, then refresh init and the beam's config."""
    import json
    import os

    import beamlock

    root = Path(root).resolve()
    beam_path, data = _run_state(root)
    if beam_path is None:
        print("upgrade: no beam in this checkout; a plain upgrade")
        return upgrade(root, source)
    before = version.version_of(root / PLUGIN_REL) or "unknown"
    out = _out_count(data)
    gen = (data.get("listener") or {}).get("generation") if isinstance(data.get("listener"), dict) else None
    print("upgrade: live, from Warp v%s. Holding Warp's passes: supervise, launches, and Slack commands wait for the run lock. "
          "%d Shuttle%s stay out and keep working; the listener%s stays up." % (
              before, out, "" if out == 1 else "s", (" (generation %s)" % gen) if gen else ""))
    with beamlock.run_lock(beam_path):
        import beam as beam_mod

        started = beam_mod.utcnow()
        held = beam_mod.load_json(beam_path)
        held["upgrade"] = {"state": "running", "from": before, "startedAt": started}
        beam_mod.atomic_write(beam_path, json.dumps(held, indent=2) + "\n", data=held)
        code = upgrade(root, source)
        plugin = root / PLUGIN_REL
        after = version.version_of(plugin) or before
        new_scripts = plugin / "scripts"
        env = dict(os.environ, WARP_LIVE_UPGRADE="1")
        runner = init_runner or (lambda cmd: __import__("subprocess").run(cmd, capture_output=True, text=True, env=env, timeout=300))
        for cmd in (
            [sys.executable, str(new_scripts / "install.py"), "init", "--root", str(root), "--live"],
            [sys.executable, str(new_scripts / "config_sync.py"), "--beam", str(beam_path)],
        ):
            if not (Path(cmd[1]).is_file()):
                print("upgrade: %s is not in the new plugin; skipped" % Path(cmd[1]).name)
                continue
            done = runner(cmd)
            for line in ((getattr(done, "stdout", "") or "") + (getattr(done, "stderr", "") or "")).splitlines():
                if line.strip():
                    print("  " + line)
        live = json.loads(beam_path.read_text())
        record = {"state": "done", "from": before, "to": after, "startedAt": started, "doneAt": beam_mod.utcnow(),
                  "shuttlesOut": _out_count(live)}
        live["upgrade"] = record
        live["pluginVersion"] = after
        beam_mod.atomic_write(beam_path, json.dumps(live, indent=2) + "\n", data=live)
        beam_mod.journal(beam_path, {"type": "upgrade", "from": before, "to": after, "live": True, "shuttlesOut": record["shuttlesOut"]})
    lines = ["herald: Warp upgraded live from v%s to v%s. %d Shuttle%s kept working; nothing was paused or restarted."
             % (before, after, record["shuttlesOut"], "" if record["shuttlesOut"] == 1 else "s")]
    try:
        import notify

        lines += notify.pass_post(root, lines)
    except Exception as exc:
        lines.append("herald: not written (%s)" % exc)
    for line in lines:
        print(line)
    print("upgrade: done. The run lock is released and the run goes on.")
    print("next: re-read %s/skills/warp/SKILL.md and %s/rules/*.mdc now: they replace what you loaded. Then run the next "
          "pass as usual (inbound.py apply-pending, supervise). Shuttles already out keep their prompts and run the new "
          "scripts; new Shuttles get the new prompts." % (plugin, plugin))
    print("next: do not reload Cursor while Shuttles are out: a reload ends the subagents. Reload when the run is paused "
          "or no Shuttle is out, to list new slash commands.")
    return code


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Upgrade the installed Warp plugin", epilog=HELP)
    parser.add_argument("--root", default=".")
    parser.add_argument("--source", help="plugin tree to copy (default: the tree this command is running from)")
    parser.add_argument("--live", action="store_true", help="upgrade while the run runs (the default when it is running)")
    parser.add_argument("--no-live", action="store_true", help="a plain upgrade even while the run runs")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    source = Path(args.source).resolve() if args.source else None
    root = Path(args.root)
    _path, data = _run_state(root.resolve())
    if args.live or (_running(data) and not args.no_live):
        return live_upgrade(root, source)
    if _running(data):
        print("warn: the run is running and %d Shuttles are out. This plain upgrade does not hold the run, and a Cursor "
              "reload ends the subagents; /warp-upgrade without --no-live upgrades live." % _out_count(data))
    return upgrade(root, source)


if __name__ == "__main__":
    sys.exit(main())
