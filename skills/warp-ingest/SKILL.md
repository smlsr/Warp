---
name: warp-ingest
description: "Build the Warp beam from CURSOR_PLAN, the build map, and plan/schedule.json. Use before the first dispatch or when the ticket graph is regenerated."
---

# Warp ingest

Build `.warp/beam.json` from the program graph. Do not hand-write tickets.

## Sources, in order

1. `HOS/plan/schedule.json` — authoritative for deps, locks, gates, size, owner, critical path. 366 tickets, gates G0–G11, lock conflicts.
2. `HOS/spec/CURSOR_PLAN.md` — wave notes and gate commands. Use it to confirm gate checks, not to invent edges.
3. `HOS/plan/BUILD_MAP.html` and `HOS/docs/FAST-BUILD-MAP.html` — human view. Do not parse the HTML if `schedule.json` is present.
4. `HOS/jira/HOS-all-tickets.json` — AC text. Link later by tempId; do not copy every AC into the beam.

## Run

```bash
cp <plugin>/assets/config.example.yaml .warp/config.yaml
python3 <plugin>/scripts/beam.py ingest \
  --schedule HOS/plan/schedule.json \
  --plan HOS/spec/CURSOR_PLAN.md \
  --out .warp/beam.json \
  --max-agents 18 \
  --model claude-sonnet-5.5
python3 <plugin>/scripts/beam.py check --beam .warp/beam.json
python3 <plugin>/scripts/beam.py board --beam .warp/beam.json
```

Edit `.warp/config.yaml` for messenger and caps. The ingest copies defaults into the beam; the yaml is what agents re-read.

## Map Jira keys

When the import sheet `tempId → HOS-nnn` exists, set keys:

```bash
python3 <plugin>/scripts/beam.py set --beam .warp/beam.json --id L-01 --jira HOS-14
```

Until a key exists, branch names use the tempId only.

## What good looks like

- `check` prints `ok 366 tickets`.
- First `ready` list is the no-dep set (the W0 parallel tickets), critical path first (`L-01` before unlocked work).
- S and M are `autoMerge: true`. L and XL are false.
- Gates are `pending`. Nothing is green without evidence.

Re-ingest only on a regenerated graph. It overwrites ticket fields and keeps no live status. To refresh a live beam, do not re-ingest; patch with `set`.
