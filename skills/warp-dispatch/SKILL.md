---
name: warp-dispatch
description: "Compute the ready set and claim tickets under lock, gate, and agent caps. Use when Warp is choosing the next Shuttles to start."
---

# Warp dispatch

`scripts/beam.py ready` is the only ready-set. Do not reimplement it in the chat. It recomputes every pending gate first. A gate turns green when every member is merged or done. Post `herald: <gate> pending cleared. Members merged. Tick ran.` when that line is present. `start` lines are the same ids as the table. Claim each id once.

Run `python3 <plugin>/scripts/resume_hint.py --beam .warp/beam.json` before the first claim. If the beam is paused or missing, do not claim. Run `python3 <plugin>/scripts/beam.py watchdog --beam .warp/beam.json` before `ready()`. For each `shuttle: replace <id>` line, start exactly one Shuttle for that same ticket. Status is `recovering`. Keep the branch, pull request, `jira.startedAt`, and locks. Do not queue a duplicate and do not release the lock. A second tick must not start a second Shuttle for that id. `shuttle: alive` and `shuttle: fresh` are not new work. `shuttle: alarm <id> worker-died` means `maxRecoveries` is spent: do not start another. `watchdog: skipped` means the run is paused or stopped: do not recover. When that ticket's subagent finishes, reconcile it from the beam before the next claim. `python3 <plugin>/scripts/session_note.py --type subagent-stop --beam .warp/beam.json` records the finish. That journal line does not launch a ticket.

## Rules the script enforces

- Paused beam returns nothing.
- Deps must be `merged`, `done`, or `skipped`.
- A blocking gate that is not green blocks any non-member with that gate's member in its ancestor set. G0 members can run; tickets that depend on L-01, M-01, or D-01 cannot, until G0 is green.
- Active lock paths must not overlap, including prefix (`internal/app` blocks `internal/app/stub`).
- Cap is `maxAgents` minus active. There is no per-person cap.
- Order is starred (including the critical path), then priority, then lowest `rank`. When the plan has no `rank`, a higher `rankDays` starts first.
- The cap is `maxAgents`. A project may set 18. A pull request keeps the slot until the provider check rollup is green. It keeps its locks until merge or park.
- `subagentVm` defaults to false. `runner: local` forces it false and forces `launch` to `worktree`. `memoryCheck` defaults to false, so only that cap applies; `true` lets `free -g` lower it. On a cloud runner, `true` starts one subagent per ticket on its own dedicated VM, in parallel up to `maxAgents`. A same hostname, or `subagentVm: false`, is one worktree per ticket. Start one subagent per ticket in its own worktree, in parallel up to `maxAgents`. That is the one cap on agents that run at once, here or on their own VMs. Never two workers in one worktree. Never two workers on one ticket.

Before the first claim of a running cloud session, `prompt_gate.py check` must print `prompt-gate: ok`. A missing tool means do not claim. `refuse: no cloud environment` means the same: do not claim, unless the beam has `promptForced` from `scan.py start --force`. A local runner does not block. Cloud subagents use the MCP servers at cursor.com/agents, not this session.

## Claim

For each id `ready` prints, claim it, then start that ticket in its own checkout. Do not implement it here.

Claim the ticket first. Then run `checkout.py launch`. It is the only place a Shuttle prompt comes from, and it records the Shuttle in `.warp/agents.json` before it prints. `spawn: closed <id>` and exit 2 mean the run is paused, stopped, or finished, or the ticket is merged or parked: no prompt was printed, so start nothing. Lines above `prompt: pass every line below this one to the subagent, and nothing above it` are for you. One of them is `name: [warp:<instance>] <ticket> <step>`: give the subagent exactly that name. Pass the subagent only the lines below the mark. When `subagentVm` is true it prints the dedicated-VM prompt. The subagent fetches the latest main, clones it, and creates `warp/<id>-<jira>` itself. Its first step is `hostname` and `free -g`. It writes `.warp/tickets/<id>/` on that branch and pushes. When `subagentVm` is false, or the hostname matches this machine, launch fetches origin and adds `<worktreeRoot>/<id>` on `warp/<id>-<jira>` from `origin/main`. Start one subagent in that worktree with the prompt it prints. The prompt names the ticket, Jira key, locks, acceptance, branch, and absolute worktree path. The subagent works only inside that path. It commits, pushes, and opens or updates the pull request. It runs `checkCommand` and reports every acceptance criterion. It never merges, never touches the parent checkout, and never calls Jira or Slack. It never starts an agent, sets a timer, or waits on the pull request. It returns one line. `result: <id> stopped <reason>` is a clean stop after `reap: exit`: do not relaunch that ticket because of it. Opening the pull request is not done. `orchestrator.py supervise` continues with Bugbot, fixes, and the merge queue. A non-zero launch is not a reason to end the turn. `checkout.py implement` must not be how the ticket gets built. After the subagent returns, run `checkout.py verify`. A different hostname is a VM. The same hostname records a fallback, posts one warning per run, and checks locks. Merge or park runs `checkout.py remove`. `/warp-resume` reuses a surviving worktree and branch. `/in-cloud` in the Agents Window is the manual fallback, one ticket per invocation.

Optional `launch: agent` prints an IMPLEMENT prompt for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts. See `assets/KICKOFF.md`.

```bash
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json \
  --id A-03 --status claimed --agent shuttle-A-03 --branch warp/A-03
```

Then heartbeat that claim: `python3 <plugin>/scripts/beam.py heartbeat --beam .warp/beam.json --id A-03 --agent shuttle-A-03`. `beam.py set --status claimed` records `claimedAt`. The heartbeat records `lastSeenAt` and the agent id.

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
- Start a gate-dependent ticket because the member PR is open. Open is not merged. A pending gate stays pending until every member is merged or done.
