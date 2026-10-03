import sqlite3

import pytest

from lib.graph import browse
from lib.graph import schema as schema_mod
from lib.graph import store as store_mod
from lib.graph.store import GraphStore
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)

ROUTES = [("status", {}, None), ("observations", {}, None),
          ("observations", {"q": "tea", "state": "all"}, None), ("entities", {}, None),
          ("entity", {}, 1)]


@pytest.fixture
def seeded(store):
    store.add_observation("alpha likes tea", entity="Alpha", embed=False)
    store.add_edge("Alpha", "Beta", "knows")
    return store


def _snapshot(store):
    with store.read() as c:
        return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in ("meta", "entities", "observations", "edges", "observation_mentions")}


def test_read_ro_refuses_writes(seeded):
    with seeded.read_ro() as c:
        for sql in ("INSERT INTO meta(key, value) VALUES ('x','y')",
                    "UPDATE observations SET content='z'", "DELETE FROM observations"):
            with pytest.raises(sqlite3.OperationalError):
                c.execute(sql)


def test_handle_leaves_database_unchanged(seeded):
    before = _snapshot(seeded)
    data_dir = seeded.path.parent.parent
    for route, q, eid in ROUTES:
        status, _ = browse.handle(route, q, data_dir, eid)
        assert status == 200, route
    assert _snapshot(seeded) == before


def test_missing_db_is_503_and_not_created(tmp_path):
    for route, q, eid in ROUTES:
        status, body = browse.handle(route, q, tmp_path, eid)
        assert status == 503 and body["error"] == "graph_not_initialised"
        assert body["detail"] == "memory graph is not initialised; run: karakos migrate"
    assert not (tmp_path / "memory").exists()


def test_no_write_paths_touched(seeded, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("write path used")
    monkeypatch.setattr(GraphStore, "write", boom)
    monkeypatch.setattr(schema_mod, "ensure_schema", boom)
    monkeypatch.setattr(store_mod, "ensure_schema", boom)
    monkeypatch.setattr(GraphStore, "set_embedding", boom)
    real = store_mod.open_graph

    def guarded(d, create=False):
        assert not create
        return real(d, create=create)
    monkeypatch.setattr(browse, "open_graph", guarded)
    for route, q, eid in ROUTES:
        assert browse.handle(route, q, seeded.path.parent.parent, eid)[0] == 200


def test_newer_schema_is_not_initialised(seeded):
    with seeded.write() as c:
        c.execute("UPDATE meta SET value=? WHERE key='graph_schema'",
                  (str(schema_mod.SCHEMA_VERSION + 1),))
    status, body = browse.handle("status", {}, seeded.path.parent.parent)
    assert status == 503 and body["error"] == "graph_not_initialised"
