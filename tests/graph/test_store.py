import threading

import pytest

from lib.graph.schema import GraphNotInitialised
from lib.graph.store import open_graph
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)


def test_duplicate_observation_same_id(store):
    a, c1 = store.add_observation("Likes  Tea", entity="Bob", embed=False)
    b, c2 = store.add_observation("likes tea", entity="bob", embed=False)
    assert (a, c1, c2) == (b, True, False)
    d, c3 = store.add_observation("likes tea", kind="episode", entity="bob", embed=False)
    assert c3 and d != a


def test_entity_alias_resolution(store):
    eid, created = store.add_entity("Robert", kind="person", aliases=["Bob", "Bobby"])
    assert created
    again, created2 = store.add_entity("bob", kind="person")
    assert (again, created2) == (eid, False)
    assert store.get_entity("Bobby")["id"] == eid
    other, created3 = store.add_entity("Bob", kind="dog")
    assert created3 and other != eid


def test_edge_reinforcement_and_cap(store):
    i, created, w = store.add_edge("a", "b", "knows")
    assert created and w == 1.0
    j, created, w = store.add_edge("a", "b", "knows")
    assert (j, created) == (i, False) and w == pytest.approx(1.1)
    for _ in range(100):
        _, _, w = store.add_edge("a", "b", "knows")
    assert w == 5.0
    with pytest.raises(ValueError):
        store.add_edge("a", "A", "self")
    with pytest.raises(KeyError):
        store.add_edge("a", "zzz", "x", create_missing=False)
    assert store.neighbors(store.get_entity("a")["id"])[0]["name"] == "b"


def test_open_missing_does_not_create(tmp_path):
    with pytest.raises(GraphNotInitialised):
        open_graph(tmp_path)
    assert not (tmp_path / "memory").exists()


def test_concurrent_writers(store):
    errs = []

    def work(t):
        try:
            for i in range(25):
                store.add_observation(f"t{t} item {i}", embed=False)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(t,)) for t in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert store.status()["observations"] == 50


def test_status_and_unembedded(store):
    store.add_observation("x", embed=False)
    assert store.status()["unembedded"] == 1
    t, i, text = store.iter_unembedded(10)[0]
    assert (t, text) == ("observations", "x")
