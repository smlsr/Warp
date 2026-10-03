#!/usr/bin/env bash
# Checkpoint note on agent stop. State is already in .warp/beam.json.
set -euo pipefail
ROOT="${CURSOR_PROJECT_DIR:-$(pwd)}"
if [[ -d "$ROOT/.warp" ]]; then
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"type\":\"session-stop\"}" >> "$ROOT/.warp/journal.jsonl"
fi
exit 0
