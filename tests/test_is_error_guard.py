"""An errored `result` is never posted as chat; only known walls take the wall path."""

import asyncio
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path

import pytest

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"

OAUTH = "OAuth token has expired. Please run /login · sk-" + "z" * 30
WALL = "You've hit your session limit · resets 3pm"


@pytest.fixture
def ags(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("ags_iserr_under_test", AGENT_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["ags_iserr_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return m


def _drive(ags, reply):
    posted = []

    async def go():
        await ags.init_db()
        try:
            ags.agent_locks["amos"] = asyncio.Lock()
            ags.agent_states["amos"] = "IDLE"

            async def fake_post(agent, channel_id, content, **kw):
                posted.append(content)
                return "d1"

            async def noop(*a, **k):
                return None

            async def fake_read(agent, channel_id, ids):
                return reply

            ags.post_to_discord = fake_post
            ags.start_typing = noop
            ags.stop_typing = noop
            ags.post_cost_update = noop
            ags.update_session_tokens = noop
            ags.send_to_agent = noop
            ags.read_agent_response = fake_read
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id)"
                " VALUES ('amos','c','42','mike','hello','m1')")
            await ags.db.commit()
            await ags.process_agent_queue("amos")
            async with ags.db.execute("SELECT * FROM message_queue WHERE message_id='m1'") as c:
                return await c.fetchone()
        finally:
            for t in list(ags.agent_hold_tasks.values()):
                t.cancel()
            await ags.db.close()

    row = asyncio.run(go())
    return row, posted


def test_oauth_failure_posts_generic_notice_and_fails_the_row(ags, caplog):
    with caplog.at_level(logging.ERROR):
        row, posted = _drive(ags, (OAUTH, {"is_error": True}))
    assert posted == [ags.GENERIC_TURN_ERROR]
    assert row["processed"] == ags.STATUS_CRASHED
    assert "OAuth" not in (row["response"] or "")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "OAuth token has expired" in logged       # raw text is in the log...
    assert "z" * 30 not in logged                     # ...redacted


def test_wall_text_takes_the_wall_path_without_raw_text(ags):
    row, posted = _drive(ags, (WALL, {"is_error": True}))
    assert row["processed"] == ags.STATUS_QUEUED
    assert row["not_before"] and row["not_before"] > time.time() - 5
    assert len(posted) == 1 and "held" in posted[0]
    assert "session limit" not in posted[0] and ags.GENERIC_TURN_ERROR not in posted[0]


def test_success_is_unchanged(ags):
    row, posted = _drive(ags, ("all good", {"is_error": False}))
    assert row["processed"] == ags.STATUS_COMPLETE
    assert posted == ["all good"]
