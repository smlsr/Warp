# Warp agents

Every pull request bumps the patch version and adds a changelog entry.

- The version lives in `VERSION`. `.cursor-plugin/plugin.json` must carry the same number.
- `CHANGELOG.md` needs a `## <version>` heading for that number.
- `python3 scripts/bump_version.py` bumps the patch and inserts a changelog stub. Use `--minor` or `--major` only when the change calls for it. Replace the stub with what changed.
- Do not ship a change on the current version. The next patch after 1.3.6 is 1.3.7, then 1.3.8.
- `docs/COMMANDS.md` is the command reference and `docs/CONFIG.md` is the config reference. A new flag or a new key in `assets/config.example.yaml` belongs in those files. `tests/test_docs.py` checks the keys.
- `python3 scripts/check_version.py` fails when the files disagree or the changelog has no entry. CI fails when the pull request version is not newer than `main`.
