"""Operator surface: /outbox routes and the CLI agree (spec 6.1)."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
PACKAGE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import outbox as ob  # noqa: E402

T0 = 1_000_000.0
AUTH = {"Authorization": "Bearer t0ken"}


@pytest.fixture
def ags(tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("ags_outbox_routes", PACKAGE_ROOT / "bin" / "agent-server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_outbox_routes"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    module.AGENT_SERVER_TOKEN = "t0ken"
    module.OUTBOX_CLOCK = lambda: T0 + 10
    return module


def seed(ags):
    conn = ags._outbox_store()
    pending, _ = ob.enqueue(conn, "amos", "1", "pending text", now=T0)
    dead, _ = ob.enqueue(conn, "amos", "2", "dead text SECRET", now=T0)
    ob.claim_due(conn, T0)
    ob.record_failure(conn, pending, "retry", 500, "HTTP 500", None, T0)   # claimed both: both fail
    ob.record_failure(conn, dead, "permanent", 403, "HTTP 403", None, T0)
    return pending, dead


def call(ags, method, path, **kw):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        client = TestClient(TestServer(ags.create_app(with_lifecycle=False), host="127.0.0.1"))
        await client.start_server()
        try:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()
        finally:
            await client.close()
    return asyncio.run(go())


def test_auth_required(ags):
    seed(ags)
    for method, path in (("GET", "/outbox"), ("GET", "/outbox/x"), ("POST", "/outbox/x/retry"),
                         ("POST", "/outbox/x/discard")):
        assert call(ags, method, path)[0] == 401


def test_list_hides_content_unless_dead_with_include_content(ags):
    pending, dead = seed(ags)
    st, body = call(ags, "GET", "/outbox", headers=AUTH)
    assert st == 200 and {r["id"] for r in body["rows"]} == {pending, dead}
    assert all("content" not in r for r in body["rows"])
    _, body = call(ags, "GET", "/outbox?status=dead", headers=AUTH)
    assert [r["id"] for r in body["rows"]] == [dead] and "content" not in body["rows"][0]
    _, body = call(ags, "GET", "/outbox?status=dead&include_content=1", headers=AUTH)
    assert body["rows"][0]["content"] == "dead text SECRET"
    _, body = call(ags, "GET", "/outbox?status=pending&include_content=1", headers=AUTH)
    assert "content" not in body["rows"][0]
    _, body = call(ags, "GET", "/outbox?limit=1", headers=AUTH)
    assert len(body["rows"]) == 1


def test_show_has_events_and_no_content(ags):
    _, dead = seed(ags)
    st, body = call(ags, "GET", f"/outbox/{dead}", headers=AUTH)
    assert st == 200 and "content" not in body
    assert [e["event"] for e in body["events"]] == ["enqueued", "attempt_failed", "dead"]
    assert call(ags, "GET", "/outbox/ob-nope", headers=AUTH)[0] == 404


def test_no_store_is_empty_not_an_error(ags):
    assert call(ags, "GET", "/outbox", headers=AUTH) == (200, {"rows": []})
    assert call(ags, "POST", "/outbox/ob-x/retry", headers=AUTH)[0] == 404
    assert not ags.OUTBOX_PATH.exists()


def test_retry_and_discard(ags):
    pending, dead = seed(ags)
    st, body = call(ags, "POST", f"/outbox/{dead}/retry", headers=AUTH)
    assert st == 200 and body["status"] == "pending"
    row = ob.get(ags._outbox_store(), dead)
    assert row["attempts"] == 0 and row["dead_reason"] is None
    assert call(ags, "POST", f"/outbox/{dead}/retry", headers=AUTH)[0] == 409     # not dead any more
    st, body = call(ags, "POST", f"/outbox/{dead}/discard", headers=AUTH)
    assert st == 200 and body["status"] == "discarded"
    assert ob.stats(ags._outbox_store(), T0 + 20)["pending"] == 1                  # discarded rows are not counted
    events = [e["event"] for e in ob.show(ags._outbox_store(), dead)["events"]]
    assert events[-2:] == ["retried_manually", "discarded"]


def test_cli_and_routes_agree(ags, tmp_path):
    pending, dead = seed(ags)
    db = str(ags.OUTBOX_PATH)

    def cli(*args):
        r = subprocess.run([sys.executable, str(PACKAGE_ROOT / "lib" / "outbox.py"), "--db", db, *args],
                           capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
        return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None)

    rc, stats = cli("stats")
    assert rc == 0 and stats["pending"] == 1 and stats["dead"] == 1
    rc, rows = cli("list", "--status", "dead")
    assert [r["id"] for r in rows] == [dead] and "content" not in rows[0]
    rc, shown = cli("show", dead)
    assert [e["event"] for e in shown["events"]] == ["enqueued", "attempt_failed", "dead"]
    assert cli("retry", dead)[0] == 0 and cli("retry", dead)[0] == 1
    assert ob.get(ags._outbox_store(), dead)["status"] == "pending"
    assert cli("discard", dead)[0] == 0
    _, body = call(ags, "GET", f"/outbox/{dead}", headers=AUTH)
    assert body["status"] == "discarded"
    assert [e["event"] for e in body["events"]][-2:] == ["retried_manually", "discarded"]
    assert cli("show", "ob-nope")[0] == 1


def test_cli_without_a_store_fails_cleanly(tmp_path):
    r = subprocess.run([sys.executable, str(PACKAGE_ROOT / "lib" / "outbox.py"), "--db",
                        str(tmp_path / "none.db"), "stats"], capture_output=True, text=True)
    assert r.returncode == 1 and not (tmp_path / "none.db").exists()
