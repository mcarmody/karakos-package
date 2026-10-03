"""ServerState late binding, the shard seam, notify_enqueued, active_turns,
the server wrappers and the loop's location (step 2.0)."""

import asyncio
import inspect
import re
from pathlib import Path

from harness import Harness

ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


def test_state_reads_server_globals_at_access_time(harness):
    h = harness(agents=["a"])
    seen = []

    async def scenario():
        async with h:
            mod = h.module
            first_db = mod.db

            async def other_post(agent, channel_id, content, reply_to=None,
                                 dead_letter=False, queue_message_id=None):
                seen.append(content)
                return "other-1"

            mod.post_to_discord = other_post
            assert mod.STATE.post_to_discord is other_post
            assert mod.STATE.db is first_db
            h.script(default={"text": "hello"})
            await h.send("a", "x", channel_id="5")
            await h.wait_idle("a")
            sentinel = object()
            mod.db = sentinel
            assert mod.STATE.db is sentinel
            mod.db = first_db

    run(scenario())
    assert seen == ["hello"]
    assert h.discord == []  # the replacement, not the harness recorder, got it


def test_shard_seam_is_identity_over_agent_config(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            st, mod = h.module.STATE, h.module
            assert st.shard_ids() == list(mod.agent_config)
            for x in mod.agent_config:
                assert st.agent_of(x) == x
                assert st.cfg(x) == mod.agent_config[x]
            assert st.cfg("nope") == {}

    run(scenario())


def test_notify_enqueued_branches(harness):
    h = harness(agents=["a"])
    calls = []

    async def scenario():
        async with h:
            mod = h.module
            import turn_loop

            async def fake_drain(state, shard):
                calls.append(("drain", shard))

            async def fake_typing(agent, channel_id):
                calls.append(("typing", agent, channel_id))

            turn_loop_drain, mod_typing = turn_loop.drain_shard, mod.start_typing
            turn_loop.drain_shard = fake_drain
            mod.start_typing = fake_typing
            try:
                for st in ("IDLE", "PROCESSING", "ERROR_RECOVERY", None):
                    if st is None:
                        mod.agent_states.pop("a", None)
                    else:
                        mod.agent_states["a"] = st
                    turn_loop.notify_enqueued(mod.STATE, "a", "9")
                    await asyncio.sleep(0.01)
                    calls.append(("--", st))
            finally:
                turn_loop.drain_shard = turn_loop_drain
                mod.start_typing = mod_typing
                mod.agent_states["a"] = "IDLE"

    run(scenario())
    assert calls == [("drain", "a"), ("--", "IDLE"),
                     ("typing", "a", "9"), ("--", "PROCESSING"),
                     ("--", "ERROR_RECOVERY"), ("--", None)]


def test_active_turns_set_during_turn_and_empty_after(harness):
    h = harness(agents=["a"])
    during = []

    async def scenario():
        async with h:
            h.script(default={"text": "ok", "delay_ms": 100})
            await h.send("a", "x")
            await h.wait_for(lambda: "a" in h.module.STATE.active_turns)
            during.append(h.module.STATE.active_turns["a"])
            await h.wait_idle("a")
            assert h.module.STATE.active_turns == {}

    run(scenario())
    assert during[0].shard == "a" and len(during[0].message_ids) == 1


def test_active_turns_cleared_after_read_error(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            mod = h.module

            async def boom(agent, channel_id, message_ids=None):
                raise RuntimeError("read blew up")

            mod.read_agent_response = boom
            mod.agent_states["a"] = "HOLD"
            await h.send("a", "x")
            mod.agent_states["a"] = "IDLE"
            try:
                await mod.process_agent_queue("a")
            except RuntimeError:
                pass
            assert mod.STATE.active_turns == {}
            assert "a" not in mod.agent_turn_context

    run(scenario())


def test_wrapper_signatures_unchanged():
    import importlib.util
    src = (ROOT / "bin" / "agent-server.py").read_text()
    for name, params in (
        ("process_agent_queue", "agent: str"),
        ("send_to_agent", "agent: str, content: str, message_ids: List[str]"),
    ):
        assert re.search(rf"async def {name}\(\s*{re.escape(params)}\s*\)", src), name
    assert re.search(
        r"async def read_agent_response\(\s*agent: str, channel_id: str, "
        r"message_ids: Optional\[List\[str\]\] = None\s*\)", src)


def test_wrappers_exist_with_prerefactor_signatures(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            m = h.module
            assert list(inspect.signature(m.process_agent_queue).parameters) == ["agent"]
            assert list(inspect.signature(m.send_to_agent).parameters) == [
                "agent", "content", "message_ids"]
            assert list(inspect.signature(m.read_agent_response).parameters) == [
                "agent", "channel_id", "message_ids"]
            assert inspect.signature(m.read_agent_response).parameters[
                "message_ids"].default is None

    run(scenario())


def test_loop_lives_in_turn_loop_only():
    server = (ROOT / "bin" / "agent-server.py").read_text()
    loop = (ROOT / "lib" / "turn_loop.py").read_text()
    pat = r"async def (claim_next|run_turn|finish_turn|drain_shard)\b"
    assert not re.search(pat, server)
    found = re.findall(pat, loop)
    assert sorted(found) == ["claim_next", "drain_shard", "finish_turn", "run_turn"]
