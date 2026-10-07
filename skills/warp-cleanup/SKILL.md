---
name: warp-cleanup
description: "List registered Warp agents and archive idle cloud agents. Use when agents pile up, a ticket merges or parks, or the run stops."
---

# Warp cleanup

`.warp/agents.json` is the registry: id, ticket, role, session, started, ended, and state. Every spawn and every exit is a row. The file is committed with the beam. It never contains `CURSOR_API_KEY`.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
python3 <plugin>/scripts/agents.py check --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
```

The slash command can be missing from Cursor's plugin command index. The script does not need it:

```bash
python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json
python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
python3 .cursor/plugins/warp/scripts/agents.py ?
```

`agents.py ?` prints the options. Quote `?` if the shell expands it.

## What the lines mean

`agent:` is one registry row. `spawn: open` means a new agent is allowed. `spawn: closed paused`, `spawn: closed stopped`, and `spawn: closed done` mean do not spawn. `cleanup: archived <id>` means `POST /v1/agents/{id}/archive` succeeded. `cleanup: link https://cursor.com/agents/<id>` means there is no key, so archive that agent in the Cursor UI. `cleanup: no CURSOR_API_KEY` means the same.

`--cloud` calls `GET https://api.cursor.com/v1/agents` (Basic auth, the key then a colon). `--apply` archives IDLE agents from that list and ended registry rows whose id starts with `bc-` or `bc_`. Local ids (`subagent:`, `shuttle-<id>-rN`, `listener-rN`, `bugbot:`) are printed and not sent to the API.

## An existing pile

Agents left by a run from before this registry are not in `.warp/agents.json`. They still show up on `GET /v1/agents` as IDLE. Set `CURSOR_API_KEY` in the environment, never in a file that is committed, and run:

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply
```

That archives the idle cloud agents the key can see, including a pile of about 199 Shuttles, Bugbot runs, and listeners. Without the key, archive each `https://cursor.com/agents/<id>` link in the Cursor UI.

Do not dispatch. Do not merge. Do not start a listener.
