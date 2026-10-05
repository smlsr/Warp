#!/usr/bin/env bash
# Record that a subagent ended. Warp reconciles the ticket on the next tick.
# Local IDE duplicate of scripts/session_note.py. Cloud runners do not execute this hook.
set -euo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CURSOR_PROJECT_DIR="${CURSOR_PROJECT_DIR:-$(pwd)}"
exec python3 "$PLUGIN/scripts/session_note.py" --type subagent-stop
