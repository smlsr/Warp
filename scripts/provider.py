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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import herald_fmt as fmt  # noqa: E402

SUPPORTED = ("github", "bitbucket")
FALSE = {"false", "no", "off", "0"}


def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 20) -> subprocess.CompletedProcess:
    if cmd and cmd[0] == "git":
        cmd = ["git", "-c", "maintenance.auto=false", "-c", "commit.gpgsign=false", *cmd[1:]]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def truthy(v: str) -> bool:
    return str(v).strip().lower() not in FALSE


def github_slug(remote: str) -> Optional[str]:
    if not remote:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote.strip())
    if not match:
        return None
    return "%s/%s" % (match.group(1), match.group(2))


def remote_host(remote: str) -> Optional[str]:
    if not remote:
        return None
    m = re.match(r"^https?://(?:[^@/]+@)?([^/:]+)", remote) or re.match(r"^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/]", remote)
    host = m.group(1).lower() if m else None
    return host if host and "." in host else None


def detect_provider(remote: str) -> Optional[str]:
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


def resolve(root: Path, cfg: Optional[dict] = None, gh: Optional[dict] = None) -> dict:
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


def note(root: Path, beam_path: Optional[Path], tid: Optional[str], reason: str, what: str = "local-only") -> str:
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


def drop_ticket_beam(root: Path) -> None:
    """Unstage `.warp/beam.json` from a squash that is about to be committed.

    If the base already has a beam, that copy is restored into the index.
    Ticket directories under `.warp/tickets/` are left staged.
    """
    name = ".warp/beam.json"
    listed = _run(["git", "diff", "--cached", "--name-only", "--", name], root)
    if listed.returncode != 0 or name not in (listed.stdout or "").splitlines():
        return
    on_base = _run(["git", "cat-file", "-e", "HEAD:%s" % name], root).returncode == 0
    if on_base:
        _run(["git", "checkout", "HEAD", "--", name], root)
        return
    _run(["git", "rm", "-r", "--cached", "--ignore-unmatch", "--", name], root)
    path = root / name
    if path.is_file():
        path.unlink()


def _live_payload(root: Path) -> dict:
    """Sanitized live beam, journal, and board. Does not rewrite the working files."""
    warp = root / ".warp"
    if not (warp / "beam.json").is_file():
        return {}
    import state_commit

    return state_commit.state_payload(warp)


def _clean_state_edits(root: Path) -> Optional[str]:
    """Refuse product dirt. A dirty live beam is set aside; the caller snapshotted it."""
    import state_commit

    listed = _run(["git", "status", "--porcelain", "--untracked-files=no"], root)
    if listed.returncode != 0:
        return "could not read git status"
    allowed = {".warp/%s" % name for name in state_commit.STATE_NAMES}
    dirty = []
    blocked = []
    for line in (listed.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path in allowed:
            dirty.append(path)
        else:
            blocked.append(path)
    if blocked:
        return "working tree has uncommitted changes; commit or stash them, then retry"
    if dirty:
        restored = _run(["git", "checkout", "--", *dirty], root)
        if restored.returncode != 0:
            return "could not set aside the live beam before merge"
    return None


def _stage_live(root: Path, payload: dict) -> None:
    """Put the parent's live state into the index. Never stages config.yaml."""
    for rel, text in payload.items():
        if rel == ".warp/config.yaml" or rel.endswith("/config.yaml"):
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _run(["git", "add", "--", rel], root)


def merge_local(root: Path, branch: str, base: str, message: str) -> tuple[bool, str]:
    """Squash-merge `branch` into `base`. Leaves the repo as it found it on failure.

    The ticket branch's beam is dropped. The parent's live beam, journal, and
    board are what land on the base. Secrets are stripped. config.yaml is not
    committed. When origin exists, the base branch is pushed.
    """
    root = root.resolve()
    if not fmt.git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return False, f"branch {branch} does not exist"
    if not fmt.git(root, "rev-parse", "--verify", "-q", f"refs/heads/{base}"):
        return False, f"base branch {base} does not exist"
    if branch == base:
        return False, "branch and base are the same"
    live = _live_payload(root)
    blocked = _clean_state_edits(root)
    if blocked:
        return False, blocked
    original = fmt.git(root, "symbolic-ref", "--short", "-q", "HEAD") or fmt.git(root, "rev-parse", "HEAD")
    if _run(["git", "checkout", "-q", base], root).returncode != 0:
        return False, f"could not check out {base}"
    try:
        r = _run(["git", "merge", "--squash", branch], root)
        if r.returncode != 0:
            _run(["git", "reset", "--merge"], root)
            return False, "merge conflict; nothing was merged. Resolve on the ticket branch and retry"
        # The ticket branch carries a launch snapshot of the beam so the Agent
        # can read its claim. That file is stale. Do not take it onto the base.
        # The parent's live beam is staged instead. .warp/tickets/<id>/ stays.
        drop_ticket_beam(root)
        _stage_live(root, live)
        if not fmt.git(root, "diff", "--cached", "--name-only"):
            return True, f"{branch} has no changes beyond {base}; nothing to merge"
        c = _run(["git", "commit", "-q", "-m", message], root)
        if c.returncode != 0:
            _run(["git", "reset", "--merge"], root)
            return False, "commit failed: " + (c.stderr.strip() or c.stdout.strip() or "unknown error")
        import state_commit

        state_commit.push_ref(root, base)
        return True, fmt.git(root, "rev-parse", "--short", "HEAD")
    finally:
        _run(["git", "checkout", "-q", original], root)
        # The live beam was committed on the base. Switching back to a ticket
        # branch that does not track that file deletes it. Put the snapshot
        # back so the parent still has its beam.
        for rel, text in (live or {}).items():
            path = root / rel
            if path.is_file() or not text:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


PROTECTION_MARKERS = (
    "branch protection",
    "required status check",
    "required reviews",
    "reviews are required",
    "changes must be made through a pull request",
    "failing checks",
    "not mergeable",
    "pull request is not mergeable",
    "required status checks",
)


def protection_rejects(text: str) -> bool:
    """True when a merge reply says branch protection would reject it."""
    low = (text or "").lower()
    return any(marker in low for marker in PROTECTION_MARKERS)


def payload_has_merge_queue(doc) -> bool:
    """True when a GitHub ruleset payload contains a merge_queue rule."""
    found = []

    def walk(node) -> None:
        if isinstance(node, dict):
            kind = str(node.get("type") or "").replace("-", "_").lower()
            if kind in {"merge_queue", "mergequeue"}:
                found.append(True)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return bool(found)


def interpret_rollup(checks: list) -> str:
    """GitHub statusCheckRollup or Bitbucket commit statuses.

    green only when every check finished successfully. An empty list is pending.
    """
    if not checks:
        return "pending"
    bad = False
    pending = False
    success = {"success", "successful", "skipped", "neutral", "ok"}
    failure = {
        "failure",
        "failed",
        "cancelled",
        "canceled",
        "timed_out",
        "action_required",
        "error",
        "startup_failure",
    }
    waiting = {"queued", "in_progress", "pending", "waiting", "requested", "inprogress"}
    for item in checks:
        if not isinstance(item, dict):
            pending = True
            continue
        conclusion = str(item.get("conclusion") or item.get("state") or "").strip().casefold()
        status = str(item.get("status") or "").strip().casefold()
        if conclusion in success:
            continue
        if conclusion in failure:
            bad = True
            continue
        if status in waiting or conclusion in waiting or not conclusion:
            pending = True
            continue
        pending = True
    if bad:
        return "red"
    if pending:
        return "pending"
    return "green"


def detect_merge_queue(root: Path, cfg: Optional[dict] = None) -> Optional[bool]:
    """True or false when GitHub rulesets can be read. None when they cannot."""
    resolved = resolve(root, cfg)
    if resolved.get("provider") != "github":
        return None
    slug = github_slug(resolved.get("remote") or "")
    if not slug or not shutil.which("gh"):
        return None
    listing = _run(["gh", "api", "repos/%s/rulesets" % slug], root)
    if listing.returncode != 0:
        return None
    try:
        rows = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        one = _run(["gh", "api", "repos/%s/rulesets/%s" % (slug, row["id"])], root)
        if one.returncode != 0:
            continue
        try:
            doc = json.loads(one.stdout or "{}")
        except json.JSONDecodeError:
            continue
        if payload_has_merge_queue(doc):
            return True
    return False


def fetch_check_rows(root: Path, pr: str, provider_name: Optional[str] = None) -> Optional[list]:
    """statusCheckRollup rows, or None when the provider cannot be asked.

    Each row keeps name, conclusion, and log. A dead Agent's pull request still
    reports make ci and Bugbot from here when the ticket directory has no check line.
    """
    if provider_name == "bitbucket" or not pr or not shutil.which("gh"):
        return None
    text = str(pr)
    if "github.com" not in text and not text.isdigit():
        return None
    viewed = _run(["gh", "pr", "view", text, "--json", "statusCheckRollup"], root)
    if viewed.returncode != 0:
        return None
    try:
        doc = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    checks = doc.get("statusCheckRollup") or []
    if not isinstance(checks, list):
        return None
    rows = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("context") or "").strip()
        rows.append(
            {
                "name": name,
                "conclusion": item.get("conclusion") or item.get("state") or "",
                "status": item.get("status") or "",
                "log": item.get("details") or item.get("output") or "",
            }
        )
    return rows


def fetch_rollup(root: Path, pr: str, provider: Optional[str]) -> str:
    """Ask the configured provider. Unknown or unreachable stays pending."""
    if provider == "bitbucket":
        return "pending"
    if not shutil.which("gh"):
        return "pending"
    viewed = _run(["gh", "pr", "view", str(pr), "--json", "statusCheckRollup"], root)
    if viewed.returncode != 0:
        return "pending"
    try:
        doc = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return "pending"
    checks = doc.get("statusCheckRollup") or []
    return interpret_rollup(checks if isinstance(checks, list) else [])


def merge_connected(root: Path, number: str, cfg: Optional[dict] = None) -> tuple[str, str]:
    """Enqueue, or merge, or reject. Rejected is never a merged pull request."""
    cfg = cfg or fmt.read_config(root)
    resolved = resolve(root, cfg)
    provider = resolved.get("provider")
    detected = detect_merge_queue(root, cfg) if provider == "github" else None
    if merge_queue_on(cfg, detected):
        if provider == "github" and shutil.which("gh"):
            queued = _run(["gh", "pr", "merge", str(number), "--auto", "--squash"], root)
            detail = (queued.stdout or queued.stderr or "enqueued").strip()
        else:
            detail = "merge queue is on; the pull request was not merged from this agent"
        return "enqueue", detail
    if provider != "github" or not shutil.which("gh"):
        return "rejected", "no merge command for this provider; not marked merged"
    merged = _run(["gh", "pr", "merge", str(number), "--squash"], root)
    text = ((merged.stderr or "") + "\n" + (merged.stdout or "")).strip()
    if merged.returncode != 0 or protection_rejects(text):
        return "rejected", text or "merge rejected"
    viewed = _run(["gh", "pr", "view", str(number), "--json", "state,mergeCommit"], root)
    if viewed.returncode != 0:
        return "rejected", "could not confirm the pull request; not marked merged"
    try:
        doc = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return "rejected", "could not read the pull request; not marked merged"
    if str(doc.get("state") or "").upper() != "MERGED":
        return "rejected", "pull request is not merged"
    commit = doc.get("mergeCommit") or {}
    sha = commit.get("oid") if isinstance(commit, dict) else ""
    return "merged", sha or "merged"


def merge_queue_on(cfg: dict, detected: Optional[bool]) -> bool:
    raw = (cfg or {}).get("mergeQueue")
    if isinstance(raw, bool):
        configured = raw
    elif raw is None or str(raw).strip() == "":
        configured = False
    else:
        configured = truthy(str(raw))
    if configured:
        return True
    return detected is True


def _mark_merged(beam_path: Path, tid: str, detail: str) -> None:
    """Record the local merge on a full beam ticket so the Jira move is printed."""
    if not beam_path.is_file():
        return
    try:
        t = json.loads(beam_path.read_text())["tickets"][tid]
    except Exception:
        return
    if not isinstance(t, dict) or "status" not in t or not isinstance(t.get("pr"), dict):
        return
    sha = detail if re.fullmatch(r"[0-9a-f]{7,40}", detail or "") else None
    try:
        import beam

        beam.cmd_set(
            beam_path,
            argparse.Namespace(
                id=tid,
                status="merged",
                sha=sha,
                via="local",
                agent=None,
                branch=None,
                jira=None,
                pr=None,
                bugbot=None,
                ci=None,
                alarm=None,
                attempts=None,
            ),
        )
    except SystemExit as e:
        print(f"jira: could not mark {tid} merged ({e})")
    except Exception as e:
        print(f"jira: could not mark {tid} merged ({e})")


def set_key(text: str, key: str, raw: str) -> str:
    pat = re.compile(rf"^{key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _: f"{key}: {raw}", text, count=1)
    return text.rstrip("\n") + f"\n{key}: {raw}\n"


def init_steps(root: Path, dry: bool = False, gh: Optional[dict] = None) -> list[tuple[str, str]]:
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


PROVIDER_HELP = """
examples:
  python3 scripts/provider.py ?
  python3 scripts/provider.py resolve --root .
  python3 scripts/provider.py resolve --json
  python3 scripts/provider.py merge-local --id T-9 --branch warp/T-9
  python3 scripts/provider.py note --id T-9 --reason "no GitHub connector"

resolve reads gitProvider (auto, github, bitbucket). auto uses the origin
host. No origin, another host, or pushMerge false is local-only.
merge-local squash-merges --branch into --base (default baseBranch) and
requires --id and --branch. note requires --reason and writes the outbox line.
merge-pr enqueues when mergeQueue is true or GitHub reports a merge queue.
A direct merge that branch protection rejects is not marked merged.
rollup reads statusCheckRollup (GitHub) or a --checks file (GitHub or Bitbucket).

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""


def main() -> None:
    p = argparse.ArgumentParser(
        description="Git provider and local mode",
        epilog=PROVIDER_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    pq = sub.add_parser("merge-pr")
    pq.add_argument("--root", default=".")
    pq.add_argument("--beam", default=".warp/beam.json")
    pq.add_argument("--id", required=True)
    pq.add_argument("--pr", required=True, help="pull request number or URL")
    pru = sub.add_parser("rollup")
    pru.add_argument("--root", default=".")
    pru.add_argument("--beam", default=".warp/beam.json")
    pru.add_argument("--id")
    pru.add_argument("--pr", default="")
    pru.add_argument("--checks", help="JSON list of provider check objects")
    import usage

    args = p.parse_args(usage.normalize_argv(None))
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
            try:
                import jira_sync

                loaded = json.loads(beam_path.read_text())
                ticket = loaded["tickets"][args.id]
                if isinstance(ticket, dict) and "status" in ticket and isinstance(ticket.get("pr"), dict):
                    block = jira_sync.review_block(ticket, jira_sync.settings(beam_path, loaded))
                else:
                    block = None
            except Exception:
                block = None
            if block:
                print(f"{args.id}: refusing merge: {block}")
                print("Leave the ticket where it is (awaiting_approval or review) and tell the user. Nothing was changed.")
                sys.exit(1)
            ok, detail = merge_local(root, args.branch, base, msg)
            if ok:
                print(f"merged {args.branch} into {base} locally ({detail}). Not pushed.")
                note(root, beam_path, args.id, f"merged {args.branch} into {base} locally ({detail})", what="a local merge")
                _mark_merged(beam_path, args.id, detail)
                import state_commit

                state_commit.publish_base(root, str(beam_path))
            else:
                print(f"local merge failed: {detail}")
                print("Leave the ticket where it is (awaiting_approval or review) and tell the user. Nothing was changed.")
                sys.exit(1)
        elif args.cmd == "note":
            print(note(root, Path(args.beam), args.id, args.reason))
        elif args.cmd == "rollup":
            checks = None
            if args.checks:
                checks = json.loads(Path(args.checks).read_text())
                result = interpret_rollup(checks if isinstance(checks, list) else [])
            else:
                loaded = {}
                beam_path = Path(args.beam)
                if beam_path.is_file():
                    loaded = json.loads(beam_path.read_text())
                provider_name = (loaded.get("config") or {}).get("gitProvider") or resolve(root).get("provider")
                result = fetch_rollup(root, args.pr, provider_name)
            print(result)
            if args.id and Path(args.beam).is_file():
                import beam as beam_mod
                import orchestrator

                beam_path = Path(args.beam)
                data = json.loads(beam_path.read_text())
                ticket = data["tickets"][args.id]
                pr = ticket.setdefault("pr", {})
                pr["rollup"] = result
                if args.checks and isinstance(checks, list):
                    pr["checks"] = checks
                yaml_text = ""
                cfg_path = beam_mod.config_yaml_path(beam_path)
                if cfg_path.is_file():
                    try:
                        yaml_text = cfg_path.read_text()
                    except OSError:
                        yaml_text = ""
                sent = orchestrator.send_back_red_checks(data, yaml_text, only_id=args.id)
                beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
                print("%s rollup %s" % (args.id, result))
                for line in orchestrator.format_send_back(sent, data):
                    print(line)
        elif args.cmd == "merge-pr":
            beam_path = Path(args.beam)
            data = json.loads(beam_path.read_text()) if beam_path.is_file() else {}
            cfg = dict(data.get("config") or {})
            cfg_file = root / ".warp" / "config.yaml"
            if cfg_file.is_file():
                import prompt_gate

                cfg.update(prompt_gate.effective_config(data, root))
            number = str(args.pr).rstrip("/").split("/")[-1]
            outcome, detail = merge_connected(root, number, cfg)
            ticket = (data.get("tickets") or {}).get(args.id)
            if outcome == "merged" and ticket:
                _mark_merged(beam_path, args.id, detail if re.fullmatch(r"[0-9a-f]{7,40}", detail or "") else "")
                print("merged %s" % args.id)
            elif ticket:
                import orchestrator

                orchestrator.apply_reported_merge(ticket, outcome, sha=None)
                import beam as beam_mod

                beam_mod.atomic_write(beam_path, json.dumps(data, indent=2) + "\n")
                print("%s %s" % (args.id, outcome))
                print("not merged")
                print(detail)
                if outcome == "rejected":
                    sys.exit(1)
            else:
                print(outcome)
                print(detail)
                if outcome == "rejected":
                    sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:  # fail-soft for resolve and note
        print(f"git: skipped ({e})")


if __name__ == "__main__":
    main()
