---
name: shuttle-run
description: "Run one claimed ticket from Jira through PR and Bugbot. Use when a Shuttle is started with IMPLEMENT <id>. Does not merge."
---

# Shuttle run

You were started with `IMPLEMENT <id>` in this repo. Local and cloud are the same workspace: the clone. Do not implement from chat memory.

## Load the repo before editing

1. Read `.cursor/rules/`, including every rule with `alwaysApply: true`. Read rules whose globs match the lock paths.
2. Read `AGENTS.md` and `CLAUDE.md` at the repo root and under the ticket lock path, if they exist.
3. Read the preamble the plan names.
4. Read `.warp/config.yaml` and your claim in `.warp/beam.json`. Status must be `claimed` and `agent` must be you. Otherwise stop.
5. Fetch the Jira issue if a key is set. Otherwise read the ticket in `CURSOR_PLAN.md`. Read every acceptance criterion.

## Then

1. Jira status. If the ticket has a Jira key and `jira.startedAt` is empty in the beam, run `python3 <plugin>/scripts/jira_sync.py plan --beam .warp/beam.json --id <id> --event claim` and follow it: read the issue's status and transitions through the Jira MCP server, run `jira_sync.py pick`, apply the transition, then `jira_sync.py record`. No Jira connector, no matching transition, or a failed call is not an error: record `unavailable`, `no-transition`, or `failed`, which saves a note to `.warp/outbox.md`, and continue.
2. Set `planning`. Plan the diff inside the lock paths only. An escape is `alarm` / `lock-escape`.
3. Set `coding`. Branch `warp/<id>-<jiraKey>` off `baseBranch` from `python3 <plugin>/scripts/provider.py resolve`. Use `config.model` (default `grok-4.7-high`, Grok 4.7 High, not a fast variant).
4. Implement. Prove each acceptance criterion. Checkpoint spend with `scripts/beam.py spend`.
5. If resolve says connected, push and open the pull request with the first method in `methods` (the named MCP server, or `gh` for GitHub). If it says local, or every method fails, do not push and do not error: `python3 <plugin>/scripts/provider.py note --id <id> --reason "<why>"` and hand Reed the branch name. Comment the URL or the branch on the Jira issue. Set `review`.
6. If Reed returns fixes and attempts remain, set `fix` and push only in connected mode. Past `maxFixAttempts`, set `alarm`.

If status is already `coding` or `fix` and the branch exists, continue that branch. Do not open a second pull request.

Every acceptance-criterion comment names the id and the command that passed.
