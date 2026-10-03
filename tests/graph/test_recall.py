import sqlite3

import pytest

from lib.graph import embed
from lib.graph.recall import legacy_blend, recall
from tests.graph.helpers import (ExplodingTextEmbedding, FakeTextEmbedding, blob,
                                  install_fastembed)
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)


def add(store, text, v=None, imp=5.0, **kw):
    i, _ = store.add_observation(text, importance=imp, embed=False, **kw)
    if v is not None:
        store.set_embedding("observations", i, blob(*v))
    return i


@pytest.fixture
def fake(monkeypatch):
    return install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0]})


def ids(r):
    return [x["id"] for x in r["results"]]


def test_ranking_beats_keyword(store, fake):
    kwhit = add(store, "q appears literally here", [0.0, 1.0])   # keyword hit, far vector
    near = add(store, "something unrelated", [1.0, 0.0])         # no keyword, near vector
    r = recall(store, "q")
    assert r["mode"] == "hybrid"
    assert ids(r)[0] == near and kwhit in ids(r)


def test_importance_flips_near_tie_not_bad_match(store, fake):
    a = add(store, "aaa", [1.0, 0.0], imp=2.0)
    b = add(store, "bbb", [0.98, 0.2], imp=9.0)
    assert ids(recall(store, "q"))[:2] == [b, a]
    far = add(store, "ccc", [-1.0, 0.0], imp=10.0)
    assert ids(recall(store, "q"))[-1] == far


def test_weights_tunable(store, fake, monkeypatch):
    a = add(store, "aaa", [1.0, 0.0], imp=5.0)
    b = add(store, "bbb", [0.2, 0.98], imp=10.0)
    assert ids(recall(store, "q"))[0] == a
    assert ids(recall(store, "q", weights={"vec": 0.01, "imp": 1.0}))[0] == b
    monkeypatch.setenv("KARAKOS_RECALL_WEIGHTS", "vec=.01,kw=0,name=0,imp=1")
    assert ids(recall(store, "q"))[0] == b


def test_mixed_embedded_and_unembedded(store, fake):
    emb = add(store, "other words", [1.0, 0.0])
    un = add(store, "q literal match only", None, imp=9.0)
    r = recall(store, "q")
    assert set(ids(r)) == {emb, un}
    assert next(x for x in r["results"] if x["id"] == un)["signals"]["vec"] is None


def test_limit_and_empty_query_no_model(store, monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    for i in range(5):
        add(store, f"item {i}", [1.0], imp=i)
    assert len(recall(store, "item", limit=2)["results"]) == 2
    FakeTextEmbedding.constructed = []
    r = recall(store, "   ")
    assert r["mode"] == "keyword" and r["reason"] == "empty_query" and len(r["results"]) == 5
    assert FakeTextEmbedding.constructed == []


def test_entity_name_lifts_observations(store, fake):
    eid, _ = store.add_entity("Zorblax", kind="person", aliases=["Zed"])
    mine = add(store, "totally different text", [0.0, 1.0], entity="Zorblax")
    other = add(store, "more different text", [0.0, 1.0])
    for query in ("tell me about Zorblax", "what did Zed say"):
        r = recall(store, query)
        assert ids(r)[0] == mine and r["entities"][0]["name"] == "Zorblax"
        assert r["results"][0]["signals"]["name"] == 1.0
    assert other not in ids(recall(store, "Zorblax"))[:1]


def test_name_contains_token(store, fake):
    store.add_entity("Marigold Farm")
    a = add(store, "unrelated", [0.0, 1.0], entity="Marigold Farm")
    r = recall(store, "marigold")
    assert r["results"][0]["id"] == a and r["results"][0]["signals"]["name"] == 0.6


def test_no_model_keyword_mode(store, monkeypatch):
    install_fastembed(monkeypatch, ExplodingTextEmbedding)
    add(store, "the cat sat", [1.0])
    add(store, "dogs bark", [1.0])
    r = recall(store, "cat")
    assert r["mode"] == "keyword" and r["reason"] == "embedder_unavailable"
    assert [x["content"] for x in r["results"]] == ["the cat sat"]
    store2_r = recall(store, "cat", mode="keyword")
    assert store2_r["reason"] == "keyword_requested"


def test_kill_switch_and_no_embeddings(store, fake, monkeypatch):
    add(store, "the cat sat")
    assert recall(store, "cat")["reason"] == "no_embeddings"
    assert FakeTextEmbedding.constructed == []
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")
    assert recall(store, "cat")["reason"] == "disabled"


def test_recent_filters_and_supersede(store, fake):
    a = add(store, "old", created_at="2020-01-01T00:00:00Z", domain="x")
    b = add(store, "new", created_at="2021-01-01T00:00:00Z", domain="x", kind="episode")
    c = add(store, "other", created_at="2022-01-01T00:00:00Z", domain="y")
    assert ids(recall(store, "", mode="recent")) == [c, b, a]
    assert ids(recall(store, "", mode="recent", kinds=["episode"])) == [b]
    assert ids(recall(store, "", mode="recent", domain="x")) == [b, a]
    conn = sqlite3.connect(str(store.path))
    conn.execute("UPDATE observations SET superseded_by=? WHERE id=?", (b, a))
    conn.commit()
    assert a not in ids(recall(store, "", mode="recent"))


def test_legacy_blend_formula(store, monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0]})
    a = add(store, "one", [1.0, 0.0], imp=8.0, kind="episode")
    b = add(store, "two", [0.0, 1.0], imp=4.0, kind="episode")
    add(store, "a fact", [1.0, 0.0], kind="fact")
    u = add(store, "q literal", None, imp=6.0, kind="episode")
    r = legacy_blend(store, "q", 10)
    by = {e["id"]: e for e in r["episodes"]}
    assert set(by) == {a, b, u}
    assert by[a]["score"] == pytest.approx(0.75 * 1.0 + 0.25 * 0.8, abs=1e-4)
    assert by[b]["score"] == pytest.approx(0.75 * 0.5 + 0.25 * 0.4, abs=1e-4)
    assert by[u]["score"] == pytest.approx(0.75 * 0.75 + 0.25 * 0.6, abs=1e-4)
    assert r["mode"] == "semantic"


def test_legacy_blend_keyword_fallback(store, monkeypatch):
    install_fastembed(monkeypatch, ExplodingTextEmbedding)
    add(store, "cat story", [1.0], kind="episode", imp=3.0)
    add(store, "cat tale", [1.0], kind="episode", imp=7.0)
    r = legacy_blend(store, "cat")
    assert r["mode"] == "keyword" and [e["summary"] for e in r["episodes"]] == ["cat tale", "cat story"]


def test_scan_speed_2000(store, monkeypatch):
    import random
    import time
    import numpy as np
    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0]})
    rng = random.Random(1)
    conn = sqlite3.connect(str(store.path))
    for i in range(2000):
        v = np.array([rng.uniform(-1, 1) for _ in range(embed.DIM)], dtype=np.float32)
        conn.execute("INSERT INTO observations(kind, content, importance, embedding) "
                     "VALUES ('fact', ?, ?, ?)", (f"synthetic row {i}", rng.uniform(1, 9), v.tobytes()))
    conn.commit()
    conn.close()
    t = time.monotonic()
    r = recall(store, "synthetic")
    assert time.monotonic() - t < 1.0 and r["mode"] == "hybrid" and len(r["results"]) == 10
