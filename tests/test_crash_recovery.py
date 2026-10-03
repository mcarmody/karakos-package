"""crash_recovery() behaviour is unchanged by the queue-2.0 schema (spec 1.2)."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("aiohttp")
ROOT = Path(__file__).resolve().parents[1]


def test_in_progress_rows_become_crashed_and_queued_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("ags_crash_recovery", ROOT / "bin" / "agent-server.py")
    ags = importlib.util.module_from_spec(spec)
    sys.modules["ags_crash_recovery"] = ags
    spec.loader.exec_module(ags)
    posted = []

    async def fake_post(agent, channel_id, text, **kw):
        posted.append((agent, channel_id, text))
    ags.post_to_discord = fake_post

    async def go():
        await ags.init_db()
        for i, st in enumerate((0, 1, 1, 2)):
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                " message_id, processed) VALUES ('amos','c',?,'u','x',?,?)",
                ("0" if i == 2 else "55", f"m{i}", st))
        await ags.db.commit()
        await ags.crash_recovery()
        async with ags.db.execute("SELECT message_id, processed, restart_count FROM message_queue ORDER BY id") as c:
            rows = [tuple(r) for r in await c.fetchall()]
        await ags.db.close()
        return rows
    rows = asyncio.run(go())
    assert rows == [("m0", 0, 0), ("m1", 3, 0), ("m2", 3, 0), ("m3", 2, 0)]
    assert [p[1] for p in posted] == ["55"]
