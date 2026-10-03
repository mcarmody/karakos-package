"""Spawn-time stored facts, re-sourced from the graph (4.2b + Amos amendment).

load_stored_facts() reads the top `fact` observations by importance through
lib/graph (never memory.db) and the spawn prompt carries them under their own
header, distinct from the hook's [ACTIVE RECALL] block. An empty or missing
graph spawns cleanly with no block. load_memory_index (routing table) is
unchanged.
"""
import asyncio
import json
import shutil

import pytest

from conftest import PACKAGE_ROOT, import_script
from lib.graph.store import open_graph

SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"
HEADER = "# Stored Facts (knowledge graph)"


@pytest.fixture
def mod(tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    return import_script("agent-server")


def _spawn_cmd(mod, ws, monkeypatch):
    shutil.copy(SETTINGS_PATH, ws / "config" / "claude-settings.json")
    agent_dir = ws / "agents" / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SYSTEM_PROMPT.md").write_text("You are a test agent.")
    captured = {}

    class FakeStderr:
        async def readline(self):
            return b""

    class FakeProc:
        pid = 4242
        stderr = FakeStderr()

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return FakeProc()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)

    async def run():
        await mod.init_db()
        await mod.load_config()
        await mod.start_agent_subprocess("test-agent")
        await asyncio.sleep(0)

    asyncio.run(run())
    return captured["cmd"]


def _appended(cmd):
    return cmd[cmd.index("--append-system-prompt") + 1] if "--append-system-prompt" in cmd else ""


def test_no_graph_returns_empty(mod):
    assert mod.load_stored_facts("test-agent") == ""


def test_empty_graph_returns_empty(mod, tmp_workspace):
    open_graph(tmp_workspace / "data", create=True)
    assert mod.load_stored_facts("test-agent") == ""


def test_top_facts_by_importance_with_cap_and_filters(mod, tmp_workspace):
    s = open_graph(tmp_workspace / "data", create=True)
    s.add_observation("low fact", importance=2, entity="Zed", embed=False)
    s.add_observation("top fact", importance=9, entity="Alpha", domain="ops", embed=False)
    s.add_observation("mid fact", importance=5, embed=False)
    s.add_observation("other agents fact", importance=10, agent="someone-else", embed=False)
    s.add_observation("an episode", kind="episode", importance=10, embed=False)
    s.add_observation("gone", importance=8, embed=False)
    out = mod.load_stored_facts("test-agent", limit=3)
    assert out.startswith(HEADER)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(lines) == 3
    assert lines[0] == "- **Alpha [ops]:** top fact"
    assert "gone" in lines[1] and "mid fact" in lines[2]
    assert "other agents fact" not in out and "an episode" not in out
    assert "low fact" not in out  # beyond the cap of 3


def test_spawn_prompt_carries_graph_facts(mod, tmp_workspace, monkeypatch):
    s = open_graph(tmp_workspace / "data", create=True)
    s.add_observation("the build box is called anvil", importance=8, embed=False)
    cmd = _spawn_cmd(mod, tmp_workspace, monkeypatch)
    appended = _appended(cmd)
    assert HEADER in appended and "the build box is called anvil" in appended
    assert "[ACTIVE RECALL]" not in appended
    assert "Learned Facts" not in appended


def test_fresh_install_spawns_cleanly_with_no_block(mod, tmp_workspace, monkeypatch):
    assert not (tmp_workspace / "data" / "memory" / "graph.db").exists()
    cmd = _spawn_cmd(mod, tmp_workspace, monkeypatch)
    assert HEADER not in _appended(cmd)
    assert not (tmp_workspace / "data" / "memory" / "graph.db").exists()  # spawn never creates it


def test_unreadable_graph_does_not_break_spawn(mod, tmp_workspace, monkeypatch):
    p = tmp_workspace / "data" / "memory" / "graph.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00garbage" * 50)
    assert mod.load_stored_facts("test-agent") == ""
    cmd = _spawn_cmd(mod, tmp_workspace, monkeypatch)
    assert HEADER not in _appended(cmd)


def test_graph_full_of_facts_never_reads_memory_db_or_candidates(mod, tmp_workspace):
    cand = tmp_workspace / "data" / "memory-candidates"
    cand.mkdir(parents=True)
    (cand / "2026-09-28.md").write_text("- **Candidate:** must not be read\n")
    assert mod.load_stored_facts("test-agent") == ""


def test_load_memory_index(mod):
    assert mod.load_memory_index("Marvin") == ""
    mem_dir = mod.WORKSPACE_ROOT / "agents" / "Marvin" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("# Marvin Memory Index\n- [Topic](facts/topic.md)")
    assert mod.load_memory_index("Marvin") == "# Marvin Memory Index\n- [Topic](facts/topic.md)"
