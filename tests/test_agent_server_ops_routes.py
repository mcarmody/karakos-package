"""
Tests for the operational agent-server routes behind the Discord slash
commands — issue #86.

`/interrupt`, `/kill` and `/flush` are only as real as the endpoints they
POST to, so these drive the actual aiohttp route table built by
`create_app()` over real HTTP, against a real sqlite database. A test that
called `interrupt_agent()` directly would pass with the route unwired.

The `/interrupt` test in particular runs a real subprocess that behaves like
a wedged generation — it streams assistant events forever and never emits a
`result` — and asserts that the turn unwinds after the interrupt. There is no
`sleep()` anywhere: the deadline is on `asyncio.wait_for`, so the test fails
if the turn does not end rather than passing because the sleep was long
enough.
"""

import asyncio
import importlib.util
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER_PATH = PACKAGE_ROOT / "bin" / "agent-server.py"

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("aiosqlite")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Anything the /interrupt acceptance test is allowed to take. #86 says the
# generation has to stop within 5 seconds of the command.
INTERRUPT_DEADLINE_SEC = 5.0


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import bin/agent-server.py against a temp workspace."""
    workspace = tmp_path_factory.mktemp("agent-server-workspace")
    for d in ("logs/agent-streams", "data/memory", "data/health/agents"):
        (workspace / d).mkdir(parents=True, exist_ok=True)

    prev = {k: os.environ.get(k) for k in ("WORKSPACE_ROOT", "AGENT_SERVER_TOKEN")}
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    os.environ["AGENT_SERVER_TOKEN"] = TOKEN
    try:
        spec = importlib.util.spec_from_file_location(
            "agent_server_ops_under_test", AGENT_SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_server_ops_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


async def _client(server):
    """A live HTTP client bound to the real route table."""
    app = server.create_app(with_lifecycle=False)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _fresh_db(server, tmp_path):
    """A real sqlite database on the module's `db` global.

    aiosqlite runs its connection on a non-daemon thread, so every test that
    opens one must close it inside the same event loop — an unclosed handle
    hangs the interpreter at exit, and a suite that never exits looks exactly
    like a suite that never finishes.
    """
    import aiosqlite
    server.DB_PATH = tmp_path / "agent-server.db"
    server.db = await aiosqlite.connect(str(server.DB_PATH))
    server.db.row_factory = aiosqlite.Row
    await server.init_db()
    return server.db


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Auth and unknown agents — same shape on all three new routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["interrupt", "kill", "flush"])
def test_route_requires_the_bearer_token(server, route, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post(f"/agents/amos/{route}")
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 401


@pytest.mark.parametrize("route", ["interrupt", "kill", "flush"])
def test_route_rejects_an_unknown_agent(server, route, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post(f"/agents/nobody/{route}", headers=AUTH)
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 404


# ---------------------------------------------------------------------------
# /flush
# ---------------------------------------------------------------------------

def test_flush_drops_queued_messages_and_reports_the_count(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}, "kothar": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        for i in range(3):
            await db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " author_id, is_bot, content, message_id) VALUES (?,?,?,?,?,?,?,?,?)",
                ("amos", "general", "1", "discord", "mike", "2", 0, f"hi {i}", f"m{i}"))
        # Another agent's backlog must survive.
        await db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
            " author_id, is_bot, content, message_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("kothar", "general", "1", "discord", "mike", "2", 0, "keep me", "k0"))
        await db.commit()

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/flush", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()

        async with db.execute(
            "SELECT agent, COUNT(*) c FROM message_queue WHERE processed = ? GROUP BY agent",
            (server.STATUS_QUEUED,)
        ) as cur:
            remaining = {r["agent"]: r["c"] for r in await cur.fetchall()}
        await db.close()
        return body, remaining

    body, remaining = run(go())
    assert body["flushed"] == 3
    assert "amos" not in remaining, "amos still has queued messages after a flush"
    assert remaining.get("kothar") == 1, "flush hit another agent's queue"


def test_flush_on_an_empty_queue_reports_zero(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/flush", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()
            await db.close()
        return body

    assert run(go())["flushed"] == 0


# ---------------------------------------------------------------------------
# /kill
# ---------------------------------------------------------------------------

def test_kill_ends_a_real_subprocess_and_does_not_respawn_it(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})

    respawns = []

    async def no_respawn(agent):
        respawns.append(agent)

    monkeypatch.setattr(server, "start_agent_subprocess", no_respawn)

    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        server.agent_processes["amos"] = proc

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/kill", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()
        return body, proc.returncode

    body, returncode = run(go())
    assert body["was_running"] is True
    assert returncode is not None, "subprocess is still alive after /kill"
    assert respawns == [], "/kill respawned the agent — that is /reload"
    assert "amos" not in server.agent_processes


def test_kill_says_so_when_nothing_was_running(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})

    async def go():
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/kill", headers=AUTH)
            return await resp.json()
        finally:
            await client.close()

    assert run(go())["was_running"] is False


# ---------------------------------------------------------------------------
# /interrupt — the acceptance test from #86
# ---------------------------------------------------------------------------

# Streams assistant text forever and never emits a `result`, which is exactly
# what a runaway generation looks like to read_agent_response.
NEVER_ENDING_AGENT = textwrap.dedent("""
    import json, sys, time
    sys.stdin.readline()
    i = 0
    while True:
        i += 1
        sys.stdout.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "tick %d " % i}]},
        }) + "\\n")
        sys.stdout.flush()
        time.sleep(0.02)
""")


def _spawn_runaway():
    return asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", NEVER_ENDING_AGENT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


def test_interrupt_stops_a_running_generation_and_discards_the_partial(
        server, monkeypatch, tmp_path):
    """#86's sharp case. A generation is under way; /interrupt arrives over
    HTTP; the turn must unwind inside the deadline, and the half-written reply
    must not be returned for posting."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})
    monkeypatch.setattr(server, "agent_states", {})
    monkeypatch.setattr(server, "interrupted_agents", set())

    respawned = []

    async def fake_start(agent):
        respawned.append(agent)

    monkeypatch.setattr(server, "start_agent_subprocess", fake_start)

    async def go():
        await _fresh_db(server, tmp_path)
        proc = await _spawn_runaway()
        server.agent_processes["amos"] = proc
        server.agent_states["amos"] = "PROCESSING"
        proc.stdin.write(b"go\n")
        await proc.stdin.drain()

        turn = asyncio.create_task(
            server.read_agent_response("amos", "0", []))

        # Wait for the generation to actually be producing output before
        # interrupting — an interrupt that races the first token would prove
        # nothing about stopping one in flight.
        deadline = asyncio.get_event_loop().time() + 5
        while not server.response_buffers.get("amos"):
            assert not turn.done(), "turn ended before it produced anything"
            assert asyncio.get_event_loop().time() < deadline, "agent never streamed"
            await asyncio.sleep(0.01)

        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/interrupt", headers=AUTH)
            body = await resp.json()
            # The turn must end on its own once the interrupt lands. The
            # deadline is the assertion; nothing here sleeps for a fixed time.
            text, metadata = await asyncio.wait_for(turn, timeout=INTERRUPT_DEADLINE_SEC)
        finally:
            await client.close()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await server.db.close()
        return body, text, metadata, server.agent_states.get("amos")

    body, text, metadata, state = run(go())

    assert body["interrupted"] is True
    assert text == "", f"the abandoned partial reply was returned for posting: {text!r}"
    assert metadata == {}
    assert state == "IDLE", "agent left un-idle — the next message would never be picked up"
    assert respawned == ["amos"], "session was not resumed after the interrupt"


def test_interrupt_says_nothing_was_running_when_the_agent_is_idle(server, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_states", {"amos": "IDLE"})
    monkeypatch.setattr(server, "agent_processes", {})

    killed = []

    async def record_kill(agent):
        killed.append(agent)

    monkeypatch.setattr(server, "kill_agent_subprocess", record_kill)

    async def go():
        client = await _client(server)
        try:
            resp = await client.post("/agents/amos/interrupt", headers=AUTH)
            return resp.status, await resp.json()
        finally:
            await client.close()

    status, body = run(go())
    assert status == 200
    assert body["interrupted"] is False
    assert killed == [], "an idle agent's subprocess was killed by /interrupt"


def test_a_normal_turn_is_not_discarded(server, monkeypatch, tmp_path):
    """The counterweight to the discard: with no interrupt in flight,
    read_agent_response must still hand back what the agent said."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    monkeypatch.setattr(server, "agent_processes", {})
    monkeypatch.setattr(server, "agent_states", {})
    monkeypatch.setattr(server, "interrupted_agents", set())

    script = textwrap.dedent("""
        import json, sys
        sys.stdin.readline()
        sys.stdout.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "the answer"}]},
        }) + "\\n")
        sys.stdout.write(json.dumps({"type": "result", "result": "the answer"}) + "\\n")
        sys.stdout.flush()
    """)

    async def go():
        await _fresh_db(server, tmp_path)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        server.agent_processes["amos"] = proc
        server.agent_states["amos"] = "PROCESSING"
        proc.stdin.write(b"go\n")
        await proc.stdin.drain()
        try:
            text, metadata = await asyncio.wait_for(
                server.read_agent_response("amos", "0", []), timeout=10)
            await proc.wait()
        finally:
            await server.db.close()
        return text, metadata

    text, metadata = run(go())
    assert "the answer" in text
    assert metadata != {}


# ---------------------------------------------------------------------------
# /agents/{name}/queue — read and per-message cancel (issue #151)
#
# The dashboard's agent modal called GET /queue/{name} and DELETE
# /queue/{name}/{id}, neither of which was ever registered. Both 404'd, which
# is why the modal showed "Queue empty" for a backlogged agent — `data.messages
# || []` on a 404 body is an empty list, not an error.
#
# These drive the real route table over real HTTP for the same reason the
# tests above do: the handlers were not the half that was broken.
# ---------------------------------------------------------------------------

async def _queue(db, agent, *contents, processed=0, prefix="q"):
    """Insert queued rows and return their primary keys, oldest first."""
    ids = []
    for i, content in enumerate(contents):
        cur = await db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
            " author_id, is_bot, content, message_id, processed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent, "general", "1", "discord", "mike", "2", 0, content,
             f"{prefix}-{agent}-{i}", processed))
        ids.append(cur.lastrowid)
    await db.commit()
    return ids


@pytest.mark.parametrize("method", ["get", "delete"])
def test_queue_routes_require_the_bearer_token(server, method, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            path = "/agents/amos/queue" if method == "get" else "/agents/amos/queue/1"
            resp = await getattr(client, method)(path)
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 401


@pytest.mark.parametrize("method", ["get", "delete"])
def test_queue_routes_reject_an_unknown_agent(server, method, monkeypatch):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        client = await _client(server)
        try:
            path = ("/agents/nobody/queue" if method == "get"
                    else "/agents/nobody/queue/1")
            resp = await getattr(client, method)(path, headers=AUTH)
            return resp.status
        finally:
            await client.close()

    assert run(go()) == 404


def test_queue_returns_pending_and_processing_for_that_agent_only(
        server, monkeypatch, tmp_path):
    """The modal distinguishes pending from processing, and hiding the
    in-flight message would make a busy agent look idle."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}, "kothar": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        await _queue(db, "amos", "first", "second")
        await _queue(db, "amos", "in flight",
                     processed=server.STATUS_IN_PROGRESS, prefix="p")
        await _queue(db, "amos", "already answered",
                     processed=server.STATUS_COMPLETE, prefix="d")
        await _queue(db, "kothar", "not yours")

        client = await _client(server)
        try:
            resp = await client.get("/agents/amos/queue", headers=AUTH)
            body = await resp.json()
        finally:
            await client.close()
            await db.close()
        return body

    body = run(go())
    contents = [m["content"] for m in body["messages"]]
    assert contents == ["first", "second", "in flight"], contents
    assert [m["state"] for m in body["messages"]] == [
        "pending", "pending", "processing"]
    assert "not yours" not in contents, "another agent's queue leaked"
    assert "already answered" not in contents, "a finished message is not queued"
    # The shape dashboard/app/components/AgentModal.tsx destructures.
    for msg in body["messages"]:
        assert set(msg) >= {
            "id", "channel", "author", "content", "content_full_length",
            "created_at", "state"}


def test_queue_truncates_long_content_but_reports_the_real_length(
        server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})
    long_content = "x" * 5000

    async def go():
        db = await _fresh_db(server, tmp_path)
        await _queue(db, "amos", long_content)
        client = await _client(server)
        try:
            body = await (await client.get("/agents/amos/queue", headers=AUTH)).json()
        finally:
            await client.close()
            await db.close()
        return body

    msg = run(go())["messages"][0]
    assert len(msg["content"]) == server.QUEUE_PREVIEW_CHARS
    assert msg["content_full_length"] == 5000, (
        "the UI shows '...N chars' from this field; truncating it too would "
        "make a 5000-character message claim to be 200")


def test_delete_cancels_exactly_one_message(server, monkeypatch, tmp_path):
    """The reason /flush does not cover this case: flush drops the whole
    backlog, and the modal's row-level 'x' must leave the rest queued."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        ids = await _queue(db, "amos", "keep", "drop", "keep too")
        client = await _client(server)
        try:
            resp = await client.delete(f"/agents/amos/queue/{ids[1]}", headers=AUTH)
            body = await resp.json()
            status = resp.status
        finally:
            await client.close()
        async with db.execute(
            "SELECT content FROM message_queue WHERE agent = ? AND processed = ?"
            " ORDER BY id", ("amos", server.STATUS_QUEUED)) as cur:
            left = [r["content"] for r in await cur.fetchall()]
        await db.close()
        return status, body, left

    status, body, left = run(go())
    assert status == 200
    assert body["cancelled"] is True
    assert left == ["keep", "keep too"], left


def test_delete_will_not_cancel_another_agents_message(server, monkeypatch, tmp_path):
    """The agent name is in the path, so it has to be part of the predicate —
    an id alone would let one agent's modal cancel another's work."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}, "kothar": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        ids = await _queue(db, "kothar", "kothar's message")
        client = await _client(server)
        try:
            resp = await client.delete(f"/agents/amos/queue/{ids[0]}", headers=AUTH)
            status, body = resp.status, await resp.json()
        finally:
            await client.close()
        async with db.execute(
            "SELECT COUNT(*) c FROM message_queue WHERE agent = ? AND processed = ?",
            ("kothar", server.STATUS_QUEUED)) as cur:
            still_queued = (await cur.fetchone())["c"]
        await db.close()
        return status, body, still_queued

    status, body, still_queued = run(go())
    assert status == 404
    assert body["cancelled"] is False
    assert still_queued == 1, "amos cancelled kothar's queued message"


def test_delete_will_not_cancel_a_message_already_in_flight(
        server, monkeypatch, tmp_path):
    """Once the subprocess has it, dropping the row loses the record without
    stopping the work. Interrupt is the tool for that, not this."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        ids = await _queue(db, "amos", "in flight",
                           processed=server.STATUS_IN_PROGRESS)
        client = await _client(server)
        try:
            resp = await client.delete(f"/agents/amos/queue/{ids[0]}", headers=AUTH)
            status, body = resp.status, await resp.json()
        finally:
            await client.close()
            await db.close()
        return status, body

    status, body = run(go())
    assert status == 404
    assert body["cancelled"] is False


def test_delete_rejects_a_non_numeric_id(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "agent_config", {"amos": {}})

    async def go():
        db = await _fresh_db(server, tmp_path)
        client = await _client(server)
        try:
            resp = await client.delete("/agents/amos/queue/not-a-number", headers=AUTH)
            status, body = resp.status, await resp.json()
        finally:
            await client.close()
            await db.close()
        return status, body

    status, body = run(go())
    assert status == 400
    assert "Invalid" in body["error"]


def test_health_reports_uptime_and_a_total_queue_depth(server, monkeypatch, tmp_path):
    """Both feed the dashboard home page's summary cards, which showed a
    hardcoded 0 for as long as nothing served them."""
    monkeypatch.setattr(server, "agent_config", {"amos": {}, "kothar": {}})
    monkeypatch.setattr(server, "agent_states", {"amos": "IDLE", "kothar": "IDLE"})

    async def go():
        db = await _fresh_db(server, tmp_path)
        await _queue(db, "amos", "a", "b")
        await _queue(db, "kothar", "c")
        client = await _client(server)
        try:
            body = await (await client.get("/health", headers=AUTH)).json()
        finally:
            await client.close()
            await db.close()
        return body

    body = run(go())
    assert body["queue_depth"] == 3, (
        "total queue_depth must be the sum of the per-agent depths, not 0")
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
