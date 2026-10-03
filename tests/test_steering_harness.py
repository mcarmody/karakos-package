"""Mid-turn steering end to end: real agent-server, fake `claude` in queued-stdin
mode (steps 2.5 and 0.3b).

Fixture: agent `a` with shards `a` and `a-2`, agent `b`. Model time is the
script's `delay_ms` (before the reply) and a tool's `ms`. Ordering assertions
only; no absolute time below 50 ms is asserted.
"""

import asyncio
import json
import time

import pytest

SHARDS = {"a": ["a", "a-2"]}
TOOL_MS = 800


def run(coro):
    return asyncio.run(coro)


def fixture(harness, steering=None, **kw):
    # No idle window by default: a second send 200 ms after the first would
    # otherwise coalesce into the first batch instead of being steered.
    return harness(agents=["a", "b"], shards=SHARDS,
                   steering={"coalesce_ms": 0, **(steering or {})}, **kw)


def tool_turn(text="done", ms=TOOL_MS, **extra):
    return {"text": text, "tools": [{"name": "Bash", "input": {"command": "sleep"}, "ms": ms}],
            **extra}


def row(h, shard, text):
    return next(r for r in h.queue_rows(shard) if r["content"] == text)


def status(h, shard, text):
    return row(h, shard, text)["processed"]


def user_lines(h, shard):
    """User lines written to the fake's stdin (control requests excluded)."""
    return [e for e in h.stdin_events(shard) if e["event"].get("type") == "user"]


async def settle_idle(h, shard, timeout=10):
    await h.wait_idle(shard, timeout=timeout)


def posts(h, shard=None):
    """Replies posted to Discord (tool-activity lines excluded)."""
    return [d for d in h.discord
            if (shard is None or d["agent"] == shard) and not d["content"].startswith("-#")]


# -- merge at a tool boundary ----------------------------------------------------------

def test_merge_at_tool_boundary(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": tool_turn("saw: {{queued}}")}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await h.wait_for(lambda: len(user_lines(h, "a")) == 2)
            t_b = user_lines(h, "a")[1]["t"]
            first = user_lines(h, "a")[0]["t"]
            # B was written while the tool was still running.
            assert t_b - first < TOOL_MS / 1000.0
            # Both rows are in progress (not COMPLETE) while the tool runs.
            assert status(h, "a", "A-msg") == 1 and status(h, "a", "B-msg") == 1
            await settle_idle(h, "a")
            assert len(h.results("a")) == 1
            assert status(h, "a", "A-msg") == status(h, "a", "B-msg") == 2
            assert len(posts(h, "a")) == 1
            assert "B-msg" in posts(h, "a")[0]["content"]      # the model saw it
            assert len(h.cost_rows("a")) == 1
            assert row(h, "a", "B-msg")["response"] == row(h, "a", "A-msg")["response"]
            assert h.module.STATE.steered_total["a"] == 1
            assert h.module.STATE.steer["a"].pending() == []
    run(scenario())


def test_no_boundary_runs_as_next_turn(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 600}},
                    {"match": "B-msg", "step": {"text": "r-B"}}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await h.wait_for(lambda: len(h.results("a")) == 1)
            # Between the two results B is in progress: not QUEUED, not COMPLETE.
            assert status(h, "a", "A-msg") in (1, 2)
            assert status(h, "a", "B-msg") == 1
            await settle_idle(h, "a")
            assert len(h.results("a")) == 2
            assert status(h, "a", "B-msg") == 2
            assert row(h, "a", "A-msg")["response"] == "r-A"
            assert row(h, "a", "B-msg")["response"] == "r-B"
            assert [d["content"] for d in posts(h, "a")] == ["r-A", "r-B"]
    run(scenario())


def test_cli_side_coalescing(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 600}},
                    {"match": "B-msg", "step": {"text": "r-BC"}}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await h.send("a", "C-msg")
            await settle_idle(h, "a")
            replays = [r["event"] for r in h.io("a")
                       if r.get("dir") == "out" and r["event"].get("isReplay")]
            assert len(replays) == 2                      # A's, then one for B+C
            body = replays[1]["message"]["content"]
            assert "B-msg" in body and "C-msg" in body
            assert len(h.results("a")) == 2
            assert status(h, "a", "B-msg") == status(h, "a", "C-msg") == 2
            assert row(h, "a", "B-msg")["response"] == row(h, "a", "C-msg")["response"] == "r-BC"
            assert [d["content"] for d in posts(h, "a")] == ["r-A", "r-BC"]
    run(scenario())


# -- idle burst coalescing ----------------------------------------------------------------

def test_idle_burst_is_one_turn(harness):
    h = fixture(harness, steering={"coalesce_ms": 300})
    h.script(default={"text": "ok"})

    async def scenario():
        async with h:
            for name in ("X1", "X2", "X3"):
                await h.send("a", name)
                await asyncio.sleep(0.03)
            await settle_idle(h, "a")
            assert len(h.sent_to("a")) == 1
            body = h.sent_to("a")[0]
            assert all(n in body for n in ("X1", "X2", "X3"))
            assert len(h.results("a")) == 1
            assert [status(h, "a", n) for n in ("X1", "X2", "X3")] == [2, 2, 2]
    run(scenario())


def test_idle_no_window_first_runs_alone(harness):
    h = fixture(harness, steering={"coalesce_ms": 0})
    h.script(default={"text": "ok", "delay_ms": 300})

    async def scenario():
        async with h:
            for name in ("X1", "X2", "X3"):
                await h.send("a", name)
                await asyncio.sleep(0.03)
            await settle_idle(h, "a")
            sent = h.sent_to("a")
            assert "X1" in sent[0] and "X2" not in sent[0]
            # X2 and X3 were steered behind X1 and the CLI coalesces them into
            # one follow-on turn (Q2 shape): two results, one replay for both.
            assert len(h.results("a")) == 2
            replays = [r["event"] for r in h.io("a")
                       if r.get("dir") == "out" and r["event"].get("isReplay")]
            assert "X2" in replays[1]["message"]["content"]
            assert "X3" in replays[1]["message"]["content"]
            assert [status(h, "a", n) for n in ("X1", "X2", "X3")] == [2, 2, 2]
    run(scenario())


def test_idle_late_send_gets_its_own_turn(harness):
    h = fixture(harness, steering={"coalesce_ms": 300})
    h.script(default={"text": "ok"})

    async def scenario():
        async with h:
            await h.send("a", "X1")
            await asyncio.sleep(0.4)
            await h.send("a", "X2")
            await settle_idle(h, "a")
            assert len(h.sent_to("a")) == 2
            assert len(h.results("a")) == 2
    run(scenario())


def test_priority_row_is_not_delayed(harness):
    h = fixture(harness, steering={"coalesce_ms": 3000})
    h.script(default={"text": "ok"})

    async def scenario():
        async with h:
            await h.module.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
                " content, message_id, priority) VALUES ('a','c','1','local','u','P1','p1',100)")
            await h.module.db.commit()
            t0 = time.monotonic()
            h.module.turn_loop.notify_enqueued(h.module.STATE, "a", "1")
            await h.wait_for(lambda: status(h, "a", "P1") == 2, timeout=2.5)
            assert time.monotonic() - t0 < 2.5       # a 3 s window would have held it
    run(scenario())


def test_steering_disabled_restores_hold_until_idle(harness):
    h = fixture(harness, steering={"enabled": False})
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 800}},
                    {"match": "B-msg", "step": {"text": "r-B"}}])

    async def scenario():
        async with h:
            assert "--replay-user-messages" not in h.argv("a")
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await asyncio.sleep(0.3)
            assert status(h, "a", "B-msg") == 0          # waits for IDLE
            assert len(h.sent_to("a")) == 1
            await settle_idle(h, "a")
            assert [d["content"] for d in posts(h, "a")] == ["r-A", "r-B"]
    run(scenario())


# -- release on exit -----------------------------------------------------------------------

def _exit_run(harness_factory, steering):
    h = harness_factory(agents=["a", "b"], shards=SHARDS, steering=steering)
    # A answers after 500 ms and then the process dies without a result; B is
    # written (or queued) in between and is never replayed by the dead process.
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 500, "exit": True}},
                    {"match": "B-msg", "step": {"text": "r-B"}}])

    async def scenario():
        async with h:
            sid_before = h.session_id("a")
            await h.send("a", "A-msg")
            await asyncio.sleep(0.15)
            await h.send("a", "B-msg")
            await h.wait_for(lambda: status(h, "a", "B-msg") == 2, timeout=12)
            await h.wait_idle("a", timeout=10)
            assert h.session_id("a") == sid_before
            return h, (row(h, "a", "A-msg")["processed"], row(h, "a", "A-msg")["response"]), \
                [d["content"] for d in posts(h, "a")]

    return run(scenario())


def test_release_on_exit_delivers_once(harness, tmp_path_factory):
    from harness import Harness
    h, a_on, posted_on = _exit_run(
        lambda **kw: Harness(tmp_path_factory.mktemp("on"), **kw), {"coalesce_ms": 0})
    # B was written into the turn (steered) and never replayed.
    assert h.module.STATE.steered_total["a"] == 1
    # The dead process never replayed B; the new one did, once, and the reply
    # went out once. (The fake's stdin log is per session, so it holds the dead
    # process's receipt of B as well.)
    replays = [r["event"] for r in h.io("a") if r.get("dir") == "out"
               and r["event"].get("isReplay") and "B-msg" in r["event"]["message"]["content"]]
    assert len(replays) == 1
    assert posted_on.count("r-B") == 1
    assert h.module.STATE.steer["a"].pending() == []

    # A's rows follow the pre-existing crash handling: equal to a no-steering run.
    _, a_off, posted_off = _exit_run(
        lambda **kw: Harness(tmp_path_factory.mktemp("off"), **kw), {"enabled": False})
    assert a_on == a_off
    assert posted_on.count("r-B") == posted_off.count("r-B") == 1


# -- follow-on timeout ------------------------------------------------------------------------

def test_followon_timeout_bounces_and_releases(harness, monkeypatch):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 500,
                                                 "no_followon": True}},
                    {"match": "B-msg", "step": {"text": "r-B"}}])

    async def scenario():
        async with h:
            tl = h.module.turn_loop
            monkeypatch.setattr(tl, "STEER_FOLLOWON_TIMEOUT_S", 1)
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "A-msg")
            await asyncio.sleep(0.15)
            await h.send("a", "B-msg")
            h.script(default={"text": "fresh"})        # the respawned process answers normally
            await h.wait_for(lambda: status(h, "a", "B-msg") == 2, timeout=12)
            assert h.module.agent_processes["a"].pid != pid
            assert h.module.agent_states["a"] == "IDLE"
            assert h.module.STATE.steer["a"].pending() == []
            assert row(h, "a", "B-msg")["response"] == "fresh"
    run(scenario())


# -- self-started second turn (Q6) ---------------------------------------------------------------

def bg_turn(**kw):
    return {"text": "started", "tools": [{"name": "Task", "input": {}, "ms": 0}],
            "background_task": {"ms": 50, **kw}, "after_text": "bg done"}


def test_self_started_second_turn(harness):
    h = fixture(harness)
    h.script(default=bg_turn())

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await settle_idle(h, "a")
            # No row belongs to the self-started turn, so idleness alone does not
            # say its post and cost have landed.
            await h.wait_for(lambda: len(posts(h, "a")) == 2 and len(h.cost_rows("a")) == 2)
            assert len(h.results("a")) == 2
            assert [d["content"] for d in posts(h, "a")] == ["started", "bg done"]
            assert status(h, "a", "A-msg") == 2
            assert len(h.cost_rows("a")) == 2
    run(scenario())


def test_self_turn_not_posted_when_disabled(harness, monkeypatch):
    h = fixture(harness)
    h.script(default=bg_turn())

    async def scenario():
        async with h:
            monkeypatch.setattr(h.module.turn_loop, "POST_SELF_TURNS", False)
            await h.send("a", "A-msg")
            await settle_idle(h, "a")
            await h.wait_for(lambda: len(h.cost_rows("a")) == 2)
            assert [d["content"] for d in posts(h, "a")] == ["started"]
    run(scenario())


def test_stale_self_turn_is_consumed_before_next_write(harness, monkeypatch):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": bg_turn(after_ms=600)},
                    {"match": "B-msg", "step": {"text": "r-B"}}])

    async def scenario():
        async with h:
            monkeypatch.setattr(h.module.turn_loop, "SELF_TURN_WINDOW_S", 0.2)
            await h.send("a", "A-msg")
            await h.wait_for(lambda: status(h, "a", "A-msg") == 2)
            await h.wait_for(lambda: h.module.agent_states["a"] == "IDLE")
            await asyncio.sleep(0.6)            # the self turn starts and finishes unread
            await h.send("a", "B-msg")
            await settle_idle(h, "a")
            assert row(h, "a", "B-msg")["response"] == "r-B"
            assert [d["content"] for d in posts(h, "a")] == ["started", "bg done", "r-B"]
    run(scenario())


# -- hive rows are never steered ---------------------------------------------------------------------

async def db_insert(h, agent, name, channel_id="1", **cols):
    base = {"agent": agent, "channel": "c", "channel_id": channel_id, "server": "local",
            "author": "u", "author_id": "1", "is_bot": 0, "content": name, "message_id": name}
    base.update(cols)
    await h.module.db.execute(
        f"INSERT INTO message_queue ({','.join(base)}) VALUES ({','.join('?' * len(base))})",
        list(base.values()))
    await h.module.db.commit()


def test_call_row_is_not_steered_and_runs_alone(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": tool_turn("r-A")},
                    {"match": "CALL", "step": {"text": "r-call"}},
                    {"match": "C-msg", "step": {"text": "r-C"}}])

    async def scenario():
        async with h:
            await h.send("b", "A-msg")
            await asyncio.sleep(0.2)
            await db_insert(h, "b", "CALL", call_id="c1", reply_to_agent="a",
                            channel="call", channel_id="0")
            h.module.turn_loop.notify_enqueued(h.module.STATE, "b", "0")
            await asyncio.sleep(0.2)
            assert status(h, "b", "CALL") == 0          # not steered into A's turn
            assert len(user_lines(h, "b")) == 1
            await settle_idle(h, "b")
            sent = h.sent_to("b")
            assert any("CALL" in s and "A-msg" not in s for s in sent)   # claimed alone
    run(scenario())


def test_ordinary_row_stays_queued_during_a_call_turn(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "CALL", "step": tool_turn("r-call")},
                    {"match": "C-msg", "step": {"text": "r-C"}}])

    async def scenario():
        async with h:
            await db_insert(h, "b", "CALL", call_id="c1", reply_to_agent="a",
                            channel="call", channel_id="1")
            h.module.turn_loop.notify_enqueued(h.module.STATE, "b", "1")
            await h.wait_for(lambda: status(h, "b", "CALL") == 1)
            await asyncio.sleep(0.2)
            await h.send("b", "C-msg")
            await asyncio.sleep(0.3)
            assert status(h, "b", "C-msg") == 0          # QUEUED: never merged into a call turn
            assert len(user_lines(h, "b")) == 1
            await settle_idle(h, "b")
            assert status(h, "b", "C-msg") == 2
    run(scenario())


# -- isolation ---------------------------------------------------------------------------------------

def test_steering_into_one_shard_leaves_others_alone(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": tool_turn("r-A")}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await h.wait_for(lambda: len(user_lines(h, "a")) == 2)
            for other in ("a-2", "b"):
                assert user_lines(h, other) == []
                assert h.queue_rows(other) == []
                assert h.module.STATE.steer.get(other) is None \
                    or h.module.STATE.steer[other].pending() == []
            await settle_idle(h, "a")
    run(scenario())


def test_agent_level_disable_is_per_agent(harness):
    h = harness(agents={"a": {}, "b": {"steering": {"enabled": False}}},
                shards=SHARDS)
    h.script(rules=[{"match": "A-msg", "step": {"text": "r-A", "delay_ms": 800}}])

    async def scenario():
        async with h:
            assert "--replay-user-messages" in h.argv("a")
            assert "--replay-user-messages" not in h.argv("b")
            await h.send("b", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("b", "B-msg")
            await asyncio.sleep(0.3)
            assert status(h, "b", "B-msg") == 0
            await settle_idle(h, "b")
    run(scenario())


def test_other_channel_row_is_not_steered(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": tool_turn("r-A")}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg", channel_id="1")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg", channel_id="2")
            await asyncio.sleep(0.3)
            assert status(h, "a", "B-msg") == 0
            assert len(user_lines(h, "a")) == 1
            await settle_idle(h, "a")
            assert status(h, "a", "B-msg") == 2
    run(scenario())


def test_line_allowance_per_turn(harness):
    h = fixture(harness)       # default max_lines_per_turn: 8
    h.script(rules=[{"match": "A-msg", "step": tool_turn("r-A", ms=2000)}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            names = [f"S{i}" for i in range(1, 10)]
            for n in names:
                await h.send("a", n)
                await asyncio.sleep(0.03)
            await asyncio.sleep(0.3)
            assert [status(h, "a", n) for n in names[:8]] == [1] * 8
            assert status(h, "a", "S9") == 0                 # the ninth waits for the next turn
            await settle_idle(h, "a", timeout=15)
            assert status(h, "a", "S9") == 2
            assert len(h.results("a")) == 2
    run(scenario())


# -- replay events do not leak ---------------------------------------------------------------------------

def test_replay_events_do_not_leak_into_tool_lines_or_context(harness, tmp_path, monkeypatch):
    # Both runs use the fake's queued mode; only --replay-user-messages differs.
    monkeypatch.setenv("FAKE_CLAUDE_QUEUED", "1")

    def one(steering):
        from pathlib import Path
        from harness import Harness
        ws = Path(tmp_path) / ("on" if steering else "off")
        ws.mkdir()
        h = Harness(ws, agents=["a", "b"], steering={"enabled": steering})
        h.script(default={"text": "ok", "tools": [
            {"name": "Bash", "input": {"command": "ls"}, "ms": 10},
            {"name": "Read", "input": {"file_path": "/x"}, "ms": 10}],
            "usage": {"input_tokens": 7, "cache_creation_input_tokens": 3,
                      "cache_read_input_tokens": 500, "output_tokens": 2}})

        async def go():
            async with h:
                assert ("--replay-user-messages" in h.argv("a")) is steering
                await h.send("a", "hello")
                await h.wait_idle("a")
                ctx = (await h.module.get_context_tokens()).get("a")
                tool_lines = [d["content"] for d in h.discord if d["content"].startswith("-#")]
                events = [r["content"] for r in h._query(
                    "SELECT content FROM turn_events ORDER BY id")]
                replayed = any(r["event"].get("isReplay") for r in h.io("a")
                               if r.get("dir") == "out")
                assert replayed is steering
                return ctx, tool_lines, events
        return run(go())

    assert one(True) == one(False)


# -- observability -----------------------------------------------------------------------------------------

def test_agents_endpoint_reports_steering(harness):
    h = fixture(harness)
    h.script(rules=[{"match": "A-msg", "step": tool_turn("r-A")}])

    async def scenario():
        async with h:
            await h.send("a", "A-msg")
            await asyncio.sleep(0.2)
            await h.send("a", "B-msg")
            await h.wait_for(lambda: len(user_lines(h, "a")) == 2)
            resp = await h.client.get("/agents", headers=h._headers())
            shards = {s["id"]: s for ag in (await resp.json())["agents"] for s in ag["shards"]}
            assert shards["a"]["steer_pending"] == 1 and shards["a"]["steered_total"] == 1
            assert shards["a-2"]["steer_pending"] == 0 and shards["a-2"]["steered_total"] == 0
            await settle_idle(h, "a")
            resp = await h.client.get("/agents", headers=h._headers())
            shards = {s["id"]: s for ag in (await resp.json())["agents"] for s in ag["shards"]}
            assert shards["a"]["steer_pending"] == 0 and shards["a"]["steered_total"] == 1
    run(scenario())
