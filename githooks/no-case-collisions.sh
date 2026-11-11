#!/usr/bin/env bash
# Warp no-case-collisions hook (1.7.24).
#
# Rejects a commit (pre-commit) or a push (pre-push) that would carry two paths differing only in case, such as
# docs/spec/GATES.md and docs/spec/gates.md. On macOS (APFS) and Windows (NTFS) those are one file and two Warp
# writers overwrote each other on every tick; on Linux they are two files and both landed in pull requests.
# It also rejects adding a docs/spec spec file under a non-canonical spelling (docs/spec/gates.md when the canonical
# name is docs/spec/GATES.md).
#
# Canonical spelling: docs/spec/*.md uppercase (GATES.md, DECISIONS.md, CHANGES.md, GAP-REPORT.md, INFRA.md,
# PROCESSES.md, SEED.md, VERSIONING.md, VERIFICATION.md, DOCS-IN-CODE.md); README.md, LICENSE and CHANGELOG.md as
# written; anything else kebab-lowercase.
#
# Repair: `make fix-gates` (GATES.md) or `make fix-case-collisions` (every pair), which run
# .cursor/plugins/warp/scripts/fix_gates_case.sh.
#
# Escape hatch, for the rare legitimate case: `git commit --no-verify` / `git push --no-verify`.
#
# Usage: no-case-collisions.sh pre-commit | pre-push   (installed by Warp's init and upgrade between
#        "# BEGIN WARP HOOK" and "# END WARP HOOK" in .git/hooks/pre-commit and .git/hooks/pre-push)

set -u
mode="${1:-pre-commit}"

canonical() {
  local p="$1" dir base low
  dir=$(dirname -- "$p"); base=$(basename -- "$p")
  low=$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')
  case "$low" in
    docs/spec/*.md)
      if [ "$(dirname -- "$low")" = "docs/spec" ]; then
        printf 'docs/spec/%s.md\n' "$(printf '%s' "${base%.*}" | tr '[:lower:]' '[:upper:]')"; return
      fi ;;
  esac
  case "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" in
    readme.md) printf '%s/README.md\n' "$dir" | sed 's#^\./##'; return ;;
    license) printf '%s/LICENSE\n' "$dir" | sed 's#^\./##'; return ;;
    changelog.md) printf '%s/CHANGELOG.md\n' "$dir" | sed 's#^\./##'; return ;;
  esac
  printf '%s\n' "$low" | tr '_ ' '--'
}

if [ "$mode" = "pre-push" ]; then
  # Everything the push would put on the server is in HEAD's tree (and the diff from the remote is a subset of it).
  names=$(git ls-tree -r --name-only HEAD 2>/dev/null)
else
  # The index is the commit: what is staged plus every tracked file from HEAD.
  names=$(git ls-files 2>/dev/null)
fi

pairs=$(printf '%s\n' "$names" | awk 'NF { l = tolower($0); if ((l in seen) && seen[l] != $0) print seen[l] "\t" $0; else if (!(l in seen)) seen[l] = $0 }')

bad=0
if [ -n "$pairs" ]; then
  bad=1
  echo "Warp: $mode refused: paths that differ only in case (one file on macOS/Windows, two on Linux):" >&2
  gates=0
  while IFS="$(printf '\t')" read -r a b; do
    [ -z "$a" ] && continue
    echo "  $a  <->  $b    canonical: $(canonical "$a")" >&2
    case "$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')" in docs/spec/gates.md) gates=1 ;; esac
  done <<EOF2
$pairs
EOF2
  if [ "$gates" = 1 ]; then
    echo "Fix: make fix-gates   (or: bash .cursor/plugins/warp/scripts/fix_gates_case.sh)" >&2
  else
    echo "Fix: make fix-case-collisions   (or: bash .cursor/plugins/warp/scripts/fix_gates_case.sh --all)" >&2
  fi
fi

if [ "$mode" != "pre-push" ]; then
  # A docs/spec spec file added under a non-canonical spelling.
  added=$(git diff --cached --name-only --diff-filter=AR 2>/dev/null)
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    low=$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')
    case "$low" in
      docs/spec/gates.md|docs/spec/decisions.md|docs/spec/changes.md|docs/spec/gap-report.md|docs/spec/infra.md|\
      docs/spec/processes.md|docs/spec/seed.md|docs/spec/versioning.md|docs/spec/verification.md|docs/spec/docs-in-code.md)
        want=$(canonical "$p")
        if [ "$p" != "$want" ]; then
          [ "$bad" = 0 ] && echo "Warp: $mode refused:" >&2
          bad=1
          echo "  $p is not the canonical spelling: use $want" >&2
          case "$low" in docs/spec/gates.md) echo "Fix: make fix-gates" >&2 ;; *) echo "Fix: make fix-case-collisions" >&2 ;; esac
        fi ;;
    esac
  done <<EOF3
$added
EOF3
fi

if [ "$bad" = 1 ]; then
  echo "Escape hatch for a legitimate case: --no-verify." >&2
  exit 1
fi
exit 0
