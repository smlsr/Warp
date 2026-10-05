# Contributing

Every change bumps the patch version and adds a changelog entry.

`VERSION` is the source of truth. `.cursor-plugin/plugin.json` must match it, and `CHANGELOG.md` must have a `## <version>` heading for it.

```bash
python3 scripts/bump_version.py
python3 scripts/check_version.py
```

`--minor` and `--major` are there when a patch bump is the wrong one. Replace the changelog stub with what changed. `python3 scripts/bump_version.py ?` prints the options. The next patch after 1.3.6 is 1.3.7.

Pull requests run `scripts/check_version.py --against origin/main`. A version that is not newer than `main` fails the check.
