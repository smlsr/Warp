---
name: warp-report
description: Write the completion report, or a partial snapshot while the run is still going
---

Write `.warp/warp-complete.html`. The run writes it on its own when every ticket is merged, done, skipped, blocked, or alarmed, and when the run is stopped, unless `reportOnComplete` is false.

```bash
python3 <plugin>/scripts/report.py --beam .warp/beam.json
python3 <plugin>/scripts/report.py --beam .warp/beam.json --partial
python3 <plugin>/scripts/report.py --beam .warp/beam.json --out .warp/warp-complete.html --open
```

Without `--partial`, a run that still has queued or active tickets exits 2 and writes nothing. `--partial` writes a snapshot with a Partial banner. `--open` prints a `file://` URL. `report.py ?` prints the options.

The file is one HTML page with inline CSS and inline SVG. It has no network resources. It stays under `.warp`, which `/warp-init` gitignores. Herald posts the headline totals and the local path. The path is not a remote link.

Waves are concurrency-run segments. A ticket is active from claim until it leaves the working set. Stretches with the same set of active tickets are one wave. An empty gap is not a wave, and a handoff that keeps the count the same but changes who is running is two waves.
