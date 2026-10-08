---
name: warp-cleanup
description: "List registered Warp agents and clear idle or leftover cloud agents for this repo. Use when agents pile up, after a pause or stop without CURSOR_API_KEY, or to check that a run left nothing behind."
---

# Warp cleanup

`.warp/agents.json` is the registry: id, ticket, role, session, started, ended, and state. Every spawn and every exit is a row. The file is committed with the beam. It never contains `CURSOR_API_KEY`. `docs/LIFECYCLE.md` is the rule for how agents start and end.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py check --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

The slash command can be missing from Cursor's plugin command index. The script does not need it:

```bash
python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 .cursor/plugins/warp/scripts/agents.py ?
```

`agents.py ?` prints the options. Quote `?` if the shell expands it.

## What the lines mean

`agent:` is one registry row. `spawn: open` means a new agent is allowed. `spawn: closed paused`, `spawn: closed stopped`, and `spawn: closed done` mean do not spawn. `cloud: id= name= repo= status= updated=` plus a link is one cloud match. `cleanup: count N` is how many agents would be archived. `cleanup: dry-run` means nothing was changed. `cleanup: would cancel <id>` is a running match that `--apply --running` would cancel. `cleanup: skip <id> reason=self` is the agent running the command. `cleanup: skip <id> reason=parent` is this run's parent. `cleanup: skip <id> reason=running` is a RUNNING or ACTIVE agent and no `--running`. `cleanup: cancelled <id>` means `POST /v1/agents/{id}/runs/{runId}/cancel` succeeded. `cleanup: archived <id>` means `POST /v1/agents/{id}/archive` succeeded. `cleanup: link https://cursor.com/agents/<id>` means there is no key, so archive that agent in the Cursor UI. `cleanup: no CURSOR_API_KEY` means the same. `cleanup: cut short` means the time limit was reached: run it again.

`--cloud` calls `GET https://api.cursor.com/v1/agents` (Basic auth, the key then a colon). List rows omit repos and the prompt, so a candidate is read with `GET /v1/agents/{id}` before it can match. `--apply` archives the dry-run matches. A match is in this checkout's origin repository, and it is in `.warp/agents.json` or its name starts with this run's `[warp:<instance>]` tag, or its prompt is one Warp printed. Every agent Warp starts is named `[warp:<instance>] <ticket> <step>` or `[warp:<instance>] listener`: Cursor has no tag field, so the name is the tag. `<instance>` is the six characters on the beam that name this run. `agents.py list` prints it as `instance: warp:<instance> host= machine=`, and the dry run prints `cleanup: tag [warp:<instance>]`. Other agents in the repo are not matched, whatever words are in their names, and neither are another Warp run's. `--tag <instance>` names another run's tag in place of this one. `--tag all` matches the tag of any Warp run. `--untagged` adds agents from before the tag, matched loosely on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot). That can match someone else's agent in the same repo, so read that dry run before `--apply`. The agent running the command stays. This run's parent stays. A RUNNING or ACTIVE agent stays unless `--running`. Local ids (`subagent:`, `shuttle-<id>-rN`, `listener`, `parent:`, `bugbot:`) are printed and not sent to the API. Archive is reversible in the Cursor UI. Warp never deletes an agent.

`--running` cancels the latest run of each RUNNING or ACTIVE match, then archives it. It is refused while the run is running: `/warp-pause` or `/warp-stop` first. `--force` overrides that and cancels live work.

`--all-idle` drops the role check and stays on this repo. `--any-repo` drops the repo check. Together they are every IDLE agent the key can see.

## What already clears agents

`/warp-pause` and `/warp-stop` end every registry row and, with the key, cancel and archive every cloud agent in the registry and every agent in this repo whose name carries this run's `[warp:<instance>]` tag. Merge and park archive that ticket's agents. A fix round on a new VM retires the VM the ticket left. The end of a run does the same teardown once. Each Shuttle and each listener poll also checks for itself with `agents.py reap` and exits on `reap: exit`. This command is for what is left: a run that halted without the key, agents from before the registry, and a check that nothing remains.

## An existing pile

Agents left by a run from before 1.5.0 are not in `.warp/agents.json` and do not carry this run's `[warp:<instance>]` tag. `--untagged` matches them loosely: a Shuttle, IMPLEMENT, fix, rebase, listener, or Bugbot in the name or prompt, in this repo. That can also match someone else's agent, so read the dry run. Set `CURSOR_API_KEY` in the environment, never in a file that is committed. `/warp-stop` first, so nothing new starts. Then run the dry run from that checkout:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --untagged --running
```

Read the list and the count. Then clear those matches:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --untagged --apply --running
```

That is the pair for a pile of about 199 Shuttles, Bugbot runs, and listeners in this repo, idle or still running. `--all-idle` and `--any-repo` stay off. Without the key, archive each `https://cursor.com/agents/<id>` link in the Cursor UI.

Do not dispatch. Do not merge. Do not start a listener.
