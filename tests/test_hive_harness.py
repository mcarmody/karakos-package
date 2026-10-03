"""Buzz and hive call end to end: real agent-server, fake `claude`, real
mcp/tools-server.py as the fake's MCP child (step 2.3).

Fixture: agent `a` with shards `a` and `a-2`, agent `b` with its default shard.
Deadlock tests that must name shard `a` use agents without siblings, because an
id that is both an agent id and a shard id means the agent.
"""

import asyncio
import json
import sqlite3
import time

import pytest

SHARDS = {"a": ["a", "a-2"]}
HIVE_FROM_A = "hive call from a,"


def run(coro):
    return asyncio.run(coro)


def rule(match, step, shard=None):
    r = {"match": match, "step": step}
    if shard:
        r["shard"] = shard
    return r


def tool(name, **args):
    return {"tool": name, "args": args}


def call_rows(h, shard):
    return [r for r in h.queue_rows(shard) if r["call_id"] and r["reply_to_agent"]]


def reply_rows(h, shard):
    return [r for r in h.queue_rows(shard) if r["call_id"] and not r["reply_to_agent"]]


def buzz_rows(h, shard):
    return [r for r in h.queue_rows(shard) if r["message_id"].startswith("buzz-")]


async def settle(h, *shards, timeout=15):
    for s in shards:
        await h.wait_idle(s, timeout=timeout)


def spoken(h, agent):
    return [m["content"] for m in h.discord if m["agent"] == agent]


# -- buzz -----------------------------------------------------------------------

def test_buzz_leaves_a_row_and_does_not_wait(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("GO", {"mcp": [tool("buzz", to="b", message="ping b")],
                            "text": "{{mcp:0.status}}"}, shard="^a$")])
            await h.send("a", "GO")
            await h.wait_for(lambda: buzz_rows(h, "b"), timeout=15)
            await settle(h, "a", "b")

    run(scenario())
    (row,) = buzz_rows(h, "b")
    assert row["call_id"] is None and row["reply_to_agent"] is None
    assert row["author"] == "a" and row["depth"] == 1 and row["channel_id"] == "0"
    assert "[buzz from a] ping b" in h.sent_to("b")[0]
    assert h.queue_rows("a")[0]["response"] == "queued"
    assert len(h.cost_rows("a")) == 1
    assert spoken(h, "b") == []          # channel 0: nothing posted for b's turn


def test_buzz_to_own_agent_lands_on_sibling(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("GO", {"mcp": [tool("buzz", to="a", message="note")],
                            "text": "{{mcp:0.to}}"}, shard="^a$")])
            await h.send("a", "GO")
            await h.wait_for(lambda: buzz_rows(h, "a-2"), timeout=15)
            await settle(h, "a", "a-2")

    run(scenario())
    assert h.queue_rows("a")[0]["response"] == "a-2"
    assert buzz_rows(h, "a-2")[0]["author"] == "a"
    assert "[buzz from a] note" in h.sent_to("a-2")[0]


def test_sixth_buzz_is_refused_and_counter_resets(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)
    six = [tool("buzz", to="b", message=f"m{i}") for i in range(6)]

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("GO", {"mcp": six, "text": "{{mcp:4.status}}/{{mcp:5.error}}"},
                     shard="^a$")])
            await h.send("a", "GO")
            await settle(h, "a")
            first = h.queue_rows("a")[0]["response"]
            await h.send("a", "GO")
            await settle(h, "a")
            await settle(h, "b")
            return first, h.queue_rows("a")[1]["response"]

    first, second = run(scenario())
    assert first == "queued/buzz_limit"
    assert second == "queued/buzz_limit"   # the counter reset: 5 more went through
    assert len(buzz_rows(h, "b")) == 10


# -- hive call --------------------------------------------------------------------

def answered_script(h):
    h.script(default={"text": "ok"}, rules=[
        rule("hive call from", {"text": "42"}, shard="^b$"),
        rule("GO", {"mcp": [tool("hive_call", to="b", question="meaning?")],
                    "text": "answer={{mcp:0.answer}}"}, shard="^a$")])


def test_hive_call_answered_inside_the_callers_turn(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            answered_script(h)
            await h.send("a", "GO")
            await settle(h, "a", "b")
            return await h.hive_calls()

    calls = run(scenario())
    (call,) = call_rows(h, "b")
    (reply,) = reply_rows(h, "a")
    assert call["processed"] == 2 and reply["processed"] == 2
    assert call["call_id"] == reply["call_id"]
    assert json.loads(reply["content"])["answer"] == "42"
    # one turn on a, one result, one cost row; the answer is inside its reply
    assert len(h.sent_to("a")) == 1
    assert len(h.cost_rows("a")) == 1
    human = [r for r in h.queue_rows("a") if not r["call_id"]]
    assert human[0]["response"] == "answer=42"
    assert "answer=42" in spoken(h, "a")
    assert all(m["agent"] != "b" for m in h.discord)
    assert "[hive call from a, depth 1 of 2] meaning?" in h.sent_to("b")[0]
    assert [c["status"] for c in calls] == ["answered"]
    assert calls[0]["duration_ms"] is not None and calls[0]["answer"] == "42"


def test_hive_hook_runs_before_other_turn_hooks(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            assert h.module.STATE.hooks.on_turn_end[0] is h.module.hive_on_turn_end
            assert h.module.STATE.hooks.on_turn_start[0] is h.module.hive_on_turn_start

    run(scenario())


def test_isolation_sibling_untouched(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            answered_script(h)
            await h.send("a", "GO")
            await settle(h, "a", "b", "a-2")

    run(scenario())
    assert h.queue_rows("a-2") == [] and h.sent_to("a-2") == []
    assert [len(h.cost_rows(s)) for s in ("a", "b", "a-2")] == [1, 1, 0]


def test_refusals_over_http(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def post(path, body):
        r = await h.client.post(path, headers=h._headers(), json=body)
        return r.status, await r.json()

    async def scenario():
        async with h:
            h.script(default={"text": "ok"})
            out = {}
            out["caller"] = await post("/hive/call", {"from": "zzz", "to": "b", "question": "q"})
            out["idle"] = await post("/hive/call", {"from": "a", "to": "b", "question": "q"})
            out["bz_target"] = await post("/hive/buzz", {"from": "a", "to": "nobody", "message": "m"})
            out["bz_empty"] = await post("/hive/buzz", {"from": "a", "to": "b", "message": ""})
            out["unauth"] = (await h.client.post("/hive/buzz", json={})).status
            out["unknown"] = (await h.client.get("/hive/call/c-nope", headers=h._headers())).status
            return out

    out = run(scenario())
    assert out["caller"][0] == 404 and out["caller"][1]["error"] == "unknown_caller"
    assert out["idle"][0] == 409 and out["idle"][1]["error"] == "caller_not_in_turn"
    assert out["bz_target"][0] == 404 and out["bz_target"][1]["error"] == "unknown_target"
    assert "b" in out["bz_target"][1]["detail"]
    assert out["bz_empty"][0] == 400
    assert out["unauth"] == 401 and out["unknown"] == 404
    assert call_rows(h, "b") == [] and buzz_rows(h, "b") == []


# -- reply rows are never turn input ------------------------------------------------

def test_reply_row_for_idle_caller_is_never_a_turn(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            import msgqueue
            h.script(default={"text": "ok"})
            db = h.module.db
            await db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                " message_id, call_id, created_at) VALUES ('a','call','0','b','{}',"
                " 'reply-x', 'c-x', datetime('now'))")
            await db.commit()
            h.module.turn_loop.notify_enqueued(h.module.STATE, "a", "0")
            await asyncio.sleep(0.3)
            assert await msgqueue.claim_batch(db, "a", 20) == []
            assert h.sent_to("a") == []
            n = await msgqueue.reap_hive_rows(db, time.time() + 700,
                                              h.module.hive_lib.HIVE_REPLY_TTL_S)
            return n

    assert run(scenario()) == 1
    assert h.queue_rows("a")[0]["processed"] == 4


# -- depth cap ------------------------------------------------------------------------

def test_depth_cap_two_for_calls(harness):
    h = harness(agents=["a", "b", "c", "d"])

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from b", {"mcp": [tool("hive_call", to="d", question="q3")],
                                          "text": "C:{{mcp:0.error}}"}, shard="^c$"),
                rule("hive call from a", {"mcp": [tool("hive_call", to="c", question="q2")],
                                          "text": "B:{{mcp:0.answer}}"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="q1")],
                            "text": "A:{{mcp:0.answer}}"}, shard="^a$")])
            await h.send("a", "GO")
            await settle(h, "a", "b", "c", "d")
            return await h.hive_calls()

    calls = run(scenario())
    assert h.queue_rows("a")[0]["response"] == "A:B:C:depth_exceeded"
    assert h.queue_rows("d") == []
    assert sorted(c["depth"] for c in calls) == [1, 2]


def test_depth_cap_two_for_buzzes(harness):
    h = harness(agents=["a", "b", "c", "d"])

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("buzz from b", {"mcp": [tool("buzz", to="d", message="m3")],
                                     "text": "C:{{mcp:0.error}}"}, shard="^c$"),
                rule("buzz from a", {"mcp": [tool("buzz", to="c", message="m2")],
                                     "text": "B:{{mcp:0.status}}"}, shard="^b$"),
                rule("GO", {"mcp": [tool("buzz", to="b", message="m1")],
                            "text": "A"}, shard="^a$")])
            await h.send("a", "GO")
            await h.wait_for(lambda: buzz_rows(h, "c"), timeout=15)
            await settle(h, "a", "b", "c", "d")

    run(scenario())
    assert [r["depth"] for r in buzz_rows(h, "b")] == [1]
    assert [r["depth"] for r in buzz_rows(h, "c")] == [2]
    assert h.queue_rows("c")[0]["response"] == "C:depth_exceeded"
    assert h.queue_rows("d") == []


# -- deadlock ---------------------------------------------------------------------------

def test_self_call_refused_without_a_row(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("GO", {"mcp": [tool("hive_call", to="a-2", question="me?")],
                            "text": "{{mcp:0.error}}"}, shard="^a-2$")])
            await h.send("a-2", "GO")
            await settle(h, "a-2")

    run(scenario())
    assert h.queue_rows("a-2")[0]["response"] == "self_call"
    assert not any(call_rows(h, s) for s in ("a", "a-2", "b"))


def test_two_shards_calling_each_other_is_refused_at_once(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from a", {"mcp": [tool("hive_call", to="a", question="back?")],
                                          "text": "B:{{mcp:0.error}}"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="hi", timeout=60)],
                            "text": "A:{{mcp:0.answer}}"}, shard="^a$")])
            t0 = time.monotonic()
            await h.send("a", "GO")
            await settle(h, "a", "b", timeout=20)
            return time.monotonic() - t0

    elapsed = run(scenario())
    assert elapsed < 30                      # ordering, not a clock: well under 60 s
    assert h.queue_rows("a")[0]["response"] == "A:B:deadlock"
    assert call_rows(h, "a") == []           # b's call to a never got a row


def test_three_cycle_last_call_refused(harness):
    """a waits on b, c waits on a, then b calls c: closing the cycle. (Reached
    from separate human turns: a chain from one turn hits the depth cap first.)"""
    h = harness(agents=["a", "b", "c"])

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from a", {"delay_ms": 2500,
                                          "mcp": [tool("hive_call", to="c", question="q")],
                                          "text": "B:{{mcp:0.error}}"}, shard="^b$"),
                rule("GO-A", {"mcp": [tool("hive_call", to="b", question="q")],
                              "text": "A:{{mcp:0.answer}}"}, shard="^a$"),
                rule("GO-C", {"mcp": [tool("hive_call", to="a", question="q")],
                              "text": "C:{{mcp:0.answer}}"}, shard="^c$")])
            await h.send("a", "GO-A")
            await h.wait_for(lambda: h.module.STATE.hive.open, timeout=15)
            await h.send("c", "GO-C")
            await h.wait_for(lambda: len(h.module.STATE.hive.open) == 2, timeout=15)
            await settle(h, "a", "b", "c", timeout=25)

    run(scenario())
    assert h.queue_rows("a")[0]["response"] == "A:B:deadlock"
    assert call_rows(h, "c") == []           # b's call to c never got a row


def test_call_to_own_agent_id_picks_sibling_else_self_call(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from a", {"text": "sibling here"}, shard="^a-2$"),
                rule("SELF-B", {"mcp": [tool("hive_call", to="b", question="me?")],
                                "text": "{{mcp:0.error}}"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="a", question="you?")],
                            "text": "{{mcp:0.answer}}"}, shard="^a$")])
            await h.send("a", "GO")
            await h.send("b", "SELF-B")
            await settle(h, "a", "b", "a-2")

    run(scenario())
    assert h.queue_rows("a")[0]["response"] == "sibling here"
    assert len(call_rows(h, "a-2")) == 1
    assert h.queue_rows("b")[0]["response"] == "self_call"


# -- timeouts ---------------------------------------------------------------------------

def test_expired_in_queue_one_reply_row_and_expired_status(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            h.module.hive_lib.HIVE_MIN_TIMEOUT_S = 0.3
            h.script(default={"text": "ok"}, rules=[
                rule("LONG", {"delay_ms": 4000, "text": "slow"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="q", timeout=0)],
                            "text": "{{mcp:0.status}}"}, shard="^a$")])
            await h.send("b", "LONG")
            await h.wait_for(lambda: h.module.agent_states.get("b") == "PROCESSING")
            await h.send("a", "GO")
            await settle(h, "a", "b", timeout=20)
            return await h.hive_calls()

    try:
        calls = run(scenario())
    finally:
        import hive
        hive.HIVE_MIN_TIMEOUT_S = 5
    assert h.queue_rows("a")[0]["response"] == "expired"
    (reply,) = reply_rows(h, "a")
    assert json.loads(reply["content"])["error"] == "expired"
    (call,) = call_rows(h, "b")
    assert call["processed"] == 4
    assert [c["status"] for c in calls] == ["expired"]
    assert len(h.sent_to("b")) == 1          # only LONG ran on b


def test_callee_mid_answer_when_caller_gives_up_gets_late_reply_dropped(harness):
    h = harness(agents=["a", "b"])
    states = {}

    async def scenario():
        async with h:
            h.module.hive_lib.HIVE_MIN_TIMEOUT_S = 0.3
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from", {"delay_ms": 4500, "text": "too late"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="q", timeout=0)],
                            "text": "{{mcp:0.status}}"}, shard="^a$")])
            await h.send("a", "GO")
            await settle(h, "a", timeout=20)
            states["call_at_timeout"] = call_rows(h, "b")[0]["processed"]
            states["open"] = dict(h.module.STATE.hive.open)
            await settle(h, "b", timeout=20)
            states["calls"] = await h.hive_calls()

    try:
        run(scenario())
    finally:
        import hive
        hive.HIVE_MIN_TIMEOUT_S = 5
    assert h.queue_rows("a")[0]["response"] == "timeout"
    assert states["call_at_timeout"] == 1 and states["open"] == {}
    (reply,) = reply_rows(h, "a")
    assert (reply["processed"], reply["response"]) == (4, "late")
    assert len(h.sent_to("a")) == 1
    assert [c["status"] for c in states["calls"]] == ["timeout"]


# -- callee failures ---------------------------------------------------------------------

def failure_script(h, step):
    h.script(default={"text": "ok"}, rules=[
        rule("hive call from", step, shard="^b$"),
        rule("GO", {"mcp": [tool("hive_call", to="b", question="q", timeout=60)],
                    "text": "{{mcp:0.status}}/{{mcp:0.error}}"}, shard="^a$")])


def test_callee_is_error_reply(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            failure_script(h, {"is_error": True, "text": "boom"})
            await h.send("a", "GO")
            await settle(h, "a", "b")
            return await h.hive_calls()

    calls = run(scenario())
    assert h.queue_rows("a")[0]["response"] == "error/callee_error"
    assert calls[0]["status"] == "error"
    assert call_rows(h, "b")[0]["processed"] == 3


def test_callee_exits_mid_answer(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            failure_script(h, {"exit": True, "text": "partial"})
            await h.send("a", "GO")
            await settle(h, "a", timeout=20)
            return await h.hive_calls()

    calls = run(scenario())
    assert h.queue_rows("a")[0]["response"] == "error/callee_failed"
    assert calls[0]["status"] == "error"
    # Deviation from the spec's "row CRASHED by the respawn watcher": the turn's
    # finish_turn (shard lock held) writes the reply and marks the row COMPLETE
    # before the watcher gets the lock, so the watcher's UPDATE matches nothing.
    assert call_rows(h, "b")[0]["processed"] == 2


def test_callee_usage_wall_row_is_held_and_never_runs(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            h.module.hive_lib.HIVE_MIN_TIMEOUT_S = 0.3
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from", {"is_error": True,
                                        "text": "You've hit your session limit · resets 3pm"},
                     shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="q", timeout=0)],
                            "text": "{{mcp:0.status}}"}, shard="^a$")])
            await h.send("a", "GO")
            await settle(h, "a", timeout=20)
            await asyncio.sleep(0.3)
            return await h.hive_calls()

    try:
        calls = run(scenario())
    finally:
        import hive
        hive.HIVE_MIN_TIMEOUT_S = 5
    # The held row outlives its queue deadline, so 1.2's expiry (or the caller's
    # deadline) ends the call; either way it is skipped and answers nothing.
    # GET runs 1.2's expire on the callee, so the held QUEUED row always ends as
    # `expired` (not the caller timeout); it is skipped and answers nothing.
    assert h.queue_rows("a")[0]["response"] == "expired"
    assert call_rows(h, "b")[0]["processed"] == 4
    (reply,) = reply_rows(h, "a")
    assert json.loads(reply["content"])["error"] == "expired"
    assert len(h.sent_to("b")) == 1
    assert calls[0]["status"] == "expired"


# -- the caller side ends ----------------------------------------------------------------

def _blocked_caller(h):
    h.script(default={"text": "ok"}, rules=[
        rule("LONG", {"delay_ms": 4000, "text": "slow"}, shard="^b$"),
        rule("GO", {"mcp": [tool("hive_call", to="b", question="q", timeout=60)],
                    "text": "{{mcp:0.status}}"}, shard="^a$")])


@pytest.mark.parametrize("how", ["interrupt", "kill"])
def test_caller_interrupted_or_killed_cancels_the_call(harness, how):
    h = harness(agents=["a", "b"])
    seen = {}

    async def scenario():
        async with h:
            _blocked_caller(h)
            await h.send("b", "LONG")
            await h.wait_for(lambda: h.module.agent_states.get("b") == "PROCESSING")
            await h.send("a", "GO")
            await h.wait_for(lambda: h.module.STATE.hive.open, timeout=15)
            if how == "interrupt":
                await h.interrupt("a")
            else:
                r = await h.client.post("/agents/a/kill", headers=h._headers())
                assert r.status == 200
            await h.wait_for(lambda: not h.module.STATE.hive.open, timeout=10)
            await h.wait_for(lambda: call_rows(h, "b")[0]["processed"] == 4, timeout=10)
            await settle(h, "b", timeout=20)
            seen["calls"] = await h.hive_calls()

    run(scenario())
    (call,) = call_rows(h, "b")
    assert (call["processed"], call["response"]) == (4, "cancelled")
    assert len(h.sent_to("b")) == 1          # nothing ran on b for the call
    assert seen["calls"][0]["status"] == "timeout"


def test_restart_abandons_queued_call_and_reply_rows(harness, tmp_workspace):
    h = harness(agents=["a", "b"])

    async def first():
        async with h:
            h.script(default={"text": "ok"})

    run(first())
    conn = sqlite3.connect(str(h.module.DB_PATH))
    ins = ("INSERT INTO message_queue (agent, channel, channel_id, author, content,"
           " message_id, call_id, reply_to_agent) VALUES (?, 'hive', '0', 'x', 'q', ?, ?, ?)")
    conn.execute(ins, ("b", "call-c-r", "c-r", "a"))
    conn.execute(ins, ("a", "reply-c-r", "c-r", None))
    conn.commit()
    conn.close()

    from harness import Harness
    h2 = Harness(tmp_workspace, agents=["a", "b"])

    async def second():
        async with h2:
            h2.script(default={"text": "ok"})
            await asyncio.sleep(0.5)

    run(second())
    for shard, mid in (("b", "call-c-r"), ("a", "reply-c-r")):
        row = [r for r in h2.queue_rows(shard) if r["message_id"] == mid][0]
        assert (row["processed"], row["response"]) == (4, "abandoned")
    assert h2.sent_to("b") == [] and h2.sent_to("a") == []


# -- steering and stealing exclusions (2.5 / 2.4) -------------------------------------------

def test_call_rows_are_not_steered_into(harness):
    """A call turn in flight takes no steered line, and a call row is never
    steered into someone else's turn (step 2.5)."""
    h = harness(agents=["a", "b"], shards=SHARDS, steering={"coalesce_ms": 0})

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from", {"text": "42", "delay_ms": 800}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="meaning?")],
                            "text": "answer={{mcp:0.answer}}"}, shard="^a$")])
            await h.send("a", "GO")
            await h.wait_for(lambda: call_rows(h, "b") and call_rows(h, "b")[0]["processed"] == 1)
            await asyncio.sleep(0.2)
            await h.send("b", "ordinary")        # b is mid call turn
            await asyncio.sleep(0.3)
            ordinary = [r for r in h.queue_rows("b") if r["content"] == "ordinary"][0]
            assert ordinary["processed"] == 0     # QUEUED: not merged into the call turn
            await settle(h, "a", "b")
            sent = h.sent_to("b")
            assert len(sent) == 2 and "ordinary" not in sent[0] and "hive call" not in sent[1]

    run(scenario())


def test_call_rows_are_not_stealable(harness):
    pytest.skip("2.4 (work stealing) is not merged: nothing to exclude call rows from yet")


# -- lost wake-up in the caller's long poll -------------------------------------------------

def test_reply_landing_between_the_poll_checks_is_not_missed(harness):
    """Cause of the full-suite timeouts in test_depth_cap_two_for_calls and
    test_two_shards_calling_each_other_is_refused_at_once. GET /hive/call checks
    for a reply, then reads the call row (a second await), then waits for a
    wake-up on the caller. A reply inserted and notified in that gap was missed
    (the notify task had already run), and the poll then slept its whole 20 s slice
    while the answer sat in the queue. Under load the gap widens; here it is held
    open on purpose, so a regression fails in seconds, not by luck."""
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            real = h.module._hive_fetch

            async def slow(sql, params=()):
                rows = await real(sql, params)
                if sql.startswith("SELECT processed, response"):
                    await asyncio.sleep(1.0)       # the gap: after the reply check, before waiting
                return rows
            h.module._hive_fetch = slow
            h.script(default={"text": "ok"}, rules=[
                rule("hive call from a", {"text": "answer-b"}, shard="^b$"),
                rule("GO", {"mcp": [tool("hive_call", to="b", question="q")],
                            "text": "A:{{mcp:0.answer}}"}, shard="^a$")])
            t0 = time.monotonic()
            await h.send("a", "GO")
            await settle(h, "a", "b", timeout=10)
            return time.monotonic() - t0

    elapsed = run(scenario())
    assert h.queue_rows("a")[0]["response"] == "A:answer-b"
    assert elapsed < 8                    # a missed wake-up costs the 20 s poll slice
