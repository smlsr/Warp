---
name: warp-cleanup
description: "List registered Warp agents and dry-run or archive idle cloud agents for this repo. Use when agents pile up, a ticket merges or parks, or the run stops."
---

# Warp cleanup

`.warp/agents.json` is the registry: id, ticket, role, session, started, ended, and state. Every spawn and every exit is a row. The file is committed with the beam. It never contains `CURSOR_API_KEY`.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py check --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
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

`agent:` is one registry row. `spawn: open` means a new agent is allowed. `spawn: closed paused`, `spawn: closed stopped`, and `spawn: closed done` mean do not spawn. `cloud: id= name= repo= status= updated=` plus a link is one cloud match. `cleanup: count N` is how many IDLE agents would be archived. `cleanup: dry-run` means nothing was archived. `cleanup: skip <id> reason=self` is the agent running the command. `cleanup: skip <id> reason=running` is a RUNNING or ACTIVE agent. `cleanup: archived <id>` means `POST /v1/agents/{id}/archive` succeeded. `cleanup: link https://cursor.com/agents/<id>` means there is no key, so archive that agent in the Cursor UI. `cleanup: no CURSOR_API_KEY` means the same.

`--cloud` calls `GET https://api.cursor.com/v1/agents` (Basic auth, the key then a colon). List rows omit repos and the prompt, so a candidate is read with `GET /v1/agents/{id}` before it can match. `--apply` archives the dry-run matches. A match is IDLE, its repository is this checkout's origin, and it is in `.warp/agents.json` or its name or prompt is a Warp role (Shuttle, IMPLEMENT, fix, rebase, listener, Warp-triggered Bugbot). The agent running the command stays. A RUNNING or ACTIVE agent stays. Local ids (`subagent:`, `shuttle-<id>-rN`, `listener-rN`, `bugbot:`) are printed and not sent to the API.

`--all-idle` drops the role check and stays on this repo. `--any-repo` drops the repo check. Together they are every IDLE agent the key can see.

## An existing pile

Agents left by a run from before this registry are not in `.warp/agents.json`. A Shuttle, IMPLEMENT, fix, rebase, listener, or Warp-triggered Bugbot whose repository is this repo still matches. Set `CURSOR_API_KEY` in the environment, never in a file that is committed, and run the dry run from that checkout:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud
```

Read the list and the count. Then archive those matches:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
```

That is the pair for a pile of about 199 Shuttles, Bugbot runs, and listeners in this repo. `--all-idle` and `--any-repo` stay off. Without the key, archive each `https://cursor.com/agents/<id>` link in the Cursor UI.

Do not dispatch. Do not merge. Do not start a listener.
