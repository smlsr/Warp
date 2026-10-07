#!/usr/bin/env python3
"""One checkout per ticket.

Cloud: this plugin cannot create a Cursor cloud agent. There is no API to
call. `launch` commits the beam, journal, board, and that ticket's claim
onto the ticket branch, pushes the branch, then tells the Warp session to
start one new Agent checked out on that branch. An Agent is a separate
top-level cloud agent: own conversation, own VM, own checkout. Do not start
a Subagent. Do not use the Task tool for a ticket. Do not start it on a
fresh clone of main. `implement` refuses to do the ticket in this process.

Local: `add` and `remove` run `git worktree add` and `git worktree remove`
on the ticket branch that already has .warp/beam.json. One worktree, one
branch, one ticket. Not a Subagent inside the orchestrator checkout.

  python3 scripts/checkout.py ?
  python3 scripts/checkout.py launch --beam .warp/beam.json --id T-1
  python3 scripts/checkout.py add --beam .warp/beam.json --id T-1 --root .
  python3 scripts/checkout.py remove --beam .warp/beam.json --id T-1 --root .
  python3 scripts/checkout.py bind --beam .warp/beam.json --id T-1 --agent bc-1
  python3 scripts/checkout.py implement --id T-1

?, help, -h, and --help print this text. Quote ? if the shell expands it.
"""

from __future__ import annotations

import argparse
import json
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


def worktree_rel(ticket_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(ticket_id))
    return ".warp/worktrees/%s" % (safe or "ticket")


def launch_lines(ticket_id: str, base: str, branch: str) -> list:
    """Instruction for one new Agent. Printed only after the branch has a beam.

    base is the branch the ticket branch was cut from. The Agent does not
    check out base. This script cannot create the Agent.
    """
    del base
    return [
        orchestrator.TICKET_AGENT_INSTRUCTION,
        "launch: one new Agent per ticket",
        "checkout: %s" % branch,
        "branch: %s" % branch,
        "prompt: IMPLEMENT %s" % ticket_id,
        "beam: .warp/beam.json",
        "claim: read it from that checkout",
        "refuse: in-process",
        "refuse: Subagent",
        "refuse: Task",
        "refuse: clone of main",
        "The plugin has no cloud-agent API. Do not implement this ticket in the orchestrator checkout.",
        "Do not start this Agent when the branch has no .warp beam.",
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
    """The ticket row the Agent must be able to read from its own checkout."""
    if ticket.get("agent"):
        return True
    return ticket.get("status") in _CLAIM_STATUSES


def _launch_cloud(root: Path, path: Path, data: dict, ticket: dict, branch: str, base: str, ticket_id: str) -> int:
    """Publish the claim onto the ticket branch, then print the new Agent instruction.

    The instruction is not printed when the branch has no .warp beam.
    """
    if not has_claim(ticket):
        print("refuse: ticket %s has no claim; do not start the Agent" % ticket_id)
        return 2
    ticket["branch"] = branch
    ticket["checkout"] = "cloud-vm"
    _save(path, data)
    published = state_commit.publish_ticket(root, str(path), ticket_id, push=True)
    if published != 0:
        return published
    if not state_commit.branch_has_beam(root, branch):
        print("refuse: branch %s has no .warp beam" % branch)
        print("refuse: do not start the Agent")
        return 2
    shown_base = base if base != "HEAD" else "main"
    for line in launch_lines(ticket_id, shown_base, branch):
        print(line)
    return 0


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _save(path: Path, beam: dict) -> None:
    import beam as beam_mod

    beam_mod.atomic_write(path, json.dumps(beam, indent=2) + "\n")


def _ticket(beam: dict, tid: str) -> dict:
    ticket = (beam.get("tickets") or {}).get(tid)
    if not ticket:
        sys.exit("unknown ticket %s" % tid)
    return ticket


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
    if orchestrator.checkout_kind(cfg) != "worktree":
        sys.exit("refuse: runner is not local; launch a cloud agent instead of adding a worktree")
    ticket = _ticket(beam, tid)
    if ticket.get("agent") and _occupied(ticket):
        sys.exit("refuse: ticket %s already has agent %s" % (tid, ticket.get("agent")))
    rel = ticket.get("worktree") or worktree_rel(tid)
    reason = worktree_block(beam, tid, rel)
    if reason:
        sys.exit("refuse: " + reason)
    branch = orchestrator.branch_name(ticket, cfg)
    path = root / rel
    if path.exists() and _git(root, "rev-parse", "--git-path", "worktrees").returncode == 0:
        listed = _git(root, "worktree", "list", "--porcelain")
        if str(path.resolve()) in listed.stdout or str(path) in listed.stdout:
            ticket["worktree"] = rel
            ticket["branch"] = branch
            ticket["checkout"] = "worktree"
            return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    start = base or str(cfg.get("baseBranch") or "HEAD")
    have = _git(root, "rev-parse", "--verify", "-q", "refs/heads/%s" % branch)
    if have.returncode == 0:
        cmd = ["worktree", "add", str(path), branch]
    else:
        cmd = ["worktree", "add", "-b", branch, str(path), start]
    made = _git(root, *cmd)
    if made.returncode != 0:
        sys.exit("git worktree add failed: %s" % (made.stderr.strip() or made.stdout.strip()))
    ticket["worktree"] = rel
    ticket["branch"] = branch
    ticket["checkout"] = "worktree"
    ticket["agent"] = ticket.get("agent") or ("worktree:%s" % rel)
    return rel


def remove_worktree(root: Path, beam: dict, tid: str) -> str:
    ticket = _ticket(beam, tid)
    rel = ticket.get("worktree")
    if not rel:
        return ""
    path = root / rel
    if path.exists():
        removed = _git(root, "worktree", "remove", "--force", str(path))
        if removed.returncode != 0:
            sys.exit("git worktree remove failed: %s" % (removed.stderr.strip() or removed.stdout.strip()))
    ticket["worktree"] = None
    if str(ticket.get("agent") or "").startswith("worktree:"):
        ticket["agent"] = None
    return rel or ""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="One checkout per ticket", epilog=HELP)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common_beam = ("--beam",)
    for name in ("launch", "add", "remove", "bind", "implement"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--beam", default=".warp/beam.json")
        cmd.add_argument("--id", required=True)
        cmd.add_argument("--root", default=".")
        if name in {"launch", "add"}:
            cmd.add_argument("--base", default="")
        if name == "bind":
            cmd.add_argument("--agent", required=True)

    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    root = Path(args.root).resolve()
    if args.cmd == "implement":
        print("refuse: the orchestrator does not implement tickets in this checkout")
        print("refuse: in-process")
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
        if orchestrator.checkout_kind(cfg) == "worktree":
            print(orchestrator.LOCAL_WORKTREE_INSTRUCTION)
            print("local: git worktree")
            print("python3 scripts/checkout.py add --beam %s --id %s --root ." % (args.beam, args.id))
            return 0
        return _launch_cloud(root, path, data, ticket, branch, base, args.id)
    if args.cmd == "add":
        if orchestrator.checkout_kind(cfg) != "worktree":
            return _launch_cloud(root, path, data, ticket, branch, base, args.id)
        ticket["branch"] = branch
        ticket["checkout"] = "worktree"
        _save(path, data)
        published = state_commit.publish_ticket(root, str(path), args.id, push=False)
        if published != 0:
            return published
        if not state_commit.branch_has_beam(root, branch):
            print("refuse: branch %s has no .warp beam" % branch)
            print("refuse: do not start the Agent")
            return 2
        data = _load(path)
        rel = add_worktree(root, data, args.id, base)
        _save(path, data)
        print("worktree %s branch %s" % (rel, branch))
        return 0
    if args.cmd == "remove":
        rel = remove_worktree(root, data, args.id)
        _save(path, data)
        print("removed %s" % (rel or args.id))
        return 0
    if args.cmd == "bind":
        bind_agent(data, args.id, args.agent)
        _save(path, data)
        print("agent %s ticket %s" % (args.agent, args.id))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
