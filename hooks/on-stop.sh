#!/usr/bin/env bash
# Checkpoint note on agent stop. State is already in .warp/beam.json.
# Local IDE duplicate of scripts/session_note.py. Cloud runners do not execute this hook.
set -euo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CURSOR_PROJECT_DIR="${CURSOR_PROJECT_DIR:-$(pwd)}"
exec python3 "$PLUGIN/scripts/session_note.py" --type session-stop
