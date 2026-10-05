# Warp agents

Every pull request bumps the patch version and adds a changelog entry.

- The version lives in `VERSION`. `.cursor-plugin/plugin.json` must carry the same number.
- `CHANGELOG.md` needs a `## <version>` heading for that number.
- `python3 scripts/bump_version.py` bumps the patch and inserts a changelog stub. Use `--minor` or `--major` only when the change calls for it. Replace the stub with what changed.
- Do not ship a change on the current version. The next patch after 1.3.10 is 1.3.11, then 1.3.12.
- `docs/COMMANDS.md` is the command reference and `docs/CONFIG.md` is the config reference. The README command table, [docs/GUIDE.md](docs/GUIDE.md) (Jira from import to merge), [docs/RUNBOOK.md](docs/RUNBOOK.md) (transitions and nothing linked), [docs/STATE.md](docs/STATE.md), and [docs/CONNECTORS.md](docs/CONNECTORS.md) have to match the same behavior. A new flag, command, skill, or config key belongs in those files. `tests/test_docs.py` checks config keys, every `commands/*.md` name, and every `jira_sync.py` subcommand.
- Chat commands live in `commands/` and `skills/`. Agent-only skills are `shuttle-run`, `warp-dispatch`, and `warp-alarm`. Hooks in `hooks/hooks.json` are session start, stop, and subagent stop. They print a resume hint and do not dispatch. `/warp-allow-notify` writes a separate `beforeMCPExecution` hook.
- `python3 scripts/check_version.py` fails when the files disagree or the changelog has no entry. CI fails when the pull request version is not newer than `main`.
