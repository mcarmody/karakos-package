"""lib/token_budget.py (spec 2.7). Pure."""
import asyncio
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import token_budget as tb  # noqa: E402

B = 1000
M = 1800


def test_pause_on_crossing_and_truth_table():
    assert tb.pause_state(100, 999, B, None) == (False, None)
    assert tb.pause_state(100, 1000, B, None) == (True, 100)
    # inside the minimum pause: paused even when usage is under budget
    assert tb.pause_state(100 + 29 * 60, 0, B, 100) == (True, 100)
    # at 30 min and under budget: resumes
    assert tb.pause_state(100 + M, 999, B, 100) == (False, None)
    # past the minimum but still over: stays, keeps the first paused_since
    assert tb.pause_state(100 + M + 500, 1000, B, 100) == (True, 100)


def test_no_flapping_when_usage_hovers_at_the_ceiling():
    paused, since, spans, start = False, None, [], None
    for i in range(400):
        usage = 1000 if i % 2 == 0 else 990  # hovers around the ceiling
        was = paused
        paused, since = tb.pause_state(i * 60, usage, B, since)
        if paused and not was:
            start = i * 60
        if was and not paused:
            spans.append(i * 60 - start)
    assert spans and all(s >= M for s in spans)  # no pause shorter than the minimum


def test_notice_cooldown():
    assert tb.should_announce_pause(10, None)
    assert not tb.should_announce_pause(3599, 0)
    assert tb.should_announce_pause(3600, 0)


def test_usage_in_window(tmp_path):
    async def go():
        tb._cache.clear()
        async with aiosqlite.connect(tmp_path / "u.db") as db:
            await db.execute("CREATE TABLE cost_events (id INTEGER PRIMARY KEY, agent TEXT,"
                             " input_tokens INTEGER, output_tokens INTEGER,"
                             " timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            ins = ("INSERT INTO cost_events (agent, input_tokens, output_tokens, timestamp)"
                   " VALUES (?, ?, ?, datetime(?, 'unixepoch'))")
            now = 1_800_000_000
            for agent, i, o, age in (("a", 100, 50, 60), ("a-2", 10, 5, 3600),
                                     ("a", 7, 7, 5 * 3600), ("b", 999, 999, 60)):
                await db.execute(ins, (agent, i, o, now - age))
            await db.commit()
            assert await tb.usage_in_window(db, ["a", "a-2"], tb.TOKEN_BUDGET_WINDOW_S, now) == 165
            # cached for 60 s, bypassed on request
            await db.execute("DELETE FROM cost_events")
            await db.commit()
            assert await tb.usage_in_window(db, ["a", "a-2"], tb.TOKEN_BUDGET_WINDOW_S, now + 5) == 165
            assert await tb.usage_in_window(db, ["a", "a-2"], tb.TOKEN_BUDGET_WINDOW_S, now + 5,
                                            use_cache=False) == 0
            # a missing table fails open
        async with aiosqlite.connect(tmp_path / "empty.db") as db2:
            assert await tb.usage_in_window(db2, ["a"], 14400, 1) == 0
    asyncio.run(go())
