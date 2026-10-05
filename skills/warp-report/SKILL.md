---
name: warp-report
description: "Write the Warp completion report (.warp/warp-complete.html), or a partial snapshot with --partial. Use when the user asks for the run report or /warp-report."
---

# Warp report

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

`report.py ?` prints the options. Quote `?` if the shell expands it.

The canonical file is `.warp/warp-complete.html`. `reportPath` writes the same file again when it is a different path. The default path is inside `.warp`, which is gitignored.

If the command says `not complete; pass --partial`, the run still has queued or active tickets. Re-run with `--partial` for a snapshot. Do not invent token or cost numbers. If `usage` was not recorded, the report shows n/a.

Hand the user the path and the totals in the Final counts section. The page is local. It is not on the remote.
