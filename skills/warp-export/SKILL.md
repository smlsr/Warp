---
name: warp-export
description: "Write Warp's suggested plan as JSON and Markdown so another model can analyze the order, batches, gates, and review holds. Use when the user wants the plan out of Warp."
---

# Warp export

```bash
python3 <plugin>/scripts/scan.py export --beam .warp/beam.json --out .warp/WARP_PLAN.json
```

Writes `.warp/WARP_PLAN.json` and `.warp/WARP_PLAN.md`. `scan.py export ?` prints the options. Quote `?` if the shell expands it.

The markdown is the brief to hand to another model: first batch, suggested parallel batches, gates, and which ids wait for approval. It is a suggestion, not a calendar. Editing it does not change the live beam until `/warp-import`.
