#!/usr/bin/env python3
"""Git provider and local-only mode for Warp.

Warp opens, reviews, and merges pull requests through whatever the repo uses:
GitHub or Bitbucket, via a connected MCP server or (GitHub) the gh CLI. This
script decides which, and when none is usable it says so and Warp works
local-only instead of failing. Warp never installs gh, creates keys, or
connects an app. It only reports what is missing.

  resolve      print the mode, provider, and the order of tools to try
  merge-local  squash-merge a ticket branch into the base branch, locally
  note         record that a ticket ran local-only (outbox + Herald message)
  init         (used by /warp-init) detect the provider and write it to config

Config (.warp/config.yaml):
  gitProvider  auto | github | bitbucket   auto reads the origin remote host
  githubMcp    connected GitHub MCP server name
  bitbucketMcp connected Bitbucket MCP server name
  ghCli        true: GitHub may use the gh CLI when it is installed and logged in
  pushMerge    true: connected mode. false: local-only, never push or open PRs
  baseBranch   branch tickets branch from and merge into; empty detects it

Mode is local when pushMerge is false, there is no origin remote, or the
origin host is not GitHub or Bitbucket. Otherwise it is connected, and the
agent tries the tools in `methods` in order. If none works for a ticket it
falls back to local for that ticket and runs `note`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import herald_fmt as fmt  # noqa: E402

SUPPORTED = ("github", "bitbucket")
FALSE = {"false", "no", "off", "0"}


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    if cmd and cmd[0] == "git":
        cmd = ["git", "-c", "maintenance.auto=false", *cmd[1:]]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def truthy(v: str) -> bool:
    return str(v).strip().lower() not in FALSE


def remote_host(remote: str) -> str | None:
    if not remote:
        return None
    m = re.match(r"^https?://(?:[^@/]+@)?([^/:]+)", remote) or re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/]", remote)
    host = m.group(1).lower() if m else None
    return host if host and "." in host else None


def detect_provider(remote: str) -> str | None:
    """github, bitbucket, another host name for unsupported hosts, or None with no usable remote."""
    host = remote_host(remote)
    if not host:
        return None
    if "github" in host:
        return "github"
    if "bitbucket" in host:
        return "bitbucket"
    return host


def gh_status() -> dict:
    """Is the gh CLI installed and logged in? Read-only; nothing is installed or changed."""
    if not shutil.which("gh"):
        return {"installed": False, "authenticated": False}
    try:
        ok = _run(["gh", "auth", "status"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return {"installed": True, "authenticated": ok}


def default_base(root: Path, cfg: dict) -> str:
    if cfg.get("baseBranch"):
        return cfg["baseBranch"]
    head = fmt.git(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for name in ("main", "master"):
        if fmt.git(root, "rev-parse", "--verify", "-q", f"refs/heads/{name}"):
            return name
    return fmt.git(root, "symbolic-ref", "--short", "-q", "HEAD") or "main"


def resolve(root: Path, cfg: dict | None = None, gh: dict | None = None) -> dict:
    root = root.resolve()
    cfg = cfg or fmt.read_config(root)
    remote = fmt.git(root, "remote", "get-url", "origin")
    detected = detect_provider(remote)
    configured = (cfg.get("gitProvider") or "auto").lower()
    out = {
        "mode": "local",
        "provider": None,
        "configured": configured,
        "remote": remote or None,
        "detected": detected,
        "methods": [],
        "baseBranch": default_base(root, cfg),
        "reason": "",
        "missing": [],
    }
    if not truthy(cfg.get("pushMerge", "true")):
        out["reason"] = "pushMerge is false"
        return out
    if not remote:
        out["reason"] = "no origin remote"
        return out
    provider = detected if configured == "auto" else configured
    if provider not in SUPPORTED:
        if configured != "auto":
            out["reason"] = f"gitProvider {configured!r} is not github, bitbucket, or auto"
        else:
            out["reason"] = f"origin host {detected!r} is not GitHub or Bitbucket, which are the supported providers"
        return out
    out["mode"], out["provider"] = "connected", provider
    methods = [f"mcp:{cfg.get(provider + 'Mcp') or provider}"]
    if provider == "github" and truthy(cfg.get("ghCli", "true")):
        gh = gh or gh_status()
        if gh["authenticated"]:
            methods.append("gh")
        elif gh["installed"]:
            out["missing"].append("gh is installed but not logged in (run `gh auth login` yourself; Warp will not)")
        else:
            out["missing"].append("gh CLI is not installed")
    out["methods"] = methods
    out["missing"].append(
        f"a connected {provider.capitalize()} server named {cfg.get(provider + 'Mcp') or provider!r} in Cursor Settings "
        "(Warp cannot check this; if it is missing the ticket falls back to local-only)"
    )
    return out


def explain(r: dict) -> list[str]:
    if r["mode"] == "local":
        return [
            f"git: local-only mode ({r['reason']}). Never push, never open PRs, never use a provider connector.",
            f"git: work on local branches off {r['baseBranch']}; Reed merges with `provider.py merge-local`.",
        ]
    lines = [f"git: connected mode, provider {r['provider']}, base {r['baseBranch']}. Try in order: {', '.join(r['methods'])}."]
    lines.append("git: if none of those is available for a ticket, do not error: fall back to local-only for it and run `provider.py note`.")
    return lines


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def note(root: Path, beam_path: Path | None, tid: str | None, reason: str, what: str = "local-only") -> str:
    """Outbox line, journal entry, and a Herald message that a ticket ran local-only."""
    root = root.resolve()
    text = f"{tid or 'Warp'} ran {what}: {reason}. Nothing was pushed and no pull request was opened."
    outbox = root / ".warp" / "outbox.md"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with outbox.open("a") as f:
        f.write(f"\n## {_now()}\n\n{text}\n")
    if beam_path and beam_path.is_file():
        with (beam_path.parent / "journal.jsonl").open("a") as f:
            f.write(json.dumps({"ts": _now(), "type": "local-only", "id": tid, "reason": reason}) + "\n")
    try:
        import notify

        msg = fmt.message(
            root,
            "Local-only mode",
            intro=text,
            facts=[("Ticket", tid)] if tid else [],
            footer="Set pushMerge: true and connect a git provider to push and open pull requests.",
        )
        notify.report(notify.build("local-only", root, msg=msg))
    except Exception as e:
        print(f"herald: not posted ({e})")
    return text


def merge_local(root: Path, branch: str, base: str, message: str) -> tuple[bool, str]:
    """Squash-merge `branch` into `base`. Leaves the repo as it found it on failure."""
    root = root.resolve()
    if not fmt.git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return False, f"branch {branch} does not exist"
    if not fmt.git(root, "rev-parse", "--verify", "-q", f"refs/heads/{base}"):
        return False, f"base branch {base} does not exist"
    if branch == base:
        return False, "branch and base are the same"
    if fmt.git(root, "status", "--porcelain", "--untracked-files=no"):
        return False, "working tree has uncommitted changes; commit or stash them, then retry"
    original = fmt.git(root, "symbolic-ref", "--short", "-q", "HEAD") or fmt.git(root, "rev-parse", "HEAD")
    if _run(["git", "checkout", "-q", base], root).returncode != 0:
        return False, f"could not check out {base}"
    try:
        r = _run(["git", "merge", "--squash", branch], root)
        if r.returncode != 0:
            _run(["git", "reset", "--merge"], root)
            return False, "merge conflict; nothing was merged. Resolve on the ticket branch and retry"
        if not fmt.git(root, "diff", "--cached", "--name-only"):
            return True, f"{branch} has no changes beyond {base}; nothing to merge"
        c = _run(["git", "commit", "-q", "-m", message], root)
        if c.returncode != 0:
            _run(["git", "reset", "--merge"], root)
            return False, "commit failed: " + (c.stderr.strip() or c.stdout.strip() or "unknown error")
        return True, fmt.git(root, "rev-parse", "--short", "HEAD")
    finally:
        _run(["git", "checkout", "-q", original], root)


def set_key(text: str, key: str, raw: str) -> str:
    pat = re.compile(rf"^{key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _: f"{key}: {raw}", text, count=1)
    return text.rstrip("\n") + f"\n{key}: {raw}\n"


def init_steps(root: Path, dry: bool = False, gh: dict | None = None) -> list[tuple[str, str]]:
    """For /warp-init: detect the provider, write it, and say what is missing."""
    root = root.resolve()
    steps: list[tuple[str, str]] = []
    cfg_file = root / ".warp" / "config.yaml"
    text = cfg_file.read_text() if cfg_file.is_file() else None
    cfg = fmt.read_config(root)
    remote = fmt.git(root, "remote", "get-url", "origin")
    detected = detect_provider(remote)
    configured = (cfg["gitProvider"] or "auto").lower()

    if not remote:
        if truthy(cfg["pushMerge"]):
            if text is not None and not dry:
                text = set_key(text, "pushMerge", "false")
                cfg_file.write_text(text)
            steps.append(("done", "no origin remote: set pushMerge: false (local-only). Warp will not push or open PRs. Set it to true after you add a remote"))
        else:
            steps.append(("skip", "no origin remote; pushMerge is already false (local-only)"))
        return steps

    if detected in SUPPORTED:
        if configured in ("", "auto"):
            if text is not None and not dry:
                cfg_file.write_text(set_key(text, "gitProvider", f'"{detected}"'))
            steps.append(("done", f"origin is {detected}: set gitProvider to {detected!r}"))
        elif configured != detected:
            steps.append(("warn", f"gitProvider is {configured!r} but origin looks like {detected}; left as is"))
        else:
            steps.append(("skip", f"gitProvider already {detected!r}"))
    else:
        steps.append(("warn", f"origin host {detected!r} is not GitHub or Bitbucket. Warp will run local-only here; set pushMerge: false to make that explicit"))
        return steps

    provider = detected if configured in ("", "auto") else configured
    if provider in SUPPORTED and truthy(cfg["pushMerge"]):
        mcp = cfg.get(provider + "Mcp") or provider
        if provider == "github":
            g = gh or gh_status()
            if truthy(cfg["ghCli"]) and g["authenticated"]:
                steps.append(("info", "gh CLI is installed and logged in; Warp uses it if no GitHub connector is connected"))
            else:
                why = "gh is installed but not logged in" if g["installed"] else "gh is not installed"
                steps.append(("warn", f"needs a connected GitHub server in Cursor Settings (named {mcp!r}) or an authenticated gh CLI; {why}. Warp installs and logs in nothing. Without either it runs local-only"))
        else:
            steps.append(("warn", f"needs a connected Bitbucket server in Cursor Settings (named {mcp!r}). Warp installs nothing. Without it it runs local-only"))
    return steps


def main() -> None:
    p = argparse.ArgumentParser(description="Git provider and local mode")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("resolve")
    pr.add_argument("--root", default=".")
    pr.add_argument("--json", action="store_true")
    pm = sub.add_parser("merge-local")
    pm.add_argument("--root", default=".")
    pm.add_argument("--beam", default=".warp/beam.json")
    pm.add_argument("--id", required=True)
    pm.add_argument("--branch", required=True)
    pm.add_argument("--base")
    pm.add_argument("--message")
    pn = sub.add_parser("note")
    pn.add_argument("--root", default=".")
    pn.add_argument("--beam", default=".warp/beam.json")
    pn.add_argument("--id")
    pn.add_argument("--reason", required=True)
    args = p.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.cmd == "resolve":
            r = resolve(root)
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                print("\n".join(explain(r)))
                for m in r["missing"]:
                    print(f"git: missing: {m}")
                print(json.dumps(r))
        elif args.cmd == "merge-local":
            base = args.base or resolve(root)["baseBranch"]
            beam_path = Path(args.beam)
            summary = ""
            try:
                summary = json.loads(beam_path.read_text())["tickets"][args.id].get("summary") or ""
            except Exception:
                pass
            msg = args.message or f"[{args.id}] {summary}".strip()
            ok, detail = merge_local(root, args.branch, base, msg)
            if ok:
                print(f"merged {args.branch} into {base} locally ({detail}). Not pushed.")
                note(root, beam_path, args.id, f"merged {args.branch} into {base} locally ({detail})", what="a local merge")
            else:
                print(f"local merge failed: {detail}")
                print("Leave the ticket where it is (awaiting_approval or review) and tell the user. Nothing was changed.")
                sys.exit(1)
        elif args.cmd == "note":
            print(note(root, Path(args.beam), args.id, args.reason))
    except SystemExit:
        raise
    except Exception as e:  # fail-soft for resolve and note
        print(f"git: skipped ({e})")


if __name__ == "__main__":
    main()
