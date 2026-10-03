"""Steering against a real `claude` + haiku, through the real agent-server (step 2.5).

Slow; run by hand before 2.5 merges. Skips without a CLI and credential. The
harness is the real server with the real binary put first on PATH (no fake), so
this exercises what the fake cannot: that real replay content matches the
ledger, rows complete on the turn's `result`, and a control_request interrupt
keeps the process. Expected cost under $0.15.

Q1: a `sleep 5` tool turn plus one mid-turn line -> one result, both rows
COMPLETE. Q3: interrupt-with-message -> same PID, message row COMPLETE.
"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.realcli]

HAVE_CRED = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or (Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                     / ".credentials.json").exists())


def _need_cli():
    if shutil.which("claude") is None or not HAVE_CRED:
        if os.environ.get("CI") or os.environ.get("RELEASE_GATE"):
            pytest.fail("real claude CLI/credential required in the release gate")
        pytest.skip("no claude CLI or credential")


def _real_harness(tmp_path, monkeypatch):
    import harness as harness_pkg
    real = shutil.which("claude")
    bin_dir = tmp_path / "real-bin"
    bin_dir.mkdir()
    (bin_dir / "claude").symlink_to(real)
    monkeypatch.setattr(harness_pkg, "FAKE_BIN_DIR", bin_dir)
    ws = tmp_path / "ws"
    ws.mkdir()
    return harness_pkg.Harness(ws, agents={"a": {"model": "haiku"}},
                               steering={"coalesce_ms": 0})


def _row(h, text):
    return next(r for r in h.queue_rows("a") if text in r["content"])


def test_q1_mid_tool_line_is_merged_and_both_rows_complete(tmp_path, monkeypatch):
    _need_cli()
    h = _real_harness(tmp_path, monkeypatch)

    async def scenario():
        async with h:
            await h.send("a", "Use the Bash tool to run exactly: sleep 5. Then reply DONE1.")
            await h.wait_for(lambda: any(
                "Bash" in str(e) for e in h.stream_events("a")), timeout=90)
            await h.send("a", "Also include the word SECOND2 in your final reply.")
            await h.wait_idle("a", timeout=120)
            assert len(h.results("a")) == 1
            assert _row(h, "sleep 5")["processed"] == 2
            assert _row(h, "SECOND2")["processed"] == 2
            assert h.module.STATE.steered_total.get("a") == 1
            assert h.module.STATE.steer["a"].pending() == []
            assert "SECOND2" in _row(h, "SECOND2")["response"]

    asyncio.run(scenario())


def test_q3_interrupt_with_message_keeps_the_process(tmp_path, monkeypatch):
    _need_cli()
    h = _real_harness(tmp_path, monkeypatch)

    async def scenario():
        async with h:
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "Use the Bash tool to run exactly: sleep 20. Then reply DONE.")
            await h.wait_for(lambda: any(
                "Bash" in str(e) for e in h.stream_events("a")), timeout=90)
            resp = await h.client.post(
                "/agents/a/interrupt", headers=h._headers(),
                json={"message": "Reply with only the word AFTER.", "channel_id": "1"})
            assert (await resp.json())["interrupted"] is True
            await h.wait_for(lambda: _row(h, "AFTER")["processed"] == 2, timeout=120)
            assert h.module.agent_processes["a"].pid == pid
            assert "AFTER" in _row(h, "AFTER")["response"]
            assert [r["subtype"] for r in h.results("a")][0] == "error_during_execution"

    asyncio.run(scenario())
