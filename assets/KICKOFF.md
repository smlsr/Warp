# Kickoff

Warp starts each ticket as one subagent. `subagentVm` defaults to false: a git worktree, capped by `maxAgents` (default 18). `memoryCheck` defaults to false. `runner: local` forces `subagentVm` false. On a cloud runner, `true` asks for a dedicated VM with its own clone and branch, not a git worktree on this machine. The parent starts them in parallel up to `maxAgents`. A same hostname uses a git worktree.

```
[warp:a1b2c3] API-01 implement
SUBAGENT API-01
```

The first line is the tag. It is also the subagent's name. `a1b2c3` is the instance: six characters that name this Warp run, printed by `/warp-start`. Cursor has no tag field on an agent, so the name is how this run's agents are found among all the others.

The id is the plan id (`API-01`, `P-001`, `L-01`). The worktree is a checkout of `warp/<id>-<jira>` from `origin/main`, so `.cursor/`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` are on disk. The message names the ticket, Jira key, locks, acceptance, branch, absolute worktree path, and the subagent's agent id (`agent: <id>`). The subagent works only inside that path. It does not call Jira or Slack.

The message comes from `checkout.py launch` and nowhere else. It carries the worker contract: one step, one result line, never start another agent, never subscribe, set a timer, loop, or wait. It also carries the reap command. The subagent runs it first and stops on `reap: exit`.

Optional `launch: agent` still starts with `IMPLEMENT API-01` for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts.

First, the reap check from the prompt. Then the first reads, before any edit:

1. `.cursor/rules/` and any rule with `alwaysApply: true`
2. `AGENTS.md` and `CLAUDE.md` at the repo root and in the ticket's lock path
3. The module preamble the plan names
4. `.warp/config.yaml` and the claim for this id in `.warp/beam.json`
5. The Jira issue, or the ticket section in `CURSOR_PLAN.md` if there is no key

Then implement inside the lock paths only. Model is `config.model`.
