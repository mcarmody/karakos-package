"""Respawn must --resume a begun session; a turn whose CLI died is never "complete".

Found by the real-CLI smoke: after /reload the server passed --session-id for a
session that already existed ("Session ID ... is already in use"), and the dying
CLI's turns were recorded complete with an empty response.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).parent / "harness" / "bin" / "claude"
STATUS_COMPLETE, STATUS_CRASHED = 2, 3


def run(coro):
    return asyncio.run(coro)


def _spawn_fake(log_dir, *flags):
    env = {**os.environ, "FAKE_CLAUDE_LOG_DIR": str(log_dir)}
    return subprocess.run(
        [str(FAKE), "-p", "--input-format", "stream-json",
         "--output-format", "stream-json", *flags],
        input=json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        capture_output=True, text=True, env=env, timeout=30)


def test_fake_cli_rejects_session_id_reuse_like_the_real_one(tmp_path):
    first = _spawn_fake(tmp_path, "--session-id", "sess-1")
    assert first.returncode == 0
    again = _spawn_fake(tmp_path, "--session-id", "sess-1")
    assert again.returncode == 1
    assert "already in use" in again.stderr
    assert _spawn_fake(tmp_path, "--resume", "sess-1").returncode == 0


def test_reload_resumes_the_existing_session(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            sid = h.session_id("a")
            first = h.argv("a")
            assert "--session-id" in first and "--resume" not in first
            h.script(default={"text": "one"})
            await h.send("a", "first")
            await h.wait_idle("a")
            await h.module.reload_agent("a")
            await h.wait_for(lambda: "--resume" in (h.argv("a") or []))
            assert h.argv("a")[h.argv("a").index("--resume") + 1] == sid
            h.script(default={"text": "two"})
            await h.send("a", "second")
            await h.wait_idle("a")

    run(scenario())
    assert [r["response"] for r in h.queue_rows("a")] == ["one", "two"]


def test_reload_of_a_never_messaged_session_still_uses_session_id(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            await h.module.reload_agent("a")
            await asyncio.sleep(0.3)
            argv = h.argv("a")
            assert "--session-id" in argv and "--resume" not in argv

    run(scenario())


def test_turn_whose_cli_died_is_crashed_not_complete_and_queue_keeps_draining(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(default={"exit": True, "text": "partial"})
            await h.send("a", "dies")
            await h.wait_idle("a")
            h.script(default={"text": "alive"})
            await h.send("a", "after")
            await h.wait_idle("a")

    run(scenario())
    rows = h.queue_rows("a")
    assert rows[0]["processed"] == STATUS_CRASHED
    assert rows[0]["response"]          # never silently empty
    assert rows[1]["processed"] == STATUS_COMPLETE and rows[1]["response"] == "alive"


def _fail_first_write(h):
    real = h.module.send_to_agent
    calls = {"n": 0}

    async def flaky(agent, content, message_ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return await real(agent, content, message_ids)

    h.module.send_to_agent = flaky
    return calls


@pytest.mark.parametrize("steering", [{"enabled": False}, {"enabled": True}])
def test_failed_primary_write_requeues_and_is_answered_after_respawn(harness, steering):
    h = harness(agents=["a"], steering=steering)
    holder = []

    async def scenario():
        async with h:
            holder.append(_fail_first_write(h))
            h.script(default={"text": "answered"})
            await h.send("a", "urgent")
            await h.wait_for(lambda: h.queue_rows("a")[0]["processed"] == STATUS_COMPLETE,
                             timeout=10)

    run(scenario())
    row = h.queue_rows("a")[0]
    assert holder[0]["n"] >= 2
    assert row["response"] == "answered", row
