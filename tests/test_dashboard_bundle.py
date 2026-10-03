"""bin/build-dashboard-bundle.sh and bin/dashboard-stage.sh (ANDURIL 5.3).

Fixture source trees only: no network, no real Next build, no HOME. The build
command is replaced through DASHBOARD_BUILD_CMD and `npm` is a stub. The
household denylist is the dashboard's own (scripts/check-package-bundle.mjs in
the source tree, run by the bundle script); the fixture ships a one-needle
stand-in for it, and the real one is exercised by the slow test below when a
fetched source tarball is present.
"""
import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
BUNDLE = ROOT / "bin" / "build-dashboard-bundle.sh"
STAGE = ROOT / "bin" / "dashboard-stage.sh"
SHA = "c" * 40

pytestmark = pytest.mark.skipif(not shutil.which("node") or not shutil.which("git"),
                                reason="needs node and git")

CHECK_STUB = (
    "// stand-in for karakos-dashboard scripts/check-package-bundle.mjs (fixture)\n"
    "import { readdirSync, readFileSync, statSync } from 'node:fs';\n"
    "import { join } from 'node:path';\n"
    "let bad = 0;\n"
    "const walk = (d) => { for (const n of readdirSync(d)) { const p = join(d, n);\n"
    "  if (statSync(p).isDirectory()) walk(p);\n"
    "  else if (/readShardsFile/.test(readFileSync(p, 'utf-8'))) { console.error('household: ' + p); bad = 1; } } };\n"
    "walk(process.argv[2]); process.exit(bad);\n"
)


def _make_source(tmp, extra=None):
    src = tmp / "srcroot" / "dash-src"
    (src / "scripts").mkdir(parents=True)
    (src / "app").mkdir()
    (src / "lib").mkdir()
    (src / "public").mkdir()
    (src / "app" / "page.tsx").write_text("export default function P(){return null}\n")
    (src / "lib" / "x.ts").write_text("export const x = 1\n")
    (src / "public" / "icon.txt").write_text("i\n")
    (src / "package.json").write_text('{"name":"d"}\n')
    (src / "package-lock.json").write_text("{}\n")
    (src / "next.config.mjs").write_text("export default {}\n")
    (src / "scripts" / "check-package-bundle.mjs").write_text(CHECK_STUB)
    for rel, text in (extra or {}).items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    tgz = tmp / "src.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(src, arcname="mcarmody-karakos-dashboard-cccccccc")
    pins = tmp / "pins"
    pins.mkdir(exist_ok=True)
    (pins / "dashboard.ref").write_text(SHA + "\n")
    (pins / "dashboard.ref.sha256").write_text(hashlib.sha256(tgz.read_bytes()).hexdigest() + "\n")
    return tgz, pins


def _bundle(tmp, build_cmd, **kw):
    tgz, pins = _make_source(tmp, **kw)
    out = tmp / "out"
    env = {**os.environ, "HOME": str(tmp / "home"), "DASHBOARD_PIN_DIR": str(pins),
           "DASHBOARD_BUILD_CMD": build_cmd, "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["bash", str(BUNDLE), "--src", str(tgz), "--out-dir", str(out)],
                       env=env, capture_output=True, text=True)
    return r, out


GOOD_BUILD = "mkdir -p .next/server .next/cache && echo ok > .next/server/a.js && echo c > .next/cache/c"


def test_bundle_excludes_source_cache_and_has_runtime_files(tmp_path):
    r, out = _bundle(tmp_path, GOOD_BUILD)
    assert r.returncode == 0, r.stderr
    f = out / f"karakos-dashboard-bundle-{SHA[:12]}.tar.gz"
    assert f.exists() and (out / (f.name + ".sha256")).exists()
    names = tarfile.open(f).getnames()
    top = {n.split("/")[1] for n in names if "/" in n}
    assert {".next", "public", "package.json", "package-lock.json", "next.config.mjs"} <= top
    assert not top & {"app", "pages", "components", "src", "lib", "scripts"}
    assert not any(n.endswith(".map") for n in names)
    assert not any(".next/cache" in n for n in names)
    want = (out / (f.name + ".sha256")).read_text().split()[0]
    assert want == hashlib.sha256(f.read_bytes()).hexdigest()


def test_bundle_fails_on_source_map(tmp_path):
    r, out = _bundle(tmp_path, GOOD_BUILD + " && echo m > .next/server/a.js.map")
    assert r.returncode != 0
    assert ".map" in r.stderr
    assert not list(out.glob("*.tar.gz"))


def test_bundle_fails_when_household_code_is_in_the_output(tmp_path):
    r, out = _bundle(tmp_path, GOOD_BUILD + " && echo readShardsFile > .next/server/b.js")
    assert r.returncode != 0
    assert "household" in r.stderr
    assert not list(out.glob("*.tar.gz"))


def test_bundle_rejects_unpinned_source_tarball(tmp_path):
    tgz, pins = _make_source(tmp_path)
    (pins / "dashboard.ref.sha256").write_text("0" * 64 + "\n")
    r = subprocess.run(["bash", str(BUNDLE), "--src", str(tgz), "--out-dir", str(tmp_path / "o")],
                       env={**os.environ, "DASHBOARD_PIN_DIR": str(pins), "DASHBOARD_BUILD_CMD": GOOD_BUILD},
                       capture_output=True, text=True)
    assert r.returncode != 0 and "dashboard.ref.sha256" in r.stderr


# --- Dockerfile stage path selection (bin/dashboard-stage.sh) -----------------

def _fake_npm(tmp):
    bindir = tmp / "fakebin"
    bindir.mkdir()
    npm = bindir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f'echo "npm $*" >> "{tmp}/npm.log"\n'
        'case "$*" in\n'
        '  ci*) mkdir -p node_modules/better-sqlite3 ;;\n'
        '  "run build:package") mkdir -p .next public && echo ok > .next/built.js ;;\n'
        'esac\n')
    npm.chmod(0o755)
    return bindir


def _stage(tmp, inputs, env_extra=None):
    ind = tmp / "in"
    ind.mkdir()
    pins = tmp / "pins"
    for n in ("dashboard.ref", "dashboard.ref.sha256"):
        shutil.copy(pins / n, ind / n)
    for p in inputs:
        shutil.copy(p, ind / p.name)
    env = {**os.environ, "PATH": f"{_fake_npm(tmp)}:{os.environ['PATH']}", "HOME": str(tmp / "home"),
           "GIT_CONFIG_GLOBAL": "/dev/null", **(env_extra or {})}
    outd = tmp / "stage-out"
    r = subprocess.run(["sh", str(STAGE), str(ind), str(outd)], env=env, capture_output=True, text=True)
    log = (tmp / "npm.log").read_text() if (tmp / "npm.log").exists() else ""
    return r, outd, log, ind


def _real_bundle(tmp):
    r, out = _bundle(tmp, GOOD_BUILD)
    assert r.returncode == 0, r.stderr
    return next(out.glob("*.tar.gz"))


def test_stage_uses_bundle_without_building(tmp_path):
    bundle = _real_bundle(tmp_path)
    sha = bundle.with_name(bundle.name + ".sha256")
    r, outd, log, ind = _stage(tmp_path, [bundle, sha])
    assert r.returncode == 0, r.stderr
    assert "npm ci --omit=dev" in log
    assert "build" not in log, log
    for p in (".next", "node_modules", "public", "package.json", "next.config.mjs", ".dashboard-ref"):
        assert (outd / p).exists(), p
    assert (outd / ".dashboard-ref").read_text().strip() == SHA


def test_stage_builds_from_source_when_no_bundle(tmp_path):
    tgz, pins = _make_source(tmp_path)
    r, outd, log, _ = _stage(tmp_path, [tgz.rename(tgz.with_name("karakos-dashboard.tar.gz"))])
    assert r.returncode == 0, r.stderr
    assert "npm run build:package" in log
    assert (outd / ".next" / "built.js").exists()
    assert (outd / ".dashboard-ref").read_text().strip() == SHA


def test_stage_fails_clearly_with_no_input(tmp_path):
    _make_source(tmp_path)
    r, _, _, _ = _stage(tmp_path, [])
    assert r.returncode != 0 and "no dashboard input" in r.stderr


def test_stage_rejects_tampered_bundle(tmp_path):
    bundle = _real_bundle(tmp_path)
    sha = bundle.with_name(bundle.name + ".sha256")
    sha.write_text("0" * 64 + "  " + bundle.name + "\n")
    r, _, log, _ = _stage(tmp_path, [bundle, sha])
    assert r.returncode != 0 and "sha256" in r.stderr
    assert "npm" not in log


def test_stage_rejects_bundle_for_a_different_ref(tmp_path):
    bundle = _real_bundle(tmp_path)
    sha = bundle.with_name(bundle.name + ".sha256")
    r, _, _, _ = _stage(tmp_path, [bundle, sha], {"DASHBOARD_REF": "d" * 40})
    assert r.returncode != 0 and "does not match ref" in r.stderr


def test_stage_rejects_source_with_wrong_hash(tmp_path):
    tgz, pins = _make_source(tmp_path)
    (pins / "dashboard.ref.sha256").write_text("0" * 64 + "\n")
    r, _, log, _ = _stage(tmp_path, [tgz.rename(tgz.with_name("karakos-dashboard.tar.gz"))])
    assert r.returncode != 0 and "sha256" in r.stderr
    assert "npm" not in log


@pytest.mark.slow
def test_real_pinned_source_builds_a_clean_bundle(tmp_path):
    """Needs vendor/karakos-dashboard.tar.gz (bin/fetch-dashboard.sh, token) and
    network for npm. Runs the dashboard's real build and its real bundle check."""
    src = ROOT / "vendor" / "karakos-dashboard.tar.gz"
    if not src.exists():
        pytest.skip("no fetched dashboard source in vendor/")
    r = subprocess.run(["bash", str(BUNDLE), "--src", str(src), "--out-dir", str(tmp_path)],
                       capture_output=True, text=True, timeout=1500)
    assert r.returncode == 0, r.stderr[-2000:]
    names = tarfile.open(next(tmp_path.glob("*.tar.gz"))).getnames()
    assert not any(n.endswith(".map") for n in names)
