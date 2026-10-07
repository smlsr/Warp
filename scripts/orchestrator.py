#!/usr/bin/env python3
"""Warp orchestrator policy.

One orchestrator dispatches, merges, and tracks. It writes no ticket product
code. It is the only thing that merges to the base branch. Ticket Agents never
merge. A ticket is not a Subagent.

The cap is config maxAgents. A project may set 18. This module does not treat
any number as a built-in cap.

  python3 scripts/orchestrator.py ?
  python3 scripts/orchestrator.py queue --beam .warp/beam.json
  python3 scripts/orchestrator.py fail --beam .warp/beam.json --id T-1 --output "ci red"
  python3 scripts/orchestrator.py rebuild --beam .warp/beam.json --facts facts.json
  python3 scripts/orchestrator.py resolve --path shared/notes.md --append-only shared/notes.md
  python3 scripts/orchestrator.py dispatch --beam .warp/beam.json
dispatch recomputes pending gates first. A gate turns green when every member
is merged or done, then this command prints the start lines.
  python3 scripts/orchestrator.py record --beam .warp/beam.json --id T-1 --plan "..." --result "..."

?, help, -h, and --help print this text. Quote ? if the shell expands it.
checkCommand and appendOnlyPaths come from config. An empty checkCommand means
Bugbot and CI as configured. `make ci` is a check command; the space stays.
A red `make ci` is stored on pr.check and printed as send-back plus a start
line, with the log. A green result is stored and is not a failure. The third
red parks the ticket (status parked). appendOnlyPaths defaults to empty.
send-back is also the result when a conflict is outside that list. resolve
takes --base, --ours, and --theirs.
opened records a pull request when the Shuttle exits. That does not free the
slot. merge records merged only for outcome merged. enqueue and rejected do
not. mergeQueue defaults to false. A detected GitHub merge queue also enqueues.
The orchestrator does not implement a ticket. checkout.py implement refuses.
checkout.py launch does not create the branch. It prints an IMPLEMENT prompt
that contains the claim. The Agent clones main, then creates the branch.
A beam file does not have to exist on the branch before the Agent starts.
parent-exit prints parent: stay while a ticket is claimed, in review,
awaiting checks, or queued, and parent: exit when the run is paused or
stopped, or when nothing is claimed, queued, or awaiting checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Statuses that still hold lock paths. A green PR stays here until merge or park.
LOCK_HELD = {
    "claimed",
    "recovering",
    "planning",
    "coding",
    "review",
    "bugbot_running",
    "fix",
    "awaiting_approval",
    "merging",
}
# The Agent is still in the slot. A green PR is not in this set.
SLOT_HELD = {"claimed", "recovering", "planning", "coding", "bugbot_running", "fix"}
MERGED_ON_BASE = {"merged", "done"}
SETTLED = {"merged", "done", "parked", "skipped"}
MERGE_STEPS = ("rebase", "check", "merge", "delete-branch", "dispatch")
_FALSE = {"false", "no", "off", "0"}


def _flag(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE


def agent_cap(config: Optional[dict]) -> Optional[int]:
    """Concurrent Agents from config. None when maxAgents is unset.

    Callers that need a number use the product default from beam.default_config.
    Nothing here treats 18 as a special cap. A project may set 18.
    """
    raw = (config or {}).get("maxAgents")
    if raw is None or str(raw).strip() == "":
        return None
    return max(0, int(raw))


def check_command(config: Optional[dict]) -> str:
    raw = (config or {}).get("checkCommand")
    if raw is None:
        return ""
    return str(raw).strip()


MAKE_CI = "make ci"
_CHECK_RED = {
    "red",
    "fail",
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "timed_out",
    "action_required",
    "error",
    "startup_failure",
}
_CHECK_GREEN = {"green", "pass", "passed", "success", "successful", "ok", "neutral", "skipped"}
# A Shuttle is already on the ticket. Do not send the same red check again.
_CHECK_BUSY = {"fix", "claimed", "planning", "coding", "recovering", "merging"}
# Nothing to send back to. A queued ticket has not run the check yet.
_CHECK_SKIP = {"merged", "done", "parked", "skipped", "queued", "blocked"}


def norm_check_name(value) -> str:
    """Fold a check command or CI check name. `make ci` stays two words."""
    return " ".join(str(value or "").strip().strip("\"'").casefold().split())


def is_make_ci(value) -> bool:
    return norm_check_name(value) == MAKE_CI


def check_command_text(text: str) -> str:
    """The checkCommand line from config yaml, including a two-word command.

    `checkCommand: make ci` is the command `make ci`. The space is part of
    the command. A comment on that line is not.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("checkCommand:"):
            continue
        raw = stripped.split(":", 1)[1]
        raw = raw.split("#", 1)[0].strip().strip("\"'")
        return raw
    return ""


def resolve_check_command(config: Optional[dict], yaml_text: Optional[str] = None) -> str:
    """Beam config, then the yaml line. An empty beam value does not hide `make ci`."""
    cmd = check_command(config)
    if cmd:
        return cmd
    return check_command_text(yaml_text or "")


def conclusion_kind(value) -> str:
    """`red`, `green`, or empty. The command name `make ci` is neither."""
    raw = str(value or "").strip().casefold()
    if raw in _CHECK_RED:
        return "red"
    if raw in _CHECK_GREEN:
        return "green"
    return ""


def _check_rows(ticket: dict) -> list:
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    rows = pr.get("checks")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _row_name(item: dict) -> str:
    return str(item.get("name") or item.get("context") or "").strip()


def _row_log(item: dict, name: str) -> str:
    for key in ("log", "output", "details"):
        text = item.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return "%s red" % (name or MAKE_CI)


def make_ci_result(ticket: dict, command: str) -> Optional[dict]:
    """The current `make ci` result, or None when this ticket has no such check.

    A provider check named `make ci`, or checkCommand `make ci`, counts.
    The words `make ci` are the name, not a failure, until a conclusion is red.
    """
    if not isinstance(ticket, dict):
        return None
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    command_is = is_make_ci(command) or is_make_ci(pr.get("checkName"))
    matched = [item for item in _check_rows(ticket) if is_make_ci(_row_name(item))]
    if matched:
        reds = [item for item in matched if conclusion_kind(item.get("conclusion") or item.get("state")) == "red"]
        if reds:
            item = reds[-1]
            name = _row_name(item) or MAKE_CI
            return {"name": MAKE_CI, "kind": "red", "log": _row_log(item, name)}
        greens = [item for item in matched if conclusion_kind(item.get("conclusion") or item.get("state")) == "green"]
        if greens:
            return {"name": MAKE_CI, "kind": "green", "log": ""}
        return None
    if not command_is:
        return None
    check_kind = conclusion_kind(pr.get("check"))
    ci_kind = conclusion_kind(pr.get("ci"))
    if check_kind == "red" or ci_kind == "red":
        log = pr.get("checkLog") or pr.get("ciLog") or "make ci red"
        return {"name": MAKE_CI, "kind": "red", "log": str(log)}
    if check_kind == "green" or ci_kind == "green":
        return {"name": MAKE_CI, "kind": "green", "log": ""}
    if conclusion_kind(pr.get("rollup")) == "red":
        log = pr.get("checkLog") or "make ci red"
        return {"name": MAKE_CI, "kind": "red", "log": str(log)}
    return None


def _store_make_ci(ticket: dict, result: dict, command: str) -> None:
    pr = ticket.get("pr")
    if not isinstance(pr, dict):
        pr = {}
        ticket["pr"] = pr
    pr["checkName"] = result["name"]
    if result["kind"] == "red":
        pr["check"] = "red"
        pr["checkLog"] = result["log"]
        return
    if result["kind"] == "green":
        pr["check"] = "green"
        pr.pop("checkLog", None)
        # That command's CI field was the red `make ci` result. It is green now.
        if is_make_ci(command) and conclusion_kind(pr.get("ci")) == "red":
            pr["ci"] = "green"


def send_back_red_checks(data: dict, yaml_text: str = "", only_id: Optional[str] = None) -> list:
    """Send a red `make ci` back to that ticket's Shuttle. One attempt per return.

    The log is the check output. Status `fix` keeps the slot and the locks.
    The third red parks. A Shuttle already on the ticket is left alone.
    A green `make ci` is recorded and is not a failure.
    """
    if not isinstance(data, dict):
        return []
    command = resolve_check_command(data.get("config") or {}, yaml_text)
    tickets = data.get("tickets") if isinstance(data.get("tickets"), dict) else {}
    actions = []
    for tid in sorted(tickets):
        if only_id and tid != only_id:
            continue
        ticket = tickets[tid]
        if not isinstance(ticket, dict):
            continue
        result = make_ci_result(ticket, command)
        if not result or not result.get("kind"):
            continue
        status = ticket.get("status")
        # Record green and red on merged tickets too. A green `make ci` clears
        # the stale CI field so a gate can advance. Merged work is not sent back.
        _store_make_ci(ticket, result, command)
        if result["kind"] != "red":
            continue
        if status in _CHECK_SKIP or status in _CHECK_BUSY:
            continue
        if status == "alarm" and str(ticket.get("alarm") or "") not in {"", "ci-red", "gate-red"}:
            continue
        outcome = note_failure(ticket, result["log"], data.get("config") or {})
        if outcome == "fix":
            ticket["agent"] = None
            ticket["alarm"] = None
        actions.append({"id": ticket.get("id") or tid, "outcome": outcome, "log": result["log"]})
    return actions


def format_send_back(actions: list, beam: dict) -> list:
    """`send-back <id> fix` and the start line for that Shuttle. Parked does not start."""
    cfg = (beam or {}).get("config") or {}
    tickets = (beam or {}).get("tickets") or {}
    launch = launch_for(cfg)
    extra = " launch=%s" % launch.get("launch")
    if launch.get("refuseInProcess"):
        extra += " refuse-in-process"
    kind = checkout_kind(cfg)
    lines = []
    if any(action.get("outcome") == "fix" for action in actions or []):
        instruction = (launch.get("instruction") or "").strip()
        if instruction:
            lines.append(instruction)
    for action in actions or []:
        tid = action.get("id")
        outcome = action.get("outcome")
        lines.append("send-back %s %s" % (tid, outcome))
        if outcome != "fix":
            continue
        ticket = tickets.get(tid) or {}
        lines.append("start %s checkout=%s branch=%s%s" % (tid, kind, branch_name(ticket, cfg), extra))
    return lines


def required_checks(config: Optional[dict]) -> dict:
    """The check the merge queue runs on a rebased head.

    An empty checkCommand keeps Bugbot and CI as configured. Warp does not
    invent a build command.
    """
    cmd = check_command(config)
    if not cmd:
        return {
            "mode": "bugbot-ci",
            "command": None,
            "detail": "checkCommand is unset; required checks are Bugbot and CI as configured",
        }
    return {"mode": "command", "command": cmd, "detail": "run checkCommand on the rebased head"}


def append_only_paths(config: Optional[dict]) -> list:
    raw = (config or {}).get("appendOnlyPaths")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [_norm(part) for part in text.split(",") if part.strip()]
    return [_norm(part) for part in raw if str(part).strip()]


def blockers(ticket: dict) -> list:
    """Dependency list plus the plan's after list. Order kept, duplicates dropped."""
    found = []
    for key in ("deps", "after", "blockedBy"):
        raw = ticket.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        for item in raw:
            dep = str(item).strip()
            if dep and dep not in found:
                found.append(dep)
    return found


def merged_on_base(status: Optional[str]) -> bool:
    """An open or green pull request is not merged. skipped is not merged."""
    return status in MERGED_ON_BASE


def lock_paths(ticket: dict) -> list:
    """The ticket's full lock list. Display code may shorten this. Ready must not."""
    raw = ticket.get("locks") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw:
        path = _norm(item)
        if path and path not in out:
            out.append(path)
    return out


def _norm(path) -> str:
    return str(path).strip().replace("\\", "/").rstrip("/")


def bugbot_applies(ticket: dict, config: Optional[dict]) -> bool:
    cfg = config or {}
    if not _flag(cfg.get("bugbotRequired"), True):
        return False
    if not ticket.get("autoMerge") and not _flag(cfg.get("bugbotManual"), True):
        return False
    return True


def provider_rollup(ticket: dict) -> str:
    """GitHub or Bitbucket check rollup stored on the pull request.

    Empty when the provider has not reported one yet. `green` is the only
    value that frees a slot. Bugbot is one of those checks, after the push.
    """
    pr = ticket.get("pr") or {}
    raw = str(pr.get("rollup") or "").strip().casefold()
    if raw in {"success", "pass"}:
        return "green"
    if raw in {"failure", "failed", "fail"}:
        return "red"
    if raw in {"green", "red", "pending"}:
        return raw
    return ""


def checks_green(ticket: dict, config: Optional[dict]) -> bool:
    """Required checks for freeing a slot and for entering the merge queue.

    A provider rollup, when present, is the check. The slot stays occupied
    until that rollup is green. When no rollup has been recorded, CI must be
    green and Bugbot must pass when it applies, or `pr.check` when
    checkCommand is set. Bugbot runs on the pull request after push.
    """
    pr = ticket.get("pr") or {}
    rollup = provider_rollup(ticket)
    if rollup:
        return rollup == "green"
    if check_command(config):
        return str(pr.get("check") or "").strip().casefold() == "green"
    if str(pr.get("ci") or "").strip().casefold() != "green":
        return False
    if not bugbot_applies(ticket, config):
        return True
    return str(pr.get("bugbot") or "").strip().casefold() == "pass"


def bugbot_where() -> str:
    """Bugbot runs on the pull request after push, not inside the VM."""
    return "pull-request"


def note_pr_opened(ticket: dict, url: str, when: Optional[str] = None) -> None:
    """The Shuttle exited with a pull request. That does not free the slot."""
    pr = ticket.setdefault("pr", {})
    if url:
        pr["url"] = url
    if when and not pr.get("openedAt"):
        pr["openedAt"] = when
    pr["agentFinished"] = True
    pr["opened"] = True


def holds_slot(ticket: dict, config: Optional[dict] = None) -> bool:
    """True while the ticket still occupies a maxAgents slot.

    Opening a pull request means the Shuttle finished. The slot stays occupied
    until the provider check rollup is green. review with a green rollup,
    awaiting_approval, and merging do not hold a slot.
    """
    status = ticket.get("status")
    if status in SLOT_HELD:
        return True
    if status == "review" and not checks_green(ticket, config):
        return True
    return False


def holds_locks(ticket: dict) -> bool:
    """Locks stay until the PR is merged or the ticket is parked."""
    return ticket.get("status") in LOCK_HELD


def is_starred(ticket: dict) -> bool:
    if ticket.get("starred") or ticket.get("star") or ticket.get("critical"):
        return True
    return False


def _priority_key(ticket: dict):
    raw = ticket.get("priority")
    if raw is None or raw is False:
        return (1, 0)
    if isinstance(raw, str) and raw.strip().casefold() in {"", "normal", "none", "false"}:
        return (1, 0)
    if isinstance(raw, bool):
        return (0, 0)
    if isinstance(raw, (int, float)):
        return (0, int(raw))
    text = str(raw).strip()
    if text.isdigit():
        return (0, int(text))
    return (0, 0)


def _rank_key(ticket: dict):
    raw = ticket.get("rank")
    if raw is not None and str(raw).strip() != "":
        try:
            return (0, int(raw))
        except (TypeError, ValueError):
            pass
    # No explicit rank: Warp's rankDays score, higher means sooner.
    try:
        days = int(ticket.get("rankDays") or 0)
    except (TypeError, ValueError):
        days = 0
    return (1, -days)


def sort_key(ticket: dict, base_red: bool = False):
    """Starred first, then priority, then lowest rank.

    A base fix sorts ahead of every rank while the base branch is red.
    """
    fix = 0 if base_red and ticket.get("baseFix") else 1
    star = 0 if is_starred(ticket) else 1
    pri_bucket, pri_value = _priority_key(ticket)
    rank_bucket, rank_value = _rank_key(ticket)
    return (fix, star, pri_bucket, pri_value, rank_bucket, rank_value, str(ticket.get("id") or ""))


def base_is_green(beam: dict) -> bool:
    return beam.get("baseGreen") is not False


def needs_base_fix(beam: dict) -> bool:
    """True when the base is red and no ticket is already the fix."""
    if base_is_green(beam):
        return False
    for ticket in (beam.get("tickets") or {}).values():
        if not ticket.get("baseFix"):
            continue
        if holds_slot(ticket, beam.get("config")) or ticket.get("status") in {"queued", "blocked"}:
            return False
    return True


def checkout_kind(config: Optional[dict]) -> str:
    """One isolated checkout. Cloud uses its own VM. Local uses its own worktree."""
    runner = str((config or {}).get("runner") or "cloud").strip().casefold()
    if runner == "local":
        return "worktree"
    return "cloud-vm"


def branch_name(ticket: dict, config: Optional[dict] = None) -> str:
    """Keep Warp's warp/<id> name. Do not force a feature/ prefix.

    An existing branch on the ticket is kept. baseBranch is where it starts.
    """
    existing = ticket.get("branch")
    if existing:
        return str(existing)
    tid = ticket.get("id")
    key = ticket.get("jiraKey")
    if key:
        return "warp/%s-%s" % (tid, key)
    return "warp/%s" % tid


def _nonempty_record(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        if str(value.get("body") or "").strip():
            return True
        if value.get("blockers") or value.get("decisions") or value.get("summary"):
            return True
        return False
    return bool(value)


def records_present(ticket: dict) -> bool:
    """The PR has to carry a plan record and a result record."""
    return _nonempty_record(ticket.get("plan")) and _nonempty_record(ticket.get("result"))


def ensure_plan(ticket: dict) -> dict:
    plan = ticket.get("plan")
    if isinstance(plan, dict):
        plan.setdefault("blockers", [])
        plan.setdefault("decisions", [])
        plan.setdefault("body", plan.get("body") or "")
        return plan
    body = plan if isinstance(plan, str) else ""
    plan = {"body": body, "blockers": [], "decisions": []}
    ticket["plan"] = plan
    return plan


def note_failure(ticket: dict, output: str, config: Optional[dict] = None) -> str:
    """Red PR or red after rebase. The third failure parks the ticket.

    Attempts 1 and 2 go back to that ticket's Agent on the same branch (status fix)
    and keep the locks. The third releases the locks by leaving the active
    set, leaves the branch and the pull request open, and writes the blocker
    on the plan record.
    """
    cfg = config or {}
    try:
        cap = int(cfg.get("maxFixAttempts") or 3)
    except (TypeError, ValueError):
        cap = 3
    if cap < 1:
        cap = 1
    ticket["attempts"] = int(ticket.get("attempts") or 0) + 1
    attempt = int(ticket["attempts"])
    plan = ensure_plan(ticket)
    parked = attempt >= cap
    plan["blockers"].append({"attempt": attempt, "output": output, "parked": parked})
    if parked:
        ticket["status"] = "parked"
        ticket["alarm"] = "parked"
        ticket["agent"] = None
        return "parked"
    ticket["status"] = "fix"
    return "fix"


def record_notes(ticket: dict, plan: Optional[str] = None, result: Optional[str] = None) -> None:
    if plan is not None:
        body = ensure_plan(ticket)
        body["body"] = plan
    if result is not None:
        current = ticket.get("result")
        if not isinstance(current, dict):
            current = {}
        current["body"] = result
        ticket["result"] = current


def _proceeded(ticket: dict) -> bool:
    pr = ticket.get("pr") or {}
    return bool(pr.get("proceededBy") or pr.get("approvedBy") or pr.get("approvedAt"))


def in_merge_queue(ticket: dict, config: Optional[dict]) -> bool:
    """Sizes outside autoMergeSizes stay out until a person proceeds.

    Sizes in the list enter when required checks are green and the plan and
    result records are on the ticket. Sub-agents never put themselves here
    by merging.
    """
    if ticket.get("status") not in {"review", "merging"}:
        return False
    if not checks_green(ticket, config):
        return False
    if not records_present(ticket):
        return False
    if ticket.get("autoMerge"):
        return True
    return _proceeded(ticket)


def merge_queue(beam: dict) -> list:
    """Serial merge order: starred first, then lowest rank. Empty when the base is red."""
    if not base_is_green(beam):
        return []
    cfg = beam.get("config") or {}
    rows = [t for t in (beam.get("tickets") or {}).values() if in_merge_queue(t, cfg)]
    rows.sort(key=lambda t: sort_key(t, base_red=False))
    return rows


def stack_groups(queue: list, red: bool = False) -> list:
    """Stack auto-merge PRs in rank order, or one-by-one after a red stack.

    One group is one candidate. The orchestrator runs the check once for a
    stack. On red, the next pass uses one pull request per group.
    """
    if not queue:
        return []
    if red:
        return [[ticket] for ticket in queue]
    return [list(queue)]


def next_merge(beam: dict, stacked_red: bool = False) -> Optional[dict]:
    """One merge candidate. None when the base is red or the queue is empty."""
    if not base_is_green(beam):
        return None
    groups = stack_groups(merge_queue(beam), red=stacked_red)
    if not groups:
        return None
    group = groups[0]
    return {
        "steps": list(MERGE_STEPS),
        "ids": [t.get("id") for t in group],
        "serial": True,
        "stacked": len(group) > 1,
    }


def is_append_only(path: str, paths: list) -> bool:
    wanted = _norm(path)
    return bool(wanted) and wanted in {_norm(p) for p in paths}


def _lines(text: str) -> list:
    if text is None:
        return []
    return str(text).splitlines()


def added_lines(base: str, side: str) -> list:
    """Lines the side added. Base lines stay. Deletions are ignored."""
    base_lines = _lines(base)
    added = []
    index = 0
    for line in _lines(side):
        if index < len(base_lines) and line == base_lines[index]:
            index += 1
        else:
            added.append(line)
    return added


def merge_append(base: str, ours: str, theirs: str) -> str:
    """Keep the base and every line either side added. Do not reorder the base."""
    lines = _lines(base) + added_lines(base, ours) + added_lines(base, theirs)
    trailing = ""
    for text in (base, ours, theirs):
        if text and str(text).endswith("\n"):
            trailing = "\n"
            break
    return "\n".join(lines) + trailing


def resolve_file(path: str, base: str, ours: str, theirs: str, append_only: list) -> dict:
    if not is_append_only(path, append_only):
        return {"action": "send-back", "path": _norm(path)}
    return {"action": "resolved", "path": _norm(path), "text": merge_append(base, ours, theirs)}


def resolve_conflicts(files: list, append_only: list) -> dict:
    """Append-only files keep both sides. Any other conflict sends the ticket back.

    A mixed set is send-back. The other files are not hand-merged.
    """
    results = []
    for item in files:
        results.append(
            resolve_file(
                item.get("path") or "",
                item.get("base") or "",
                item.get("ours") or "",
                item.get("theirs") or "",
                append_only,
            )
        )
    sent = [row for row in results if row["action"] == "send-back"]
    if sent:
        return {"action": "send-back", "paths": [row["path"] for row in sent]}
    return {"action": "resolved", "files": results}


def classify_ticket(ticket: dict, fact: Optional[dict]) -> str:
    """Restart class from git, not from a stale beam status.

    merged: the work is on the base branch.
    in-flight: an open branch or an open pull request.
    pending: everything else.
    """
    fact = fact or {}
    if fact.get("on_base"):
        return "merged"
    if fact.get("open_branch") or fact.get("open_pr"):
        return "in-flight"
    return "pending"


def apply_class(ticket: dict, klass: str, fact: Optional[dict] = None) -> None:
    """Write the class onto the ticket. An explicit park stays parked."""
    fact = fact or {}
    if klass == "merged":
        if ticket.get("status") != "done":
            ticket["status"] = "merged"
        return
    if ticket.get("status") == "parked":
        return
    if klass == "in-flight":
        if ticket.get("status") not in LOCK_HELD:
            ticket["status"] = "review" if fact.get("open_pr") else "claimed"
        return
    if ticket.get("status") not in {"skipped", "alarm"}:
        ticket["status"] = "queued"
        ticket["agent"] = None


def rebuild(beam: dict, facts: dict) -> dict:
    """Classify every ticket and apply it. Returns id -> class."""
    classes = {}
    facts = facts or {}
    for tid, ticket in (beam.get("tickets") or {}).items():
        fact = facts.get(tid) or {}
        klass = classify_ticket(ticket, fact)
        classes[tid] = klass
        apply_class(ticket, klass, fact)
    return classes


def parked_blocks(beam: dict) -> list:
    """Parked tickets and the ids whose dependency or after list names them."""
    tickets = list((beam.get("tickets") or {}).values())
    rows = []
    for ticket in tickets:
        if ticket.get("status") != "parked":
            continue
        tid = ticket.get("id")
        blocks = []
        for other in tickets:
            if other.get("id") == tid:
                continue
            if tid in blockers(other):
                blocks.append(other.get("id"))
        rows.append(
            {
                "id": tid,
                "branch": ticket.get("branch"),
                "pr": (ticket.get("pr") or {}).get("url"),
                "blocks": blocks,
                "blocker": _latest_blocker(ticket),
            }
        )
    rows.sort(key=lambda row: str(row.get("id")))
    return rows


def _latest_blocker(ticket: dict):
    plan = ticket.get("plan") if isinstance(ticket.get("plan"), dict) else {}
    blockers_list = plan.get("blockers") or []
    if not blockers_list:
        return None
    last = blockers_list[-1]
    if isinstance(last, dict):
        return last.get("output")
    return str(last)


def program_done(beam: dict) -> bool:
    """Every ticket merged or explicitly parked, and the base branch green.

    skipped is an explicit decision not to build that ticket. There is no
    ticket-count target.
    """
    tickets = list((beam.get("tickets") or {}).values())
    if not tickets:
        return False
    if not base_is_green(beam):
        return False
    return all((t.get("status") in SETTLED) for t in tickets)


def merge_queue_configured(config: Optional[dict]) -> bool:
    """config mergeQueue. Default false. Absent is false."""
    return _flag((config or {}).get("mergeQueue"), False)


def merge_queue_active(config: Optional[dict], detected: Optional[bool] = None) -> bool:
    """Enqueue when config says so, or when the provider reports a merge queue."""
    if merge_queue_configured(config):
        return True
    return detected is True


def merge_report(config: Optional[dict], detected_queue: Optional[bool] = None, protection_reject: bool = False, succeeded: bool = False) -> str:
    """What to record after a merge attempt.

    enqueue and rejected do not count as merged. A direct merge that branch
    protection rejects is rejected.
    """
    if merge_queue_active(config, detected_queue):
        return "enqueue"
    if protection_reject or not succeeded:
        return "rejected"
    return "merged"


def apply_reported_merge(ticket: dict, outcome: str, sha: Optional[str] = None) -> bool:
    """Set status merged only when the provider actually merged."""
    pr = ticket.setdefault("pr", {})
    if outcome != "merged":
        pr["mergeAttempt"] = outcome
        return False
    ticket["status"] = "merged"
    if sha:
        pr["sha"] = sha
    return True


TICKET_AGENT_INSTRUCTION = (
    "Start one new Agent for this ticket. "
    "An Agent is a separate top-level cloud agent. "
    "Own conversation, own VM, own checkout. "
    "Clone main so .cursor rules load, then create the branch from that main. "
    "The claim is in the IMPLEMENT prompt. "
    "A beam file does not have to exist on the branch before the Agent starts. "
    "Do not start a Subagent. "
    "A Subagent is a child spawned inside the orchestrator's turn (Task / subagent). "
    "It shares the parent session and checkout. Forbidden for tickets. "
    "Do not use the Task tool for a ticket. "
    "Do not implement the ticket in this turn."
)

LOCAL_WORKTREE_INSTRUCTION = (
    "One git worktree per ticket on the ticket branch that already has .warp/beam.json. "
    "Do not start a Subagent inside the orchestrator checkout. "
    "Do not use the Task tool for a ticket. "
    "Do not implement the ticket in this turn."
)


def _joined(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def acceptance_text(ticket: dict) -> str:
    """Acceptance criteria from acs, or acceptance when that is what the plan stored."""
    raw = ticket.get("acs")
    if raw is None:
        raw = ticket.get("acceptance")
    if isinstance(raw, list):
        return " | ".join(str(item) for item in raw)
    if raw is None:
        return ""
    return str(raw)


def implement_prompt(ticket: dict, config: Optional[dict] = None, branch_state: str = "missing") -> str:
    """IMPLEMENT prompt for one new Agent. The branch does not have to exist yet.

    branch_state is missing, beam-only, or work. A beam-only branch is the
    stale snapshot Shawn's run left behind. The Agent starts from latest main
    and does not keep that commit as its working tree. Work beyond the beam
    stays on that branch.
    """
    tid = str(ticket.get("id") or "")
    branch = branch_name(ticket, config)
    if branch_state == "work":
        clone = (
            "Clone main so .cursor rules load, then check out %s. "
            "That branch has work beyond the beam commit. Do not reset it to main."
        ) % branch
    elif branch_state == "beam-only":
        clone = (
            "Clone main so .cursor rules load. "
            "Start from latest main. Do not keep that stale beam as your working tree. "
            "Create %s from that main."
        ) % branch
    else:
        clone = "Clone main so .cursor rules load, then create %s from that main." % branch
    lines = [
        "IMPLEMENT %s" % tid,
        "ticket: %s" % tid,
        "jira: %s" % (ticket.get("jiraKey") or ""),
        "locks: %s" % _joined(ticket.get("locks")),
        "acceptance: %s" % acceptance_text(ticket),
        "branch: %s" % branch,
        TICKET_AGENT_INSTRUCTION,
        clone,
        "Write .warp/tickets/%s/ and push it. The parent reads that folder later." % tid,
        "A beam file does not have to exist on the branch before the Agent starts.",
        (
            "If %s already exists and is only the beam commit, start from latest main. "
            "Do not keep that stale beam as your working tree."
        )
        % branch,
    ]
    if branch_state == "beam-only":
        lines.append("stale-beam: %s" % branch)
    elif branch_state == "work":
        lines.append("branch-kept: %s" % branch)
    return "\n".join(lines)


# Claimed work, review, checks still running, and the queue. The listener
# dies with the parent turn, so these keep the turn open. awaiting_approval
# is waiting on warp:proceed, which only the listener can hear.
_PARENT_STAY = {
    "queued",
    "claimed",
    "recovering",
    "planning",
    "coding",
    "fix",
    "review",
    "bugbot_running",
    "awaiting_approval",
    "merging",
}
_CHECK_PENDING = {"pending", "red", "failure", "failed", "in_progress", "inprogress", "queued"}


def ticket_keeps_parent(ticket: dict) -> bool:
    """True when this ticket is claimed, in review, awaiting checks, or queued."""
    if not isinstance(ticket, dict):
        return False
    status = ticket.get("status")
    if status in _PARENT_STAY:
        return True
    if status in SETTLED or status in {"blocked", "alarm"}:
        return False
    pr = ticket.get("pr") if isinstance(ticket.get("pr"), dict) else {}
    rollup = str(pr.get("rollup") or "").strip().casefold()
    ci = str(pr.get("ci") or "").strip().casefold()
    return rollup in _CHECK_PENDING or ci in _CHECK_PENDING


def parent_may_exit(beam: dict) -> bool:
    """The parent may end the turn only when the listener is allowed to stop.

    Pause and stop may exit even if tickets are still claimed. A running beam
    stays up while any ticket is claimed, in review, awaiting checks, or queued.
    A launch refusal is not an idle beam.
    """
    if beam.get("paused") or (beam.get("runState") or "running") in {"paused", "stopped"}:
        return True
    for ticket in (beam.get("tickets") or {}).values():
        if ticket_keeps_parent(ticket):
            return False
    return True


def launch_for(config: Optional[dict]) -> dict:
    """How the Warp session starts one ticket. Never in this process.

    Cloud starts one new Agent with an IMPLEMENT prompt. The branch does not
    have to exist yet. Local is one git worktree. Neither path is a Subagent
    or the Task tool.
    """
    kind = checkout_kind(config)
    if kind == "worktree":
        return {
            "launch": "worktree",
            "refuseInProcess": True,
            "instruction": LOCAL_WORKTREE_INSTRUCTION,
        }
    return {
        "launch": "new-agent",
        "refuseInProcess": True,
        "instruction": TICKET_AGENT_INSTRUCTION,
    }


def format_dispatch(data: dict) -> list:
    """Lines the dispatch tick prints. Empty when nothing can start."""
    import beam as beam_mod

    rows = beam_mod.ready(data)
    cap = beam_mod.configured_cap(data.get("config") or {})
    actions = dispatch_actions(data, rows, cap=cap)
    lines = []
    seen = set()
    for action in actions:
        instruction = (action.get("instruction") or "").strip()
        if instruction and instruction not in seen:
            lines.append(instruction)
            seen.add(instruction)
    for action in actions:
        extra = " launch=%s" % action.get("launch")
        if action.get("refuseInProcess"):
            extra += " refuse-in-process"
        if action["action"] == "dispatch-base-fix":
            lines.append("dispatch-base-fix checkout=%s%s" % (action["checkout"], extra))
        else:
            lines.append(
                "start %s checkout=%s branch=%s%s" % (action["id"], action["checkout"], action["branch"], extra)
            )
    return lines


def dispatch_actions(beam: dict, ready_rows: list, cap: Optional[int] = None) -> list:
    """What to start after a dispatch loop: base fix first, then ready tickets.

    ready_rows is the list beam.ready already capped. A red base reserves one
    slot inside ready() when no fix ticket exists, so this prepends that fix
    and does not add a second one. cap is maxAgents. When it is omitted, the
    config value is used. An unset cap does not invent a slot.
    """
    if (beam.get("runState") or "running") in {"paused", "stopped"} or beam.get("paused"):
        return []
    cfg = beam.get("config") or {}
    if cap is None:
        cap = agent_cap(cfg)
    actions = []
    launch = launch_for(cfg)
    if needs_base_fix(beam) and cap is not None:
        used = sum(1 for t in (beam.get("tickets") or {}).values() if holds_slot(t, cfg))
        if used < cap:
            actions.append(
                {
                    "action": "dispatch-base-fix",
                    "aheadOfRank": True,
                    "checkout": checkout_kind(cfg),
                    **launch,
                }
            )
    for ticket in ready_rows:
        action = {
            "action": "start",
            "id": ticket.get("id"),
            "branch": branch_name(ticket, cfg),
            "checkout": checkout_kind(cfg),
            "base": (cfg.get("baseBranch") or "") or "base",
            **launch,
        }
        if action.get("launch") == "new-agent":
            action["instruction"] = implement_prompt(ticket, cfg)
        actions.append(action)
    return actions


def shared_branches(beam: dict) -> list:
    """Two in-flight tickets on one branch. The orchestrator must not start that."""
    seen = {}
    bad = []
    for ticket in (beam.get("tickets") or {}).values():
        if not holds_locks(ticket):
            continue
        branch = ticket.get("branch")
        if not branch:
            continue
        if branch in seen:
            bad.append((seen[branch], ticket.get("id"), branch))
        else:
            seen[branch] = ticket.get("id")
    return bad


HELP = __doc__


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _save(path: Path, beam: dict) -> None:
    import beam as beam_mod

    beam_mod.atomic_write(path, json.dumps(beam, indent=2) + "\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Warp orchestrator", epilog=HELP)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pq = sub.add_parser("queue")
    pq.add_argument("--beam", required=True)
    pq.add_argument("--stacked-red", action="store_true")

    pf = sub.add_parser("fail")
    pf.add_argument("--beam", required=True)
    pf.add_argument("--id", required=True)
    pf.add_argument("--output", required=True)

    prb = sub.add_parser("rebuild")
    prb.add_argument("--beam", required=True)
    prb.add_argument("--facts", required=True, help="JSON object of id to {on_base, open_branch, open_pr}")

    pres = sub.add_parser("resolve")
    pres.add_argument("--path", required=True)
    pres.add_argument("--append-only", action="append", default=[])
    pres.add_argument("--base", default="")
    pres.add_argument("--ours", default="")
    pres.add_argument("--theirs", default="")

    pd = sub.add_parser("dispatch")
    pd.add_argument("--beam", required=True)

    px = sub.add_parser("parent-exit")
    px.add_argument("--beam", required=True)

    prec = sub.add_parser("record")
    prec.add_argument("--beam", required=True)
    prec.add_argument("--id", required=True)
    prec.add_argument("--plan")
    prec.add_argument("--result")

    pop = sub.add_parser("opened")
    pop.add_argument("--beam", required=True)
    pop.add_argument("--id", required=True)
    pop.add_argument("--pr", required=True)

    pm = sub.add_parser("merge")
    pm.add_argument("--beam", required=True)
    pm.add_argument("--id", required=True)
    pm.add_argument("--outcome", required=True, choices=["merged", "enqueue", "rejected"])
    pm.add_argument("--sha")

    import usage

    args = parser.parse_args(usage.normalize_argv(argv))
    if args.cmd == "queue":
        data = _load(Path(args.beam))
        nxt = next_merge(data, stacked_red=bool(args.stacked_red))
        if not nxt:
            print("(none)")
            return 0
        print(" ".join(nxt["ids"]))
        print("steps: %s" % " ".join(nxt["steps"]))
        return 0
    if args.cmd == "fail":
        path = Path(args.beam)
        data = _load(path)
        ticket = (data.get("tickets") or {}).get(args.id)
        if not ticket:
            sys.exit("unknown ticket %s" % args.id)
        outcome = note_failure(ticket, args.output, data.get("config") or {})
        _save(path, data)
        print("%s %s" % (args.id, outcome))
        return 0
    if args.cmd == "rebuild":
        path = Path(args.beam)
        data = _load(path)
        facts = json.loads(Path(args.facts).read_text())
        classes = rebuild(data, facts)
        _save(path, data)
        for tid in sorted(classes):
            print("%s %s" % (tid, classes[tid]))
        return 0
    if args.cmd == "resolve":
        result = resolve_file(args.path, args.base, args.ours, args.theirs, args.append_only)
        print(result["action"])
        if result["action"] == "send-back":
            print(result["path"])
            return 0
        sys.stdout.write(result["text"])
        if not result["text"].endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if args.cmd == "dispatch":
        import beam as beam_mod

        path = Path(args.beam)
        herald, data = beam_mod.refresh_pending_gates(path, run_dispatch=False)
        for line in herald:
            print(line)
        if not data:
            data = _load(path)
        for line in format_dispatch(data):
            print(line)
        return 0
    if args.cmd == "parent-exit":
        data = _load(Path(args.beam))
        if parent_may_exit(data):
            print("parent: exit")
        else:
            print("parent: stay")
        return 0
    if args.cmd == "record":
        path = Path(args.beam)
        data = _load(path)
        ticket = (data.get("tickets") or {}).get(args.id)
        if not ticket:
            sys.exit("unknown ticket %s" % args.id)
        record_notes(ticket, plan=args.plan, result=args.result)
        _save(path, data)
        print("%s recorded" % args.id)
        return 0
    if args.cmd == "opened":
        path = Path(args.beam)
        data = _load(path)
        ticket = (data.get("tickets") or {}).get(args.id)
        if not ticket:
            sys.exit("unknown ticket %s" % args.id)
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        note_pr_opened(ticket, args.pr, stamp)
        if ticket.get("status") in {"claimed", "recovering", "planning", "coding", "fix"}:
            ticket["status"] = "review"
        _save(path, data)
        print("%s pr opened %s" % (args.id, args.pr))
        print("slot: held until rollup green")
        return 0
    if args.cmd == "merge":
        path = Path(args.beam)
        data = _load(path)
        ticket = (data.get("tickets") or {}).get(args.id)
        if not ticket:
            sys.exit("unknown ticket %s" % args.id)
        applied = apply_reported_merge(ticket, args.outcome, sha=args.sha)
        _save(path, data)
        if applied:
            print("%s merged" % args.id)
            return 0
        print("%s %s" % (args.id, args.outcome))
        print("not merged")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
