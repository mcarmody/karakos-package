"""Static checks on how the Dockerfile builds the dashboard (ANDURIL 5.3)."""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _runtime_stage():
    # The final stage starts at the last FROM.
    return DOCKERFILE[DOCKERFILE.rindex("\nFROM ") :]


def test_no_dashboard_source_dir_is_copied():
    assert not re.search(r"COPY\s+(?:--\S+\s+)*dashboard/", DOCKERFILE)
    # Nothing refers to a dashboard/ SOURCE directory in the build context;
    # the only dashboard/ paths are destinations inside /workspace.
    for line in DOCKERFILE.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"(?<![\w/.-])dashboard/", line):
            assert re.search(r"COPY\s.*\sdashboard/\S*\s*$", line) or "/workspace/dashboard" in line, line


def test_old_dashboard_tree_is_gone():
    assert not (ROOT / "dashboard").exists()


def test_runtime_stage_sets_package_profile_and_paths():
    rt = _runtime_stage()
    assert re.search(r"KARAKOS_PROFILE=package\b", rt)
    assert "KARAKOS_REGISTRY_PATH=/workspace/config/agents.yaml" in rt
    assert "WORKSPACE_ROOT=/workspace" in rt


def test_agent_server_db_path_matches_agent_server():
    m = re.search(r"AGENT_SERVER_DB_PATH=(\S+)", _runtime_stage())
    assert m, "AGENT_SERVER_DB_PATH not set in the runtime stage"
    server = (ROOT / "bin" / "agent-server.py").read_text()
    # agent-server: DB_PATH = WORKSPACE_ROOT / "data" / "memory" / "agent-server.db"
    assert re.search(r'DB_PATH\s*=\s*WORKSPACE_ROOT\s*/\s*"data"\s*/\s*"memory"\s*/\s*"agent-server.db"', server)
    assert m.group(1) == "/workspace/data/memory/agent-server.db"


def test_image_records_the_ref():
    assert "org.karakos.dashboard-ref" in DOCKERFILE
    assert "/out/.dashboard-ref" in DOCKERFILE


def test_runtime_stage_gets_no_vendor_inputs():
    # `COPY . .` of the whole context would drop the private tarball into a layer.
    assert not re.search(r"^COPY\s+(?:--\S+\s+)*\.\s+\.\s*$", DOCKERFILE, re.M)
    assert "rm -rf /src/vendor" in DOCKERFILE


def test_dockerignore_does_not_exclude_vendor_or_pins():
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert not any(x.strip("/").startswith(("vendor", "dashboard.ref", "dashboard.bundle")) for x in ignore)


def test_vendor_is_gitignored():
    assert "vendor/" in (ROOT / ".gitignore").read_text().split()
