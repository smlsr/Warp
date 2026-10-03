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

1. Set `planning`. Plan the diff inside the lock paths only. An escape is `alarm` / `lock-escape`.
2. Set `coding`. Branch `warp/<id>-<jiraKey>` off updated `main`. Use `config.model`.
3. Implement. Prove each acceptance criterion. Checkpoint spend with `scripts/beam.py spend`.
4. Open the pull request. Comment the URL on the Jira issue. Set `review` and hand the id to Reed.
5. If Reed returns fixes and attempts remain, set `fix` and push. Past `maxFixAttempts`, set `alarm`.

If status is already `coding` or `fix` and the branch exists, continue that branch. Do not open a second pull request.

Every acceptance-criterion comment names the id and the command that passed.
