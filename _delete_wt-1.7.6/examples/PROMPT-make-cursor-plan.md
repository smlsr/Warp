# Prompt — make a CURSOR_PLAN.md from specs and tickets

Paste this into a Cursor agent in a repo that has specs or tickets and no plan file. When it finishes, run `/warp-scan`.

```
Read this repo. Find specs, PRDs, ticket exports, Jira JSON, and any existing plan.
Do not write product code.

Write CURSOR_PLAN.md at the repo root in this shape:

1. A short Day 0 list of human setup.
2. Waves. Each wave is a heading "### W0 name — N tickets".
   Under it, list ticket ids. Parallel tickets are separated with ∥.
   A blocker is written "(after ID, ID)".
3. A gate before the next wave when a check must pass:
   **Gates before the next wave:**
   - *ID* — name: `command that proves it`
4. A markdown table with columns id, size, locks, deps, summary.
   Size is S, M, L, or XL. S and M are medium or below.
   locks is the directory that ticket may edit. Two tickets with the same
   lock must not run together.
5. Use the ids already in the tickets. Do not invent a second id scheme.
   If a ticket has no id, use a prefix and a number (API-01).

Keep blockers real. If the spec does not say B waits on A, do not add that edge.
End with the ticket count and the longest dependency chain.
```
