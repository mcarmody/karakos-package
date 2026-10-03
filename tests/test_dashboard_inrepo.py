"""The dashboard is source in this repo (dashboard/), built by the Dockerfile
and by the `dashboard` CI job. These tests pin that shape: the lockfile is in
step with package.json, the image stage builds from dashboard/, nothing still
fetches, pins or bundles a dashboard from elsewhere, and the copy carries no
household identifiers.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DASH = ROOT / "dashboard"
DOCKERFILE = (ROOT / "Dockerfile").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()


def _tracked(*paths):
    out = subprocess.run(
        ["git", "ls-files", *paths], cwd=str(ROOT), capture_output=True, text=True, check=True
    ).stdout
    return [l for l in out.splitlines() if l]


def _runtime_stage():
    return DOCKERFILE[DOCKERFILE.rindex("\nFROM "):]


# --- the source ---------------------------------------------------------------

def test_dashboard_source_is_tracked():
    tracked = set(_tracked("dashboard"))
    for needed in ["dashboard/package.json", "dashboard/package-lock.json", "dashboard/next.config.ts",
                   "dashboard/tsconfig.json", "dashboard/middleware.ts", "dashboard/app/layout.tsx",
                   "dashboard/README.md"]:
        assert needed in tracked, f"{needed} is not tracked"
    assert not any("node_modules/" in t or t.startswith("dashboard/.next/") for t in tracked)


def test_lockfile_matches_package_json():
    """`npm ci` fails on a lockfile that disagrees with package.json."""
    pkg = json.loads((DASH / "package.json").read_text())
    lock = json.loads((DASH / "package-lock.json").read_text())
    root = lock["packages"][""]
    assert lock["name"] == pkg["name"] == root["name"]
    assert root.get("dependencies", {}) == pkg.get("dependencies", {})
    assert root.get("devDependencies", {}) == pkg.get("devDependencies", {})


def test_package_json_has_the_scripts_ci_and_the_image_run():
    scripts = json.loads((DASH / "package.json").read_text())["scripts"]
    for name in ("build", "start", "test"):
        assert name in scripts
    assert scripts["build"] == "next build"
    assert "${DASHBOARD_PORT" in scripts["start"]


def test_no_profile_gate_or_household_gating_machinery():
    for gone in ["lib/profile.ts", "lib/profileGate.ts", "profile-routes.json", "profile-inventory.json",
                 "profile-swaps.json", "scripts/prune-profile.mjs", "lib/profile-stubs"]:
        assert not (DASH / gone).exists(), f"dashboard/{gone} should not exist"
    sources = [p for p in DASH.rglob("*") if p.suffix in {".ts", ".tsx", ".mjs"} and "node_modules" not in p.parts
               and ".next" not in p.parts]
    offenders = [str(p.relative_to(ROOT)) for p in sources if "KARAKOS_PROFILE" in p.read_text()]
    assert not offenders, f"KARAKOS_PROFILE is read by {offenders}"


def test_dashboard_copy_carries_no_household_identifiers():
    # Fragments are joined at runtime so this file does not trip the same scan.
    words = ["gide" + "on", "lau" + "ren", "carm" + "ody", "mike" + "carmody", "192." + "168", "kit" + "chen",
             "gar" + "den", "pan" + "try", "jobh" + "unt", "lam" + "men", "palan" + "tir", "amo" + "s-", "her" + "ald"]
    pat = re.compile("|".join(re.escape(w) for w in words), re.I)
    hits = []
    for rel in _tracked("dashboard"):
        p = ROOT / rel
        if p.suffix in {".woff2", ".png", ".ico"} or p.name == "package-lock.json":
            continue
        for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{rel}:{n}")
    assert not hits, hits[:10]


# --- nothing fetches, pins or bundles a dashboard from elsewhere ------------------

# Names of the removed fetch/pin/bundle machinery, joined at runtime so this
# file does not itself contain them.
_FETCH = "fetch" + "-dashboard"
_BUNDLE = "build-dashboard" + "-bundle"
_STAGE = "dashboard" + "-stage"
_REF = "dashboard" + ".ref"
_TOKEN = "DASHBOARD_FETCH" + "_TOKEN"


def test_fetch_pin_and_bundle_machinery_is_gone():
    for gone in [f"bin/{_FETCH}.sh", f"bin/{_BUNDLE}.sh", f"bin/{_STAGE}.sh",
                 _REF, _REF + ".sha256", "dashboard.bundle.sha256", "vendor"]:
        assert not (ROOT / gone).exists(), f"{gone} should not exist"
    assert "vendor-" + "dashboard" not in (ROOT / "Makefile").read_text()
    assert "vendor/" not in (ROOT / ".gitignore").read_text().split()


def test_workflows_use_no_dashboard_secret_or_bundle():
    for name, text in (("ci.yml", CI), ("release.yml", RELEASE)):
        assert _TOKEN not in text, name
        assert _REF not in text, name
        assert "dashboard-bundle" not in text, name
    assert re.search(r"^\s*contents:\s*read\b", RELEASE, re.M)
    assert not re.search(r"^\s*contents:\s*write\b", RELEASE, re.M)
    assert "DASHBOARD_REF" not in DOCKERFILE and "DASHBOARD_REF" not in RELEASE


# --- the image ---------------------------------------------------------------

def test_dashboard_stage_builds_from_the_in_repo_source():
    stage = DOCKERFILE[DOCKERFILE.index("AS dashboard-build"):DOCKERFILE.index("AS workspace-src")]
    assert "COPY dashboard/package.json dashboard/package-lock.json" in stage
    assert re.search(r"^RUN npm ci\b", stage, re.M)
    assert re.search(r"^COPY dashboard/ \./", stage, re.M)
    assert "npm run build" in stage
    assert "npm prune --omit=dev" in stage
    # Source maps stay out of the image; the native modules are smoke-checked in the stage.
    assert "*.map" in stage
    assert "require('better-sqlite3'); require('sqlite3')" in stage


def test_build_and_runtime_stages_share_node_major_on_bookworm():
    first_from = DOCKERFILE.index("\nFROM ")
    assert "ARG NODE_MAJOR=" in DOCKERFILE[:first_from]
    assert re.search(r"^FROM node:\$\{NODE_MAJOR\}-bookworm-slim AS dashboard-build", DOCKERFILE, re.M)
    assert re.search(r"^FROM python:[\d.]+-slim-bookworm", DOCKERFILE, re.M)
    assert "setup_${NODE_MAJOR}.x" in DOCKERFILE
    assert "setup_20.x" not in DOCKERFILE and "node:20" not in DOCKERFILE


def test_copy_sources_from_the_stage_exist_in_the_dashboard():
    sources = re.findall(r"COPY\s+(?:--\S+\s+)*--from=dashboard-build\s+(\S+)\s+\S+", DOCKERFILE)
    assert sources, "no COPY --from=dashboard-build lines found"
    produced_by_build = {".next", "node_modules"}
    for src in sources:
        assert src.startswith("/app/"), src
        name = src[len("/app/"):]
        if name in produced_by_build:
            continue
        assert (DASH / name).exists(), f"{src}: dashboard/{name} does not exist"


def test_runtime_stage_does_not_receive_the_raw_dashboard_source():
    assert "rm -rf /src/dashboard" in DOCKERFILE
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert "dashboard/node_modules" in ignore and "dashboard/.next" in ignore
    assert not any(x.strip("/") == "dashboard" for x in ignore), "dashboard/ must stay in the build context"


def test_runtime_stage_sets_dashboard_paths():
    rt = _runtime_stage()
    assert "KARAKOS_REGISTRY_PATH=/workspace/config/agents.yaml" in rt
    assert "WORKSPACE_ROOT=/workspace" in rt
    assert "KARAKOS_PROFILE" not in DOCKERFILE


def test_agent_server_db_path_matches_agent_server():
    m = re.search(r"AGENT_SERVER_DB_PATH=(\S+)", _runtime_stage())
    assert m, "AGENT_SERVER_DB_PATH not set in the runtime stage"
    server = (ROOT / "bin" / "agent-server.py").read_text()
    assert re.search(r'DB_PATH\s*=\s*WORKSPACE_ROOT\s*/\s*"data"\s*/\s*"memory"\s*/\s*"agent-server.db"', server)
    assert m.group(1) == "/workspace/data/memory/agent-server.db"


# --- CI ---------------------------------------------------------------------

def _job(name):
    m = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % re.escape(name), CI, re.M | re.S)
    assert m, f"no {name} job in ci.yml"
    return m.group(1)


def test_ci_has_a_dashboard_job_that_tests_and_builds():
    job = _job("dashboard")
    assert "working-directory: dashboard" in job
    assert "npm ci" in job and "npm test" in job and "npm run build" in job
    assert "secrets." not in job


def test_docker_smoke_needs_no_secret_and_checks_the_http_contract():
    job = _job("docker-smoke")
    assert "secrets." not in job and "HAS_DASHBOARD_TOKEN" not in job
    assert "if: env." not in job, "docker-smoke steps must always run"
    assert "docker build -t karakos-test:smoke ." in job
    assert "/workspace/dashboard/.next/BUILD_ID" in job
    assert "better-sqlite3" in job and "sqlite3" in job
    assert re.search(r"/login\)\" = 200", job)
    assert re.search(r"/api/agents\)\" = 401", job)
    assert re.search(r"-b jar http://localhost:3000/system\)\" = 404", job)
