---
name: warp-dispatch
description: "Compute the ready set and claim tickets under lock, gate, and agent caps. Use when Warp is choosing the next Shuttles to start."
---

# Warp dispatch

`scripts/beam.py ready` is the only ready-set. Do not reimplement it in the chat.

Run `python3 <plugin>/scripts/resume_hint.py --beam .warp/beam.json` before the first claim. If the beam is paused or missing, do not claim. When a Shuttle you spawned returns, run `python3 <plugin>/scripts/session_note.py --type subagent-stop --beam .warp/beam.json`, then reconcile that ticket from the beam before the next claim.

## Rules the script enforces

- Paused beam returns nothing.
- Deps must be `merged`, `done`, or `skipped`.
- A blocking gate that is not green blocks any non-member with that gate's member in its ancestor set. G0 members can run; tickets that depend on L-01, M-01, or D-01 cannot, until G0 is green.
- Active lock paths must not overlap, including prefix (`internal/app` blocks `internal/app/stub`).
- Cap is `maxAgents` minus active. There is no per-person cap.
- Order is critical path, then `rankDays` descending, then id.

## Claim

For each id `ready` prints, claim it, then start a Shuttle in this same workspace (local agent or cloud agent on this clone). The kickoff is only:

```
IMPLEMENT A-03
```

Use the real id. Do not paste the rules into the prompt. The Shuttle reads `.cursor/rules`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` because its workspace is the repo. See `assets/KICKOFF.md`.

```bash
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json \
  --id A-03 --status claimed --agent shuttle-A-03 --branch warp/A-03
```

`beam.py set` prints `jira: MUST DO` and writes `.warp/jira-todo.json` when the ticket has a Jira key (including one inferred from the id, summary, or branch). Do those actions, or leave them to the Shuttle, which checks `jira.startedAt` and `jira.comments` so the move and the claim comment happen once. A ticket with no key prints "no Jira move". Neither case blocks the claim. A failed Jira call is recorded and posted by Herald. It does not unclaim the ticket, except on the first claimed ticket of the run while `jiraTransition` is true. First ticket means no ticket has `jira.startedAt` yet (nothing in this run was linked and moved to In Progress). If Jira says the issue was not found, or no real issue key can be resolved, the script releases that claim and stops the run. Post only that Herald payload. Do not also say the claim still stands, and do not start the Shuttle. A later miss, after one ticket has `jira.startedAt`, stays a per-ticket alarm and does not stop the run. `jiraTransition: false` does not stop the run.

If the spawn fails, set status back to `queued` and clear `agent` before claiming another id that shares locks. The Jira issue is left in In Progress unless `jiraRestoreOnRelease` is true, in which case `beam.py set` prints a `move back to` instruction.

Run `scripts/provider.py resolve` before the first claim in a tick and pass its mode to the Shuttles. Local mode means they do not push.

## Windows

`respectMergeWindows` defaults false so dispatch runs all day and night. Merge windows `08:30`, `13:00`, `17:00` are digest times unless the yaml sets `respectMergeWindows: true`. Auto-merge does not wait for a window.

## When the ready set stays empty

If nothing is queued or active (every ticket is merged, done, skipped, blocked, or alarmed), or the run was stopped, the completion report should already exist. `reportOnComplete` (default true) writes it from `beam.py set` and from `scan.py stop`. If `.warp/warp-complete.html` is missing, run:

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
```

Post the Herald payload it writes. The file is gitignored and stays on this machine.

## Do not

- Borrow a lock from a running ticket.
- Start a gate-dependent ticket because the member PR is open. Open is not merged. Merged is not green, for a gate.
