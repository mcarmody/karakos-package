"""Steering (step 2.5): pure helpers, registry key, claim_steerable."""
import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

aiosqlite = pytest.importorskip("aiosqlite")
pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import msgqueue  # noqa: E402
import registry  # noqa: E402
import steering  # noqa: E402
import turn_loop  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def line(text, kind="steer", ids=(1,), channel="1"):
    return steering.SteerLine(row_ids=list(ids), text=text, channel_id=channel,
                              written_at=0.0, kind=kind,
                              message_ids=[f"m{i}" for i in ids])


# -- Ledger.match_replay --------------------------------------------------------

def test_match_one_entry():
    led = steering.Ledger("a")
    e = line("A", "primary")
    led.append(e)
    assert led.match_replay("A") == [e]
    assert led.pending() == []


def test_match_two_entries_joined_by_newline():
    led = steering.Ledger("a")
    b, c = line("B", ids=(2,)), line("C", ids=(3,))
    led.append(b)
    led.append(c)
    assert led.match_replay("B\nC") == [b, c]
    assert led.pending() == []


def test_match_ignores_trailing_whitespace_both_sides():
    led = steering.Ledger("a")
    e = line("hello \n")
    led.append(e)
    assert led.match_replay("hello\n\n  ") == [e]


def test_no_prefix_match_counts_and_leaves_entries(caplog):
    led = steering.Ledger("a", logging.getLogger("steer-test"))
    e = line("secret text")
    led.append(e)
    with caplog.at_level(logging.WARNING, logger="steer-test"):
        assert led.match_replay("something else") == []
    assert led.unmatched == 1
    assert led.pending() == [e]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "secret text" not in warnings[0].getMessage()
    assert "something else" not in warnings[0].getMessage()


def test_entries_matched_in_order_across_two_replays():
    led = steering.Ledger("a")
    a, b, c = line("A", "primary", (1,)), line("B", ids=(2,)), line("C", ids=(3,))
    for e in (a, b, c):
        led.append(e)
    assert led.match_replay("A") == [a]
    assert led.match_replay("B\nC") == [b, c]
    assert led.pending() == []


def test_remove_and_drain_all():
    led = steering.Ledger("a")
    a, b = line("A"), line("B")
    led.append(a)
    led.append(b)
    led.remove(a)
    led.remove(a)  # absent: no error
    assert led.pending() == [b]
    assert led.drain_all() == [b]
    assert led.pending() == []


# -- steerable ----------------------------------------------------------------------

def mkstate(steer_cfg=None, alive=True, batch="default", paused=False):
    proc = SimpleNamespace(returncode=None if alive else 1) if alive is not None else None
    cfg = {"steering": steer_cfg} if steer_cfg is not None else {}
    if batch == "default":
        batch = turn_loop.TurnBatch(
            shard="a", rows=[{"channel_id": "1", "call_id": None}], message_ids=["m0"],
            channel_id="1", content="x", phase="streaming")
    gate = SimpleNamespace(paused={"a": ("breaker", None)} if paused else {})
    return SimpleNamespace(
        cfg=lambda shard: cfg,
        agent_processes={"a": proc} if proc is not None else {},
        active_turns={"a": batch} if batch is not None else {},
        usage_gate=gate)


def row(**kw):
    base = {"call_id": None, "reply_to_agent": None, "priority": 0, "channel_id": "1"}
    return {**base, **kw}


def test_steerable_accepts():
    assert steering.steerable(mkstate(), "a", row())


@pytest.mark.parametrize("why,state,r", [
    ("disabled", mkstate({"enabled": False}), row()),
    ("no process", mkstate(alive=None), row()),
    ("dead process", mkstate(alive=False), row()),
    ("no turn", mkstate(batch=None), row()),
    ("call row", mkstate(), row(call_id="c1", reply_to_agent="b")),
    ("reply row", mkstate(), row(call_id="c1")),
    ("reply_to_agent", mkstate(), row(reply_to_agent="b")),
    ("priority", mkstate(), row(priority=100)),
    ("other channel", mkstate(), row(channel_id="2")),
    ("paused", mkstate(paused=True), row()),
])
def test_steerable_refuses(why, state, r):
    assert not steering.steerable(state, "a", r), why


def test_steerable_refuses_by_batch_state():
    st = mkstate()
    b = st.active_turns["a"]
    b.phase = "closing"
    assert not steering.steerable(st, "a", row())
    b.phase = "new"
    assert not steering.steerable(st, "a", row())
    b.phase = "streaming"
    b.interrupting = True
    assert not steering.steerable(st, "a", row())
    b.interrupting = False
    b.call_id = "c9"       # a hive call turn
    assert not steering.steerable(st, "a", row())


def test_steerable_refuses_call_batch_by_row_and_handoff():
    st = mkstate(batch=turn_loop.TurnBatch(
        shard="a", rows=[{"channel_id": "1", "call_id": "c1"}], message_ids=["m"],
        channel_id="1", content="x", phase="streaming"))
    assert not steering.steerable(st, "a", row())
    st = mkstate(batch=turn_loop.TurnBatch(
        shard="a", rows=[{"channel_id": "handoff", "call_id": None}], message_ids=["m"],
        channel_id="handoff", content="x", phase="streaming"))
    assert not steering.steerable(st, "a", row(channel_id="handoff"))


def test_steerable_refuses_handoff_batch_and_row():
    # 2.6's internal rows have channel "handoff" (their channel_id is "0").
    st = mkstate(batch=turn_loop.TurnBatch(
        shard="a", rows=[{"channel": "handoff", "channel_id": "0", "call_id": None}],
        message_ids=["m"], channel_id="0", content="x", phase="streaming"))
    assert not steering.steerable(st, "a", row(channel_id="0"))
    assert not steering.steerable(mkstate(), "a", row(channel="handoff"))


def test_steerable_allowance():
    st = mkstate({"max_lines_per_turn": 2})
    st.active_turns["a"].steered_count = 1
    assert steering.steerable(st, "a", row())
    assert steering.allowance(st, "a") == 1
    st.active_turns["a"].steered_count = 2
    assert not steering.steerable(st, "a", row())
    assert steering.allowance(st, "a") == 0


def test_paused_view_helper_wins_when_present():
    st = mkstate(paused=True)
    st.paused_view = lambda shard: False
    assert steering.steerable(st, "a", row())


# -- coalesce_wait_s ------------------------------------------------------------------

def test_coalesce_wait_arithmetic():
    cfg = {"steering": {"coalesce_ms": 300}}
    r = {"call_id": None, "priority": 0, "created_at": 1000.0}
    assert steering.coalesce_wait_s(r, 1000.1, cfg) == pytest.approx(0.2)
    assert steering.coalesce_wait_s(r, 1000.0, cfg) == pytest.approx(0.3)
    assert steering.coalesce_wait_s(r, 1005.0, cfg) == 0.0       # never negative
    assert steering.coalesce_wait_s(r, 999.0, cfg) == pytest.approx(0.3)  # clock skew: not above the window


def test_coalesce_wait_zero_cases():
    r = {"call_id": None, "priority": 0, "created_at": 1000.0}
    assert steering.coalesce_wait_s(r, 1000.0, {"steering": {"coalesce_ms": 0}}) == 0
    assert steering.coalesce_wait_s(r, 1000.0, {"steering": {"enabled": False}}) == 0
    assert steering.coalesce_wait_s({**r, "call_id": "c"}, 1000.0, {}) == 0
    assert steering.coalesce_wait_s({**r, "priority": 100}, 1000.0, {}) == 0
    assert steering.coalesce_wait_s(r, 1000.0, {}, queued=20) == 0
    assert steering.coalesce_wait_s(r, 1000.0, {}, queued=3) == pytest.approx(0.3)  # default 300ms


def test_coalesce_wait_reads_stored_created_at():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    r = {"created_at": "2026-10-03 12:00:00", "priority": 0, "call_id": None}
    assert steering.coalesce_wait_s(r, now + 0.1, {}) == pytest.approx(0.2)


# -- registry ---------------------------------------------------------------------------

def _parse(extra):
    return registry.parse_registry({"version": registry.REGISTRY_VERSION, "agents": {
        "a": {"name": "a", "role": "primary", "model": "m", **extra},
        "mon": {"name": "mon", "role": "monitor", "model": "m"}}})


def test_registry_default_and_passthrough():
    assert registry._DEFAULTS["steering"] == {
        "enabled": True, "coalesce_ms": 300, "max_lines_per_turn": 8}
    assert "steering" not in _parse({}).legacy_view()["agents"]["a"]
    reg = _parse({"steering": {"enabled": False, "coalesce_ms": 0}})
    assert reg.legacy_view()["agents"]["a"]["steering"] == {"enabled": False, "coalesce_ms": 0}


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"}, {"coalesce_ms": -1}, {"coalesce_ms": 5001},
    {"max_lines_per_turn": 0}, {"max_lines_per_turn": 51}, {"max_lines_per_turn": True}, [1],
])
def test_registry_rejects(bad):
    with pytest.raises(registry.RegistryError):
        _parse({"steering": bad})


def test_registry_accepts_bounds_and_warns_on_unknown():
    _parse({"steering": {"coalesce_ms": 5000, "max_lines_per_turn": 50}})
    reg = _parse({"steering": {"bogus": 1}})
    assert any("steering.bogus" in w for w in reg.warnings)


def test_steer_config_defaults():
    sc = steering.steer_config({})
    assert (sc.enabled, sc.coalesce_ms, sc.max_lines_per_turn) == (True, 300, 8)
    assert steering.steer_config({"steering": {"enabled": False}}).enabled is False


# -- claim_steerable -----------------------------------------------------------------------

def _load(workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_steer_unit", ROOT / "bin" / "agent-server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ags_steer_unit"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


@pytest.fixture
def ags(tmp_path):
    return _load(tmp_path)


async def add(db, name, agent="a", channel_id="1", call_id=None, reply_to=None,
              priority=0, expires=None, channel="c"):
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
        " priority, call_id, reply_to_agent, expires_at)"
        " VALUES (?, ?, ?, 'u', 'x', ?, ?, ?, ?, ?)",
        (agent, channel, channel_id, name, priority, call_id, reply_to, expires))
    await db.commit()


def with_db(ags, body):
    async def go():
        await ags.init_db()
        try:
            await body(ags.db)
        finally:
            await ags.db.close()
    run(go())


def test_claim_steerable_predicates(ags):
    async def body(db):
        await add(db, "ok1")
        await add(db, "ok2")
        await add(db, "call", call_id="c1", reply_to="b")
        await add(db, "reply", call_id="c2")
        await add(db, "prio", priority=100)
        await add(db, "other-chan", channel_id="2")
        await add(db, "other-shard", agent="a-2")
        await add(db, "internal", channel="handoff")
        await add(db, "expired", expires="2000-01-01T00:00:00Z")
        rows = await msgqueue.claim_steerable(db, "a", "1", 10)
        assert [r["message_id"] for r in rows] == ["ok1", "ok2"]
        assert all(r["claimed_by"] == "a" and r["processed"] == 1 for r in rows)
        left = {r["message_id"]: r["processed"] for r in
                await db.execute_fetchall("SELECT message_id, processed FROM message_queue")}
        assert left["call"] == left["reply"] == left["prio"] == left["other-chan"] == 0
        assert left["other-shard"] == left["internal"] == 0
        assert left["expired"] == 4   # skipped by expire, never claimed
    with_db(ags, body)


def test_claim_steerable_limit_and_empty(ags):
    async def body(db):
        for i in range(5):
            await add(db, f"m{i}")
        assert len(await msgqueue.claim_steerable(db, "a", "1", 2)) == 2
        assert len(await msgqueue.claim_steerable(db, "a", "1", 10)) == 3
        assert await msgqueue.claim_steerable(db, "a", "1", 10) == []
    with_db(ags, body)


def test_claim_steerable_concurrent_partition(ags):
    async def body(db):
        for i in range(40):
            await add(db, f"m{i}")
        results = await asyncio.gather(
            *(msgqueue.claim_steerable(db, "a", "1", 3) for _ in range(200)))
        claimed = [r["id"] for rows in results for r in rows]
        assert len(claimed) == len(set(claimed)) == 40
    with_db(ags, body)


# -- db lock discipline (2.5 revision) -----------------------------------------

def test_write_commit_holds_no_lock_after_one_hop(tmp_path):
    """A write and its commit are one hop: the moment the await returns another
    connection can write at once (no pending transaction, no lock)."""
    import sqlite3

    async def scenario():
        path = str(tmp_path / "q.db")
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("CREATE TABLE message_queue (id INTEGER PRIMARY KEY, v TEXT)")
        await db.commit()
        rows, n = await msgqueue.write_commit(
            db, "INSERT INTO message_queue (v) VALUES ('x') RETURNING *", fetch=True)
        assert n == 1 and rows[0]["v"] == "x"
        assert not db.in_transaction
        other = sqlite3.connect(path, timeout=0.05)
        other.execute("INSERT INTO message_queue (v) VALUES ('y')")
        other.commit()
        other.close()
        await db.close()

    run(scenario())


def test_cancel_background_awaits_tasks_and_refuses_new():
    async def scenario():
        state = SimpleNamespace(bg_tasks=set(), steal_timers={}, closing=False)
        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(60)

        task = turn_loop.spawn(state, forever())
        await started.wait()
        await turn_loop.cancel_background(state)
        assert task.cancelled() and not state.bg_tasks
        assert turn_loop.spawn(state, forever()) is None

    run(scenario())
