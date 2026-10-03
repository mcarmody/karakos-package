"""GET /graph/* on the real agent-server (step 5.5a), via the 0.3 harness."""

import asyncio
import threading
import time

from lib.graph.store import open_graph

ROUTES = ["/graph/status", "/graph/observations", "/graph/entities", "/graph/entities/1"]


def run(coro):
    return asyncio.run(coro)


def seed(h):
    store = open_graph(h.workspace / "data", create=True)
    ids = {}
    ids["a"], _ = store.add_observation("alpha likes tea", entity="Alpha", embed=False)
    ids["b"], _ = store.add_observation("beta likes chess", entity="Beta", embed=False)
    store.add_edge("Alpha", "Beta", "knows")
    ids["alpha"] = store.get_entity("Alpha")["id"]
    return store, ids


def test_auth_required_and_ok(harness):
    h = harness(agents=["a"])
    seed(h)

    async def go():
        async with h:
            for path in ROUTES:
                r = await h.client.get(path)
                assert r.status == 401, path
                assert await r.json() == {"error": "Unauthorized"}
                r = await h.client.get(path, headers=h._headers())
                assert r.status == 200, path

    run(go())


def test_observations_entity_and_errors(harness):
    h = harness(agents=["a"])
    _, ids = seed(h)

    async def go():
        async with h:
            r = await h.client.get(f"/graph/observations?kind=fact&entity={ids['alpha']}",
                                   headers=h._headers())
            body = await r.json()
            assert [o["id"] for o in body["observations"]] == [ids["a"]]
            r = await h.client.get(f"/graph/entities/{ids['alpha']}", headers=h._headers())
            body = await r.json()
            assert set(body) == {"entity", "neighbors", "counts"}
            assert body["neighbors"][0]["name"] == "Beta"
            r = await h.client.get("/graph/entities/9999", headers=h._headers())
            assert r.status == 404 and (await r.json())["error"] == "unknown_entity"
            r = await h.client.get("/graph/observations?limit=0", headers=h._headers())
            assert r.status == 400 and (await r.json())["error"] == "bad_request"

    run(go())


def test_no_graph_is_503(harness):
    h = harness(agents=["a"])

    async def go():
        async with h:
            for path in ROUTES:
                r = await h.client.get(path, headers=h._headers())
                assert r.status == 503, path
                assert (await r.json())["error"] == "graph_not_initialised"
        assert not (h.workspace / "data" / "memory" / "graph.db").exists()

    run(go())


def test_concurrent_write_does_not_fail_reads(harness):
    h = harness(agents=["a"])
    store, _ = seed(h)

    async def go():
        async with h:
            stop = threading.Event()

            def writer():
                i = 0
                while not stop.is_set():
                    store.add_observation(f"churn {i}", embed=False)
                    i += 1
            t = threading.Thread(target=writer)
            t.start()
            try:
                for _ in range(15):
                    r = await h.client.get("/graph/observations", headers=h._headers())
                    assert r.status == 200
                    r = await h.client.get("/graph/status", headers=h._headers())
                    assert r.status == 200
            finally:
                stop.set()
                t.join()

    run(go())


def test_slow_request_does_not_block_health(harness, monkeypatch):
    import lib.graph.browse as browse
    real = browse.handle

    def slow(*a, **k):
        time.sleep(1.5)
        return real(*a, **k)
    monkeypatch.setattr(browse, "handle", slow)
    h = harness(agents=["a"])
    seed(h)

    async def go():
        async with h:
            t = asyncio.ensure_future(h.client.get("/graph/status", headers=h._headers()))
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            r = await h.client.get("/health", headers=h._headers())
            assert r.status == 200
            assert time.monotonic() - t0 < 1.0
            assert (await t).status == 200

    run(go())
