#!/usr/bin/env bash
# Refuse a Warp subagent launch the gate did not issue. Anything that is not a
# Warp launch is allowed.
# Local IDE duplicate of the gate in scripts/agents.py. Cloud runners do not execute this hook.
# Nothing depends on it: checkout.py launch is the gate, and every Shuttle runs the
# reap check from its prompt. WARP_SPAWN_GATE=off allows everything.
set -uo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CURSOR_PROJECT_DIR="${CURSOR_PROJECT_DIR:-$(pwd)}"
python3 "$PLUGIN/scripts/agents.py" hook-start || echo '{"permission":"allow"}'
