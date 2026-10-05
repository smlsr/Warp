---
name: warp-dispatch
description: "Compute the ready set and claim tickets under lock, gate, and agent caps. Use when Warp is choosing the next Shuttles to start."
---

# Warp dispatch

`scripts/beam.py ready` is the only ready-set. Do not reimplement it in the chat.

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

`beam.py set` prints a `jira:` line. For a ticket with a Jira key it asks you to move the issue to In Progress through the Jira MCP server (see `skills/shuttle-run`); do it, or leave it to the Shuttle, which checks `jira.startedAt` first so the move happens once. A ticket without a key prints "no Jira move". Neither case blocks the claim.

If the spawn fails, set status back to `queued` and clear `agent` before claiming another id that shares locks. The Jira issue is left in In Progress unless `jiraRestoreOnRelease` is true, in which case `beam.py set` prints a `move back to` instruction.

Run `scripts/provider.py resolve` before the first claim in a tick and pass its mode to the Shuttles. Local mode means they do not push.

## Windows

`respectMergeWindows` defaults false so dispatch runs all day and night. Merge windows `08:30`, `13:00`, `17:00` are digest times unless the yaml sets `respectMergeWindows: true`. Auto-merge does not wait for a window.

## Do not

- Borrow a lock from a running ticket.
- Start a gate-dependent ticket because the member PR is open. Open is not merged. Merged is not green, for a gate.
