"""TurnHooks: order, isolation, defer, suppress_post (step 2.0)."""

import asyncio
import logging

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import turn_loop  # noqa: E402
from turn_loop import TurnHooks  # noqa: E402

STATUS_QUEUED, STATUS_COMPLETE = 0, 2


def run(coro):
    return asyncio.run(coro)


class _Log:
    def __init__(self):
        self.errors = []

    def error(self, msg):
        self.errors.append(msg)


def test_fire_runs_in_order_sync_and_async():
    hooks = TurnHooks(lambda: _Log())
    out = []

    async def a(x):
        out.append(("a", x))

    def b(x):
        out.append(("b", x))

    for name in turn_loop.HOOK_NAMES:
        hooks.register(name, a)
        hooks.register(name, b)
        run(hooks.fire(name, 1))
    assert out == [(k, 1) for _ in turn_loop.HOOK_NAMES for k in ("a", "b")]


def test_exception_is_logged_and_next_hook_runs():
    log = _Log()
    hooks = TurnHooks(lambda: log)
    ran = []

    def bad(*a):
        raise ValueError("nope")

    hooks.register("on_event", bad)
    hooks.register("on_event", lambda *a: ran.append(a))
    run(hooks.fire("on_event", "s", {"type": "x"}))
    assert ran == [("s", {"type": "x"})]
    assert len(log.errors) == 1 and "on_event" in log.errors[0]


def test_registered_hooks_fire_during_a_real_turn_and_survive_a_failure(harness):
    h = harness(agents=["a"])
    seen = []

    async def scenario():
        async with h:
            hooks = h.module.STATE.hooks

            def boom(*a):
                raise RuntimeError("hook failure")

            hooks.register("before_claim", lambda shard: seen.append(("bc", shard)))
            hooks.register("on_turn_start", boom)
            hooks.register("on_turn_start",
                           lambda shard, batch: seen.append(("ts", shard, len(batch.rows))))
            hooks.register("on_event", lambda shard, ev: seen.append(("ev", ev["type"])))
            hooks.register("on_turn_end",
                           lambda shard, res: seen.append(("te", res.response_text)))
            h.script(default={"text": "fine"})
            await h.send("a", "x")
            await h.wait_idle("a")

    run(scenario())
    kinds = [s[0] for s in seen]
    assert kinds[0] == "bc" and kinds[1] == "ts" and kinds[-1] == "te"
    assert ("ts", "a", 1) in seen and ("te", "fine") in seen
    assert ("ev", "result") in seen
    assert h.queue_rows("a")[0]["processed"] == STATUS_COMPLETE


def test_before_claim_defers_leaving_row_queued_and_idle(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.module.STATE.hooks.register("before_claim", lambda shard: "budget")
            await h.send("a", "x")
            await asyncio.sleep(0.3)

    run(scenario())
    assert [r["processed"] for r in h.queue_rows("a")] == [STATUS_QUEUED]
    assert h.module.agent_states["a"] == "IDLE"
    assert h.sent_to("a") == []


def test_suppress_post_skips_discord_but_completes(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            def quiet(shard, result):
                result.suppress_post = True
                result.followups.append(lambda: seen.append("followup"))

            seen = []
            h.module.STATE.hooks.register("on_turn_end", quiet)
            h.script(default={"text": "routed elsewhere"})
            await h.send("a", "x", channel_id="3")
            await h.wait_idle("a")
            # Followups run after the shard lock is released, i.e. just after
            # the shard reads IDLE: wait for it rather than race it.
            await h.wait_for(lambda: seen)
            return seen

    seen = run(scenario())
    assert h.discord == []
    row = h.queue_rows("a")[0]
    assert row["processed"] == STATUS_COMPLETE
    assert row["response"] == "routed elsewhere"
    assert row["discord_response_id"] is None
    assert seen == ["followup"]
