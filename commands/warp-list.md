---
name: warp-list
description: List what is still out for this Warp run, running or idle. Changes nothing
---

Show every agent this run still has out. Run this one command, pass along any options the user gave, and print its output as it is. Do not cancel, archive, dispatch, or start anything.

`/warp-list ?` prints the help: what the command prints and every option below. `help`, `-h`, and `--help` do the same. Run `python3 <plugin>/scripts/agents.py list ?`, print that text, and do nothing else. Quote `?` if the shell expands it.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json
```

It prints:

- `instance: warp:<instance> host=<host> machine=<machine id>`. The tag this run's agents carry.
- One `agent:` line per row in `.warp/agents.json`, then `registry: live=<n> ended=<n>`. This covers every agent, including a subagent that shares the parent's session and is not a cloud agent of its own.
- One `cloud: id= name= repo= status= updated=` line and its link for each cloud agent in this repo that is in the registry or whose name starts with `[warp:<instance>]`. `status=ACTIVE` is still running. `status=IDLE` is not.
- `list: kept <id> reason=parent` for this run's parent, and `reason=self` for the agent running the command. `/warp-cleanup` never touches those. `reason=running-untagged` is an agent that only matched `--untagged` and is still working. `reason=running-scan` is a running agent that matched `--scan`. `reason=status` is an agent that is neither idle nor running.
- `out: tag [warp:<instance>] running=<n> idle=<n> kept=<n>`, and what `/warp-cleanup` would do.
- With `--scan`, each match is `cloud: id= name= repo= status= updated= word= field=` and its link. `field` is `name`, `summary`, `description`, or `prompt`. Then `scan: count <n>`. `--scan` crosses repos, so check that list before `--apply`.

The cloud lines need `CURSOR_API_KEY` in the environment. Without it the command prints `cloud: not read.` and the registry, with a link for each recorded cloud agent.

## Options

| Option | What it lists |
|---|---|
| none | This run: the registry, and cloud agents tagged `[warp:<instance>]` |
| `--tag <instance>` | Another run's agents in place of this one's. `a1b2c3`, `warp:a1b2c3`, and `[warp:a1b2c3]` are the same. This run's registry is left out |
| `--tag all` | Agents tagged for any Warp run. `--tag '*'` is the same. Quote the star |
| `--untagged` | Also agents with no Warp tag, from before 1.5.0, matched loosely on Warp role words in the name or prompt (Shuttle, IMPLEMENT, fix, rebase, listener, Bugbot). This can show someone else's agent |
| `--all-idle` | Every idle agent in this repo, tagged or not |
| `--any-repo` | Do not limit to this repo |
| `--scan <words>` | Every cloud agent the key can see whose name, summary or description, or first prompt contains one of the words. Ignores this repo and the tag and registry rules. Case-insensitive. `--scan` crosses repos: read this list before `--apply` |

A comma list, quoted items, and repeated flags are the same: `--scan="Jira","Fix","Bugbot"`, `--scan Jira,Fix,Bugbot`, and `--scan Jira --scan Fix`.

```bash
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --tag a1b2c3
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --tag all
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --untagged
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan="Jira","Fix","Bugbot"
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira,Fix,Bugbot
python3 <plugin>/scripts/agents.py list --beam .warp/beam.json --scan Jira --scan Fix
```

`/warp-cleanup` takes the same options and acts on exactly what this lists. The slash command can be missing from Cursor's plugin command index. The script does not need it: `python3 .cursor/plugins/warp/scripts/agents.py list --beam .warp/beam.json`. Plugin hooks do not run on cloud runners. Nothing here depends on one.
