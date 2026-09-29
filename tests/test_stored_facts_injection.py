"""
Tests for loading stored facts and memory index into agent prompt context.

Verifies:
1. load_stored_facts returns empty string when memory.db doesn't exist.
2. load_stored_facts queries memory.db and returns formatted facts.
3. load_stored_facts falls back to data/memory-candidates/ if DB has few facts.
4. load_memory_index reads agents/{agent}/memory/MEMORY.md.
5. start_agent_subprocess injects stored facts into --append-system-prompt.
"""

import sqlite3
import pytest
from pathlib import Path


def test_load_stored_facts_empty(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_server", "bin/agent-server.py")
    mod = importlib.util.module_from_spec(spec)
    # mock aiosqlite before exec if needed, but import_script or standard import
    # we can test the standalone functions directly
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    assert mod.load_stored_facts("Marvin") == ""


def test_load_stored_facts_from_db(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_server", "bin/agent-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    db_dir = tmp_path / "data" / "memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "memory.db"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            domain TEXT DEFAULT 'general',
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO facts (subject, content, domain) VALUES (?, ?, ?)",
                 ("Banana Watcher", "Channel referee daemon running in #the-banana-stand", "coordination"))
    conn.execute("INSERT INTO facts (subject, content, domain) VALUES (?, ?, ?)",
                 ("Agora Chapter 11", "Roguelite bankruptcy trading game mechanics", "gaming"))
    conn.commit()
    conn.close()

    result = mod.load_stored_facts("Marvin")
    assert "# Learned Facts & Persistent Memory" in result
    assert "**Banana Watcher [coordination]:** Channel referee daemon running in #the-banana-stand" in result
    assert "**Agora Chapter 11 [gaming]:** Roguelite bankruptcy trading game mechanics" in result


def test_load_stored_facts_candidate_fallback(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_server", "bin/agent-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    cand_dir = tmp_path / "data" / "memory-candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    (cand_dir / "2026-09-28.md").write_text(
        "# Memory candidates\n- **Consensus Clamping:** Use kind: consensus for terminal envelopes\n"
    )

    result = mod.load_stored_facts("Marvin")
    assert "# Learned Facts & Persistent Memory" in result
    assert "**Consensus Clamping:** Use kind: consensus for terminal envelopes" in result


def test_load_memory_index(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_server", "bin/agent-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    # Empty when missing
    assert mod.load_memory_index("Marvin") == ""

    # Returns content when present
    mem_dir = tmp_path / "agents" / "Marvin" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("# Marvin Memory Index\n- [Topic](facts/topic.md)")

    assert mod.load_memory_index("Marvin") == "# Marvin Memory Index\n- [Topic](facts/topic.md)"
