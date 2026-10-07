# Kickoff

Warp starts each ticket as one subagent. `subagentVm` defaults to true: the prompt asks for a dedicated VM with its own clone and branch, not a git worktree on this machine. The parent starts them in parallel up to `maxAgents`. A same hostname, or `subagentVm: false`, uses a git worktree and `maxLocalSubagents`.

```
SUBAGENT API-01
```

The id is the plan id (`API-01`, `P-001`, `L-01`). The worktree is a checkout of `warp/<id>-<jira>` from `origin/main`, so `.cursor/`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` are on disk. The message names the ticket, Jira key, locks, acceptance, branch, and absolute worktree path. The subagent works only inside that path. It does not call Jira or Slack.

Optional `launch: agent` still starts with `IMPLEMENT API-01` for one new Agent. This plugin does not call a Cloud Agents API. Clone main so `.cursor` rules load. A beam file does not have to exist on the branch before that Agent starts.

First reads, before any edit:

1. `.cursor/rules/` and any rule with `alwaysApply: true`
2. `AGENTS.md` and `CLAUDE.md` at the repo root and in the ticket's lock path
3. The module preamble the plan names
4. `.warp/config.yaml` and the claim for this id in `.warp/beam.json`
5. The Jira issue, or the ticket section in `CURSOR_PLAN.md` if there is no key

Then implement inside the lock paths only. Model is `config.model`.
