#!/usr/bin/env bash
# Record that a subagent ended. Warp reconciles the ticket on the next tick.
# Local IDE duplicate of scripts/session_note.py. Cloud runners do not execute this hook.
# The journal also gets which Warp ticket the subagent was for. No follow-up is requested.
set -euo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CURSOR_PROJECT_DIR="${CURSOR_PROJECT_DIR:-$(pwd)}"
INPUT="$(cat || true)"
printf '%s' "$INPUT" | python3 "$PLUGIN/scripts/agents.py" hook-stop >/dev/null 2>&1 || true
exec python3 "$PLUGIN/scripts/session_note.py" --type subagent-stop
