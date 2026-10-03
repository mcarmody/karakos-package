#!/usr/bin/env bash
# Household-coupling check. Scans git-tracked files against
# system/coupling-denylist.txt and prints each hit as `file:line: pattern-name`.
# Exit 1 on any hit; prints `coupling: clean` and exits 0 otherwise.
# Run from anywhere inside the repo (or set COUPLING_ROOT).
set -u

ROOT="${COUPLING_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$ROOT" || exit 2
DENY="${COUPLING_DENYLIST:-$ROOT/system/coupling-denylist.txt}"
ALLOW="${COUPLING_ALLOW:-$ROOT/system/coupling-allow.txt}"
[ -f "$DENY" ] || { echo "coupling: denylist not found: $DENY" >&2; exit 2; }

if command -v rg >/dev/null 2>&1; then
  scan() { rg --no-config -nH --no-heading --color never -e "$1" -- "${@:2}" 2>/dev/null; }
else
  scan() { grep -InHE -e "$1" -- "${@:2}" 2>/dev/null; }
fi

FILES=$(mktemp); HITS=$(mktemp); trap 'rm -f "$FILES" "$HITS"' EXIT
# Skip the rule files themselves (they hold the patterns/names) and deleted files.
git ls-files -z | while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  case "$f" in system/coupling-denylist.txt|system/coupling-allow.txt) continue;; esac
  printf '%s\0' "$f"
done > "$FILES"

while IFS=$'\t' read -r name regex pathre linere || [ -n "${name:-}" ]; do
  case "${name:-}" in ''|'#'*) continue;; esac
  [ -n "${regex:-}" ] || continue
  if [ -n "${pathre:-}" ]; then
    mapfile -d '' -t list < <(tr '\0' '\n' < "$FILES" | grep -E -e "$pathre" | tr '\n' '\0')
  else
    mapfile -d '' -t list < "$FILES"
  fi
  [ "${#list[@]}" -gt 0 ] || continue
  scan "$regex" "${list[@]}" | while IFS= read -r line; do
    # line = file:lineno:content ; file paths are repo-relative
    if [ -n "${linere:-}" ]; then
      printf '%s\n' "$line" | cut -d: -f3- | grep -qiE -e "$linere" || continue
    fi
    printf '%s\t%s\n' "$name" "$line"
  done >> "$HITS"
done < "$DENY"

# Apply allow entries and print.
count=0
while IFS=$'\t' read -r name rest; do
  file=${rest%%:*}; r2=${rest#*:}; lineno=${r2%%:*}; content=${r2#*:}
  allowed=0
  if [ -f "$ALLOW" ]; then
    while IFS=$'\t' read -r glob are || [ -n "${glob:-}" ]; do
      case "${glob:-}" in ''|'#'*) continue;; esac
      # shellcheck disable=SC2254
      case "$file" in $glob) ;; *) continue;; esac
      if [ -z "${are:-}" ] || printf '%s\n' "$content" | grep -qE -e "$are"; then allowed=1; break; fi
    done < "$ALLOW"
  fi
  [ "$allowed" -eq 1 ] && continue
  echo "$file:$lineno: $name"
  count=$((count+1))
done < <(sort -u "$HITS")

if [ "$count" -gt 0 ]; then exit 1; fi
echo "coupling: clean"
