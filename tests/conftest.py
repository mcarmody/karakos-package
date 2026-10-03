"""
Shared pytest fixtures for Karakos test suite.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent


def import_script(name: str, file_path: Path = None):
    """Import a Python script by name, handling hyphens in filenames.

    Searches bin/ and system/ directories for the script.
    """
    module_name = name.replace("-", "_")

    if file_path is None:
        for search_dir in ["bin", "system", "mcp"]:
            candidate = PACKAGE_ROOT / search_dir / f"{name}.py"
            if candidate.exists():
                file_path = candidate
                break

    if file_path is None or not file_path.exists():
        raise FileNotFoundError(f"Script not found: {name}")

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    # Don't cache in sys.modules — allows reload with different env
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace with expected directory structure."""
    dirs = [
        "data/messages",
        "data/memory",
        "data/health",
        "logs/agent-streams",
        "logs/session-summaries",
        "logs/git-events",
        "config",
        "mcp",
        "bin",
        "agents/templates",
        "inbox",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    # Minimal agents registry (schema 2): a primary plus the required monitor.
    (tmp_path / "config" / "agents.yaml").write_text(
        "version: 2\n"
        "agents:\n"
        "  test-agent:\n"
        "    name: test-agent\n"
        "    role: primary\n"
        "    system_prompt: agents/test-agent/SYSTEM_PROMPT.md\n"
        "    discord:\n"
        "      token_env: DISCORD_BOT_TOKEN_TEST\n"
        "  relay:\n"
        "    name: relay\n"
        "    role: monitor\n"
        "    model: haiku\n"
    )

    # Create minimal channels config
    channels_config = {
        "channels": {
            "general": "123456789",
            "signals": "987654321",
        }
    }
    (tmp_path / "config" / "channels.json").write_text(json.dumps(channels_config))

    return tmp_path


LEGACY_AGENTS = {
    "agents": {
        "test-agent": {
            "system_prompt": "agents/test-agent/SYSTEM_PROMPT.md",
            "discord_bot_token_env": "DISCORD_BOT_TOKEN_TEST",
        }
    }
}


@pytest.fixture
def legacy_workspace(tmp_workspace):
    """A 1.x-shaped workspace: config/agents.json, no agents.yaml, unstamped."""
    (tmp_workspace / "config" / "agents.yaml").unlink()
    (tmp_workspace / "config" / "agents.json").write_text(json.dumps(LEGACY_AGENTS))
    return tmp_workspace


@pytest.fixture
def protected_paths_config(tmp_workspace):
    """Create protected paths config for testing."""
    config = {
        "tier1_protected": [
            "system/",
            "config/",
            "bin/agent-server.py",
            "bin/relay.py",
            "Dockerfile",
        ],
        "tier2_review_required": [
            "bin/",
            "agents/templates/",
        ],
        "unprotected_overrides": [
            "agents/*/persona/",
            "agents/*/journal/",
        ],
    }
    config_path = tmp_workspace / "config" / "protected-paths.json"
    config_path.write_text(json.dumps(config))
    return config


@pytest.fixture
def memory_db(tmp_workspace):
    """Create an initialized memory database."""
    db_path = tmp_workspace / "data" / "memory" / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 5.0,
            base_importance REAL,
            channel TEXT,
            tags TEXT,
            agents TEXT,
            created_at TIMESTAMP,
            inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consolidated_at TIMESTAMP DEFAULT NULL,
            embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.8,
            domain TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            reinforcement_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
    """)
    conn.commit()
    return conn, db_path


@pytest.fixture
def harness(tmp_workspace):
    """Factory for the two-agent integration harness (tests/harness).

    Usage: `h = harness(agents=["a", "b"])`, then `async with h:` inside
    `asyncio.run(...)`. The factory exists because the harness needs a running
    event loop to start, and CI has no pytest-asyncio.
    """
    from harness import Harness

    def make(agents=("a", "b"), shards=None, work_stealing=None):
        return Harness(tmp_workspace, agents=agents if isinstance(agents, dict) else list(agents),
                       shards=shards, work_stealing=work_stealing)

    return make


@pytest.fixture
def real_template_workspace(tmp_workspace):
    """tmp_workspace with the shipped templates and a registry written by
    `registry.py init` (primary "Jarvis", id jarvis; monitor relay), so tests
    exercise the real prompt. The registry also carries model/env overrides so
    the harness's fake `claude` works; use `Harness(ws, agents=["jarvis"],
    write_config=False)`."""
    import shutil
    import yaml

    (tmp_workspace / "config" / "agents.yaml").unlink()
    shutil.rmtree(tmp_workspace / "agents")
    shutil.copytree(PACKAGE_ROOT / "agents", tmp_workspace / "agents",
                    ignore=shutil.ignore_patterns("mnemosyne", "*.generated.md"))
    sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
    try:
        import registry
    finally:
        sys.path.pop(0)
    registry.init_registry(tmp_workspace, "jarvis", "Jarvis", channels=["general"])
    for sub in ("persona", "journal", "inbox"):
        (tmp_workspace / "agents" / "jarvis" / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(PACKAGE_ROOT / "agents" / "templates" / "primary.md",
                tmp_workspace / "agents" / "jarvis" / "SYSTEM_PROMPT.md")
    shutil.copy(PACKAGE_ROOT / "agents" / "templates" / "onboarding.md",
                tmp_workspace / "agents" / "jarvis" / "onboarding.md")
    (tmp_workspace / "agents" / "relay").mkdir(exist_ok=True)
    shutil.copy(PACKAGE_ROOT / "agents" / "templates" / "relay.md",
                tmp_workspace / "agents" / "relay" / "SYSTEM_PROMPT.md")

    # Harness knobs: fake model and the fake-claude env passthrough.
    from harness import FAKE_ENV_KEYS
    path = tmp_workspace / "config" / "agents.yaml"
    doc = yaml.safe_load(path.read_text())
    for body in doc["agents"].values():
        body["model"] = "fake-model"
        body["env"] = {k: "${%s}" % k for k in FAKE_ENV_KEYS}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    (tmp_workspace / "config" / "claude-settings.json").write_text(
        json.dumps({"permissions": {"allow": [], "deny": []}}))
    return tmp_workspace
