#!/usr/bin/env bash
# Repair docs/spec/GATES.md vs docs/spec/gates.md (and, with --all, every path pair that differs only in case). 1.7.24.
#
#   bash .cursor/plugins/warp/scripts/fix_gates_case.sh          # docs/spec/GATES.md       (make fix-gates)
#   bash .cursor/plugins/warp/scripts/fix_gates_case.sh --all    # every collision           (make fix-case-collisions)
#   bash .cursor/plugins/warp/scripts/fix_gates_case.sh --path docs/spec/DECISIONS.md
#
# Two spellings in the index (a case-sensitive filesystem shows both files; macOS shows one): every version (both
# index blobs, and each working file the filesystem keeps apart) is copied to <dir>/.backup/<UTC time>/ with its
# original mtime (cp -p, and MTIMES), the contents are concatenated into the canonical file with a `---` separator, the
# other spelling is `git rm --cached` (and removed from disk on a case-sensitive filesystem), and the canonical file is
# staged. One spelling, but the wrong one: `git mv -f` through a temporary name, which records the canonical casing
# even on a case-insensitive filesystem. Already canonical: nothing changes, so it is safe to run again.
# .backup/ goes in .git/info/exclude, so `git status` stays clean. Commit the result yourself.

set -euo pipefail

target="docs/spec/GATES.md"
all=0
while [ $# -gt 0 ]; do
  case "$1" in
    --all) all=1 ;;
    --path) target="$2"; shift ;;
    --root) cd "$2"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "fix-case: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
top=$(git rev-parse --show-toplevel)
cd "$top"

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

canonical() {
  local p="$1" dir base low
  dir=$(dirname -- "$p"); base=$(basename -- "$p"); low=$(lower "$p")
  if [ "$(dirname -- "$low")" = "docs/spec" ] && [ "${low##*.}" = "md" ]; then
    printf 'docs/spec/%s.md\n' "$(printf '%s' "${base%.*}" | tr '[:lower:]' '[:upper:]')"; return
  fi
  case "$(lower "$base")" in
    readme.md) printf '%s/README.md\n' "$dir" | sed 's#^\./##'; return ;;
    license) printf '%s/LICENSE\n' "$dir" | sed 's#^\./##'; return ;;
    changelog.md) printf '%s/CHANGELOG.md\n' "$dir" | sed 's#^\./##'; return ;;
  esac
  printf '%s\n' "$low" | tr '_ ' '--'
}

case_sensitive() {
  local probe upper
  probe=$(mktemp "$top/.warp-case-probe-XXXXXX")
  upper="$(dirname "$probe")/$(basename "$probe" | tr '[:lower:]' '[:upper:]')"
  if [ "$upper" != "$probe" ] && [ -e "$upper" ]; then rm -f "$probe"; return 1; fi
  rm -f "$probe"; return 0
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo "?"; }

disk_name() {  # the name the filesystem really has for this path (case-insensitive match in its folder), or empty
  local dir base
  dir=$(dirname -- "$1"); base=$(basename -- "$1")
  [ -d "$dir" ] || return 0
  ls -1A "$dir" 2>/dev/null | awk -v want="$(lower "$base")" 'tolower($0) == want { print; exit }'
}

exclude_backup() {
  local ex
  ex="$(git rev-parse --git-path info/exclude)"
  mkdir -p "$(dirname "$ex")"
  grep -qxF ".backup/" "$ex" 2>/dev/null || printf '%s\n' ".backup/" >> "$ex"
}

to_canonical_on_disk() {  # rename the file on disk to the canonical casing (no-op when it already has it)
  local want="$1" have
  have=$(disk_name "$want")
  if [ -n "$have" ] && [ "$have" != "$(basename -- "$want")" ]; then
    mv -f -- "$(dirname -- "$want")/$have" "$want.case-tmp"
    mv -f -- "$want.case-tmp" "$want"
    return 0
  fi
  return 1
}

fix_one() {
  local want="$1" entries count wantlow
  wantlow=$(lower "$want")
  entries=$(git ls-files | awk -v w="$wantlow" 'tolower($0) == w')
  count=$(printf '%s\n' "$entries" | grep -c . || true)
  if [ "$count" -ge 2 ]; then
    local stamp dir backup merged first=1 e ordered sensitive=0
    case_sensitive && sensitive=1
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    dir=$(dirname -- "$want")
    backup="$dir/.backup/$stamp"
    mkdir -p "$backup"
    exclude_backup
    merged=$(mktemp)
    : > "$backup/MTIMES"
    # The canonical spelling first, then the others in index order.
    ordered=$( { printf '%s\n' "$entries" | grep -xF -- "$want" || true; printf '%s\n' "$entries" | grep -vxF -- "$want" || true; } )
    while IFS= read -r e; do
      [ -z "$e" ] && continue
      git show ":$e" > "$backup/$(basename -- "$e").index"
      if [ -f "$e" ] && { [ "$sensitive" = 1 ] || [ "$(disk_name "$e")" = "$(basename -- "$e")" ]; }; then
        cp -p -- "$e" "$backup/$(basename -- "$e")"
        printf '%s\t%s\n' "$e" "$(mtime "$e")" >> "$backup/MTIMES"
      fi
      if [ "$first" = 1 ]; then first=0; else printf '\n---\n\n' >> "$merged"; fi
      cat "$backup/$(basename -- "$e").index" >> "$merged"
    done <<< "$ordered"
    while IFS= read -r e; do
      { [ -z "$e" ] || [ "$e" = "$want" ]; } && continue
      git rm -q --cached -- "$e"
      if [ "$sensitive" = 1 ] && [ -f "$e" ]; then rm -f -- "$e"; fi
    done <<< "$entries"
    to_canonical_on_disk "$want" || true
    cat "$merged" > "$want"
    rm -f "$merged"
    git add -- "$want"
    echo "fix-case: merged $count spellings into $want (backup: $backup)"
    return 0
  fi
  if [ "$count" = 1 ] && [ "$entries" != "$want" ]; then
    git mv -f -- "$entries" "$want.case-tmp"
    git mv -f -- "$want.case-tmp" "$want"
    echo "fix-case: renamed $entries -> $want"
    return 0
  fi
  if [ "$count" = 1 ] && to_canonical_on_disk "$want"; then
    echo "fix-case: renamed the file on disk to $want (the index already had it)"
    return 0
  fi
  echo "fix-case: $want is canonical; nothing to fix"
}

# The 2-line canonical-casing note at the top of docs/spec/GATES.md (after any front matter), once.
NOTE1='<!-- Canonical casing: this file is docs/spec/GATES.md (uppercase). Never create docs/spec/gates.md: on macOS and Windows it is this same file, on Linux a second one. -->'
NOTE2='<!-- Warp writes it through scripts/paths.py SPEC_GATES_MD (safe_write_spec_md); the pre-commit and pre-push hooks refuse a case collision. -->'
gates_note() {
  local f="docs/spec/GATES.md" tmpf
  [ -f "$f" ] && git ls-files --error-unmatch -- "$f" >/dev/null 2>&1 || return 0
  grep -qF "Canonical casing: this file is docs/spec/GATES.md" "$f" && return 0
  tmpf=$(mktemp)
  if [ "$(head -n 1 "$f")" = "---" ]; then
    awk -v a="$NOTE1" -v b="$NOTE2" 'NR == 1 { print; next } !done && $0 == "---" { print; print a; print b; done = 1; next } { print }' "$f" > "$tmpf"
  else
    { printf '%s\n%s\n' "$NOTE1" "$NOTE2"; cat "$f"; } > "$tmpf"
  fi
  cat "$tmpf" > "$f"; rm -f "$tmpf"
  git add -- "$f"
  echo "fix-case: added the canonical-casing note to $f"
}

if [ "$all" = 1 ]; then
  groups=$(git ls-files | awk '{ l = tolower($0); n[l]++; if (!(l in first)) first[l] = $0 } END { for (l in n) if (n[l] > 1) print first[l] }')
  specs=$(git ls-files | awk 'tolower($0) ~ /^docs\/spec\/(gates|decisions|changes|gap-report|infra|processes|seed|versioning|verification|docs-in-code)\.md$/')
  todo=$( { printf '%s\n' "$groups"; printf '%s\n' "$specs"; } | while IFS= read -r p; do [ -n "$p" ] && canonical "$p"; done | sort -u)
  if [ -z "$todo" ]; then echo "fix-case: no case collisions"; exit 0; fi
  while IFS= read -r want; do [ -n "$want" ] && fix_one "$want"; done <<< "$todo"
else
  fix_one "$(canonical "$target")"
fi
gates_note
