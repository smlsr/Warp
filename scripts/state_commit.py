#!/usr/bin/env python3
"""Commit the beam, journal, and board so the next agent can see them.

Tokens, cost, API keys, and webhook URLs are stripped before the commit.
`.warp/config.yaml` stays gitignored and is not added.

  python3 scripts/state_commit.py ?
  python3 scripts/state_commit.py commit --root . --beam .warp/beam.json

?, help, -h, and --help print this text. Quote ? if the shell expands it.
The commit lands on the current branch. Pass --base when that branch must
be the configured base branch. The next cloud agent clones that branch.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


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
    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    return commit_state(Path(args.root), args.beam, args.base)


if __name__ == "__main__":
    sys.exit(main())
