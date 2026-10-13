---
name: warp-runs
description: List the Warp runs in this repo, who owns each, and how far each has got. Changes nothing
---

Show every named run on the base branch. Run this one command and print its output as it is. Do not start, pause, stop, or delete anything.

`/warp-runs ?` prints the help. Run `python3 <plugin>/scripts/runs.py ?` and print that text.

```bash
python3 <plugin>/scripts/runs.py list
```

It fetches origin first. It prints `runs: <n> on origin/<base>` and one line per run:

`run: admin (this checkout) state=running live owner=Dev_Two folder=HOS/UI/Admin merged=12 open=2 queued=27 seen=4m ago startAfter=core:G2`

- `(this checkout)` marks the run this checkout runs. A run this checkout names but has not published prints `run: <name> (this checkout) not published yet`.
- `live` means the run is `running` and its parent saved state within 30 minutes. A live run owned by another machine needs `/warp-start --takeover`.
- `owner` is the person (git `user.name`) who last started it. `startAfter` is the gate of another run it waits for, or `-`.

With a name, `/warp-runs <name>` runs `python3 <plugin>/scripts/runs.py show <name>` instead. It prints what `/warp-init <name>` would show about one run. Details: `docs/RUNS.md`.
