---
name: warp-cleanup
description: Cancel and archive what this Warp run still has out. /warp-list shows it first
---

Cancel the run of every agent this Warp run still has running, and archive all of them. Run this one command, pass along any options the user gave, and print its output as it is. `/warp-list` shows what it will act on and changes nothing.

`/warp-cleanup ?` prints the help: what the command does and every option below. `help`, `-h`, and `--help` do the same. Run `python3 <plugin>/scripts/agents.py cleanup ?`, print that text, and do nothing else. It cancels nothing and archives nothing. Quote `?` if the shell expands it.

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running
```

It needs `CURSOR_API_KEY` in the environment. The key is never written to the repo. Without it the command prints `cleanup: link https://cursor.com/agents/<id>` for each recorded cloud agent, to archive in the Cursor UI.

What it does, for each cloud agent in this repo that is in `.warp/agents.json` or whose name starts with this run's tag, `[warp:<instance>]`:

- `status=ACTIVE` or `RUNNING`: cancel its run (`POST /v1/agents/{id}/runs/{runId}/cancel`, `cleanup: cancelled <id>`), then archive it.
- `status=IDLE`: archive it (`POST /v1/agents/{id}/archive`, `cleanup: archived <id>`).
- The agent running the command stays (`reason=self`). This run's parent stays (`reason=parent`).

Archive is reversible in the Cursor UI. Warp never deletes an agent. Other agents in the repo are not matched, whatever words are in their names, and neither are another Warp run's.

While this run is running, the command archives the idle ones and leaves the running ones: `cleanup: --running refused while the run is running. /warp-pause or /warp-stop first.` That is the safe order. `--force` cancels them anyway.

## Options

`/warp-list` and `/warp-cleanup` take the same selection.

| Option | What it acts on |
|---|---|
| none | This run: the registry, and cloud agents tagged `[warp:<instance>]` |
| `--tag <instance>` | Another run's agents in place of this one's. `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same. Nothing checks whether that run is still going |
| `--tag all` | Agents tagged for any Warp run. `--tag '*'` is the same. Quote the star |
| `--untagged` | Also agents with no Warp tag, from before 1.5.0, matched loosely on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot). An idle one is archived. One that is still running is left (`reason=running-untagged`), because it may be someone else's |
| `--force` | Cancel running agents while this run is running, and cancel a running agent that only matched loosely |
| `--all-idle` | Every idle agent in this repo, tagged or not |
| `--any-repo` | Do not limit to this repo |

```bash
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --tag a1b2c3
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --tag all
python3 <plugin>/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running --untagged
```

Leave `--apply` off for a dry run: it prints each match, `cleanup: would cancel <id>` for the running ones, and `cleanup: count`, and changes nothing.

A pile of about 199 idle Shuttles, Bugbot runs, and listeners from a run before 1.5.0 carries no tag. `/warp-stop`, then `/warp-list --untagged` to read it, then `/warp-cleanup --untagged`.

You should rarely need this. `/warp-pause` and `/warp-stop` end every registered agent and, with the key, cancel and archive this run's cloud agents. Merge and park archive that ticket's agents. A fix round that moves to a new VM retires the VM it left. When every ticket is merged or parked, the same teardown runs once and the parent exits. `docs/LIFECYCLE.md` has the whole rule.

The slash command can be missing from Cursor's plugin command index. The script does not need it: `python3 .cursor/plugins/warp/scripts/agents.py cleanup --beam .warp/beam.json --cloud --apply --running`, and `python3 .cursor/plugins/warp/scripts/agents.py ?` prints the options. Quote `?` if the shell expands it.

Do not dispatch. Do not merge. Do not start a listener.
