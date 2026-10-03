import json

import pytest

from lib.graph import browse
from lib.graph.browse import BrowseError
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)


def _archive(store, oid):
    with store.write() as c:
        c.execute("UPDATE observations SET archived_at='2026-01-01T00:00:00Z' WHERE id=?", (oid,))


def _supersede(store, oid, by):
    with store.write() as c:
        c.execute("UPDATE observations SET superseded_by=? WHERE id=?", (by, oid))


@pytest.fixture
def seeded(store):
    ids = {}
    ids["a"], _ = store.add_observation("alpha likes green tea", entity="Alpha", domain="d1",
                                        agent="x", tags=["t1"], embed=False)
    ids["b"], _ = store.add_observation("beta plays chess on sundays", kind="episode",
                                        entity="Beta", domain="d2", embed=False)
    ids["m"], _ = store.add_observation("tea and chess club", kind="pattern",
                                        entity="Beta", embed=False)
    ids["old"], _ = store.add_observation("alpha likes black tea", entity="Alpha", embed=False)
    ids["arch"], _ = store.add_observation("archived tea note", embed=False)
    _archive(store, ids["arch"])
    _supersede(store, ids["old"], ids["a"])
    with store.write() as c:
        alpha = c.execute("SELECT id FROM entities WHERE name='Alpha'").fetchone()[0]
        c.execute("INSERT INTO observation_mentions VALUES (?,?)", (ids["m"], alpha))
    ids["alpha_eid"] = alpha
    ids["beta_eid"] = store.get_entity("Beta")["id"]
    return ids


def L(store, **kw):
    return browse.list_observations(store, **kw)


def test_kind_filter(store, seeded):
    rows = L(store, kind="episode")["observations"]
    assert [r["id"] for r in rows] == [seeded["b"]]


def test_entity_filter_includes_mentions(store, seeded):
    ids = {r["id"] for r in L(store, entity=seeded["alpha_eid"])["observations"]}
    assert ids == {seeded["a"], seeded["m"]}  # m only by mention
    assert L(store, entity=99999)["observations"] == []


def test_states(store, seeded):
    ids = lambda s: {r["id"] for r in L(store, state=s)["observations"]}  # noqa: E731
    assert ids("active") == {seeded[k] for k in ("a", "b", "m")}
    assert ids("archived") == {seeded["arch"]}
    assert ids("superseded") == {seeded["old"]}
    assert len(ids("all")) == 5
    row = next(r for r in L(store, state="all")["observations"] if r["id"] == seeded["old"])
    assert row["superseded_by"] == seeded["a"] and row["archived_at"] is None
    row = next(r for r in L(store, state="all")["observations"] if r["id"] == seeded["arch"])
    assert row["archived_at"]


def test_cursor_walks_every_row_once(store):
    for i in range(23):
        store.add_observation(f"note number {i}", embed=False)
    seen, cur = [], None
    while True:
        page = L(store, limit=5, cursor=cur)
        seen += [r["id"] for r in page["observations"]]
        cur = page["next"]
        if cur is None:
            break
        assert cur.startswith("b:")
    assert seen == sorted(seen, reverse=True)
    assert len(seen) == len(set(seen)) == 23


def test_search_ranks_filters_and_caps(store, seeded):
    res = L(store, q="chess")["observations"]
    assert {r["id"] for r in res} == {seeded["b"], seeded["m"]}
    # filters apply to search
    assert [r["id"] for r in L(store, q="tea", kind="pattern")["observations"]] == [seeded["m"]]
    assert seeded["old"] not in {r["id"] for r in L(store, q="tea")["observations"]}
    assert {r["id"] for r in L(store, q="tea", state="all")["observations"]} >= {seeded["old"]}
    # better match first
    store.add_observation("quokka quokka quokka", embed=False)
    store.add_observation("a quokka among many other unrelated words here today", embed=False)
    best = L(store, q="quokka")["observations"]
    assert best[0]["content"].startswith("quokka quokka")


def test_search_offset_cap(store):
    for i in range(210):
        store.add_observation(f"zebra item {i}", embed=False)
    p = L(store, q="zebra", limit=100)
    assert p["next"] == "o:100"
    p2 = L(store, q="zebra", limit=100, cursor="o:100")
    assert len(p2["observations"]) == 100 and p2["next"] is None  # 100+100 == cap
    assert L(store, q="zebra", limit=50, cursor="o:200") == {"observations": [], "next": None}
    p3 = L(store, q="zebra", limit=100, cursor="o:150")
    assert len(p3["observations"]) == 50 and p3["next"] is None


def test_search_no_tokens_and_syntax(store, seeded):
    assert L(store, q="!!! ???") == {"observations": [], "next": None}
    assert isinstance(L(store, q='"unbalanced AND (')["observations"], list)  # never raises


def test_bad_params(store, seeded):
    for kw in ({"limit": "abc"}, {"limit": 0}, {"kind": "bogus"}, {"state": "nope"},
               {"cursor": "x:1"}, {"entity": "abc"}, {"cursor": "o:3"},
               {"q": "x", "cursor": "b:3"}, {"domain": "d" * 81}):
        with pytest.raises(BrowseError) as e:
            L(store, **kw)
        assert e.value.status == 400 and e.value.code == "bad_request", kw
    assert len(L(store, limit=1000, state="all")["observations"]) == 5  # clamped


def test_rows_json_and_no_embedding(store, seeded):
    store.add_observation("embedded one", embed=False)
    with store.write() as c:
        c.execute("UPDATE observations SET embedding=? WHERE id=?", (b"\x00" * 8, seeded["a"]))
    out = L(store, state="all")
    s = json.dumps(out)
    assert "embedding" not in s
    r = next(r for r in out["observations"] if r["id"] == seeded["a"])
    assert r["entity"] == {"id": seeded["alpha_eid"], "name": "Alpha", "kind": "thing"}
    assert r["tags"] == ["t1"]
    m = next(r for r in out["observations"] if r["id"] == seeded["m"])
    assert m["mentions"] == [{"id": seeded["alpha_eid"], "name": "Alpha"}]
    json.dumps(browse.status(store))
    json.dumps(browse.list_entities(store))


def test_tags_parsing(store):
    for raw, want in ((json.dumps(["a", "b"]), ["a", "b"]), ("not json", []), (None, []),
                      ('{"a": 1}', [])):
        oid, _ = store.add_observation(f"tags {raw}", tags=raw, embed=False)
        with store.write() as c:
            c.execute("UPDATE observations SET tags=? WHERE id=?", (raw, oid))
        row = next(r for r in L(store)["observations"] if r["id"] == oid)
        assert row["tags"] == want


def test_list_entities(store, seeded):
    store.add_entity("Gamma", kind="person", aliases=["Gam_ma%"])
    names = [e["name"] for e in browse.list_entities(store)["entities"]]
    assert names == sorted(names, key=str.lower)
    assert [e["name"] for e in browse.list_entities(store, q="ALP")["entities"]] == ["Alpha"]
    assert [e["name"] for e in browse.list_entities(store, q="gam_ma%")["entities"]] == ["Gamma"]
    assert browse.list_entities(store, q="a_m")["entities"] == []  # underscore escaped
    alpha = next(e for e in browse.list_entities(store)["entities"] if e["name"] == "Alpha")
    assert alpha["observation_count"] == 2  # a + mention m; old is superseded
    with store.write() as c:
        c.execute("UPDATE entities SET archived_at='2026-01-01T00:00:00Z' WHERE name='Gamma'")
    assert "Gamma" not in [e["name"] for e in browse.list_entities(store)["entities"]]
    assert "Gamma" in [e["name"] for e in browse.list_entities(store, state="archived")["entities"]]
    assert len(browse.list_entities(store, state="all")["entities"]) == 3
    p = browse.list_entities(store, state="all", limit=2)
    assert p["next"] == "o:2"
    assert len(browse.list_entities(store, state="all", limit=2, cursor="o:2")["entities"]) == 1


def test_get_entity(store, seeded):
    store.add_entity("Alpha", aliases=["Al"])
    for i in range(55):
        store.add_edge("Alpha", f"n{i}", "knows")
    store.add_edge("Beta", "Alpha", "likes")
    out = browse.get_entity(store, seeded["alpha_eid"])
    assert out["entity"]["aliases"] == ["al"]
    assert "embedding" not in out["entity"] and out["entity"]["created_at"]
    assert len(out["neighbors"]) == 50
    assert {n["direction"] for n in out["neighbors"]} <= {"in", "out"}
    assert out["counts"] == {"fact": 1, "episode": 0, "pattern": 1}
    json.dumps(out)
    with pytest.raises(BrowseError) as e:
        browse.get_entity(store, 9999)
    assert (e.value.status, e.value.code) == (404, "unknown_entity")
