#!/usr/bin/env python3
"""One checkout per ticket.

Default (`subagentVm: false`): `launch` fetches the base branch and adds a
git worktree at `<worktreeRoot>/<id>`. `worktreeRoot` defaults to
`.warp/worktrees`, which `/warp-init` gitignores. `maxAgents`
(default 18) is the only shared-machine cap. `memoryCheck` defaults to
false, so `free -g` is not read. `true` lets `free -g` lower that cap.
`runner: local` forces `subagentVm` false and `launch` back to `worktree`.

`subagentVm: true` on a cloud runner prints a subagent prompt that asks for
a dedicated VM. The sentence is fixed: run in your own cloud environment on
a dedicated VM with its own clone and branch, not a git worktree on this
machine. The subagent fetches the latest main, clones it, and creates
`warp/<id>-<jira>` itself. Its first step is `hostname` and `free -g`. It
writes `.warp/tickets/<id>/` on that branch and pushes. The parent patches
the beam from that folder. `verify` compares the reported hostname with this
machine. A different hostname is a separate VM. The same hostname is a
worktree on this machine: the ticket log records it, shared-machine rules
apply, and one warning is posted per run.

That fallback fetches the base branch and adds the same git worktree. The subagent
works only inside that path, commits, pushes, and opens the pull request.
It never merges, never touches the parent checkout, and never calls Jira
or Slack. It writes `.warp/tickets/<id>/state.json` and `log.jsonl` at the
parent checkout by absolute path, and it returns one line.

`verify` runs after the subagent. The parent checkout must be clean apart
from `.warp/`, and the ticket diff must stay inside the worktree and the
locks. A miss raises lock-escape. Merge and park run `git worktree remove`
and `git worktree prune`. A second `launch` reuses a surviving worktree and
branch. A failed subagent result raises worker-died. The heartbeat watchdog
still covers a parent that dies mid-turn.

`launch: agent` is optional. It prints an IMPLEMENT prompt for one new Agent
and does not call a Cloud Agents API. `implement` still refuses to do the
ticket in this process.

  python3 scripts/checkout.py ?
  python3 scripts/checkout.py launch --beam .warp/beam.json --id T-1
  python3 scripts/checkout.py verify --beam .warp/beam.json --id T-1 --root .
  python3 scripts/checkout.py result --beam .warp/beam.json --id T-1 --line "result: T-1 failed" --hostname host --cwd /tmp/clone
  python3 scripts/checkout.py add --beam .warp/beam.json --id T-1 --root .
  python3 scripts/checkout.py remove --beam .warp/beam.json --id T-1 --root .
  python3 scripts/checkout.py bind --beam .warp/beam.json --id T-1 --agent bc-1
  python3 scripts/checkout.py implement --id T-1

?, help, -h, and --help print this text. Quote ? if the shell expands it.
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
import orchestrator  # noqa: E402
import state_commit  # noqa: E402

HELP = __doc__


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )


def _safe_id(ticket_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(ticket_id))
    return safe or "ticket"


def worktree_rel(ticket_id: str, config: Optional[dict] = None) -> str:
    root_name = orchestrator.worktree_root_name(config)
    return "%s/%s" % (root_name.rstrip("/"), _safe_id(ticket_id))


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "-q", ref).returncode == 0


def _resolve_ref(root: Path, name: str) -> str:
    for candidate in (name, "refs/heads/%s" % name, "origin/%s" % name):
        if candidate and _ref_exists(root, candidate):
            return candidate
    return ""


def _fetch_branch(root: Path, branch: str) -> None:
    """See a remote beam-only branch. A missing remote or ref does not refuse launch."""
    names = _git(root, "remote")
    if names.returncode != 0 or "origin" not in names.stdout.split():
        return
    _git(
        root,
        "fetch",
        "origin",
        "refs/heads/%s:refs/remotes/origin/%s" % (branch, branch),
    )


_BEAM_ONLY_PATHS = {
    ".warp/beam.json",
    ".warp/journal.jsonl",
    ".warp/BOARD.md",
    ".warp/STATUS.md",
    ".warp/board.html",
    ".warp/status.json",
}


def branch_state(root: Path, branch: str, base: str) -> str:
    """missing, beam-only, or work.

    beam-only is a branch whose diff against the base is only the Warp state
    files, including .warp/beam.json. That is the pre-made beam commit. Work
    is any other path. A missing branch does not block launch.
    """
    _fetch_branch(root, branch)
    ref = _resolve_ref(root, branch)
    if not ref:
        return "missing"
    base_name = base if base and base != "HEAD" else "main"
    base_ref = _resolve_ref(root, "origin/%s" % base_name) or _resolve_ref(root, base_name) or ""
    if not base_ref:
        base_ref = "HEAD" if _ref_exists(root, "HEAD") else ""
    if not base_ref:
        return "missing"
    diff = _git(root, "diff", "--name-only", "%s...%s" % (base_ref, ref))
    if diff.returncode != 0:
        diff = _git(root, "diff", "--name-only", "%s..%s" % (base_ref, ref))
    if diff.returncode != 0:
        return "missing"
    names = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    if not names or ".warp/beam.json" not in names:
        return "work" if names else "missing"
    if all(name in _BEAM_ONLY_PATHS for name in names):
        return "beam-only"
    return "work"


def launch_lines(ticket: dict, config: Optional[dict], branch: str, state: str, instance: str = "") -> list:
    """IMPLEMENT prompt for the optional new Agent mode. The branch does not have to exist.

    This script cannot create the Agent. It does not call a Cloud Agents API.
    """
    del branch
    prompt = orchestrator.implement_prompt(ticket, config, branch_state=state, instance=instance)
    ticket_id = ticket.get("id") or ""
    return prompt.splitlines() + [
        "name: %s" % orchestrator.agent_name(ticket, instance),
        "launch: one new Agent per ticket",
        "refuse: in-process",
        "optional: Cloud Agents API",
        "The plugin has no cloud-agent API. Do not implement this ticket in the orchestrator checkout.",
        "After the Agent id comes back, record it:",
        "python3 scripts/checkout.py bind --beam .warp/beam.json --id %s --agent <agent-id>" % ticket_id,
    ]


_CLAIM_STATUSES = {
    "claimed",
    "recovering",
    "planning",
    "coding",
    "review",
    "fix",
    "bugbot_running",
    "awaiting_approval",
    "merging",
}


def has_claim(ticket: dict) -> bool:
    """The ticket row the worker must be able to read."""
    if ticket.get("agent"):
        return True
    return ticket.get("status") in _CLAIM_STATUSES


def _has_origin(root: Path) -> bool:
    names = _git(root, "remote")
    return names.returncode == 0 and "origin" in names.stdout.split()


def _is_git(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree").returncode == 0


def _base_name(cfg: dict, base: str) -> str:
    name = (base or str(cfg.get("baseBranch") or "") or "main").strip()
    if name in {"", "HEAD"}:
        return "main"
    return name


def fetch_base(root: Path, base: str) -> str:
    """git fetch origin <base>. Return the ref the worktree starts from."""
    if _has_origin(root):
        fetched = _git(root, "fetch", "origin", base)
        remote = "origin/%s" % base
        if _ref_exists(root, remote):
            return remote
        if fetched.returncode != 0:
            sys.exit("git fetch origin %s failed: %s" % (base, (fetched.stderr or fetched.stdout).strip()))
    local = _resolve_ref(root, base)
    if local:
        return local
    if _ref_exists(root, "HEAD"):
        return "HEAD"
    sys.exit("no base ref %s" % base)


def worktree_path(root: Path, config: Optional[dict], tid: str) -> Path:
    raw = Path(orchestrator.worktree_root_name(config))
    if not raw.is_absolute():
        raw = root / raw
    return (raw / _safe_id(tid)).resolve()


def _stored_path(root: Path, stored: str) -> Path:
    path = Path(stored)
    if path.is_absolute():
        return path
    return (root / stored).resolve()


def _worktree_listed(root: Path, path: Path) -> bool:
    listed = _git(root, "worktree", "list", "--porcelain")
    text = listed.stdout
    return str(path) in text or str(path.resolve()) in text


def _local_branch(root: Path, branch: str) -> bool:
    return _git(root, "rev-parse", "--verify", "-q", "refs/heads/%s" % branch).returncode == 0


def _remote_branch(root: Path, branch: str) -> str:
    if not _has_origin(root):
        return ""
    _git(
        root,
        "fetch",
        "origin",
        "refs/heads/%s:refs/remotes/origin/%s" % (branch, branch),
    )
    remote = "origin/%s" % branch
    if _ref_exists(root, remote):
        return remote
    return ""


def ensure_worktree(root: Path, path: Path, branch: str, start: str) -> str:
    """Add the worktree, or reuse one that resume already has.

    Returns reused or created. A local branch is not reset to the base.
    """
    if path.exists() and _worktree_listed(root, path):
        return "reused"
    if path.exists() and not _worktree_listed(root, path):
        # A leftover directory that is not a worktree blocks git.
        try:
            next(path.iterdir())
            empty = False
        except StopIteration:
            empty = True
        if not empty:
            sys.exit("worktree path exists and is not a worktree: %s" % path)
        path.rmdir()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _local_branch(root, branch):
        cmd = ["worktree", "add", str(path), branch]
    else:
        remote = _remote_branch(root, branch)
        if remote:
            cmd = ["worktree", "add", "-b", branch, str(path), remote]
        else:
            cmd = ["worktree", "add", "-b", branch, str(path), start]
    made = _git(root, *cmd)
    if made.returncode != 0:
        sys.exit("git worktree add failed: %s" % (made.stderr.strip() or made.stdout.strip()))
    return "created"


def _launch_vm(root: Path, path: Path, data: dict, ticket: dict, branch: str, ticket_id: str) -> int:
    """Print the dedicated-VM prompt. The subagent clones and creates the branch."""
    if not has_claim(ticket):
        print("refuse: ticket %s has no claim; do not start the subagent" % ticket_id)
        return 2
    import agents

    refused = agents.launch_refused(data, ticket)
    if refused:
        for line in refused:
            print(line)
        return 2
    for line in agents.note_checkout(data, ticket, beam_path=path):
        print(line)
    ticket["branch"] = branch
    ticket["checkout"] = "subagent-vm"
    ticket["worktree"] = None
    ticket["isolation"] = "vm"
    ticket["agent"] = ticket.get("agent") or ("subagent:%s" % ticket_id)
    _note_live(data, ticket)
    instance = agents.ensure_instance(data, path)
    if not _save_launch(path, data, ticket_id):
        return 2
    had = False
    if _is_git(root):
        had = _local_branch(root, branch) or bool(_remote_branch(root, branch))
    if had:
        print("reused: branch %s" % branch)
    else:
        print("branch: %s" % branch)
    print("for the parent: %s" % orchestrator.VM_INSTRUCTION)
    print(orchestrator.name_line(orchestrator.agent_name(ticket, instance)))
    print(orchestrator.PROMPT_MARK)
    print(orchestrator.subagent_vm_prompt(ticket, data.get("config") or {}, branch, reused=had, instance=instance))
    return 0


def _launch_worktree(root: Path, path: Path, data: dict, ticket: dict, branch: str, base: str, ticket_id: str) -> int:
    """Fetch the base, add the worktree, print the subagent prompt."""
    if not has_claim(ticket):
        print("refuse: ticket %s has no claim; do not start the subagent" % ticket_id)
        return 2
    import agents

    refused = agents.launch_refused(data, ticket)
    if refused:
        for line in refused:
            print(line)
        return 2
    for line in agents.note_checkout(data, ticket, beam_path=path):
        print(line)
    if not _is_git(root):
        sys.exit("refuse: %s is not a git checkout" % root)
    base_name = _base_name(data.get("config") or {}, base)
    start = fetch_base(root, base_name)
    had_branch = _local_branch(root, branch) or bool(_remote_branch(root, branch))
    wt = worktree_path(root, data.get("config") or {}, ticket_id)
    listed = wt.exists() and _worktree_listed(root, wt)
    kind = ensure_worktree(root, wt, branch, start)
    rel = worktree_rel(ticket_id, data.get("config") or {})
    ticket["branch"] = branch
    ticket["worktree"] = rel
    ticket["checkout"] = "worktree"
    ticket["isolation"] = "worktree"
    ticket["agent"] = ticket.get("agent") or ("subagent:%s" % ticket_id)
    _note_live(data, ticket)
    instance = agents.ensure_instance(data, path)
    if not _save_launch(path, data, ticket_id):
        return 2
    if kind == "reused" or listed:
        print("reused: worktree %s" % wt)
    else:
        print("created: worktree %s" % wt)
    if had_branch or kind == "reused":
        print("reused: branch %s" % branch)
    else:
        print("created: branch %s" % branch)
    print("base: %s" % start)
    prompt = orchestrator.subagent_prompt(ticket, data.get("config") or {}, branch, str(wt), str(root), instance=instance)
    print("for the parent: %s" % orchestrator.SUBAGENT_INSTRUCTION)
    print(orchestrator.name_line(orchestrator.agent_name(ticket, instance)))
    print(orchestrator.PROMPT_MARK)
    print(prompt)
    return 0


def _launch_agent(root: Path, path: Path, data: dict, ticket: dict, branch: str, base: str, ticket_id: str) -> int:
    """Print the IMPLEMENT prompt. Do not create the branch, and do not call an API.

    A beam file on the branch is not a start condition. A branch that is only
    the beam commit is named in the prompt so the Agent starts from latest main.
    """
    if not has_claim(ticket):
        print("refuse: ticket %s has no claim; do not start the Agent" % ticket_id)
        return 2
    import agents

    refused = agents.launch_refused(data, ticket)
    if refused:
        for line in refused:
            print(line)
        return 2
    for line in agents.note_checkout(data, ticket, beam_path=path):
        print(line)
    ticket["branch"] = branch
    ticket["checkout"] = "cloud-vm"
    _note_live(data, ticket)
    instance = agents.ensure_instance(data, path)
    if not _save_launch(path, data, ticket_id):
        return 2
    state = branch_state(root, branch, base)
    for line in launch_lines(ticket, data.get("config") or {}, branch, state, instance=instance):
        print(line)
    return 0


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _save_launch(path: Path, data: dict, ticket_id: str) -> bool:
    """Save a launch, unless the run was paused or stopped while it ran. False means no prompt."""
    import agents

    disk = agents.halted_meanwhile(data, path)
    if disk is None:
        _save(path, data)
        return True
    state = agents.halt_state(disk)
    agents.halt_local(disk, beam_path=path, reason=state)
    _save(path, disk)
    print("spawn: closed %s" % ticket_id)
    print("refuse: Warp was %s while this launch ran. No agent starts." % state)
    return False


def _save(path: Path, beam: dict) -> None:
    import beam as beam_mod

    beam_mod.atomic_write(path, json.dumps(beam, indent=2) + "\n")


def _ticket(beam: dict, tid: str) -> dict:
    ticket = (beam.get("tickets") or {}).get(tid)
    if not ticket:
        sys.exit("unknown ticket %s" % tid)
    return ticket


def _note_live(data: dict, ticket: dict) -> None:
    """The launch holds a slot: this parent session, out, not returned."""
    import beam as beam_mod
    import pipeline

    shuttle = ticket.get("shuttle") if isinstance(ticket.get("shuttle"), dict) else {}
    step = str(shuttle.get("step") or "")
    if step not in {"implement", "fix", "restart", "repair"}:
        if ticket.get("status") == "fix" or ticket.get("phase") == "fixing":
            step = "fix"
        else:
            step = "implement"
    pipeline.mark_shuttle(ticket, step, data)
    if not ticket.get("workerStartedAt"):
        ticket["workerStartedAt"] = beam_mod.utcnow()


def _occupied(ticket: dict) -> bool:
    return orchestrator.holds_locks(ticket) or orchestrator.holds_slot(ticket, {})


def agent_block(beam: dict, tid: str, agent: str) -> Optional[str]:
    """One agent, one ticket. A second agent on the same ticket is refused."""
    if not agent:
        return "agent id is empty"
    for other in (beam.get("tickets") or {}).values():
        oid = other.get("id")
        current = other.get("agent")
        if not current or not _occupied(other):
            continue
        if oid == tid and current != agent:
            return "ticket %s already has agent %s" % (tid, current)
        if oid != tid and current == agent:
            return "agent %s is already on ticket %s" % (agent, oid)
    return None


def worktree_block(beam: dict, tid: str, path: str) -> Optional[str]:
    """One worktree, one ticket. Two tickets never share a path."""
    for other in (beam.get("tickets") or {}).values():
        if other.get("id") == tid or not other.get("worktree"):
            continue
        if not _occupied(other):
            continue
        if other.get("worktree") == path:
            return "worktree %s is already ticket %s" % (path, other.get("id"))
    return None


def bind_agent(beam: dict, tid: str, agent: str) -> str:
    reason = agent_block(beam, tid, agent)
    if reason:
        sys.exit("refuse: " + reason)
    ticket = _ticket(beam, tid)
    ticket["agent"] = agent
    ticket["checkout"] = ticket.get("checkout") or "cloud-vm"
    return agent


def add_worktree(root: Path, beam: dict, tid: str, base: str) -> str:
    cfg = beam.get("config") or {}
    ticket = _ticket(beam, tid)
    if ticket.get("agent") and _occupied(ticket) and ticket.get("checkout") == "cloud-vm":
        sys.exit("refuse: ticket %s already has agent %s" % (tid, ticket.get("agent")))
    rel = ticket.get("worktree") or worktree_rel(tid, cfg)
    reason = worktree_block(beam, tid, rel)
    if reason:
        sys.exit("refuse: " + reason)
    branch = orchestrator.branch_name(ticket, cfg)
    path = Path(rel).resolve() if Path(rel).is_absolute() else (root / rel).resolve()
    if path.exists() and _worktree_listed(root, path):
        ticket["worktree"] = rel
        ticket["branch"] = branch
        ticket["checkout"] = "worktree"
        return rel
    start = base or str(cfg.get("baseBranch") or "HEAD")
    ensure_worktree(root, path, branch, start)
    ticket["worktree"] = rel
    ticket["branch"] = branch
    ticket["checkout"] = "worktree"
    ticket["agent"] = ticket.get("agent") or ("worktree:%s" % rel)
    return rel


def prune_worktrees(root: Path) -> None:
    if not _is_git(root):
        return
    _git(root, "worktree", "prune")


def remove_worktree(root: Path, beam: dict, tid: str) -> str:
    ticket = _ticket(beam, tid)
    rel = ticket.get("worktree")
    if not rel:
        return ""
    path = Path(rel) if Path(rel).is_absolute() else (root / rel)
    if path.exists() or _worktree_listed(root, path.resolve() if path.exists() else path):
        removed = _git(root, "worktree", "remove", "--force", str(path))
        if removed.returncode != 0 and path.exists():
            sys.exit("git worktree remove failed: %s" % (removed.stderr.strip() or removed.stdout.strip()))
    ticket["worktree"] = None
    agent = str(ticket.get("agent") or "")
    if agent.startswith("worktree:") or agent.startswith("subagent:"):
        ticket["agent"] = None
    return rel or ""


def drop_worktree(root: Path, beam: dict, tid: str) -> str:
    """git worktree remove, then git worktree prune. Safe outside a checkout."""
    if not _is_git(root):
        return ""
    if tid not in (beam.get("tickets") or {}):
        prune_worktrees(root)
        return ""
    rel = remove_worktree(root, beam, tid)
    prune_worktrees(root)
    if rel:
        print("removed %s" % rel)
    print("pruned worktrees")
    return rel or ""


def _status_paths(root: Path) -> list:
    proc = _git(root, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip().strip('"')
        if path:
            found.append(path)
    return found


def _diff_names(root: Path, left: str, right: str) -> list:
    diff = _git(root, "diff", "--name-only", "%s...%s" % (left, right))
    if diff.returncode != 0:
        diff = _git(root, "diff", "--name-only", "%s..%s" % (left, right))
    if diff.returncode != 0:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _worktree_dirty(path: Path) -> list:
    if not path.is_dir():
        return []
    proc = _git(path, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[1].strip()
        name = name.strip().strip('"')
        if name:
            found.append(name)
    return found


def _raise_escape(beam_path: Path, data: dict, ticket: dict, tid: str, escaped: list) -> int:
    import ticket_state

    paths = []
    for item in escaped:
        if item and item not in paths:
            paths.append(item)
    now = ""
    try:
        import beam as beam_mod

        _now, now = beam_mod.coerce_now(None)
    except Exception:
        now = ""
    ticket_state._apply_lock_escape(beam_path, ticket, paths, now)
    ticket_state.append_status(
        beam_path.parent.parent if beam_path.parent.name == ".warp" else beam_path.parent,
        tid,
        "lock-escape",
        escaped=paths,
        alarm="lock-escape",
        push=False,
    )
    _save(beam_path, data)
    print("lock-escape: %s" % tid)
    for item in paths:
        print("escaped: %s" % item)
    return 2


def verify_ticket(root: Path, beam_path: Path, data: dict, tid: str) -> int:
    """Parent stays clean apart from .warp. The diff stays in the worktree and the locks."""
    ticket = _ticket(data, tid)
    cfg = data.get("config") or {}
    rel = ticket.get("worktree") or worktree_rel(tid, cfg)
    wt = Path(rel) if Path(str(rel)).is_absolute() else (root / rel)
    wt = wt.resolve()
    rel_text = str(rel).replace("\\", "/").rstrip("/")
    escaped = []
    for name in _status_paths(root):
        norm = name.replace("\\", "/")
        if norm == ".warp" or norm.startswith(".warp/"):
            continue
        if norm == rel_text or norm.startswith(rel_text + "/"):
            continue
        escaped.append(norm)
    branch = str(ticket.get("branch") or orchestrator.branch_name(ticket, cfg))
    base_name = _base_name(cfg, "")
    base_ref = _resolve_ref(root, "origin/%s" % base_name) or _resolve_ref(root, base_name) or "HEAD"
    locks = orchestrator.lock_paths(ticket)
    extra = orchestrator.append_only_paths(cfg)
    if _ref_exists(root, branch) or _ref_exists(root, "refs/heads/%s" % branch):
        for name in _diff_names(root, base_ref, branch):
            if not orchestrator.path_inside_locks(name, locks, extra):
                if name not in escaped:
                    escaped.append(name)
    for name in _worktree_dirty(wt):
        if not orchestrator.path_inside_locks(name, locks, extra):
            if name not in escaped:
                escaped.append(name)
    if escaped:
        return _raise_escape(beam_path, data, ticket, tid, escaped)
    if ticket.get("isolation") == "vm":
        print("verify: vm %s" % tid)
        return 0
    print("verify: ok %s" % tid)
    return 0


_REPORT_KEY = re.compile(r"(?:^|\s)(hostname|branch|cwd|memory)=(\S+)")


def parse_report(text: str, hostname: str = "", cwd: str = "") -> dict:
    """hostname=, branch=, cwd=, and memory= tokens from a one-line result."""
    found = {}
    for match in _REPORT_KEY.finditer(text or ""):
        found[match.group(1)] = match.group(2)
    if hostname:
        found["hostname"] = hostname.strip()
    if cwd:
        found["cwd"] = cwd.strip()
    return found


def _parent_hostname() -> str:
    try:
        proc = subprocess.run(["hostname"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        import socket

        return socket.gethostname()
    except OSError:
        return ""


def _append_log(root: Path, tid: str, row: dict) -> None:
    folder = root / ".warp" / "tickets" / tid
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _note_fallback(data: dict, ticket: dict, root: Path, tid: str, hostname: str, cwd: str) -> None:
    """Same hostname: this ticket is a worktree on the parent machine."""
    ticket["isolation"] = "fallback"
    ticket["checkout"] = "worktree"
    if cwd:
        path = Path(cwd)
        try:
            if path.is_dir() and path.resolve() != root.resolve():
                ticket["worktree"] = str(path.resolve())
        except OSError:
            pass
    try:
        import beam as beam_mod

        stamp = beam_mod.utcnow()
    except Exception:
        stamp = ""
    _append_log(
        root,
        tid,
        {
            "at": stamp,
            "hostname": hostname,
            "note": "same hostname as parent; shared-machine worktree",
            "type": "fallback",
        },
    )
    data["subagentVmFallback"] = True
    if data.get("subagentVmWarned"):
        print("fallback: %s" % tid)
        return
    data["subagentVmWarned"] = True
    print("warn: subagent %s stayed on this machine (hostname %s). Shared-machine rules." % (tid, hostname))
    print("herald: subagent fell back to a worktree on this machine")


def note_host(data: dict, tid: str, text: str, root: Path, hostname: str = "", cwd: str = "") -> str:
    """Compare a reported hostname with this machine. Missing hostname is unreported."""
    ticket = _ticket(data, tid)
    report = parse_report(text, hostname=hostname, cwd=cwd)
    reported = report.get("hostname") or ""
    if not reported:
        return "unreported"
    parent = _parent_hostname()
    ticket["reportedHostname"] = reported
    ticket["parentHostname"] = parent
    if ticket.get("checkout") != "subagent-vm":
        print("verify: host %s hostname=%s" % (tid, reported))
        return "worktree"
    if reported != parent:
        ticket["isolation"] = "vm"
        ticket["checkout"] = "subagent-vm"
        print("verify: vm %s hostname=%s" % (tid, reported))
        return "vm"
    _note_fallback(data, ticket, root, tid, reported, report.get("cwd") or "")
    return "fallback"


def _result_failed(text: str, forced: bool) -> bool:
    if forced:
        return True
    low = text.strip().casefold()
    if not low:
        return True
    parts = low.replace(":", " ").split()
    if "ok" in parts or "opened" in parts or "success" in parts:
        return False
    if any(word in parts for word in ("failed", "fail", "died", "dead", "error")):
        return True
    return not low.startswith("result")


def record_result(
    data: dict,
    tid: str,
    text: str,
    forced: bool,
    root: Optional[Path] = None,
    hostname: str = "",
    cwd: str = "",
    beam_path: Optional[Path] = None,
) -> str:
    ticket = _ticket(data, tid)
    line = (text or "").strip()
    ticket["subagentResult"] = line
    if root is not None:
        note_host(data, tid, line, root, hostname=hostname, cwd=cwd)
    if _result_failed(line, forced):
        orchestrator.note_subagent_failure(ticket)
        return "worker-died"
    import agents

    _named, _reason, line_agent = agents.stopped_result(line)
    folder_agent = "" if line_agent else _cloud_agent_from_folder(root, tid)
    agents.note_stopped_result(data, tid, line, beam_path=beam_path, agent_id=folder_agent)
    return "ok"


def _cloud_agent_from_folder(root: Optional[Path], tid: str) -> str:
    """The VM id in this ticket's state.json. Empty when the folder has none."""
    if root is None or not tid:
        return ""
    path = Path(root) / ".warp" / "tickets" / str(tid) / "state.json"
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("cloudAgent") or "").strip()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="One checkout per ticket", epilog=HELP)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("launch", "add", "remove", "bind", "implement", "verify", "result"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--beam", default=".warp/beam.json")
        cmd.add_argument("--id", required=True)
        cmd.add_argument("--root", default=".")
        if name in {"launch", "add"}:
            cmd.add_argument("--base", default="")
        if name == "bind":
            cmd.add_argument("--agent", required=True)
        if name == "result":
            cmd.add_argument("--line", default="")
            cmd.add_argument("--failed", action="store_true")
            cmd.add_argument("--hostname", default="")
            cmd.add_argument("--cwd", default="")
        if name == "verify":
            cmd.add_argument("--hostname", default="")
            cmd.add_argument("--cwd", default="")

    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    if args.cmd == "implement":
        print("refuse: the orchestrator does not implement tickets in this checkout")
        print("refuse: in-process")
        print(orchestrator.SUBAGENT_INSTRUCTION)
        print(orchestrator.TICKET_AGENT_INSTRUCTION)
        return 2
    path = Path(args.beam)
    if not path.is_file():
        path = root / args.beam
    if not path.is_file():
        sys.exit("no beam at %s" % args.beam)
    data = _load(path)
    cfg = data.get("config") or {}
    ticket = _ticket(data, args.id)
    base = getattr(args, "base", "") or str(cfg.get("baseBranch") or "HEAD")
    branch = orchestrator.branch_name(ticket, cfg)
    if args.cmd == "launch":
        if orchestrator.launch_mode(cfg) == "agent":
            return _launch_agent(root, path, data, ticket, branch, base, args.id)
        if orchestrator.shared_machine(cfg, data):
            gib = None
            if orchestrator.memory_check(cfg):
                gib = orchestrator.available_gib()
                if gib is not None:
                    print("memory: available %s GiB" % gib)
                cap = orchestrator.local_subagent_cap(cfg, gib)
            else:
                cap = orchestrator.shared_cap(cfg)
            room = orchestrator.local_room(data, cfg, skip_id=args.id, available=gib)
            holders = orchestrator.live_slot_holders(data, cfg, skip_id=args.id)
            shown = ", ".join(holders) if holders else "none"
            print("local-cap: %s" % cap)
            print("live shuttles %d/%d: %s" % (len(holders), cap, shown))
            if room < 1:
                print("refuse: local subagent cap %s (live %d/%d: %s)" % (cap, len(holders), cap, shown))
                return 2
            return _launch_worktree(root, path, data, ticket, branch, base, args.id)
        return _launch_vm(root, path, data, ticket, branch, args.id)
    if args.cmd == "verify":
        host = getattr(args, "hostname", "") or ""
        if host:
            note_host(data, args.id, "", root, hostname=host, cwd=getattr(args, "cwd", "") or "")
            _save(path, data)
        code = verify_ticket(root, path, data, args.id)
        return code
    if args.cmd == "result":
        outcome = record_result(
            data,
            args.id,
            getattr(args, "line", ""),
            bool(getattr(args, "failed", False)),
            root=root,
            hostname=getattr(args, "hostname", "") or "",
            cwd=getattr(args, "cwd", "") or "",
            beam_path=path,
        )
        _save(path, data)
        if outcome == "worker-died":
            print("shuttle: alarm %s worker-died" % args.id)
            print("herald: %s worker died." % args.id)
            return 0
        if data["tickets"][args.id].get("isolation") == "fallback":
            code = verify_ticket(root, path, data, args.id)
            _save(path, data)
            if code != 0:
                return code
        print("result: ok %s" % args.id)
        return 0
    if args.cmd == "add":
        if orchestrator.launch_mode(cfg) == "agent":
            return _launch_agent(root, path, data, ticket, branch, base, args.id)
        ticket["branch"] = branch
        ticket["checkout"] = "worktree"
        _save(path, data)
        published = state_commit.publish_ticket(root, str(path), args.id, push=False)
        if published != 0:
            return published
        if not state_commit.branch_has_beam(root, branch):
            print("refuse: branch %s has no .warp beam" % branch)
            print("refuse: do not start the subagent")
            return 2
        data = _load(path)
        rel = add_worktree(root, data, args.id, base)
        _save(path, data)
        print("worktree %s branch %s" % (rel, branch))
        return 0
    if args.cmd == "remove":
        rel = drop_worktree(root, data, args.id)
        _save(path, data)
        if not rel:
            print("removed %s" % args.id)
        return 0
    if args.cmd == "bind":
        bind_agent(data, args.id, args.agent)
        _save(path, data)
        print("agent %s ticket %s" % (args.agent, args.id))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
