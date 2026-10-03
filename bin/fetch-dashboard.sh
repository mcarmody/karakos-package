#!/usr/bin/env bash
# Fetch the pinned karakos-dashboard source tarball and verify it.
#
#   bin/fetch-dashboard.sh [--ref <sha>] [--out vendor/karakos-dashboard.tar.gz]
#
# karakos-dashboard is a PRIVATE repository: the token comes from GH_TOKEN (or
# whatever `gh` is logged in with) in the environment, never from a file in
# this repo, and it is never printed. The tarball is never committed
# (vendor/ is gitignored) and never published.
#
# Default ref is the one line in dashboard.ref; the tarball is verified against
# dashboard.ref.sha256. With --ref <sha> that differs from the pin, only the
# commit embedded in the tarball is checked (the pinned hash belongs to the
# pinned ref). Exit non-zero on any mismatch.
#
# Env: DASHBOARD_REPO (default mcarmody/karakos-dashboard),
#      DASHBOARD_PIN_DIR (where dashboard.ref* live; default: repo root).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN_DIR="${DASHBOARD_PIN_DIR:-$ROOT}"
REPO="${DASHBOARD_REPO:-mcarmody/karakos-dashboard}"
OUT="$ROOT/vendor/karakos-dashboard.tar.gz"
REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --out) OUT="${2:?--out needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "fetch-dashboard: unknown argument: $1" >&2; exit 2 ;;
  esac
done

PIN_REF="$(tr -d '[:space:]' < "$PIN_DIR/dashboard.ref")"
[ -n "$REF" ] || REF="$PIN_REF"
case "$REF" in
  *[!0-9a-f]*|"") echo "fetch-dashboard: ref must be a full lowercase hex commit sha" >&2; exit 2 ;;
esac
[ "${#REF}" -eq 40 ] || { echo "fetch-dashboard: ref must be 40 hex chars" >&2; exit 2; }

command -v gh >/dev/null || { echo "fetch-dashboard: gh CLI not found" >&2; exit 2; }

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

if ! gh api "repos/${REPO}/tarball/${REF}" > "$TMP" 2>/dev/null; then
  echo "fetch-dashboard: download of ${REPO}@${REF:0:12} failed (is GH_TOKEN set with read access?)" >&2
  exit 1
fi

# Commit embedded in the tarball (pax global header written by git archive).
EMBEDDED="$(gzip -dc "$TMP" 2>/dev/null | head -c 4096 | grep -a -o 'comment=[0-9a-f]\{40\}' | head -1 | cut -d= -f2 || true)"
if [ -n "$EMBEDDED" ] && [ "$EMBEDDED" != "$REF" ]; then
  echo "fetch-dashboard: tarball commit ${EMBEDDED:0:12} does not match requested ${REF:0:12}" >&2
  exit 1
fi

if [ "$REF" = "$PIN_REF" ]; then
  WANT="$(tr -d '[:space:]' < "$PIN_DIR/dashboard.ref.sha256")"
  GOT="$(sha256sum "$TMP" | cut -d' ' -f1)"
  if [ "$WANT" != "$GOT" ]; then
    echo "fetch-dashboard: sha256 mismatch for ${REF:0:12}" >&2
    echo "  pinned: $WANT" >&2
    echo "  got:    $GOT" >&2
    exit 1
  fi
else
  echo "fetch-dashboard: ref differs from dashboard.ref; skipping pinned sha256 (commit check only)" >&2
fi

mv "$TMP" "$OUT"
trap - EXIT
echo "fetch-dashboard: wrote ${OUT#"$ROOT"/} (${REF:0:12})"
