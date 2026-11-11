#!/usr/bin/env bash
# 1.7.24: fix_gates_case.sh. Run: bash tests/test_fix_gates_case.sh  (exit 0 = pass)
set -u
here="$(cd "$(dirname "$0")" && pwd)"
fix="$here/../scripts/fix_gates_case.sh"
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

newrepo() {
  rm -rf "$tmp/r"; mkdir -p "$tmp/r"; cd "$tmp/r"
  git init -q -b main .
  git config user.name T; git config user.email t@t; git config commit.gpgsign false
  git config core.ignoreCase false
}

# Both spellings committed (as a Linux pull request lands them).
newrepo
mkdir -p docs/spec
printf '# Gates (upper)\n' > docs/spec/GATES.md
printf '# gates (lower)\n' > docs/spec/gates.md
if [ "$(cat docs/spec/GATES.md)" = "# gates (lower)" ]; then
  echo "skip: case-insensitive filesystem; this test needs both files side by side"; exit 0
fi
touch -d '2026-11-01 10:00:00' docs/spec/GATES.md 2>/dev/null || touch -t 202611011000 docs/spec/GATES.md
touch -d '2026-11-02 11:00:00' docs/spec/gates.md 2>/dev/null || touch -t 202611021100 docs/spec/gates.md
upper_mtime=$(stat -c %Y docs/spec/GATES.md 2>/dev/null || stat -f %m docs/spec/GATES.md)
lower_mtime=$(stat -c %Y docs/spec/gates.md 2>/dev/null || stat -f %m docs/spec/gates.md)
git add -A && git commit -qm both

out=$(bash "$fix" 2>&1) || bad "fix exited non-zero: $out"
[ "$(git ls-files docs/spec | grep -ic '^docs/spec/gates.md$')" = 1 ] && [ "$(git ls-files docs/spec | grep -i gates)" = "docs/spec/GATES.md" ] \
  && ok "only docs/spec/GATES.md is in the index" || bad "index: $(git ls-files docs/spec)"
[ "$(ls docs/spec | grep -i '^gates.md$')" = "GATES.md" ] && ok "only GATES.md is on disk" || bad "disk: $(ls docs/spec)"
merged=$(cat docs/spec/GATES.md)
printf '%s' "$merged" | grep -qF '# Gates (upper)' && printf '%s' "$merged" | grep -qF '# gates (lower)' \
  && printf '%s\n' "$merged" | grep -qx -- '---' && ok "contents concatenated with a --- separator" || bad "merged: $merged"
backup=$(ls -d docs/spec/.backup/* 2>/dev/null | head -1)
[ -n "$backup" ] && [ -f "$backup/GATES.md" ] && [ -f "$backup/gates.md" ] && ok "both originals in .backup/" || bad "backup: $backup"
[ "$(stat -c %Y "$backup/GATES.md" 2>/dev/null || stat -f %m "$backup/GATES.md")" = "$upper_mtime" ] \
  && [ "$(stat -c %Y "$backup/gates.md" 2>/dev/null || stat -f %m "$backup/gates.md")" = "$lower_mtime" ] \
  && grep -q "docs/spec/gates.md	$lower_mtime" "$backup/MTIMES" && ok "original mtimes preserved" || bad "mtimes: $(cat "$backup/MTIMES")"
git commit -qm fix
[ -z "$(git status --porcelain)" ] && ok "git status clean (.backup/ excluded)" || bad "status: $(git status --porcelain)"

# Idempotent.
out=$(bash "$fix" 2>&1)
printf '%s' "$out" | grep -q "nothing to fix" && [ -z "$(git status --porcelain)" ] && ok "second run changes nothing" || bad "rerun: $out"

# One wrong spelling only: renamed to the canonical one.
newrepo
mkdir -p docs/spec
printf 'x\n' > docs/spec/gates.md
git add -A && git commit -qm lower
bash "$fix" >/dev/null 2>&1
[ "$(git ls-files docs/spec)" = "docs/spec/GATES.md" ] && ok "a lone gates.md is renamed to GATES.md" || bad "rename: $(git ls-files docs/spec)"

# --all fixes every pair.
newrepo
mkdir -p docs/spec
printf 'a\n' > docs/spec/DECISIONS.md; printf 'b\n' > docs/spec/decisions.md; printf 'c\n' > README.md; printf 'd\n' > readme.md
git add -A && git commit -qm pairs
bash "$fix" --all >/dev/null 2>&1
[ "$(git ls-files | sort | tr '\n' ' ')" = "README.md docs/spec/DECISIONS.md " ] && ok "--all fixes every pair" || bad "--all: $(git ls-files)"

exit $fail
