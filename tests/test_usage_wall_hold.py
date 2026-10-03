"""Usage-limit wall: the batch is held with a not-before time, not consumed.

Ports the household's usage-wall hold (59caa0c2b, 2bcc9c110, 39953b5c9,
f6b5baaaf) and rate-limit breaker (2548524f9). Before this, the `result`
event's error text was posted as the reply and the batch marked COMPLETE.
"""

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"


@pytest.fixture
def ags(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("ags_wall_under_test", AGENT_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["ags_wall_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return m


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "You've hit your session limit · resets 3pm",
    "You've hit your weekly limit",
    "Claude AI usage limit reached|1790900000",
])
def test_usage_walls_detected(ags, text):
    assert ags.classify_wall(text, True) == ags.WALL_USAGE


def test_model_wall_detected(ags):
    assert ags.classify_wall(
        "There's an issue with the selected model (x). It may not exist", True
    ) == ags.WALL_MODEL


def test_context_overflow_is_not_a_wall(ags):
    assert ags.classify_wall("Prompt is too long", True) is None
    assert ags.classify_wall("context limit exceeded", True) is None


def test_ordinary_error_and_success_are_not_walls(ags):
    assert ags.classify_wall("tool crashed", True) is None
    assert ags.classify_wall("I explained the session limit to you", False) is None


def test_rejected_event_on_error_turn_is_a_wall(ags):
    assert ags.classify_wall("weird new wording", True, {"status": "rejected"}) == ags.WALL_USAGE
    assert ags.classify_wall("ok", False, {"status": "rejected"}) is None


# --- timing ---------------------------------------------------------------

def test_reset_time_preferred(ags):
    now = 1_000_000
    t = ags.wall_not_before(ags.WALL_USAGE, "x", {"resetsAt": now + 7200}, 0, now=now)
    assert t == now + 7200 + ags.WALL_RESET_MARGIN_SECONDS


def test_epoch_in_text_used(ags):
    now = 1_000_000_000
    t = ags.wall_not_before(ags.WALL_USAGE, f"limit reached|{now + 600}", None, 0, now=now)
    assert t == now + 600 + ags.WALL_RESET_MARGIN_SECONDS


def test_past_or_missing_reset_backs_off_and_grows(ags):
    now = 1_000_000
    a = ags.wall_not_before(ags.WALL_USAGE, "x", {"resetsAt": now - 5}, 0, now=now)
    b = ags.wall_not_before(ags.WALL_USAGE, "x", None, 1, now=now)
    assert a == now + 300 and b == now + 600
    assert ags.wall_not_before(ags.WALL_MODEL, "x", None, 20, now=now) == now + 3600


def test_notice_names_time_and_omits_raw_error(ags):
    n = ags.format_wall_notice(ags.WALL_USAGE, 1_000_000)
    assert "held" in n and "UTC" in n and "hit your" not in n


# --- end to end through process_agent_queue -------------------------------

def _run(ags, scenario):
    async def go():
        await ags.init_db()
        try:
            return await scenario()
        finally:
            for t in list(ags.agent_hold_tasks.values()):
                t.cancel()
            await ags.db.close()
    return asyncio.run(go())


def _wire(ags, replies):
    posted, sent = [], []
    ags.agent_locks["amos"] = asyncio.Lock()
    ags.agent_states["amos"] = "IDLE"

    async def fake_post(agent, channel_id, content, **kw):
        posted.append(content)
        return "d1"

    async def noop(*a, **k):
        return None

    async def fake_send(agent, content, ids):
        sent.append(content)

    async def fake_read(agent, channel_id, ids):
        return replies.pop(0)

    ags.post_to_discord = fake_post
    ags.start_typing = noop
    ags.stop_typing = noop
    ags.post_cost_update = noop
    ags.update_session_tokens = noop
    ags.send_to_agent = fake_send
    ags.read_agent_response = fake_read
    return posted, sent


async def _insert(ags, mid="m1"):
    await ags.db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id)"
        " VALUES ('amos','c','42','mike','hello',?)", (mid,))
    await ags.db.commit()


async def _row(ags, mid="m1"):
    async with ags.db.execute("SELECT * FROM message_queue WHERE message_id=?", (mid,)) as c:
        return await c.fetchone()


def test_wall_requeues_notifies_once_and_does_not_hot_loop(ags):
    resets = int(time.time()) + 7200
    wall = ("You've hit your session limit", {"is_error": True,
            "rate_limit_rejected": {"status": "rejected", "resetsAt": resets}})

    async def scenario():
        posted, sent = _wire(ags, [wall])
        await _insert(ags)
        await ags.process_agent_queue("amos")
        row = await _row(ags)
        assert row["processed"] == ags.STATUS_QUEUED
        assert row["not_before"] == resets + ags.WALL_RESET_MARGIN_SECONDS
        assert len(posted) == 1 and "held" in posted[0] and "session limit" not in posted[0]
        # Re-entry while held (e.g. a new message arrives): no dispatch.
        await _insert(ags, "m2")
        await ags.process_agent_queue("amos")
        await ags.process_agent_queue("amos")
        assert len(sent) == 1 and len(posted) == 1
        assert (await _row(ags, "m2"))["processed"] == ags.STATUS_QUEUED
    _run(ags, scenario)


def test_replays_after_reset_and_completes(ags):
    wall = ("You've hit your weekly limit", {"is_error": True})

    async def scenario():
        posted, sent = _wire(ags, [wall, ("all good", {"is_error": False})])
        await _insert(ags)
        await ags.process_agent_queue("amos")
        # Window passes.
        await ags.db.execute("UPDATE message_queue SET not_before = ?", (int(time.time()) - 1,))
        await ags.db.commit()
        await ags.process_agent_queue("amos")
        row = await _row(ags)
        assert row["processed"] == ags.STATUS_COMPLETE and row["response"] == "all good"
        assert len(sent) == 2
    _run(ags, scenario)


def test_ordinary_error_is_a_failed_turn_not_complete(ags):
    async def scenario():
        _wire(ags, [("boom", {"is_error": True})])
        await _insert(ags)
        await ags.process_agent_queue("amos")
        assert (await _row(ags))["processed"] == ags.STATUS_CRASHED
    _run(ags, scenario)
