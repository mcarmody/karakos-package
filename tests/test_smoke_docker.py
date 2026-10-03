"""
Smoke tests — Verify the Docker build succeeds and core files are present.

These tests validate that the Dockerfile builds without errors and that the
resulting image contains all expected components. This would have caught both
of Ian's installation issues (missing package-lock.json, invalid route export).
"""

import re
import subprocess
import pytest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent


class TestDockerBuild:
    """Verify Docker image builds successfully."""

    @pytest.mark.slow
    @pytest.mark.docker
    def test_docker_build_succeeds(self):
        """The Docker image should build without errors."""
        result = subprocess.run(
            ["docker", "build", "-t", "karakos-test:smoke", "."],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, (
            f"Docker build failed:\n{result.stderr[-2000:]}"
        )

    @pytest.mark.slow
    @pytest.mark.docker
    def test_docker_build_has_dashboard(self):
        """Built image records the dashboard ref and loads its native modules."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "test", "karakos-test:smoke",
                "-f", "/workspace/dashboard/.dashboard-ref",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "/workspace/dashboard/.dashboard-ref missing from image"
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "node", "karakos-test:smoke", "-e",
                "require('/workspace/dashboard/node_modules/better-sqlite3');"
                "require('/workspace/dashboard/node_modules/sqlite3');console.log('native ok')",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "native ok" in result.stdout, f"native modules failed to load:\n{result.stderr}"

    @pytest.mark.slow
    @pytest.mark.docker
    def test_docker_build_has_python_deps(self):
        """Built image should have Python dependencies installed."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "karakos-test:smoke",
                "python3", "-c", "import aiohttp, aiosqlite, discord; print('ok')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Python deps missing:\n{result.stderr}"
        )

    @pytest.mark.slow
    @pytest.mark.docker
    def test_docker_build_has_node(self):
        """Built image should have Node.js installed."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "karakos-test:smoke",
                "node", "--version",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Node.js not installed in image"
        assert result.stdout.strip().startswith("v"), f"Unexpected node version: {result.stdout}"


class TestFileStructure:
    """Verify required files exist in the repository."""

    @pytest.mark.parametrize("path", [
        "Dockerfile",
        "config/docker-compose.yml",
        "config/supervisord.conf",
        "config/.env.template",
        "config/protected-paths.json",
        "bin/agent-server.py",
        "bin/relay.py",
        "bin/scheduler.py",
        "bin/oneshot.py",
        "bin/entrypoint.sh",
        "bin/capture.py",
        "bin/health-monitor.py",
        "bin/purge-data.py",
        "bin/summarize-session.py",
        "bin/poke.sh",
        "bin/heartbeat.sh",
        "bin/create-agent.sh",
        "mcp/tools-server.py",
        "setup.sh",
        "install.sh",
        "README.md",
        "requirements.txt",
    ])
    def test_required_file_exists(self, path):
        assert (PACKAGE_ROOT / path).exists(), f"Required file missing: {path}"

    def test_scripts_are_executable(self):
        """Shell scripts should have execute permission."""
        non_executable = []
        for script in PACKAGE_ROOT.glob("bin/*.sh"):
            if not os.access(script, os.X_OK):
                non_executable.append(script.name)
        assert not non_executable, (
            f"Scripts not executable: {', '.join(non_executable)}\n"
            f"Fix with: chmod +x bin/{' bin/'.join(non_executable)}"
        )

    def test_setup_is_executable(self):
        assert os.access(PACKAGE_ROOT / "setup.sh", os.X_OK), "setup.sh is not executable"

    def test_install_is_executable(self):
        assert os.access(PACKAGE_ROOT / "install.sh", os.X_OK), "install.sh is not executable"


class TestDockerCompose:
    """Verify docker-compose configuration is valid."""

    def test_compose_config_valid(self):
        """docker compose config should parse without errors.

        Creates a minimal .env if missing, since compose references it.
        """
        import shutil
        if not shutil.which("docker"):
            pytest.skip("docker not installed")
        env_path = PACKAGE_ROOT / "config" / ".env"
        created_env = False

        if not env_path.exists():
            env_path.write_text("AGENT_SERVER_TOKEN=test\n")
            created_env = True

        try:
            result = subprocess.run(
                ["docker", "compose", "-f", "config/docker-compose.yml", "config"],
                cwd=str(PACKAGE_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, (
                f"docker compose config failed:\n{result.stderr}"
            )
        finally:
            if created_env:
                env_path.unlink()


class TestDashboardPin:
    """The dashboard is karakos-dashboard at a pinned ref, not a tree in this repo."""

    def test_dashboard_ref_is_a_full_sha(self):
        ref = (PACKAGE_ROOT / "dashboard.ref").read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"dashboard.ref must be a 40-hex sha, got {ref!r}"
        assert len((PACKAGE_ROOT / "dashboard.ref").read_text().strip().splitlines()) == 1

    def test_dashboard_ref_sha256_is_hex(self):
        digest = (PACKAGE_ROOT / "dashboard.ref.sha256").read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", digest), "dashboard.ref.sha256 must be 64 hex chars"

    def test_bundle_pin_file_exists(self):
        assert (PACKAGE_ROOT / "dashboard.bundle.sha256").exists()

    def test_dashboard_source_not_tracked(self):
        out = subprocess.run(
            ["git", "ls-files", "dashboard", "vendor"],
            cwd=str(PACKAGE_ROOT), capture_output=True, text=True,
        ).stdout
        assert out.strip() == "", f"dashboard source or vendored tarball is tracked:\n{out}"
        assert not (PACKAGE_ROOT / "dashboard").exists()


class TestDockerfileCopyTargets:
    """Verify COPY sources in the Dockerfile are paths the build stage produces.

    Prevents build failures like #33 where COPY --from=dashboard-build
    referenced /app/public but no public/ directory existed. The old check
    looked in the in-repo dashboard/; the stage now builds from a pinned ref,
    so the check parses the Dockerfile and the stage script instead.
    """

    def _dockerfile(self):
        return (PACKAGE_ROOT / "Dockerfile").read_text()

    def test_declares_the_build_args(self):
        df = self._dockerfile()
        assert re.search(r"^ARG DASHBOARD_REF\b", df, re.M)
        assert re.search(r"^ARG NODE_MAJOR\b", df, re.M)

    def test_build_and_runtime_stages_share_node_major(self):
        df = self._dockerfile()
        # One ARG NODE_MAJOR before the first FROM, referenced by both stages.
        first_from = df.index("\nFROM ")
        assert "ARG NODE_MAJOR=" in df[:first_from]
        assert re.search(r"^FROM node:\$\{NODE_MAJOR\}-bookworm-slim AS dashboard-build", df, re.M)
        assert "setup_${NODE_MAJOR}.x" in df
        assert "setup_20.x" not in df and "node:20" not in df

    def test_dashboard_copy_sources_are_produced_by_the_stage(self):
        df = self._dockerfile()
        sources = re.findall(r"COPY\s+(?:--\S+\s+)*--from=dashboard-build\s+(\S+)\s+\S+", df)
        assert sources, "no COPY --from=dashboard-build lines found"
        stage = (PACKAGE_ROOT / "bin" / "dashboard-stage.sh").read_text()
        for src in sources:
            assert src.startswith("/out/"), f"{src} is not under the stage output dir /out"
            name = src[len("/out/"):]
            if name.endswith("*"):  # next.config.*: the stage copies each candidate by name
                assert name[:-1] + "mjs" in stage, f"stage script does not produce {src}"
                continue
            assert name in stage or f'"$OUT/{name}"' in stage, f"stage script does not produce {src}"


class TestSessionSecretConsistency:
    """Verify SESSION_SECRET is provisioned by the package.

    The dashboard-side half (one secret definition, no random fallback) lives
    in karakos-dashboard now; the package must still generate and document it.
    """

    def test_setup_generates_session_secret(self):
        """setup.sh must generate SESSION_SECRET in the .env file."""
        setup = (PACKAGE_ROOT / "setup.sh").read_text()

        assert "SESSION_SECRET" in setup, (
            "setup.sh does not generate SESSION_SECRET. "
            "Dashboard sessions will break on every container restart."
        )

    def test_env_template_has_session_secret(self):
        """The .env template should document SESSION_SECRET."""
        template = (PACKAGE_ROOT / "config" / ".env.template").read_text()
        assert "SESSION_SECRET" in template, (
            "SESSION_SECRET missing from .env.template"
        )


import os
