#!/usr/bin/env bash
# 1.7.24: the no-case-collisions hook. Run: bash tests/test_no_case_collisions.sh  (exit 0 = pass)
set -u
here="$(cd "$(dirname "$0")" && pwd)"
hook="$here/../githooks/no-case-collisions.sh"
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

newrepo() {
  rm -rf "$tmp/r"; mkdir -p "$tmp/r"; cd "$tmp/r"
  git init -q -b main .
  git config user.name T; git config user.email t@t; git config commit.gpgsign false
  git config core.ignoreCase false
}
check() {  # name, expected exit, must-contain
  local name="$1" want="$2" needle="$3" out code
  out=$(bash "$hook" "${4:-pre-commit}" 2>&1); code=$?
  if [ "$code" != "$want" ]; then echo "FAIL $name: exit $code, want $want: $out"; fail=1; return; fi
  if [ -n "$needle" ] && ! printf '%s' "$out" | grep -qF -- "$needle"; then echo "FAIL $name: output lacks '$needle': $out"; fail=1; return; fi
  echo "ok   $name"
}

# 1. Two staged files that differ only in case.
newrepo
mkdir -p docs/spec
echo a > docs/spec/x.md
echo b > docs/spec/X.md
if [ -f docs/spec/x.md ] && [ "$(cat docs/spec/x.md)" = "b" ]; then
  # Case-insensitive filesystem: build the two index entries directly.
  git update-index --add --cacheinfo 100644,"$(printf a | git hash-object -w --stdin)",docs/spec/x.md
  git update-index --add --cacheinfo 100644,"$(printf b | git hash-object -w --stdin)",docs/spec/X.md
else
  git add docs/spec/x.md docs/spec/X.md
fi
check "staged pair is refused" 1 "docs/spec/X.md  <->  docs/spec/x.md"

# 2. Only docs/spec/x.md staged; docs/spec/X.md already in HEAD (index + tree).
newrepo
mkdir -p docs/spec
git update-index --add --cacheinfo 100644,"$(printf b | git hash-object -w --stdin)",docs/spec/X.md
git commit -qm base
git update-index --add --cacheinfo 100644,"$(printf a | git hash-object -w --stdin)",docs/spec/x.md
check "staged against HEAD is refused" 1 "docs/spec/X.md  <->  docs/spec/x.md"

# 3. GATES.md / gates.md names the canonical spelling and make fix-gates.
newrepo
mkdir -p docs/spec
git update-index --add --cacheinfo 100644,"$(printf a | git hash-object -w --stdin)",docs/spec/GATES.md
git commit -qm base
git update-index --add --cacheinfo 100644,"$(printf b | git hash-object -w --stdin)",docs/spec/gates.md
check "gates pair names the fix" 1 "make fix-gates"
check "gates pair names the canonical spelling" 1 "canonical: docs/spec/GATES.md"

# 4. A lone non-canonical spec name is refused; a clean repo passes; pre-push checks HEAD's tree.
newrepo
mkdir -p docs/spec
git update-index --add --cacheinfo 100644,"$(printf a | git hash-object -w --stdin)",docs/spec/gates.md
check "lone lowercase spec file is refused" 1 "use docs/spec/GATES.md"
newrepo
echo ok > README.md; git add README.md; git commit -qm base
check "clean repo passes" 0 ""
git update-index --add --cacheinfo 100644,"$(printf a | git hash-object -w --stdin)",readme.md
git commit -qm twin --no-verify
check "pre-push refuses a tree with a collision" 1 "README.md  <->  readme.md" pre-push

exit $fail
