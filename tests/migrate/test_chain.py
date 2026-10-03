"""Chain placement of the memory step and the post-migration tree (7.1 slice)."""
from _memory_helpers import (fx, graph, install, runner, tree_hash, _env,  # noqa: F401
                             fake_embedder)


def test_memory_step_in_chain_after_sessions():
    names = [s.name for s in runner.load_steps()]
    assert "30_sessions" in names and "40_memory" in names
    assert names.index("30_sessions") < names.index("40_memory")
    assert names == sorted(names)
    assert names[-1] == "40_memory"      # 90_stamp is the core stamp, not a step module


def test_full_chain_post_migration_tree_is_usable(tmp_path, fake_embedder):
    install(tmp_path)
    lines = []
    rc = runner.run(tmp_path / "data", tmp_path / "config", tmp_path / "backups",
                    out=lines.append, parity_queries=20)
    assert rc == 0, lines
    data = tmp_path / "data"
    assert not (data / "memory" / "memory.db").exists()
    assert (data / "memory" / "memory.db.migrated").exists()
    # no code reads memory.db any more: the graph opens and the tools answer
    from lib.graph import tools as graph_tools
    from lib.graph.store import open_graph
    store = open_graph(data, create=False)
    res = graph_tools.memory_tool({"action": "recall", "query": "garden", "limit": 3},
                                  store, "alpha")
    assert res["results"], res
    assert graph_tools.memory_tool({"action": "status"}, store, "alpha")
