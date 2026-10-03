#!/bin/sh
# Dockerfile `dashboard-build` stage logic, as a script so it can be tested.
#
#   dashboard-stage.sh <in-dir> <out-dir>
#
# <in-dir> holds dashboard.ref, dashboard.ref.sha256, dashboard.bundle.sha256
# and optionally vendor files: karakos-dashboard.tar.gz (source) and/or
# karakos-dashboard-bundle-<sha12>.tar.gz (+ .sha256).
#
# Path selection: a prebuilt bundle, when present, is used as-is (npm ci
# --omit=dev for the native modules; NO build). Otherwise the source tarball is
# built here (npm ci, build:package, bundle check). Neither present: fail.
# Output in <out-dir>: .next node_modules public package.json next.config.*
# .dashboard-ref. No network fetch of the dashboard happens here.
# DASHBOARD_REF (env): when set, the input must be that commit.
set -eu

IN="${1:?in dir}"; OUT="${2:?out dir}"
die() { echo "dashboard-stage: $*" >&2; exit 1; }
hex() { tr -d ' \t\r\n' < "$1"; }

PIN_REF="$(hex "$IN/dashboard.ref")"
WANT_REF="${DASHBOARD_REF:-$PIN_REF}"
[ "${#WANT_REF}" -eq 40 ] || die "ref must be a 40-hex commit sha (got '$WANT_REF')"
WANT12="$(printf '%s' "$WANT_REF" | cut -c1-12)"

BUNDLE="$(ls "$IN"/karakos-dashboard-bundle-*.tar.gz 2>/dev/null | head -1 || true)"
SRC="$IN/karakos-dashboard.tar.gz"
mkdir -p "$OUT"
WORK="$(mktemp -d)"

embedded_commit() {
  gzip -dc "$1" | head -c 4096 | grep -a -o 'comment=[0-9a-f]\{40\}' | head -1 | cut -d= -f2 || true
}

if [ -n "$BUNDLE" ]; then
  echo "dashboard-stage: using prebuilt bundle $(basename "$BUNDLE") (no build)"
  case "$(basename "$BUNDLE")" in
    karakos-dashboard-bundle-"$WANT12".tar.gz) ;;
    *) die "bundle $(basename "$BUNDLE") does not match ref $WANT12" ;;
  esac
  GOT="$(sha256sum "$BUNDLE" | cut -d' ' -f1)"
  CHECKED=0
  if [ -f "$IN/dashboard.bundle.sha256" ] && grep -qE '^[0-9a-f]{64}' "$IN/dashboard.bundle.sha256" && [ "$WANT_REF" = "$PIN_REF" ]; then
    [ "$(cut -c1-64 "$IN/dashboard.bundle.sha256" | head -1)" = "$GOT" ] || die "bundle sha256 does not match dashboard.bundle.sha256"
    CHECKED=1
  fi
  if [ -f "$BUNDLE.sha256" ]; then
    [ "$(cut -c1-64 "$BUNDLE.sha256" | head -1)" = "$GOT" ] || die "bundle sha256 does not match $(basename "$BUNDLE").sha256"
    CHECKED=1
  fi
  [ "$CHECKED" = 1 ] || die "no sha256 to verify the bundle against (dashboard.bundle.sha256 or <bundle>.sha256)"
  tar -xzf "$BUNDLE" -C "$WORK"
  cd "$WORK"
  npm ci --omit=dev
elif [ -f "$SRC" ]; then
  echo "dashboard-stage: building from source tarball"
  GOT="$(sha256sum "$SRC" | cut -d' ' -f1)"
  if [ "$WANT_REF" = "$PIN_REF" ]; then
    [ "$(hex "$IN/dashboard.ref.sha256")" = "$GOT" ] || die "source tarball sha256 does not match dashboard.ref.sha256"
  fi
  EMB="$(embedded_commit "$SRC")"
  [ -z "$EMB" ] || [ "$EMB" = "$WANT_REF" ] || die "tarball commit $EMB does not equal ref $WANT_REF"
  tar -xzf "$SRC" -C "$WORK" --strip-components=1
  cd "$WORK"
# scripts/prune-profile.mjs (build:package) refuses to prune a tree that is not
# a clean git work tree; the tarball has no .git, so make one before building.
git init -q && git add -A && git -c user.email=build@localhost -c user.name=build commit -qm source
  npm ci
  npm run build:package
  node scripts/check-package-bundle.mjs .next
  [ -z "$(find .next -name '*.map' -print -quit)" ] || die "source map found in .next"
else
  die "no dashboard input: run bin/fetch-dashboard.sh (needs GH_TOKEN) or put a release bundle in vendor/"
fi

for p in .next node_modules public package.json; do
  [ -e "$p" ] || die "build produced no $p"
  cp -a "$p" "$OUT/$p"
done
rm -rf "$OUT/.next/cache"
for c in next.config.mjs next.config.js next.config.ts; do
  [ -e "$c" ] && cp -a "$c" "$OUT/$c"
done
printf '%s\n' "$WANT_REF" > "$OUT/.dashboard-ref"
rm -rf "$WORK"
