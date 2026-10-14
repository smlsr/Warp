#!/usr/bin/env python3
"""Git provider and local-only mode for Warp.

Warp opens, reviews, and merges pull requests through whatever the repo uses:
GitHub or Bitbucket, via a connected MCP server or (GitHub) the gh CLI. This
script decides which, and when none is usable it says so and Warp works
local-only instead of failing. Warp never installs gh, creates keys, or
connects an app. It only reports what is missing.

  resolve      print the mode, provider, and the order of tools to try
  merge-local  merge a ticket branch into the base branch, locally (mergeMethod: a merge commit by default, or a squash)
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
import os
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


def _note_branch(beam_path: Optional[Path], tid: Optional[str]) -> str:
    if not tid or not beam_path or not Path(beam_path).is_file():
        return ""
    try:
        data = json.loads(Path(beam_path).read_text())
    except (OSError, ValueError):
        return ""
    ticket = (data.get("tickets") or {}).get(tid) if isinstance(data, dict) else None
    return str((ticket or {}).get("branch") or "") if isinstance(ticket, dict) else ""


def branch_on_origin(root: Path, branch: str, runner=None) -> Optional[bool]:
    """True when origin has the branch, False when it does not, None when origin cannot be asked."""
    if not branch:
        return None
    run = runner or (lambda cmd: _run(cmd, root, timeout=20))
    try:
        found = run(["git", "ls-remote", "--heads", "origin", branch])
    except Exception:
        return None
    if found.returncode != 0:
        return None
    return bool((found.stdout or "").strip())


def note(root: Path, beam_path: Optional[Path], tid: Optional[str], reason: str, what: str = "local-only",
         branch: Optional[str] = None, pushed: Optional[bool] = None, runner=None) -> str:
    """Outbox line, journal entry, and a Herald message that a ticket ran local-only.

    A branch that is on origin was pushed. Then the note says so: only the pull request is missing, and the
    parent opens it (`pr: open`). Before 1.6.13 it said "Nothing was pushed" for a pushed branch.
    """
    root = root.resolve()
    branch = branch or _note_branch(beam_path, tid)
    if pushed is None:
        pushed = bool(branch_on_origin(root, branch, runner=runner)) if branch else False
    if pushed:
        kind = "pr-not-opened"
        title = "Pull request not opened"
        text = (f"{tid or 'Warp'} pushed {branch} but could not open its pull request: {reason}. "
                "The branch is on origin. The parent opens the pull request on its next pass.")
        footer = ("Nothing to change in config. If `pr: MUST DO` follows, open it with the GitHub MCP "
                  "create_pull_request tool and record it with orchestrator.py opened.")
    else:
        kind = "local-only"
        title = "Local-only mode"
        text = f"{tid or 'Warp'} ran {what}: {reason}. Nothing was pushed and no pull request was opened."
        footer = "Set pushMerge: true and connect a git provider to push and open pull requests."
    outbox = root / ".warp" / "outbox.md"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with outbox.open("a") as f:
        f.write(f"\n## {_now()}\n\n{text}\n")
    if beam_path and Path(beam_path).is_file():
        row = {"ts": _now(), "type": kind, "id": tid, "reason": reason}
        if branch:
            row["branch"] = branch
            row["pushed"] = bool(pushed)
        with (Path(beam_path).parent / "journal.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
    try:
        import notify

        facts = [("Ticket", tid)] if tid else []
        if pushed and branch:
            facts.append(("Branch", branch))
        msg = fmt.message(root, title, intro=text, facts=facts, footer=footer)
        notify.report(notify.build(kind, root, msg=msg))
    except Exception as e:
        print(f"herald: not posted ({e})")
    return text


def drop_ticket_beam(root: Path) -> None:
    """Unstage `.warp/beam.json` from a local merge that is about to be committed.

    If the base already has a beam, that copy is restored into the index.
    Ticket directories under `.warp/tickets/` are left staged.
    """
    import runs

    name = runs.git_rel(runs.current(root), ".warp/beam.json")
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
    import runs
    import state_commit

    return state_commit.state_payload(warp, runs.current(root), extras=False)


def _clean_state_edits(root: Path) -> Optional[str]:
    """Refuse product dirt. A dirty live beam is set aside; the caller snapshotted it."""
    import state_commit

    listed = _run(["git", "status", "--porcelain", "--untracked-files=no"], root)
    if listed.returncode != 0:
        return "could not read git status"
    import runs

    run = runs.current(root)
    allowed = {runs.git_rel(run, ".warp/%s" % name) for name in state_commit.STATE_NAMES}
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
    import state_commit

    for rel, text in payload.items():
        if state_commit._is_local_config(rel):
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _run(["git", "add", "--", rel], root)


MERGE_METHODS = ("merge", "squash", "rebase")
DEFAULT_MERGE_METHOD = "merge"


def merge_method(cfg: Optional[dict]) -> str:
    """`mergeMethod` from the config: merge (default, a merge commit that joins the branch), squash, or rebase.

    Anything else is merge. Before 1.6.20 every pull request was squash-merged.
    """
    value = str((cfg or {}).get("mergeMethod") or "").strip().lower()
    return value if value in MERGE_METHODS else DEFAULT_MERGE_METHOD


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def delete_on_merge(cfg: Optional[dict]) -> bool:
    """`deleteBranchOnMerge` from the config. Off by default: a merged ticket keeps its branch."""
    return _truthy((cfg or {}).get("deleteBranchOnMerge", False))


def delete_merged_branch(root: Path, branch: str, base: str = "", runner=None) -> tuple[bool, str]:
    """Delete a merged ticket's branch on origin, and its local branch when no worktree holds it.

    Never the base branch. A branch that is already gone on origin counts as deleted.
    """
    run = runner or (lambda cmd: _run(cmd, root))
    branch = str(branch or "").strip()
    if not branch or branch in {base, "main", "master", "HEAD"}:
        return False, "no ticket branch to delete"
    notes = []
    remote_ok = True
    if fmt.git(Path(root), "remote"):
        gone = run(["git", "push", "origin", "--delete", branch])
        text = ((gone.stderr or "") + (gone.stdout or "")).strip()
        if gone.returncode != 0 and "remote ref does not exist" not in text:
            remote_ok = False
            notes.append("origin: " + (text.splitlines()[-1] if text else "push --delete failed"))
    local = run(["git", "branch", "-D", branch])
    if local.returncode != 0:
        text = ((local.stderr or "") + (local.stdout or "")).strip()
        if "not found" not in text:
            # Usually a worktree still has it checked out; the worktree cleanup removes both.
            notes.append("local: " + (text.splitlines()[-1] if text else "branch -D failed"))
    return remote_ok, "; ".join(notes)


def merge_local(root: Path, branch: str, base: str, message: str, method: str = DEFAULT_MERGE_METHOD) -> tuple[bool, str]:
    """Merge `branch` into `base` here: a merge commit (default) or a squash. Leaves the repo as it found it on failure.

    `rebase` gets a merge commit locally: rebasing the ticket branch would rewrite a branch a Shuttle may still use.

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
        if method == "squash":
            r = _run(["git", "merge", "--squash", branch], root)
        else:
            r = _run(["git", "merge", "--no-ff", "--no-commit", branch], root)
        if r.returncode != 0:
            _run(["git", "reset", "--merge"], root)
            return False, "merge conflict; nothing was merged. Resolve on the ticket branch and retry"
        # The ticket branch carries a launch snapshot of the beam so the Agent
        # can read its claim. That file is stale. Do not take it onto the base.
        # The parent's live beam is staged instead. .warp/tickets/<id>/ stays.
        drop_ticket_beam(root)
        _stage_live(root, live)
        if not fmt.git(root, "diff", "--cached", "--name-only"):
            if method != "squash":
                _run(["git", "merge", "--abort"], root)
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


def repo_has_ci(root: Path) -> bool:
    """True when the checkout has a CI definition: GitHub Actions workflows or bitbucket-pipelines.yml."""
    root = Path(root)
    flows = root / ".github" / "workflows"
    if flows.is_dir() and any(p.suffix in {".yml", ".yaml"} for p in flows.iterdir() if p.is_file()):
        return True
    return (root / "bitbucket-pipelines.yml").is_file()


def interpret_rollup(checks: list) -> str:
    """GitHub statusCheckRollup or Bitbucket commit statuses.

    green only when every check finished successfully. An empty list is
    absent: the workflow never started, which is not an ordinary pending run.
    """
    if not checks:
        return "absent"
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
        if conclusion == "neutral" and _bugbot_check(item):
            # Cursor Bugbot finishes `neutral` when it found issues (`success` when it found none).
            bad = True
            continue
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


def _bugbot_check(item: dict) -> bool:
    name = str(item.get("name") or item.get("context") or "").casefold()
    return "bugbot" in name


def _bugbot_from_checks(checks: list) -> str:
    rows = [item for item in checks if isinstance(item, dict) and _bugbot_check(item)]
    if not rows:
        return ""
    rollup = interpret_rollup(rows)
    if rollup == "green":
        return "pass"
    if rollup == "red":
        return "fail"
    if rollup == "absent":
        return ""
    return "pending"


def _ci_from_checks(checks: list) -> str:
    rows = [item for item in checks if isinstance(item, dict) and not _bugbot_check(item)]
    if not checks:
        return "absent"
    if not rows:
        return "absent"
    return interpret_rollup(rows)


def _head_committed_at(doc: dict) -> str:
    commits = doc.get("commits")
    if not isinstance(commits, list) or not commits:
        return ""
    last = commits[-1]
    if not isinstance(last, dict):
        return ""
    return str(last.get("committedDate") or last.get("authoredDate") or "")


def fetch_pr_state(root: Path, pr: str) -> Optional[dict]:
    """Live GitHub pull request state, or None when gh cannot be asked.

    The snapshot carries the head sha, check rollup, CI, Bugbot, mergeable,
    mergeStateStatus, and conflicting files when the view includes them.
    """
    if not pr or not shutil.which("gh"):
        return None
    text = str(pr)
    if "github.com" not in text and not text.isdigit():
        return None
    viewed = _run(
        [
            "gh",
            "pr",
            "view",
            text,
            "--json",
            "headRefOid,statusCheckRollup,mergeable,mergeStateStatus,commits,state,mergeCommit,createdAt,mergedAt,latestReviews",
        ],
        root,
    )
    if viewed.returncode != 0:
        return None
    try:
        doc = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    checks = doc.get("statusCheckRollup") or []
    if not isinstance(checks, list):
        checks = []
    mergeable = str(doc.get("mergeable") or "")
    state = str(doc.get("mergeStateStatus") or "")
    conflicted = mergeable.upper() == "CONFLICTING" or state.upper() in {"DIRTY", "CONFLICTING"}
    files = doc.get("conflictFiles") if isinstance(doc.get("conflictFiles"), list) else []
    files = [str(path) for path in files if str(path).strip()]
    sha = str(doc.get("headRefOid") or "")
    bugbot = _bugbot_from_checks(checks)
    snap = {
        "sha": sha,
        "headSha": sha,
        "headAt": _head_committed_at(doc),
        "rollup": interpret_rollup(checks),
        "ci": _ci_from_checks(checks),
        "checks": checks,
        "checkCount": len(checks),
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "behind": state.upper() == "BEHIND",
        "conflict": files or True if conflicted else False,
        "conflictFiles": files,
        # OPEN, MERGED or CLOSED. Only MERGED, read from the provider, marks a ticket merged.
        "prState": str(doc.get("state") or "").upper(),
    }
    commit = doc.get("mergeCommit") if isinstance(doc.get("mergeCommit"), dict) else {}
    if commit.get("oid"):
        snap["mergeSha"] = str(commit.get("oid"))
    if bugbot:
        snap["bugbot"] = bugbot
    snap.update(pr_times(doc, checks))
    if bugbot == "fail" and sha:
        snap["findings"] = bugbot_findings(root, text, sha) or [
            "Cursor Bugbot found issues on %s (see its review comments on the pull request)" % sha[:12]]
    elif bugbot == "pass":
        snap["findings"] = []
    return snap


def pr_times(doc: dict, checks: list) -> dict:
    """The pull request's own timestamps from gh pr view: opened, merged, Bugbot's review, the last approval."""
    out = {}
    if doc.get("createdAt"):
        out["openedAt"] = str(doc["createdAt"])
    if doc.get("mergedAt"):
        out["mergedAt"] = str(doc["mergedAt"])
    done = [str(item.get("completedAt") or "") for item in checks
            if isinstance(item, dict) and _bugbot_check(item) and str(item.get("conclusion") or "").upper() in {"SUCCESS", "NEUTRAL"}
            and item.get("completedAt")]
    if done:
        out["bugbotAt"] = max(done)
    # When Bugbot's check started on this head. A check that has not started yet carries no time, and GitHub
    # fills an unset time with year 1.
    started = [str(item.get("startedAt") or "") for item in checks
               if isinstance(item, dict) and _bugbot_check(item) and str(item.get("startedAt") or "")[:4] not in {"", "0001"}]
    if started:
        out["bugbotStartedAt"] = max(started)
    reviews = doc.get("latestReviews") if isinstance(doc.get("latestReviews"), list) else []
    approved = [r for r in reviews if isinstance(r, dict) and str(r.get("state") or "").upper() == "APPROVED" and r.get("submittedAt")]
    if approved:
        last = max(approved, key=lambda r: str(r.get("submittedAt")))
        out["approvedAt"] = str(last["submittedAt"])
        out["approvedBy"] = str(((last.get("author") or {}).get("login")) or "github review")
    return out


_SEVERITY = re.compile(r"\*\*(\w+) Severity\*\*", re.I)


def _repo_and_number(url: str) -> tuple:
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", str(url or ""))
    return (match.group(1), match.group(2), match.group(3)) if match else ("", "", "")


def finding_line(comment: dict) -> str:
    """One Bugbot review comment as one line: `<path>:<line> <title> (<severity>)`."""
    body = str(comment.get("body") or "")
    title = ""
    for raw in body.splitlines():
        text = raw.strip()
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            break
    if not title:
        title = next((raw.strip() for raw in body.splitlines() if raw.strip() and not raw.strip().startswith("<!--")), "Bugbot finding")
    severity = _SEVERITY.search(body)
    where = str(comment.get("path") or "")
    line = comment.get("line") or comment.get("original_line")
    if where and line:
        where = "%s:%s" % (where, line)
    text = ("%s %s" % (where, title)).strip()
    if severity:
        text += " (%s)" % severity.group(1).capitalize()
    return text[:300]


def is_bugbot_comment(comment: dict) -> bool:
    user = str(((comment.get("user") or {}).get("login")) or "").casefold()
    body = str(comment.get("body") or "")
    return user.startswith("cursor") or "BUGBOT" in body or "bugbot" in user


def bugbot_findings(root: Path, url: str, head: str, runner=None) -> list:
    """Bugbot's review comments on this head commit, one line each. Empty when gh cannot read them.

    Bugbot reports what it found as review comments; its check only says `neutral`.
    """
    owner, repo, number = _repo_and_number(url)
    if not number:
        return []
    run = runner or (lambda cmd: _run(cmd, root, timeout=30))
    done = run(["gh", "api", "--paginate", "repos/%s/%s/pulls/%s/comments" % (owner, repo, number)])
    if done.returncode != 0:
        return []
    text = (done.stdout or "").strip()
    try:
        rows = json.loads(text) if text.startswith("[") and text.count("][") == 0 else json.loads("[" + text.replace("][", ",")[1:-1] + "]")
    except json.JSONDecodeError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not is_bugbot_comment(row):
            continue
        commit = str(row.get("commit_id") or row.get("original_commit_id") or "")
        if head and commit and not (commit.startswith(head) or head.startswith(commit)):
            continue
        line = finding_line(row)
        if line and line not in out:
            out.append(line)
    return out


def lookup_pr(root: Path, branch: str) -> Optional[dict]:
    """Pull request for a branch via `gh pr list --head`. None when gh cannot be asked.

    A merged request wins over an open one. Bitbucket is not asked here; the
    caller passes a --prs file for that host.
    """
    if not branch or not shutil.which("gh"):
        return None
    viewed = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number,url,state,headRefOid,headRefName",
        ],
        root,
    )
    if viewed.returncode != 0:
        return None
    try:
        rows = json.loads(viewed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return None

    def rank(row: dict) -> int:
        state = str(row.get("state") or "").upper()
        if state == "MERGED":
            return 0
        if state == "OPEN":
            return 1
        return 2

    rows.sort(key=rank)
    row = rows[0]
    state = str(row.get("state") or "").upper()
    return {
        "number": row.get("number"),
        "url": row.get("url") or "",
        "state": state,
        "sha": str(row.get("headRefOid") or ""),
        "branch": str(row.get("headRefName") or branch),
        "merged": state == "MERGED",
        "open": state == "OPEN",
    }


def fetch_rollup(root: Path, pr: str, provider: Optional[str]) -> str:
    """Ask the configured provider. Unknown or unreachable stays pending."""
    if provider == "bitbucket":
        return "pending"
    state = fetch_pr_state(root, pr)
    if not state:
        return "pending"
    return str(state.get("rollup") or "pending")


_STATE_IGNORE = ("worktrees", "previous-*", "*.lock", ".*.lock", "outbox.md", "notify-*.json", "dashboard.html")


def _merged_state_payload(root: Path, beam_path: Path, data: dict, tid: str, method: str, now_s: str) -> dict:
    """The run state as it will be once this ticket is merged: a copy of the live `.warp/` with the ticket
    marked merged and the board and status rewritten from it. The live files are not touched."""
    import tempfile

    import beam as beam_mod
    import runs as runs_mod
    import state_commit

    warp = Path(beam_path).parent
    tmp = Path(tempfile.mkdtemp(prefix="warp-merge-state-"))
    try:
        copy = tmp / ".warp"
        shutil.copytree(warp, copy, ignore=shutil.ignore_patterns(*_STATE_IGNORE))
        doc = json.loads((copy / "beam.json").read_text())
        ticket = (doc.get("tickets") or {}).get(tid)
        if isinstance(ticket, dict):
            ticket["status"] = "merged"
            ticket["phase"] = "merged"
            pr = ticket.setdefault("pr", {})
            pr["mergedAt"] = pr.get("mergedAt") or now_s
            pr["mergeMethod"] = method
            pr["stateInMerge"] = True
        beam_mod.atomic_write(copy / "beam.json", json.dumps(doc, indent=2) + "\n")
        try:
            beam_mod.write_board_files(copy / "beam.json", doc)
            import io
            from contextlib import redirect_stdout

            import scan

            with redirect_stdout(io.StringIO()):
                scan.write_status(copy / "beam.json")
        except Exception:
            pass  # the beam is what matters; a board that did not render keeps its last copy
        return state_commit.state_payload(copy, runs_mod.current(root, data), now=now_s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge_with_state(root: Path, beam_path: Path, data: dict, ticket: dict, number: str, cfg: Optional[dict] = None,
                     confirm=None, wait=None) -> tuple[str, str]:
    """Merge the pull request with one merge commit that also carries the run state, and push it to the base.

    The commit's parents are the base branch and the pull request's reviewed head, so GitHub marks the pull
    request merged as soon as main has it. Its tree is the merge plus `.warp/runs/<run>/` (`.warp/` for an
    unnamed run) as it is with this ticket merged: no separate `Warp state` commit follows. Returns
    ("merged", sha) once the provider shows the pull request MERGED, ("pushed", sha) when main has it and the
    provider has not caught up yet, or ("fallback", why) when Warp should merge through `gh pr merge` as before:
    mergeMethod is not merge, stateInMerge or publishing is off, a merge queue, the branch moved past the reviewed
    head, a conflict outside the run state, or a push the base branch refused (branch protection).
    """
    import time

    import beam as beam_mod
    import runs as runs_mod
    import state_commit

    cfg = cfg or {}
    if os.environ.get("WARP_PUBLISH", "").strip().lower() in {"0", "false", "no", "off"}:
        return "fallback", "publishing is off here (WARP_PUBLISH=0)"
    if merge_method(cfg) != "merge":
        return "fallback", "mergeMethod is %s" % merge_method(cfg)
    if not _truthy(cfg.get("stateInMerge", True)) or not _truthy(cfg.get("publishOnChange", True)):
        return "fallback", "stateInMerge or publishOnChange is off"
    if resolve(root, cfg).get("provider") != "github":
        return "fallback", "not a GitHub repository"
    if merge_queue_on(cfg, detect_merge_queue(root, cfg)):
        return "fallback", "a merge queue merges it"
    tid = str(ticket.get("id") or "")
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    branch = str(ticket.get("branch") or pr.get("branch") or "")
    reviewed = str(pr.get("headSha") or "")
    base = runs_mod.base_name(root, cfg)
    if not branch or not reviewed:
        return "fallback", "no branch or reviewed head recorded"
    fetched = _run(["git", "fetch", "-q", "origin", base, branch], root, timeout=60)
    if fetched.returncode != 0:
        return "fallback", "fetch failed: " + ((fetched.stderr or fetched.stdout).strip().splitlines() or ["?"])[-1]
    head = fmt.git(root, "rev-parse", "origin/%s" % branch)
    if head != reviewed:
        return "fallback", "the branch is at %s, not the reviewed head %s" % (head[:12], reviewed[:12])
    onto = fmt.git(root, "rev-parse", "origin/%s" % base)
    merged = _run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", onto, head], root, timeout=60)
    lines = [line.strip() for line in (merged.stdout or "").splitlines() if line.strip()]
    if merged.returncode not in (0, 1) or not lines:
        return "fallback", "git merge-tree failed: " + ((merged.stderr or "").strip().splitlines() or ["?"])[-1]
    tree, conflicts = lines[0], lines[1:]
    now_s = beam_mod.utcnow()
    payload = _merged_state_payload(root, Path(beam_path), data, tid, "merge", now_s)
    outside = [path for path in conflicts if path not in payload]
    if outside:
        return "fallback", "conflicts outside the run state: " + ", ".join(outside[:5])
    owner, _repo, _num = _repo_and_number(pr.get("url") or "")
    title = "[%s] %s" % (tid, ticket.get("summary") or "")
    run = runs_mod.current(root, data)
    message = "Merge pull request #%s from %s%s\n\n%s\n\nWarp state: %s, with %s merged." % (
        number, (owner + "/") if owner else "", branch, title.strip(), ("%s run" % run) if run else "the run", tid)
    code, sha = state_commit._build_commit(root, onto, payload, message, tree=tree, parents=(onto, head))
    if code != 0 or not sha:
        return "fallback", "could not write the merge commit"
    pushed = _run(["git", "push", "origin", "%s:refs/heads/%s" % (sha, base)], root, timeout=120)
    if pushed.returncode != 0:
        text = (pushed.stderr or pushed.stdout or "").strip()
        return "fallback", "the push to %s was refused: %s" % (base, (text.splitlines() or ["?"])[-1])
    look = confirm or (lambda: _pr_state(root, number))
    nap = wait or time.sleep
    for attempt in range(6):
        if look() == "MERGED":
            return "merged", sha
        nap(2)
    return "pushed", sha


def _pr_state(root: Path, number: str) -> str:
    viewed = _run(["gh", "pr", "view", str(number), "--json", "state"], root)
    try:
        return str(json.loads(viewed.stdout or "{}").get("state") or "").upper()
    except json.JSONDecodeError:
        return ""


def merge_connected(root: Path, number: str, cfg: Optional[dict] = None) -> tuple[str, str]:
    """Enqueue, or merge, or reject. Rejected is never a merged pull request."""
    cfg = cfg or fmt.read_config(root)
    resolved = resolve(root, cfg)
    provider = resolved.get("provider")
    detected = detect_merge_queue(root, cfg) if provider == "github" else None
    flag = "--" + merge_method(cfg)
    if merge_queue_on(cfg, detected):
        if provider == "github" and shutil.which("gh"):
            queued = _run(["gh", "pr", "merge", str(number), "--auto", flag], root)
            detail = (queued.stdout or queued.stderr or "enqueued").strip()
        else:
            detail = "merge queue is on; the pull request was not merged from this agent"
        return "enqueue", detail
    if provider != "github" or not shutil.which("gh"):
        return "rejected", "no merge command for this provider; not marked merged"
    merged = _run(["gh", "pr", "merge", str(number), flag], root)
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


def _package_dirs(root: Path, paths: list) -> list:
    """Folders with a package.json test script that hold, or sit above, one of these repo paths."""
    found = []
    for package in sorted(Path(root).rglob("package.json")):
        if "node_modules" in package.parts or ".warp" in package.parts:
            continue
        try:
            doc = json.loads(package.read_text())
        except (OSError, ValueError):
            continue
        script = str(((doc.get("scripts") or {}) if isinstance(doc, dict) else {}).get("test") or "")
        if not script or "no test specified" in script:
            continue
        rel = package.parent.relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        for path in paths:
            path = str(path).strip("/")
            if not rel or path.startswith(rel + "/") or path == rel or rel.startswith(path + "/") or rel == path:
                if rel not in found:
                    found.append(rel)
                break
    return found


def merge_check_command(root: Path, ticket: dict, cfg: Optional[dict]) -> str:
    """The command run on a pull request's head before Warp merges it. Empty when there is none.

    `checkCommand` when it is set. Otherwise (`mergeCheck: auto`, the default) the test script of each package
    that holds the ticket's locks (`npm test`, after `npm ci` when there is a lockfile), or `make ci` from a root
    Makefile with a `ci:` target. `mergeCheck: off` turns it off.
    """
    cfg = cfg or {}
    if str(cfg.get("mergeCheck") or "auto").strip().casefold() in {"off", "false", "no", "0"}:
        return ""
    import orchestrator

    explicit = orchestrator.check_command(cfg)
    if explicit:
        return explicit
    root = Path(root)
    commands = []
    for rel in _package_dirs(root, list(ticket.get("locks") or [])):
        where = rel or "."
        install = "npm ci --silent" if (root / where / "package-lock.json").is_file() else ""
        step = "(cd %s && %s%snpm test --silent)" % (shlex_quote(where), install, " && " if install else "")
        commands.append(step)
    if commands:
        return " && ".join(commands)
    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^ci:", makefile.read_text(errors="replace"), re.M):
        return "make ci"
    return ""


def shlex_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def pre_merge_check(root: Path, ticket: dict, cfg: Optional[dict], runner=None, minutes: int = 10) -> tuple:
    """Run the merge check on the pull request's head in a throwaway worktree.

    Returns (green|red|skip, detail). Before 1.6.17 nothing ran the tests before a merge: a repo with no CI merged
    a Shuttle's red test (WV-15-BB1).
    """
    import subprocess
    import tempfile

    root = Path(root)
    try:
        minutes = int((cfg or {}).get("mergeCheckMinutes") or minutes)
    except (TypeError, ValueError):
        pass
    cmd = merge_check_command(root, ticket, cfg)
    if not cmd:
        return "skip", "no test command (set checkCommand, or add a test script)"
    branch = str(ticket.get("branch") or "")
    if not branch:
        return "skip", "no branch"
    run = runner or (lambda argv, cwd=None, timeout=60: subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout))
    fetched = run(["git", "-c", "maintenance.auto=false", "fetch", "origin", branch], cwd=str(root))
    if fetched.returncode != 0:
        return "skip", "could not fetch %s: %s" % (branch, (fetched.stderr or fetched.stdout or "").strip()[-200:])
    where = tempfile.mkdtemp(prefix="warp-merge-check-")
    added = run(["git", "-c", "maintenance.auto=false", "worktree", "add", "--detach", where, "FETCH_HEAD"], cwd=str(root))
    if added.returncode != 0:
        return "skip", "could not check out %s: %s" % (branch, (added.stderr or added.stdout or "").strip()[-200:])
    try:
        try:
            done = run(["bash", "-c", cmd], cwd=where, timeout=max(1, int(minutes)) * 60)
        except subprocess.TimeoutExpired:
            return "red", "%s timed out after %d minutes" % (cmd, minutes)
        tail = "\n".join(((done.stdout or "") + "\n" + (done.stderr or "")).strip().splitlines()[-25:])
        return ("green" if done.returncode == 0 else "red"), "%s\n%s" % (cmd, tail)
    finally:
        run(["git", "-c", "maintenance.auto=false", "worktree", "remove", "--force", where], cwd=str(root))


def open_pr(root: Path, branch: str, base: str, title: str, body: str, runner=None) -> tuple[str, str]:
    """A pull request for a Shuttle's pushed branch: (found|opened|missing|failed, url or reason).

    found: one is already open for the branch. missing: the branch is not on origin.
    """
    run = runner or (lambda cmd: _run(cmd, root, timeout=60))
    if not shutil.which("gh") and runner is None:
        return "failed", "gh is not installed"
    listed = run(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"])
    if listed.returncode == 0:
        try:
            rows = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            rows = []
        if rows and isinstance(rows[0], dict) and rows[0].get("url"):
            return "found", str(rows[0]["url"])
    remote = run(["git", "ls-remote", "--heads", "origin", branch])
    if remote.returncode == 0 and not (remote.stdout or "").strip():
        return "missing", "branch %s is not on origin" % branch
    made = run(["gh", "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body])
    text = ((made.stdout or "") + "\n" + (made.stderr or "")).strip()
    if made.returncode != 0:
        return "failed", (text.splitlines() or ["gh pr create failed"])[-1]
    urls = re.findall(r"https://\S+/pull/\d+", text)
    return ("opened", urls[-1]) if urls else ("failed", "no pull request url in the gh output")


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
merge-local merges --branch into --base (default baseBranch; mergeMethod, a merge commit by default) and
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
    pn.add_argument("--branch", help="the ticket's branch (default: from the beam); checked on origin")
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
            local_cfg = {}
            try:
                import prompt_gate

                local_cfg = dict(json.loads(beam_path.read_text()).get("config") or {})
                local_cfg.update(prompt_gate.effective_config(json.loads(beam_path.read_text()), root))
            except Exception:
                pass
            method = merge_method(local_cfg)
            if method == "rebase":
                print("merge-local: mergeMethod rebase is merged with a merge commit locally")
            ok, detail = merge_local(root, args.branch, base, msg, method=method)
            if ok and delete_on_merge(local_cfg):
                gone, why = delete_merged_branch(root, args.branch, base)
                print(f"branch deleted {args.branch}" + (f" ({why})" if why else "") if gone else f"branch not deleted {args.branch}: {why}")
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
            print(note(root, Path(args.beam), args.id, args.reason, branch=args.branch))
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
