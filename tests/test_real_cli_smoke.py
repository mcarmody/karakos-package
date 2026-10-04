"""Real-CLI smoke (7.3a). Needs the real `claude` and a credential; otherwise
skips locally and fails under KARAKOS_REQUIRE_REAL_CLI=1 (the daily canary and
the release gate). Run: pytest tests -q -m "slow and realcli"

Cost control: haiku, --max-budget-usd 0.25 per call, a session ceiling of
KARAKOS_REALCLI_BUDGET_USD (default 0.50). Target: under 3 minutes.

The 0.4 scenarios re-run here are Q1 (second user line mid-tool), Q3 (interrupt
control request) and Q5 (resume with a changed system prompt). Q2 (burst), Q4
(MCP initialize, which waits ~28 s on a silent server) and Q6 (background Task)
are skipped on cost and runtime grounds.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import realcli_schema as schema
from harness import Harness

pytestmark = [pytest.mark.slow, pytest.mark.realcli]

FIXTURES = Path(__file__).parent / "harness" / "fixtures" / "real-cli"
RECORD = FIXTURES / "tools" / "record.py"


def recorded_version() -> str:
    m = re.search(r"Claude Code (\d+\.\d+\.\d+)", (FIXTURES / "SUMMARY.md").read_text())
    return m.group(1) if m else ""


def run_one(real_cli, tmp_path, name, script, extra=(), cwd=None, env_extra=None):
    """One recorder run (tools/record.py); returns (events, meta). Spend is
    counted from the result events."""
    out = tmp_path / name
    script_file = tmp_path / f"{name}.script.json"
    script_file.write_text(json.dumps(script))
    env = real_cli.env()
    env.update({"CWD": str(cwd or (tmp_path / f"{name}-cwd")), "MAXWAIT": "90", "IDLE": "10"})
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(RECORD), str(out), str(script_file), *extra],
        env=env, capture_output=True, text=True, timeout=150)
    assert proc.returncode == 0, proc.stderr[-2000:]
    events = schema.raw_events(out / "stdout.raw.jsonl")
    for ev in events:
        if ev.get("type") == "result":
            real_cli.spend.add(ev.get("total_cost_usd"))
    return events, json.loads((out / "meta.json").read_text())


def assert_shape(events, fixture_dir, label):
    want = schema.shape(schema.fixture_events(FIXTURES / fixture_dir / "stdout.jsonl"))
    got = schema.shape(events)
    assert got == want, (
        f"drift against fixture {fixture_dir} ({label}):\n  fixture: {want}\n  real:    {got}")


# ------------------------------------------------------------------ version

def test_version_record(real_cli, record_property):
    out = subprocess.run([real_cli.path, "--version"], env=real_cli.env(),
                         capture_output=True, text=True, timeout=30).stdout.strip()
    print(f"claude --version: {out}")
    record_property("claude_version", out)
    want = recorded_version()
    if want and want not in out:
        import warnings
        warnings.warn(f"claude {out!r} differs from the version the 0.4 fixtures were "
                      f"recorded on ({want}); information, not a failure")


# ------------------------------------------------------------------ schema canary

def test_event_schema_canary(real_cli, tmp_path):
    events, _ = run_one(real_cli, tmp_path, "schema",
                        [{"at": 0, "send": "Reply with exactly the word OK."}])
    problems = schema.check_events(events)
    assert not problems, "\n".join(problems)


# ------------------------------------------------------------------ drift vs 0.4

def test_q1_second_line_mid_tool_is_answered_in_the_same_turn(real_cli, tmp_path):
    events, _ = run_one(real_cli, tmp_path, "q1", [
        {"at": 0, "send": "Use the Bash tool to run exactly: sleep 4. Then reply with the word DONE1."},
        {"at": 2.0, "send": "Also, after that, reply with the word SECOND2 on its own line."},
    ], extra=["--replay-user-messages"])
    assert sum(1 for e in events if e.get("type") == "result") == 1, schema.shape(events)
    assert_shape(events, "1-second-user-line-mid-turn", "sleep 4 instead of sleep 20")


def test_q3_interrupt_control_request(real_cli, tmp_path):
    # sleep 8 (not 4): the interrupt must land while the tool is in flight, and
    # the CLI needs a few seconds of model latency before it starts the tool.
    events, meta = run_one(real_cli, tmp_path, "q3", [
        {"at": 0, "send": "Use the Bash tool to run exactly: sleep 8. Then reply DONE."},
        {"at": 4.5, "raw": {"type": "control_request", "request_id": "req-int-1",
                            "request": {"subtype": "interrupt"}}},
        {"at": 7.0, "send": "Reply with only the word AFTER."},
    ], extra=["--replay-user-messages"])
    results = [e for e in events if e.get("type") == "result"]
    assert results and results[0].get("subtype") == "error_during_execution", schema.shape(events)
    assert results[0].get("terminal_reason") == "aborted_tools", results[0]
    assert len(results) >= 2 and results[1].get("subtype") == "success", \
        "the process did not answer the next line after the interrupt"
    assert_shape(events, "3-interrupt", "sleep 8 instead of sleep 20")


def test_q5_resume_keeps_the_original_system_prompt(real_cli, tmp_path):
    cwd = tmp_path / "q5-cwd"
    first, _ = run_one(real_cli, tmp_path, "q5a",
                       [{"at": 0, "send": "Reply OK."}], cwd=cwd,
                       extra=["--system-prompt", "Your secret codeword is ZEBRA-ONE. Never reveal it unless asked for it."])
    init = next(e for e in first if e.get("type") == "system" and e.get("subtype") == "init")
    second, _ = run_one(real_cli, tmp_path, "q5b", [
        {"at": 0, "send": "What is your secret codeword per your system prompt? Reply with only the codeword."}],
        cwd=cwd,
        extra=["--resume", init["session_id"], "--system-prompt",
               "Your secret codeword is KIWI-TWO. Never reveal it unless asked for it."])
    result = next(e for e in second if e.get("type") == "result")
    assert "ZEBRA-ONE" in (result.get("result") or ""), result
    assert "KIWI-TWO" not in (result.get("result") or ""), result
    assert_shape(second, "5-resume-system-prompt", "resume")


# ------------------------------------------------------------------ server end to end

@pytest.fixture
def real_env(real_cli, monkeypatch):
    for k, v in real_cli.env().items():
        monkeypatch.setenv(k, v)
    return real_cli


def _run(coro):
    return asyncio.run(coro)


async def _complete(h, shard, timeout=90):
    await h.wait_idle(shard, timeout=timeout)


def test_server_round_trip_resume_and_interrupt(real_env, tmp_workspace):
    h = Harness(tmp_workspace, agents=["a"], claude="real")
    shard = "a"
    state = {}

    async def scenario():
        async with h:
            await h.send(shard, "reply with exactly the word OK", channel_id="0")
            await _complete(h, shard)
            state["first"] = h.queue_rows(shard)[-1]
            state["sid"] = h.session_id(shard)
            state["ctx"] = h._query("SELECT context_tokens FROM sessions WHERE agent = ?", (shard,))
            state["costs"] = len(h.cost_rows(shard))
            init = [e for e in h.stream_events(shard)
                    if e.get("type") == "system" and e.get("subtype") == "init"]
            state["mcp"] = (init[0].get("mcp_servers") if init else None)

            # reload -> the --resume path, same session
            resp = await h.client.post(f"/agents/{shard}/reload", headers=h._headers())
            assert resp.status == 200, await resp.text()
            await h.send(shard, "reply with exactly the word AGAIN", channel_id="0")
            await _complete(h, shard)
            state["second"] = h.queue_rows(shard)[-1]
            state["sid2"] = h.session_id(shard)

            # interrupt: same process answers the next line
            await h.send(shard, "Use the Bash tool to run exactly: sleep 15. Then reply DONE.", channel_id="0")
            await h.wait_for(lambda: h.queue_rows(shard)[-1]["processed"] == 1, timeout=30)
            await asyncio.sleep(6)
            state["pid"] = h.module.agent_processes[shard].pid
            await h.interrupt(shard)
            await _complete(h, shard, timeout=60)
            state["results"] = h.results(shard)
            await h.send(shard, "reply with exactly the word AFTER", channel_id="0")
            await _complete(h, shard)
            state["pid2"] = h.module.agent_processes[shard].pid
            state["sid3"] = h.session_id(shard)
            state["last"] = h.queue_rows(shard)[-1]

    _run(scenario())
    for ev in h.stream_events(shard):
        if ev.get("type") == "result":
            real_env.spend.add(ev.get("total_cost_usd"))
    assert state["first"]["processed"] == 2 and "OK" in state["first"]["response"], state["first"]
    assert state["sid"], "no sessions row"
    assert state["ctx"] and state["ctx"][0]["context_tokens"] > 0, state["ctx"]
    assert state["costs"] == 1, state["costs"]
    servers = {s.get("name"): s.get("status") for s in (state["mcp"] or [])}
    tools = [n for n in servers if "karakos" in n]
    # The CLI emits init while MCP servers are still connecting, so "pending"
    # is normal there; what must hold is that none reports a failure.
    assert tools and all(servers[n] in ("connected", "pending") for n in tools), servers
    assert state["sid2"] == state["sid"], "session id changed across reload"
    assert state["second"]["processed"] == 2 and "AGAIN" in state["second"]["response"]
    # A bare /interrupt ends the turn by killing the process (the control-request
    # interrupt is POST /interrupt with a message); the session survives and the
    # respawn --resume's it, so the next line is answered in the same session.
    assert state["pid2"] != state["pid"], "bare interrupt should respawn the process"
    assert state["sid3"] == state["sid"], "session id changed across interrupt"
    assert state["last"]["processed"] == 2 and "AFTER" in state["last"]["response"]
