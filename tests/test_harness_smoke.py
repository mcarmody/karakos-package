"""Smoke tests for the two-agent harness: real agent-server, fake `claude`."""

import asyncio

from harness import Harness

STATUS_COMPLETE = 2


def run(coro):
    return asyncio.run(coro)


def test_single_agent_round_trip(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(default={"text": "pong: {{text}}"})
            await h.send("a", "ping", channel_id="42")
            await h.wait_idle("a")

    run(scenario())
    rows = h.queue_rows("a")
    assert [r["processed"] for r in rows] == [STATUS_COMPLETE]
    assert rows[0]["response"] == "pong: " + h.sent_to("a")[0]
    assert "ping" in h.sent_to("a")[0]
    assert len(h.cost_rows()) == 1
    assert h.cost_rows()[0]["agent"] == "a"
    replies = [d for d in h.discord if d["channel_id"] == "42"]
    assert replies and replies[-1]["content"].startswith("pong:")


def test_two_agents_run_concurrently(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            h.script(default={"text": "I am {{env:KARAKOS_AGENT}}", "delay_ms": 200})
            await h.send("a", "hi")
            await h.send("b", "hi")
            await asyncio.gather(h.wait_idle("a"), h.wait_idle("b"))

    run(scenario())
    assert h.session_id("a") != h.session_id("b")
    assert h.argv("a") is not None and h.argv("b") is not None
    assert h.argv("a")[h.argv("a").index("--session-id") + 1] == h.session_id("a")
    assert h.queue_rows("a")[0]["response"] == "I am a"
    assert h.queue_rows("b")[0]["response"] == "I am b"


def test_argv_has_settings_model_and_system_prompt(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            await h.send("a", "x")
            await h.wait_idle("a")

    run(scenario())
    argv = h.argv("a")
    assert "--settings" in argv
    assert argv[argv.index("--model") + 1] == "fake-model"
    assert "You are harness agent a." in argv[argv.index("--system-prompt") + 1]


def test_model_comes_from_config(tmp_workspace):
    h = Harness(tmp_workspace, agents={"a": {"model": "haiku-test"}})

    async def scenario():
        async with h:
            await h.send("a", "x")
            await h.wait_idle("a")

    run(scenario())
    argv = h.argv("a")
    assert argv[argv.index("--model") + 1] == "haiku-test"


def test_exit_triggers_respawn_notice(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(default={"text": "dying", "exit": True})
            await h.send("a", "boom", channel_id="7")
            await h.wait_for(lambda: any("restarted" in d["content"] for d in h.discord))
            # The respawned process is a live, idle agent again.
            h.script(default={"text": "back"})
            await h.wait_idle("a")
            await h.send("a", "again", channel_id="7")
            await h.wait_idle("a")

    run(scenario())
    notices = [d for d in h.discord if "restarted" in d["content"]]
    assert notices and notices[0]["channel_id"] == "7"
    assert h.queue_rows("a")[-1]["response"] == "back"


def test_hang_then_interrupt_returns_to_idle(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(default={"hang": True})
            await h.send("a", "stuck")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            await h.wait_for(lambda: h.sent_to("a"))
            result = await h.interrupt("a")
            assert result["interrupted"] is True
            await h.wait_idle("a")
            assert h.module.agent_states["a"] == "IDLE"
            # No half-reply was posted for the abandoned turn.
            assert not [d for d in h.discord if d["channel_id"] == "1"]
            h.script(default={"text": "alive"})
            await h.send("a", "next")
            await h.wait_idle("a")

    run(scenario())
    assert h.queue_rows("a")[-1]["response"] == "alive"


def test_error_step_fails_turn_without_crashing_server(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(default={"text": "something broke", "is_error": True})
            await h.send("a", "oops")
            await h.wait_idle("a")
            health = await h.client.get(
                "/health", headers={"Authorization": "Bearer harness-token"})
            assert health.status == 200
            h.script(default={"text": "fine"})
            await h.send("a", "retry")
            await h.wait_idle("a")

    run(scenario())
    rows = h.queue_rows("a")
    # An errored turn never surfaces raw CLI text (B8).
    assert rows[0]["response"] == "The agent hit an error and the turn did not complete."
    assert rows[1]["response"] == "fine"


def test_tool_events_usage_sidechain_and_duplicate_ids(harness):
    h = harness(agents=["a"])
    tools = [
        {"name": "Read", "input": {"file_path": "/x"}, "message_id": "m1",
         "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 10, "output_tokens": 1}},
        {"name": "Bash", "input": {"command": "ls"}, "message_id": "m2",
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 20, "output_tokens": 2}},
        {"name": "Bash", "input": {"command": "pwd"}, "message_id": "m2",
         "parent_tool_use_id": "toolu_parent"},
    ]

    async def scenario():
        async with h:
            h.script(default={"text": "done", "tools": tools})
            await h.send("a", "work")
            await h.wait_idle("a")

    run(scenario())
    assistants = [e for e in h.stream_events("a") if e["type"] == "assistant"]
    tool_events = [e for e in assistants
                   if e["message"]["content"][0]["type"] == "tool_use"]
    assert [e["message"]["id"] for e in tool_events] == ["m1", "m2", "m2"]
    assert [e["message"]["usage"]["input_tokens"] for e in tool_events[:2]] == [1, 2]
    assert [e["parent_tool_use_id"] for e in tool_events] == [None, None, "toolu_parent"]
