# Kickoff

Warp starts each ticket as an agent whose workspace is the repo. Local and cloud use the same words.

```
IMPLEMENT API-01
```

The id is the plan id (`API-01`, `P-001`, `L-01`). The workspace is the clone, so `.cursor/`, `AGENTS.md`, `CLAUDE.md`, and `.warp/` are on disk. The message does not carry the rules. The agent reads them.

First reads, before any edit:

1. `.cursor/rules/` and any rule with `alwaysApply: true`
2. `AGENTS.md` and `CLAUDE.md` at the repo root and in the ticket's lock path
3. The module preamble the plan names
4. `.warp/config.yaml` and the claim for this id in `.warp/beam.json`
5. The Jira issue, or the ticket section in `CURSOR_PLAN.md` if there is no key

Then implement inside the lock paths only. Model is `config.model`.
