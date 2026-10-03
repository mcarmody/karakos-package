"""Spec 1.5: session context size comes from the last main-thread API call."""

import asyncio
import json
from pathlib import Path

from harness import Harness

FIXTURE = (Path(__file__).parent / "harness" / "fixtures" / "real-cli"
           / "6-sidechain-and-duplicates" / "stdout.jsonl")


def run(coro):
    return asyncio.run(coro)


def U(inp=5, cc=100, cr=60000, out=7):
    return {"input_tokens": inp, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "output_tokens": out}


def ctx_row(h, shard):
    return h._query("SELECT context_tokens, context_updated_at, input_tokens "
                    "FROM sessions WHERE agent = ?", (shard,))[0]


async def turn(h, agent="a", text="go", **step):
    h.script(default=step)
    await h.send(agent, text)
    await h.wait_idle(agent)


# -- pure function ----------------------------------------------------------

def test_usage_context_tokens(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            f = h.module.usage_context_tokens
            assert f(None) == 0
            assert f({}) == 0
            assert f({"output_tokens": 9}) == 0
            assert f({"input_tokens": None, "cache_creation_input_tokens": None,
                      "cache_read_input_tokens": None}) == 0
            assert f({"input_tokens": 3, "cache_read_input_tokens": None}) == 3
            assert f(U()) == 5 + 100 + 60000

    run(scenario())


# -- turn behaviour ---------------------------------------------------------

def test_last_call_not_sum(harness):
    h = harness(agents=["a"])
    tools = [{"name": "Bash", "usage": U()} for _ in range(7)]
    summed = {k: v * 8 for k, v in U().items()}

    async def scenario():
        async with h:
            await turn(h, text="ok", usage=U(), tools=tools, result_usage=summed)

    run(scenario())
    row = ctx_row(h, "a")
    assert row["context_tokens"] == 5 + 100 + 60000
    assert row["context_updated_at"]
    assert row["input_tokens"] == 40  # uncached turn input, unchanged
    cost = h.cost_rows()[0]
    assert cost["input_tokens"] == 40  # cost rollup keeps the summed field


def test_no_assistant_usage(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            # The result carries usage, but no assistant event does.
            mod = h.module
            events = [
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "hi"}]}},
                {"type": "result", "subtype": "success", "result": "hi",
                 "usage": U(), "total_cost_usd": 0.01, "duration_ms": 1},
            ]
            _, md = await read_events(mod, "a", events)
            assert md["context_tokens"] == 0

    run(scenario())


async def read_events(mod, agent, events):
    """Feed raw stream events through the real read_agent_response."""
    class FakeStdout:
        def __init__(self, lines):
            self.lines = [json.dumps(e).encode() + b"\n" for e in lines]

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class FakeProc:
        returncode = None

        def __init__(self, lines):
            self.stdout = FakeStdout(lines)

    saved = mod.agent_processes.get(agent)
    mod.agent_processes[agent] = FakeProc(events)
    try:
        return await mod.read_agent_response(agent, "1", [])
    finally:
        if saved is None:
            mod.agent_processes.pop(agent, None)
        else:
            mod.agent_processes[agent] = saved


def test_sidechain_ignored(harness):
    h = harness(agents=["a"])
    main_last = U(inp=1, cc=0, cr=61000)
    side = U(inp=1, cc=0, cr=5000)
    tools = [
        {"name": "Task", "usage": U(cr=30000)},
        {"name": "Bash", "usage": side, "parent_tool_use_id": "toolu_x"},
        {"name": "Read", "usage": main_last},
        {"name": "Bash", "usage": side, "parent_tool_use_id": "toolu_x"},
    ]

    async def scenario():
        async with h:
            await turn(h, text="ok", usage=U(cr=20000), tools=tools)

    run(scenario())
    assert ctx_row(h, "a")["context_tokens"] == 61001


def test_real_cli_fixture_sidechain_and_duplicates(harness):
    """Real CLI stream (step 0.4 q6): context is the last main-thread usage."""
    events = [json.loads(l)["event"] for l in FIXTURE.read_text().splitlines() if l.strip()]
    first_result = next(i for i, e in enumerate(events) if e.get("type") == "result")
    events = events[:first_result + 1]
    main = [e for e in events if e.get("type") == "assistant"
            and e.get("parent_tool_use_id") is None and e["message"].get("usage")]
    assert main, "fixture has no main-thread assistant usage"
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            _, md = await read_events(h.module, "a", events)
            return md

    md = run(scenario())
    u = main[-1]["message"]["usage"]
    expect = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
        + (u.get("cache_read_input_tokens") or 0)
    assert md["context_tokens"] == expect


def test_endpoints_and_reset(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            await turn(h, "a", text="ok", usage=U(cr=40000))
            await turn(h, "b", text="ok", usage=U(cr=9000))
            r = await h.client.get("/agents", headers=h._headers())
            agents = {a["name"]: a for a in (await r.json())["agents"]}
            assert agents["a"]["context_tokens"] == 40105
            assert agents["b"]["context_tokens"] == 9105
            assert agents["a"]["shards"]["a"]["context_tokens"] == 40105
            r = await h.client.get("/health", headers=h._headers())
            health = (await r.json())["agents"]
            assert health["a"]["context_tokens"] == 40105
            await h.client.post("/agents/a/reset", headers=h._headers())
            r = await h.client.get("/agents", headers=h._headers())
            agents = {a["name"]: a for a in (await r.json())["agents"]}
            assert agents["a"]["context_tokens"] == 0
            assert agents["b"]["context_tokens"] == 9105

    run(scenario())
