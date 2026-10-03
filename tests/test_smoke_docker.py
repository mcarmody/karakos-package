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
    def test_docker_build_has_dashboard(self):
        """Built image holds the built dashboard and loads its native modules."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "test", "karakos-test:smoke",
                "-f", "/workspace/dashboard/.next/BUILD_ID",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "/workspace/dashboard/.next/BUILD_ID missing from image"
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


class TestSessionSecretConsistency:
    """Verify SESSION_SECRET is provisioned by the package.

    The dashboard-side half (one secret definition, no random fallback) is
    tested in dashboard/; the package must still generate and document it.
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
