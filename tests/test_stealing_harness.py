"""Work stealing end to end: real agent-server, fake `claude` (step 2.4).

Fixture: agent `a` with shards `a` and `a-2`, agent `b` with its default shard.
`a` is kept busy with a slow X1; created_at has one-second resolution, so a
waiting row becomes stealable between ~0.3 s and ~1.3 s after it arrives.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

SHARDS = {"a": ["a", "a-2"]}
STEAL = {"enabled": True, "after_s": 0.3, "max_rows": 5}
BUSY_MS = 3000


def run(coro):
    return asyncio.run(coro)


def fixture(harness, steal=STEAL, steering=None):
    return harness(agents=["a", "b"], shards=SHARDS, work_stealing=steal, steering=steering)


def slow_x1(extra=()):
    return [{"match": "X1", "shard": "^a$", "step": {"text": "r-X1", "delay_ms": BUSY_MS}},
            *extra]


def say(text):
    return {"default": {"text": text}}


def row(h, shard, text):
    return next(r for r in h.queue_rows(shard) if r["content"] == text)


def insert(h, agent, name, channel_id="9", age=60, **kw):
    """Insert a queued row directly (old enough to steal). From a running scenario call it
    through asyncio.to_thread: a blocking sqlite write on the server's own loop cannot wait
    out a lock the server holds across an await, and fails "database is locked" after 5 s."""
    created = (datetime.now(timezone.utc) - timedelta(seconds=age)).strftime("%Y-%m-%d %H:%M:%S")
    cols = {"agent": agent, "channel": kw.pop("channel", "c"), "channel_id": channel_id,
            "server": "local", "author": "u", "author_id": "1", "is_bot": 0,
            "content": name, "message_id": name, "created_at": created, **kw}
    if os.environ.get("KARAKOS_DB_TRACE"):
        probe = sqlite3.connect(str(h.module.DB_PATH), timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN EXCLUSIVE")
            probe.execute("ROLLBACK")
        except sqlite3.OperationalError:
            h.dump_lock_state(f"probe before {name}")
        finally:
            probe.close()
    conn = sqlite3.connect(str(h.module.DB_PATH))
    try:
        conn.execute(f"INSERT INTO message_queue ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     list(cols.values()))
        conn.commit()
    except sqlite3.OperationalError:
        if os.environ.get("KARAKOS_DB_TRACE"):
            h.dump_lock_state(name)
        raise
    finally:
        conn.close()


def wake(h, shard, channel_id="9"):
    h.module.turn_loop.notify_enqueued(h.module.STATE, shard, channel_id)


async def start_busy(h, rules=None):
    h.script(default={"text": "ok"}, rules=slow_x1(rules or []))
    await h.send("a", "X1", channel_id="1")
    await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")


async def agents_json(h):
    resp = await h.client.get("/agents", headers=h._headers())
    return await resp.json()


def shard_entry(body, shard):
    for a in body["agents"]:
        for s in a.get("shards", []):
            if s["id"] == shard:
                return s


# -- basic --------------------------------------------------------------------

def test_basic_steal(harness, caplog):
    h = fixture(harness)
    caplog.set_level(logging.INFO)

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            t0 = time.monotonic()
            await h.wait_for(lambda: row(h, "a", "X2")["claimed_by"] == "a-2", timeout=4)
            assert time.monotonic() - t0 < BUSY_MS / 1000 - 0.5
            await h.wait_for(lambda: row(h, "a", "X2")["processed"] == 2, timeout=4)
            # X1 is still running while X2 was answered
            assert row(h, "a", "X1")["processed"] == 1
            assert [d for d in h.discord if d["channel_id"] == "2"][0]["agent"] == "a-2"
            await h.wait_idle("a", timeout=8)
            await h.wait_idle("a-2")
            assert h.module.STATE.steal_timers == {}
            body = await agents_json(h)
            assert shard_entry(body, "a-2")["stolen_total"] == 1
            assert shard_entry(body, "a")["stolen_total"] == 0

    run(scenario())
    x1, x2 = row(h, "a", "X1"), row(h, "a", "X2")
    assert (x1["processed"], x1["claimed_by"]) == (2, "a")
    assert (x2["processed"], x2["agent"], x2["claimed_by"]) == (2, "a", "a-2")
    assert any("X2" in t for t in h.sent_to("a-2"))
    assert not any("X2" in t for t in h.sent_to("a"))
    assert len(h.cost_rows("a-2")) == 1
    assert len(h.cost_rows("a")) == 1
    assert sum("steal thief=a-2 victim=a rows=1" in r.getMessage() for r in caplog.records) == 1
    assert not any("X2" in r.getMessage() for r in caplog.records
                   if "steal thief" in r.getMessage())
    assert len([d for d in h.discord if d["channel_id"] == "2"]) == 1


def test_off_by_default(harness, monkeypatch):
    h = harness(agents=["a", "b"], shards=SHARDS)
    calls = []

    async def scenario():
        async with h:
            import msgqueue
            real = msgqueue.claim_stolen

            async def counting(*a, **k):
                calls.append(a)
                return await real(*a, **k)
            monkeypatch.setattr(msgqueue, "claim_stolen", counting)
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(2.0)
            assert row(h, "a", "X2")["processed"] == 0
            assert h.module.STATE.steal_timers == {}
            await h.wait_idle("a", timeout=8)
            body = await agents_json(h)
            assert shard_entry(body, "a-2")["stolen_total"] == 0

    run(scenario())
    assert calls == []
    assert row(h, "a", "X2")["claimed_by"] == "a"
    assert any("X2" in t for t in h.sent_to("a"))


# -- continuity ---------------------------------------------------------------

def test_same_channel_not_stolen(harness):
    # Steering off: with it on (the default) a same-channel row is steered into
    # the busy turn rather than left waiting, which is not what this tests.
    h = fixture(harness, steering={"enabled": False})

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="1")
            await asyncio.sleep(2.2)
            assert row(h, "a", "X2")["processed"] == 0
            assert row(h, "a", "X2")["claimed_by"] is None
            await h.wait_idle("a", timeout=8)

    run(scenario())
    assert row(h, "a", "X2")["claimed_by"] == "a"
    assert h.sent_to("a-2") == []


def test_waiting_rows_stolen_together_in_order(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await h.send("a", "X3", channel_id="2")
            await h.wait_idle("a-2", timeout=6)
            await h.wait_idle("a", timeout=8)

    run(scenario())
    got = h.sent_to("a-2")
    assert len(got) == 1 and got[0].index("X2") < got[0].index("X3")
    assert row(h, "a", "X2")["claimed_by"] == row(h, "a", "X3")["claimed_by"] == "a-2"


# -- exclusions in a live run ---------------------------------------------------

def test_exclusions_left_for_the_victim(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            now = int(time.time())
            await asyncio.to_thread(insert, h, "a", "call-row", channel_id="0", call_id="cid", reply_to_agent="b")
            await asyncio.to_thread(insert, h, "a", "buzz-row", channel_id="0", channel="hive")
            await asyncio.to_thread(insert, h, "a", "prio-row", channel_id="0", priority=5)
            await asyncio.to_thread(insert, h, "a", "held-row", channel_id="0", not_before=now + 600)
            wake(h, "a", "0")
            await asyncio.sleep(2.0)
            for name in ("call-row", "buzz-row", "prio-row", "held-row"):
                r = row(h, "a", name)
                assert r["processed"] == 0 and r["claimed_by"] is None, name
            assert h.sent_to("a-2") == []

    run(scenario())


def test_agent_boundary(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(2.0)
            assert h.sent_to("b") == []
            assert row(h, "a", "X2")["claimed_by"] in (None, "a-2")
            await h.wait_idle("a", timeout=8)
        # b never stole
        assert not any(r["claimed_by"] == "b" for r in h.queue_rows("a"))

    run(scenario())


# -- thief / victim conditions ----------------------------------------------------

def test_thief_error_recovery_does_not_steal(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            h.module.agent_states["a-2"] = "ERROR_RECOVERY"
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(2.0)
            assert row(h, "a", "X2")["claimed_by"] is None
            h.module.agent_states["a-2"] = "IDLE"
            await h.wait_idle("a", timeout=8)

    run(scenario())


def test_thief_held_by_wall_does_not_steal(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.to_thread(insert, h, "a-2", "own-held", channel_id="0", not_before=int(time.time()) + 600)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(2.0)
            assert row(h, "a", "X2")["claimed_by"] is None
            assert h.sent_to("a-2") == []

    run(scenario())


def test_usage_gate_paused_thief_does_not_steal(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            gate = h.module.STATE.usage_gate
            gate.paused["a-2"] = ("budget", None)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(2.0)
            assert row(h, "a", "X2")["claimed_by"] is None
            assert h.module.STATE.steal_timers == {}
            gate.paused.pop("a-2")

    run(scenario())


def test_victim_in_error_recovery_is_stolen_from(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            h.script(default=say("stolen-ok"))
            h.module.agent_states["a"] = "ERROR_RECOVERY"
            await asyncio.to_thread(insert, h, "a", "waiting", channel_id="7", age=1)
            wake(h, "a", "7")
            await h.wait_for(lambda: row(h, "a", "waiting")["processed"] == 2, timeout=5)

    run(scenario())
    assert row(h, "a", "waiting")["claimed_by"] == "a-2"
    assert h.sent_to("a") == []


def test_thief_runs_its_own_row_first(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h)
            await asyncio.sleep(0.1)
            await h.send("a-2", "Y1", channel_id="3")
            await h.send("a", "X2", channel_id="2")
            await h.wait_idle("a-2", timeout=6)
            await h.wait_idle("a", timeout=8)

    run(scenario())
    got = h.sent_to("a-2")
    assert "Y1" in got[0] and "X2" in got[1]


# -- wake-up without polling ------------------------------------------------------

def test_no_timer_without_waiting_rows_one_per_pair(harness):
    h = fixture(harness)

    async def scenario():
        async with h:
            st = h.module.STATE
            assert st.steal_timers == {}
            await start_busy(h)
            await asyncio.sleep(0.1)
            assert st.steal_timers == {}
            await h.send("a", "X2", channel_id="2")
            await asyncio.sleep(0.05)
            assert list(st.steal_timers) == [("a-2", "a")]
            await h.send("a", "X3", channel_id="3")
            await asyncio.sleep(0.05)
            assert list(st.steal_timers) == [("a-2", "a")]
            await h.wait_idle("a-2", timeout=6)
            await h.wait_idle("a", timeout=8)
            assert st.steal_timers == {}

    run(scenario())


# -- failure handling unchanged ---------------------------------------------------

def _final(h, text):
    r = row(h, "a", text)
    return (r["processed"], r["response"])


def test_thief_exit_matches_unstolen(harness):
    exit_rule = [{"match": "X2", "step": {"exit": True}}]
    h = fixture(harness)

    async def stolen():
        async with h:
            await start_busy(h, exit_rule)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await h.wait_for(lambda: row(h, "a", "X2")["processed"] not in (0, 1), timeout=8)
            await asyncio.sleep(0.3)
            return _final(h, "X2")

    got = run(stolen())
    assert row(h, "a", "X2")["claimed_by"] == "a-2"

    # the same script on an unstolen row (stealing off)
    h2 = harness(agents=["a", "b"], shards=SHARDS)

    async def plain():
        async with h2:
            h2.script(default={"text": "ok"}, rules=exit_rule)
            await h2.send("a", "X2", channel_id="2")
            await h2.wait_for(lambda: row(h2, "a", "X2")["processed"] not in (0, 1), timeout=8)
            await asyncio.sleep(0.3)
            return _final(h2, "X2")

    assert got == run(plain())


def test_thief_wall_returns_rows_under_victim(harness):
    wall = [{"match": "X2", "shard": "^a-2$",
             "step": {"text": f"Claude AI usage limit reached|{int(time.time()) + 600}",
                      "is_error": True}}]
    h = fixture(harness)

    async def scenario():
        async with h:
            await start_busy(h, wall)
            await asyncio.sleep(0.1)
            await h.send("a", "X2", channel_id="2")
            await h.wait_for(lambda: row(h, "a", "X2")["not_before"], timeout=5)
            r = row(h, "a", "X2")
            assert (r["processed"], r["claimed_by"], r["agent"]) == (0, None, "a")
            await asyncio.sleep(1.8)
            # held behind the wall: not stolen again
            assert row(h, "a", "X2")["claimed_by"] is None
            assert len(h.cost_rows("a-2")) == 1

    run(scenario())


# -- reload -----------------------------------------------------------------------

def test_remove_victim_skips_queued_leaves_stolen(harness):
    h = fixture(harness)
    rules = [{"match": "X1", "shard": "^a-2$", "step": {"text": "r", "delay_ms": BUSY_MS}},
             {"match": "X2", "step": {"text": "r2", "delay_ms": BUSY_MS}}]

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=rules)
            await h.send("a-2", "X1", channel_id="1")
            await h.wait_for(lambda: h.module.agent_states.get("a-2") == "PROCESSING")
            await asyncio.sleep(0.1)
            await h.send("a-2", "X2", channel_id="2")
            await h.wait_for(lambda: row(h, "a-2", "X2")["claimed_by"] == "a", timeout=4)
            await h.send("a-2", "X3", channel_id="3")  # a is busy now: stays queued
            keep = [s for s in h.module.effective_specs() if s.id != "a-2"]
            await h.module.sync_shards(keep)
            assert row(h, "a-2", "X3")["processed"] == 4
            assert row(h, "a-2", "X2")["processed"] == 1
            assert row(h, "a-2", "X2")["claimed_by"] == "a"

    run(scenario())


# -- needs 2.5 --------------------------------------------------------------------

def test_coalescing_floor(harness):
    """With steering on, a row younger than the coalescing window is not stolen
    even when after_s would allow it (2.4's floor, 2.5's key)."""
    from lib import registry
    assert "steering" in registry._DEFAULTS
    h = fixture(harness, steal={"enabled": True, "after_s": 0, "max_rows": 5},
                steering={"enabled": True, "coalesce_ms": 1500})
    busy = [{"match": "X1", "shard": "^a$", "step": {"text": "r-X1", "delay_ms": 6000}}]

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=busy)
            await h.send("a", "X1", channel_id="1")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING", timeout=4)
            t0 = time.monotonic()
            await h.send("a", "X2", channel_id="2")     # another channel: not steerable either
            await asyncio.sleep(1.0)
            assert row(h, "a", "X2")["claimed_by"] is None      # inside the window
            await h.wait_for(lambda: row(h, "a", "X2")["claimed_by"] == "a-2", timeout=5)
            assert time.monotonic() - t0 >= 1.5
            await h.wait_idle("a", timeout=10)

    run(scenario())


