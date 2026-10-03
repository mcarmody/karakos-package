#!/usr/bin/env bash
# Build tests/migrate/fixtures/<tag>.tar.gz: a throwaway 1.x install for <tag>,
# seeded with five agents (one custom), queue rows in every status, sessions,
# 40 memory episodes and 10 facts, a custom hook and a custom .env variable.
#
#   tools/make-fixture.sh <tag>            from a git archive of the tag; the tag's own
#                                          schema code builds the databases. No Docker.
#   tools/make-fixture.sh <tag> --docker   builds the tagged image under a unique compose
#                                          project (karakos-fx-<tag>-<rand>) and seeds in it.
#
# Never touches a real install or HOME: everything lives in a temp dir.
set -euo pipefail
TAG="${1:?usage: make-fixture.sh <tag> [--docker]}"
MODE="${2:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/tests/migrate/fixtures/$TAG.tar.gz"
TMP="$(mktemp -d)"
RAND="$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
PROJECT="karakos-fx-${TAG//./-}-$RAND"
cleanup() {
  if [ "$MODE" = "--docker" ]; then
    docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
    docker image rm -f "$PROJECT" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$TMP/tree" "$(dirname "$OUT")"
git -C "$REPO" archive "$TAG" | tar -x -C "$TMP/tree"
if [ "$MODE" = "--docker" ]; then
  docker build -q -t "$PROJECT" "$TMP/tree" >/dev/null
  mkdir -p "$TMP/out"
  docker run --rm --name "$PROJECT" -v "$REPO/tools:/seed:ro" -v "$TMP/out:/out" \
    --entrypoint python3 "$PROJECT" /seed/seed_fixture.py /workspace "$TAG" /out/fixture.tar.gz
  cp "$TMP/out/fixture.tar.gz" "$OUT"
else
  python3 "$REPO/tools/seed_fixture.py" "$TMP/tree" "$TAG" "$OUT"
fi
echo "$OUT"
