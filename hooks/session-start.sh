#!/usr/bin/env bash
# Resume hint. Never dispatch from a hook.
set -euo pipefail
ROOT="${CURSOR_PROJECT_DIR:-$(pwd)}"
BEAM="$ROOT/.warp/beam.json"
if [[ -f "$BEAM" ]]; then
  python3 - << PY
import json
p="$BEAM"
b=json.load(open(p))
m=b.get("metrics") or {}
print(f"WARP beam: paused={b.get('paused')} done={m.get('done')}/{m.get('total')} eta_h={m.get('etaHours')} alarms={m.get('byStatus',{}).get('alarm',0)}")
print("Read .warp/BOARD.md then run /warp-status before dispatching.")
PY
else
  echo "WARP: no .warp/beam.json — run /warp-ingest before /warp."
fi
