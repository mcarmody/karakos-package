"""Operator pause (6.4): the store, and the gate end to end (real server, fake claude).

Agent `a` has shards `a` and `a-2`; `b` has its default shard; the harness adds a
`monitor`. Time is `operator_pause._now`; timers poll it every 20 ms.
"""
import asyncio
import json
import sys
import time

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import operator_pause  # noqa: E402
import steering  # noqa: E402
import stall  # noqa: E402

SHARDS = {"a": ["a", "a-2"]}
NO_STEER = {"enabled": False}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock(monkeypatch):
    t = [time.time()]
    monkeypatch.setattr(operator_pause, "_now", lambda: t[0])
    monkeypatch.setattr(operator_pause, "RESUME_POLL_S", 0.02)
    return t


# -- store ------------------------------------------------------------------

def test_store_round_trip_and_atomic_write(tmp_path):
    p = tmp_path / "data" / "operator-pause.json"
    s = operator_pause.PauseStore(p)
    until = s.pause(["a", "a-2"], 5, "me", now=1000.0)
    assert until == 1300.0
    assert not list(p.parent.glob("*.tmp"))
    again = operator_pause.PauseStore(p).load()
    assert again.pauses == s.pauses
    assert again.pauses["a"] == {"since": 1000.0, "until": 1300.0, "by": "me",
                                 "reason": "manual"}
    assert json.loads(p.read_text())["pauses"]["a-2"]["until"] == 1300.0


def test_store_missing_and_corrupt_file_are_empty(tmp_path):
    p = tmp_path / "operator-pause.json"
    assert operator_pause.PauseStore(p).load().pauses == {}
    p.write_text("{not json")
    assert operator_pause.PauseStore(p).load().pauses == {}
    p.write_text('{"pauses": []}')
    assert operator_pause.PauseStore(p).load().pauses == {}


def test_store_expiry_and_open_ended(tmp_path):
    s = operator_pause.PauseStore(tmp_path / "p.json")
    s.pause(["a"], 1, None, now=100.0)
    s.pause(["b"], None, None, now=100.0)
    assert s.active("a", now=159.0) and not s.active("a", now=160.0)
    assert s.active("b", now=10 ** 9)
    assert s.expired(now=160.0) == ["a"]
    assert s.resume(["a", "zzz"]) == ["a"]
    assert s.active("a", now=0) is None


@pytest.mark.parametrize("minutes", [0, -1, 1441])
def test_store_rejects_minutes_out_of_range(tmp_path, minutes):
    with pytest.raises(ValueError):
        operator_pause.PauseStore(tmp_path / "p.json").pause(["a"], minutes, None)


# -- harness helpers ----------------------------------------------------------

async def post(h, path, body=None):
    resp = await h.client.post(path, headers=h._headers(), json=body)
    return resp.status, await resp.json()


async def shard_entry(h, shard):
    body = await (await h.client.get("/agents", headers=h._headers())).json()
    return [s for a in body["agents"] for s in a["shards"] if s["id"] == shard][0]


def queued(h, shard):
    return [r for r in h.queue_rows(shard) if r["processed"] == 0]


async def settle(secs=0.3):
    await asyncio.sleep(secs)


# -- the gate -----------------------------------------------------------------

def test_paused_shard_starts_nothing_and_resume_drains(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            code, body = await post(h, "/agents/a/pause?shard=a", {"minutes": None})
            assert (code, body) == (200, {"paused": ["a"], "until": None})
            before = h.argv("a")
            await h.send("a", "held")
            await h.send("a-2", "sibling")
            await h.send("b", "other")
            await h.wait_idle("a-2")
            await h.wait_idle("b")
            await settle()
            assert len(queued(h, "a")) == 1
            assert h.sent_to("a") == []
            assert h.argv("a") == before
            code, body = await post(h, "/agents/a/resume?shard=a")
            assert body == {"resumed": ["a"]}
            await h.wait_idle("a")
            assert len(h.sent_to("a")) == 1

    run(scenario())


def test_turn_in_progress_finishes_then_pause_holds(harness, clock):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            h.script(rules=[{"match": "slow", "step": {"text": "r-slow", "delay_ms": 500}}])
            await h.send("a", "slow")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            await post(h, "/agents/a/pause", {"minutes": None})
            await h.send("a", "after")
            await h.wait_for(lambda: h.row_status("a", 1) == 2 or
                             any(r["processed"] == 2 for r in h.queue_rows("a")))
            await settle()
            assert any(d["content"] == "r-slow" for d in h.discord)
            assert len(queued(h, "a")) == 1
            await post(h, "/agents/a/resume")
            await h.wait_idle("a")
            assert len(h.sent_to("a")) == 2

    run(scenario())


def test_timed_pause_resumes_on_its_own(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            await post(h, "/agents/b/pause", {"minutes": 10})
            await h.send("b", "later")
            await settle()
            assert len(queued(h, "b")) == 1
            clock[0] += 11 * 60
            await h.wait_idle("b")
            assert len(h.sent_to("b")) == 1
            assert (await shard_entry(h, "b"))["paused"] is None

    run(scenario())


def test_pause_survives_restart_and_expired_does_not(harness, clock, tmp_workspace):
    from harness import Harness
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def first():
        async with h:
            await post(h, "/agents/a/pause", {"minutes": None})
            await post(h, "/agents/b/pause", {"minutes": 5})

    run(first())
    clock[0] += 10 * 60
    h2 = Harness(tmp_workspace, agents=["a", "b"], steering=NO_STEER)

    async def second():
        async with h2:
            await h2.send("a", "held")
            await h2.send("b", "free")
            await h2.wait_idle("b")
            await settle()
            assert len(queued(h2, "a")) == 1 and h2.sent_to("a") == []
            assert (await shard_entry(h2, "a"))["paused"]["reason"] == "manual"
            assert (await shard_entry(h2, "b"))["paused"] is None

    run(second())


def test_agent_id_pauses_every_shard_and_shard_query_one(harness, clock):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)

    async def scenario():
        async with h:
            code, body = await post(h, "/agents/a/pause", {"minutes": 30, "by": "owner"})
            assert body["paused"] == ["a", "a-2"] and body["until"] == clock[0] + 1800
            for s in ("a", "a-2"):
                e = (await shard_entry(h, s))["paused"]
                assert e == {"reason": "manual", "until": clock[0] + 1800}
            assert (await shard_entry(h, "b"))["paused"] is None
            await post(h, "/agents/a/resume")
            code, body = await post(h, "/agents/a/pause?shard=a-2", {"minutes": None})
            assert body["paused"] == ["a-2"]
            assert (await shard_entry(h, "a"))["paused"] is None
            assert (await post(h, "/agents/nobody/pause", {}))[0] == 404
            assert (await post(h, "/agents/nobody/resume"))[0] == 404
            assert (await post(h, "/agents/a/pause", {"minutes": 1441}))[0] == 400
            assert (await post(h, "/agents/a/pause", {"minutes": 0}))[0] == 400
            assert (await post(h, "/agents/a/pause", {"minutes": "x"}))[0] == 400

    run(scenario())


def test_monitor_agent_can_be_paused(harness, clock):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            code, body = await post(h, "/agents/monitor/pause", {"minutes": None})
            assert code == 200 and body["paused"] == ["monitor"]
            assert (await shard_entry(h, "monitor"))["paused"]["reason"] == "manual"

    run(scenario())


def test_paused_shard_is_not_a_hive_callee_and_calls_are_failed(harness, clock):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            h.script(rules=[{"match": "hold", "step": {"text": "ok", "delay_ms": 800}}])
            await h.send("a", "hold")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            await h.module.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " is_bot, content, message_id, call_id, reply_to_agent)"
                " VALUES ('b', 'call', '0', 'local', 'a', 1, 'q', 'call-1', 'c1', 'a')")
            await h.module.db.commit()
            await post(h, "/agents/b/pause", {"minutes": None})
            await h.wait_for(lambda: any(r["response"] == "callee_paused"
                                         for r in h.queue_rows("b")))
            code, body = await post(h, "/hive/call", {"from": "a", "to": "b", "question": "q"})
            assert code == 409 and body["error"] == "callee_paused"
            specs = h.module.effective_specs()
            states = {s.id: "IDLE" for s in specs}
            import hive
            assert hive.pick_callee(specs, "b", "a", states, {}, {},
                                    paused={"b"}) == (None, "callee_paused")

    run(scenario())


def test_post_hive_call_to_a_paused_shard_is_409_callee_paused(harness, clock):
    """The POST gate on its own (no queued call to fail): paused_view makes the
    shard unavailable to a call, a time-limited pause counts like an open-ended
    one, a buzz is still accepted, and resume reopens it. The caller must be in
    a turn, so `a` is held busy."""
    h = harness(agents=["a", "b"], steering=NO_STEER)
    call = {"from": "a", "to": "b", "question": "q"}

    async def scenario():
        async with h:
            h.script(rules=[{"match": "hold", "step": {"text": "ok", "delay_ms": 1500}}])
            await h.send("a", "hold")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            for body in ({"minutes": None}, {"minutes": 5}):
                assert (await post(h, "/agents/b/pause", body))[0] == 200
                assert operator_pause.paused_view(h.module.STATE, "b")["reason"] == "manual"
                code, resp = await post(h, "/hive/call", call)
                assert code == 409 and resp["error"] == "callee_paused", resp
                assert [r for r in h.queue_rows("b") if r["call_id"]] == []   # no call row
                code, _ = await post(h, "/hive/buzz", {"from": "a", "to": "b", "message": "m"})
                assert code == 202                              # a buzz waits; it is not refused
                assert (await post(h, "/agents/b/resume"))[0] == 200
            code, resp = await post(h, "/hive/call", call)
            assert code == 202, resp                            # resumed: callable again

    run(scenario())


def test_steerable_is_false_for_a_paused_shard(harness, clock):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            state = h.module.STATE
            assert steering.paused(state, "a") is False
            await post(h, "/agents/a/pause", {"minutes": None})
            assert steering.paused(state, "a") is True
            await post(h, "/agents/a/resume")
            assert steering.paused(state, "a") is False

    run(scenario())


def test_beacon_carries_paused_and_diagnose_says_gate(harness, clock):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            await post(h, "/agents/a/pause", {"minutes": None})
            beacon = h.beacon("a")
            assert beacon["paused"]["reason"] == "manual"
            d = stall.diagnose({**beacon, "state": "IDLE"}, {})
            assert d.cause == "paused_by_gate" and d.severity == "info"
            await post(h, "/agents/a/resume")
            assert h.beacon("a")["paused"] is None

    run(scenario())


def test_one_notice_per_pause_to_the_first_human_channel(harness, clock):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            await post(h, "/agents/a/pause", {"minutes": None})
            await h.poke("a", "cron", "bot row", channel_id="7")
            await h.send("a", "one", channel_id="1")
            await h.wait_for(lambda: any(d["channel_id"] == "1" for d in h.discord))
            await h.send("a", "two", channel_id="1")
            await settle()
            notes = [d for d in h.discord if "paused" in d["content"]]
            assert len(notes) == 1 and notes[0]["channel_id"] == "1"
            assert notes[0]["content"] == "This agent is paused until it is resumed by the owner."
            await post(h, "/agents/a/resume")
            await h.wait_idle("a")
            await post(h, "/agents/a/pause", {"minutes": 5})
            await h.send("a", "three", channel_id="1")
            await h.wait_for(lambda: len([d for d in h.discord if "paused" in d["content"]]) == 2)

    run(scenario())
