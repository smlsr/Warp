#!/usr/bin/env bash
# Record that a subagent ended. Warp reconciles the ticket on the next tick.
set -euo pipefail
ROOT="${CURSOR_PROJECT_DIR:-$(pwd)}"
mkdir -p "$ROOT/.warp"
echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"type\":\"subagent-stop\"}" >> "$ROOT/.warp/journal.jsonl"
exit 0
