#!/usr/bin/env bash
# Resume hint. Never dispatch from a hook.
# Local IDE duplicate of scripts/resume_hint.py. Cloud runners do not execute this hook.
set -euo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CURSOR_PROJECT_DIR="${CURSOR_PROJECT_DIR:-$(pwd)}"
exec python3 "$PLUGIN/scripts/resume_hint.py"
