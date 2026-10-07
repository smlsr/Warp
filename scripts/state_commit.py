#!/usr/bin/env python3
"""Commit the beam, journal, and board so the next Agent can see them.

Tokens, cost, API keys, and webhook URLs are stripped before the commit.
`.warp/config.yaml` stays gitignored and is not added.

`commit` lands on the current branch. Pass --base when that branch must be
the configured base branch.

`publish` fetches origin immediately before it creates a ticket branch, and
cuts that branch from the tip of the base branch. A ticket branch already in
flight is not rebased. Then it puts the beam, journal, board, and that
ticket's claim on the branch and pushes it. A new Agent checks out that
branch. It does not clone main. If the branch has no .warp beam, publish
refuses and the Agent must not start.

  python3 scripts/state_commit.py ?
  python3 scripts/state_commit.py commit --root . --beam .warp/beam.json
  python3 scripts/state_commit.py publish --root . --beam .warp/beam.json --id T-1
  python3 scripts/state_commit.py publish --id T-1 --no-push

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

HELP = __doc__
STATE_NAMES = (
    "beam.json",
    "journal.jsonl",
    "STATUS.md",
    "status.json",
    "BOARD.md",
    "board.html",
)
SECRET_KEYS = {
    "token",
    "tokens",
    "tokensin",
    "tokensout",
    "tokenscached",
    "cost",
    "spend",
    "usage",
    "apikey",
    "apitoken",
    "accesstoken",
    "webhook",
    "webhookurl",
    "secret",
    "password",
    "authorization",
}
WEBHOOK_RE = re.compile(
    r"https?://[^\s\"']*(?:hooks\.slack\.com|outlook\.office\.com/webhook|webhook\.site)[^\s\"']*",
    re.I,
)


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def secret_key(key: str) -> bool:
    return _norm_key(key) in SECRET_KEYS


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    return WEBHOOK_RE.sub("[redacted]", text)


def sanitize(value):
    """Drop token, cost, key, and webhook fields. Nested objects included."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if secret_key(key):
                continue
            out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_journal(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            lines.append(redact_text(line))
            continue
        lines.append(json.dumps(sanitize(row), sort_keys=True))
    return ("\n".join(lines) + "\n") if lines else ""


def prepare(warp: Path) -> list:
    """Rewrite state files in place with secrets removed. Returns paths that exist."""
    written = []
    beam = warp / "beam.json"
    if beam.is_file():
        data = sanitize(json.loads(beam.read_text()))
        beam.write_text(json.dumps(data, indent=2) + "\n")
        written.append(beam)
    journal = warp / "journal.jsonl"
    if journal.is_file():
        journal.write_text(sanitize_journal(journal.read_text()))
        written.append(journal)
    for name in STATE_NAMES:
        if name in {"beam.json", "journal.jsonl"}:
            continue
        path = warp / name
        if not path.is_file():
            continue
        if name.endswith(".json"):
            path.write_text(json.dumps(sanitize(json.loads(path.read_text())), indent=2) + "\n")
        else:
            path.write_text(redact_text(path.read_text()))
        written.append(path)
    return written


def _git(
    root: Path,
    *args: str,
    input_text: Optional[str] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )


def state_payload(warp: Path) -> dict:
    """Sanitized state file text, keyed by repo-relative path.

    The live files are not rewritten. `.warp/config.yaml` is never included.
    """
    payload = {}
    beam = warp / "beam.json"
    if beam.is_file():
        data = sanitize(json.loads(beam.read_text()))
        payload[".warp/beam.json"] = json.dumps(data, indent=2) + "\n"
    journal = warp / "journal.jsonl"
    if journal.is_file():
        text = sanitize_journal(journal.read_text())
        if text:
            payload[".warp/journal.jsonl"] = text
    for name in STATE_NAMES:
        if name in {"beam.json", "journal.jsonl"}:
            continue
        path = warp / name
        if not path.is_file():
            continue
        if name.endswith(".json"):
            payload[".warp/%s" % name] = json.dumps(sanitize(json.loads(path.read_text())), indent=2) + "\n"
        else:
            payload[".warp/%s" % name] = redact_text(path.read_text())
    return payload


def branch_has_beam(root: Path, branch: str) -> bool:
    """True when that branch's tree contains .warp/beam.json."""
    shown = _git(root, "cat-file", "-e", "%s:.warp/beam.json" % branch)
    return shown.returncode == 0


def _show_beam(root: Path, branch: str) -> Optional[dict]:
    shown = _git(root, "show", "%s:.warp/beam.json" % branch)
    if shown.returncode != 0:
        return None
    try:
        data = json.loads(shown.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _base_name(root: Path, config: dict) -> str:
    name = str((config or {}).get("baseBranch") or "").strip()
    if name:
        return name
    head = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    current = head.stdout.strip()
    if head.returncode == 0 and current:
        return current
    return "main"


def _remote_names(root: Path) -> list:
    return [line.strip() for line in _git(root, "remote").stdout.splitlines() if line.strip()]


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "-q", ref).returncode == 0


def _prepare_branch(root: Path, branch: str, base_name: str, remote: str) -> int:
    """Create a new ticket branch from the fetched tip of the base.

    A branch that already exists locally, or already exists on the remote,
    is kept. It is not rebased. That rebase belongs to the merge queue.
    """
    if _ref_exists(root, "refs/heads/%s" % branch):
        print("branch: %s kept" % branch)
        return 0
    if remote not in _remote_names(root):
        if not _ref_exists(root, "refs/heads/%s" % base_name) and not _ref_exists(root, base_name):
            print("refuse: base branch %s does not exist" % base_name)
            return 2
        made = _git(root, "branch", branch, base_name)
        if made.returncode != 0:
            print("refuse: could not create %s from %s" % (branch, base_name))
            print((made.stderr or made.stdout).strip())
            return 2
        print("base: %s" % base_name)
        return 0
    fetched = _git(root, "fetch", remote)
    if fetched.returncode != 0:
        print("refuse: fetch %s failed; do not cut the ticket branch from a stale base" % remote)
        print((fetched.stderr or fetched.stdout).strip())
        return 2
    remote_branch = "refs/remotes/%s/%s" % (remote, branch)
    if _ref_exists(root, remote_branch):
        made = _git(root, "branch", branch, "%s/%s" % (remote, branch))
        if made.returncode != 0:
            print("refuse: could not create %s from %s/%s" % (branch, remote, branch))
            print((made.stderr or made.stdout).strip())
            return 2
        print("branch: %s kept" % branch)
        return 0
    remote_base = "refs/remotes/%s/%s" % (remote, base_name)
    if not _ref_exists(root, remote_base):
        print("refuse: %s has no %s after fetch; do not cut the ticket branch" % (remote, base_name))
        return 2
    tip = _git(root, "rev-parse", "%s/%s" % (remote, base_name))
    made = _git(root, "branch", branch, "%s/%s" % (remote, base_name))
    if made.returncode != 0:
        print("refuse: could not create %s from %s/%s" % (branch, remote, base_name))
        print((made.stderr or made.stdout).strip())
        return 2
    print("base: %s/%s %s" % (remote, base_name, tip.stdout.strip()))
    return 0


def _commit_files_on_branch(root: Path, branch: str, files: dict, message: str) -> int:
    """Commit files onto branch without moving HEAD or the working tree."""
    tmp = Path(tempfile.mkdtemp(prefix="warp-publish-"))
    index = tmp / "index"
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    try:
        read = _git(root, "read-tree", branch, env=env)
        if read.returncode != 0:
            print("refuse: could not read %s" % branch)
            print((read.stderr or read.stdout).strip())
            return 2
        _git(root, "update-index", "--force-remove", ".warp/config.yaml", env=env)
        for rel, text in files.items():
            if rel == ".warp/config.yaml" or rel.endswith("/config.yaml"):
                continue
            blob = _git(root, "hash-object", "-w", "--stdin", input_text=text, env=env)
            if blob.returncode != 0 or not blob.stdout.strip():
                print("refuse: could not write %s" % rel)
                return 2
            cached = _git(
                root,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644,%s,%s" % (blob.stdout.strip(), rel),
                env=env,
            )
            if cached.returncode != 0:
                print("refuse: could not stage %s" % rel)
                print((cached.stderr or cached.stdout).strip())
                return 2
        tree = _git(root, "write-tree", env=env)
        if tree.returncode != 0 or not tree.stdout.strip():
            print("refuse: could not write the tree")
            return 2
        new_tree = tree.stdout.strip()
        old = _git(root, "rev-parse", "%s^{tree}" % branch)
        parent = _git(root, "rev-parse", branch)
        if old.returncode == 0 and old.stdout.strip() == new_tree:
            return 0
        committed = _git(
            root,
            "commit-tree",
            new_tree,
            "-p",
            parent.stdout.strip(),
            "-m",
            message,
        )
        if committed.returncode != 0 or not committed.stdout.strip():
            print("refuse: could not commit the beam onto %s" % branch)
            print((committed.stderr or committed.stdout).strip())
            return 2
        updated = _git(root, "update-ref", "refs/heads/%s" % branch, committed.stdout.strip())
        if updated.returncode != 0:
            print("refuse: could not update %s" % branch)
            print((updated.stderr or updated.stdout).strip())
            return 2
        return 0
    finally:
        try:
            index.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass


def _push_branch(root: Path, branch: str, remote: str) -> int:
    names = _remote_names(root)
    if remote not in names:
        print("refuse: no remote %s; do not start the Agent on a fresh clone of main" % remote)
        return 2
    pushed = _git(root, "push", "-u", remote, "refs/heads/%s:refs/heads/%s" % (branch, branch))
    if pushed.returncode != 0:
        print("refuse: push of %s failed; do not start the Agent" % branch)
        print((pushed.stderr or pushed.stdout).strip())
        return 2
    remote_beam = _git(root, "show", "%s/%s:.warp/beam.json" % (remote, branch))
    if remote_beam.returncode != 0:
        print("refuse: %s/%s has no .warp beam; do not start the Agent" % (remote, branch))
        return 2
    return 0


def publish_ticket(
    root: Path,
    beam_name: str = ".warp/beam.json",
    ticket_id: str = "",
    remote: str = "origin",
    push: bool = True,
) -> int:
    """Commit this ticket's beam, journal, board, and claim onto its branch.

    Does not move the orchestrator checkout. Cloud pushes the branch before
    an Agent may start. A missing .warp/beam.json refuses.
    """
    import orchestrator

    root = root.resolve()
    beam_path = Path(beam_name)
    if not beam_path.is_absolute():
        beam_path = root / beam_path
    if not beam_path.is_file():
        print("refuse: no beam at %s; do not start the Agent" % beam_path)
        return 2
    warp = beam_path.parent
    data = json.loads(beam_path.read_text())
    ticket = (data.get("tickets") or {}).get(ticket_id)
    if not isinstance(ticket, dict):
        print("refuse: unknown ticket %s; do not start the Agent" % ticket_id)
        return 2
    cfg = data.get("config") or {}
    branch = orchestrator.branch_name(ticket, cfg)
    base_name = _base_name(root, cfg)
    payload = state_payload(warp)
    if ".warp/beam.json" not in payload:
        print("refuse: no beam to publish; do not start the Agent")
        return 2
    if ".warp/config.yaml" in payload:
        print("refuse: .warp/config.yaml must not be committed")
        return 2
    if _prepare_branch(root, branch, base_name, remote) != 0:
        return 2
    code = _commit_files_on_branch(
        root,
        branch,
        payload,
        "Warp state: beam, journal, and board for %s" % ticket_id,
    )
    if code != 0:
        return code
    if not branch_has_beam(root, branch):
        print("refuse: branch %s has no .warp beam; do not start the Agent" % branch)
        return 2
    published = _show_beam(root, branch)
    claim = ((published or {}).get("tickets") or {}).get(ticket_id)
    if not isinstance(claim, dict):
        print("refuse: branch %s beam has no claim for %s; do not start the Agent" % (branch, ticket_id))
        return 2
    config_tracked = _git(root, "cat-file", "-e", "%s:.warp/config.yaml" % branch)
    if config_tracked.returncode == 0:
        print("refuse: branch %s commits .warp/config.yaml; do not start the Agent" % branch)
        return 2
    if push:
        pushed = _push_branch(root, branch, remote)
        if pushed != 0:
            return pushed
        remote_beam = _show_beam(root, "%s/%s" % (remote, branch))
        remote_claim = ((remote_beam or {}).get("tickets") or {}).get(ticket_id)
        if not isinstance(remote_claim, dict):
            print("refuse: %s/%s has no claim for %s; do not start the Agent" % (remote, branch, ticket_id))
            return 2
    agent = claim.get("agent") or ""
    print("published: %s" % branch)
    print("beam: .warp/beam.json")
    print("claim: %s status=%s agent=%s" % (ticket_id, claim.get("status") or "", agent))
    if push:
        print("pushed: %s %s" % (remote, branch))
    print("checkout: %s" % branch)
    return 0


def push_ref(root: Path, branch: str, remote: str = "origin") -> int:
    """Push `branch` when `remote` exists. No remote is not an error."""
    if remote not in _remote_names(root):
        return 0
    pushed = _git(root, "push", remote, "refs/heads/%s:refs/heads/%s" % (branch, branch))
    if pushed.returncode != 0:
        print("state: push failed")
        print((pushed.stderr or pushed.stdout).strip())
        return 1
    print("state: pushed %s/%s" % (remote, branch))
    return 0


def publish_base(root: Path, beam_name: str = ".warp/beam.json", push: bool = True) -> int:
    """Commit the live beam, journal, and board onto the base branch and push.

    Secrets are stripped. `.warp/config.yaml` is not committed. A checkout that
    is not a git repo does nothing. The parent calls this at the end of a tick
    and after a merge so a fresh clone of main has the latest beam.
    """
    root = Path(root).resolve()
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return 0
    beam_path = Path(beam_name)
    if not beam_path.is_absolute():
        beam_path = root / beam_path
    if not beam_path.is_file():
        return 0
    try:
        data = json.loads(beam_path.read_text())
    except (OSError, json.JSONDecodeError):
        print("state: beam is not json")
        return 1
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    base = _base_name(root, config)
    head = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    current = head.stdout.strip()
    if head.returncode == 0 and current == base:
        code = commit_state(root, str(beam_path), base)
    else:
        payload = state_payload(beam_path.parent)
        if ".warp/beam.json" not in payload:
            print("state: nothing to commit")
            return 0
        code = _commit_files_on_branch(root, base, payload, "Warp state: beam, journal, and board")
        if code == 0:
            print("state: committed on %s" % base)
    if code != 0 or not push:
        return code
    return push_ref(root, base)


def load_main_beam(beam_path: Path, replace: bool = False) -> list:
    """Fetch the base branch and load its beam when the local one is missing or empty.

    A fresh checkout must not start from an empty beam when main has one.
    A local beam that already has tickets is kept unless `replace` is true.
    `/warp-update-state` replaces, then puts local pause, stop, and claims back.
    The caller patches in-flight tickets from each `.warp/tickets/<id>/` directory.
    """
    beam_path = Path(beam_path)
    if not beam_path.is_absolute():
        beam_path = Path.cwd() / beam_path
    root = beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return []
    lines = []
    local = None
    if beam_path.is_file():
        try:
            local = json.loads(beam_path.read_text())
        except (OSError, json.JSONDecodeError):
            local = None
    config = {}
    if isinstance(local, dict) and isinstance(local.get("config"), dict):
        config = local["config"]
    base = _base_name(root, config)
    ref = base
    if "origin" in _remote_names(root):
        fetched = _git(root, "fetch", "origin", base)
        if fetched.returncode != 0:
            lines.append("beam: fetch failed")
        else:
            lines.append("beam: fetched origin/%s" % base)
            remote_ref = "refs/remotes/origin/%s" % base
            if _ref_exists(root, remote_ref):
                ref = "origin/%s" % base
    shown = _git(root, "show", "%s:.warp/beam.json" % ref)
    if shown.returncode != 0 or not shown.stdout.strip():
        lines.append("beam: %s has no beam" % ref)
        return lines
    try:
        remote = json.loads(shown.stdout)
    except json.JSONDecodeError:
        lines.append("beam: %s beam is not json" % ref)
        return lines
    if not isinstance(remote, dict):
        lines.append("beam: %s beam is not json" % ref)
        return lines
    remote_tickets = remote.get("tickets") if isinstance(remote.get("tickets"), dict) else {}
    local_tickets = {}
    if isinstance(local, dict) and isinstance(local.get("tickets"), dict):
        local_tickets = local["tickets"]
    if not replace:
        if beam_path.is_file() and local_tickets:
            lines.append("beam: kept local")
            return lines
        if beam_path.is_file() and not remote_tickets:
            lines.append("beam: kept local")
            return lines
    beam_path.parent.mkdir(parents=True, exist_ok=True)
    beam_path.write_text(json.dumps(remote, indent=2) + "\n")
    lines.append("beam: loaded from %s" % ref)
    return lines


def commit_state(root: Path, beam_name: str = ".warp/beam.json", base: str = "") -> int:
    root = root.resolve()
    beam_path = Path(beam_name)
    if not beam_path.is_absolute():
        beam_path = root / beam_path
    warp = beam_path.parent
    if base:
        head = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
        current = head.stdout.strip()
        if head.returncode != 0 or current != base:
            print("refuse: commit the beam on %s (current is %s)" % (base, current or "detached"))
            return 2
    prepare(warp)
    rels = []
    for name in STATE_NAMES:
        path = warp / name
        if path.is_file():
            rels.append(str(path.relative_to(root)))
    if not rels:
        print("state: nothing to commit")
        return 0
    added = _git(root, "add", "--", *rels)
    if added.returncode != 0:
        print(added.stderr.strip() or "git add failed")
        return 1
    staged = _git(root, "diff", "--cached", "--name-only", "--", *rels)
    if not staged.stdout.strip():
        print("state: nothing to commit")
        return 0
    committed = _git(
        root,
        "commit",
        "-m",
        "Warp state: beam, journal, and board",
    )
    if committed.returncode != 0:
        print(committed.stderr.strip() or committed.stdout.strip() or "git commit failed")
        return 1
    print(committed.stdout.strip())
    print("state: committed %s" % ", ".join(rels))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Commit Warp state without secrets", epilog=HELP)
    sub = parser.add_subparsers(dest="cmd", required=True)
    commit = sub.add_parser("commit")
    commit.add_argument("--root", default=".")
    commit.add_argument("--beam", default=".warp/beam.json")
    commit.add_argument("--base", default="", help="require HEAD to be this branch")
    pub = sub.add_parser("publish")
    pub.add_argument("--root", default=".")
    pub.add_argument("--beam", default=".warp/beam.json")
    pub.add_argument("--id", required=True)
    pub.add_argument("--remote", default="origin")
    pub.add_argument("--no-push", action="store_true", help="commit the branch and do not push")
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "publish":
        return publish_ticket(
            Path(args.root),
            args.beam,
            args.id,
            remote=args.remote,
            push=not args.no_push,
        )
    return commit_state(Path(args.root), args.beam, args.base)


if __name__ == "__main__":
    sys.exit(main())
