#!/usr/bin/env bash
# Build the pruned dashboard BUNDLE from the pinned source tarball.
#
#   bin/build-dashboard-bundle.sh [--src vendor/karakos-dashboard.tar.gz] [--out-dir vendor]
#
# The dashboard source is never published; a release publishes only this
# bundle: .next (no cache, no standalone copy, no trace), public, package.json,
# package-lock.json and a compiled next.config.mjs. No app/, pages/,
# components/, lib/ or src/ source, and no *.map files. The result is
# <out-dir>/karakos-dashboard-bundle-<sha12>.tar.gz plus a .sha256 beside it.
#
# Runs the source tree's own scripts/check-package-bundle.mjs (5.1) on the
# packed .next, so a household route/module in the bundle fails the build.
# Env: DASHBOARD_PIN_DIR (dashboard.ref* location; default repo root),
#      DASHBOARD_BUILD_CMD (default: npm ci && npm run build:package; a test
#      hook), DASHBOARD_VERIFY=0 to skip the pinned-sha256 check of --src.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN_DIR="${DASHBOARD_PIN_DIR:-$ROOT}"
SRC="$ROOT/vendor/karakos-dashboard.tar.gz"
OUT_DIR="$ROOT/vendor"
BUILD_CMD="${DASHBOARD_BUILD_CMD:-npm ci && npm run build:package}"

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="${2:?--src needs a value}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?--out-dir needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "build-dashboard-bundle: unknown argument: $1" >&2; exit 2 ;;
  esac
done
die() { echo "build-dashboard-bundle: $*" >&2; exit 1; }

[ -f "$SRC" ] || die "source tarball not found: $SRC (run bin/fetch-dashboard.sh)"
REF="$(tr -d '[:space:]' < "$PIN_DIR/dashboard.ref")"
SHA12="${REF:0:12}"
if [ "${DASHBOARD_VERIFY:-1}" = 1 ]; then
  [ "$(tr -d '[:space:]' < "$PIN_DIR/dashboard.ref.sha256")" = "$(sha256sum "$SRC" | cut -d' ' -f1)" ] \
    || die "source tarball does not match dashboard.ref.sha256"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src" "$WORK/stage" "$OUT_DIR"
tar -xzf "$SRC" -C "$WORK/src" --strip-components=1

( cd "$WORK/src" && \
  { git init -q && git add -A && git -c user.email=build@localhost -c user.name=build commit -qm source; } \
  && bash -c "$BUILD_CMD" ) >&2   # (git: build:package prunes only a clean git tree)
[ -d "$WORK/src/.next" ] || die "build produced no .next"

S="$WORK/src"; B="$WORK/stage"
# Source maps expose the source: refuse, do not strip silently.
if [ -n "$(find "$S/.next" -name '*.map' -print -quit)" ]; then
  die "source map (*.map) found in .next; productionBrowserSourceMaps must be false"
fi

cp -a "$S/.next" "$B/.next"
rm -rf "$B/.next/cache" "$B/.next/standalone" "$B/.next/trace" "$B/.next/trace-build"
[ -d "$S/public" ] && cp -a "$S/public" "$B/public"
cp -a "$S/package.json" "$S/package-lock.json" "$B/"

# next.config.ts needs typescript at start, which --omit=dev does not install:
# ship it compiled. The config holds no household code (profile gate only).
if [ -f "$S/next.config.ts" ]; then
  ( cd "$S" && node -e '
    const ts = require("typescript"), fs = require("fs");
    const out = ts.transpileModule(fs.readFileSync("next.config.ts", "utf8"),
      { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
    fs.writeFileSync(process.argv[1], out.outputText);
  ' "$B/next.config.mjs" )
elif [ -f "$S/next.config.mjs" ]; then cp -a "$S/next.config.mjs" "$B/"
elif [ -f "$S/next.config.js" ]; then cp -a "$S/next.config.js" "$B/"
fi

# Reuse 5.1's check (its own denylist/inventory) on what we are about to ship.
if [ -f "$S/scripts/check-package-bundle.mjs" ]; then
  ( cd "$S" && node scripts/check-package-bundle.mjs "$B/.next" ) >&2 \
    || die "bundle check failed: household code in the bundle"
else
  die "source tree has no scripts/check-package-bundle.mjs"
fi

# Belt and braces: nothing source-shaped at the top level of the bundle.
for d in app pages components src lib; do
  [ ! -e "$B/$d" ] || die "bundle contains source dir $d/"
done

BUNDLE="$OUT_DIR/karakos-dashboard-bundle-${SHA12}.tar.gz"
# Deterministic packing (sorted, fixed mtime/owner, gzip -n) so a rebuild of
# identical output yields an identical hash.
( cd "$B" && tar --sort=name --mtime='2020-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner -cf - . ) | gzip -n -9 > "$BUNDLE"
( cd "$OUT_DIR" && sha256sum "$(basename "$BUNDLE")" > "$(basename "$BUNDLE").sha256" )
echo "build-dashboard-bundle: wrote ${BUNDLE#"$ROOT"/} ($(cut -c1-12 "$BUNDLE.sha256"))"
