"""Migrator step 20_queue (spec 1.2)."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.migrate import runner  # noqa: E402

OLD = """CREATE TABLE message_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, channel TEXT NOT NULL,
 channel_id TEXT NOT NULL, server TEXT DEFAULT 'discord', author TEXT NOT NULL,
 author_id TEXT DEFAULT '0', is_bot INTEGER DEFAULT 0, content TEXT NOT NULL,
 message_id TEXT UNIQUE NOT NULL, mentions_agent INTEGER DEFAULT 0, attachments TEXT,
 processed INTEGER DEFAULT 0, response TEXT, discord_response_id TEXT,
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processing_started_at TIMESTAMP,
 processed_at TIMESTAMP, not_before INTEGER);
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("KARAKOS_SKIP_STAMP_CHECK", raising=False)
    monkeypatch.delenv("KARAKOS_ENV", raising=False)


def make(tmp_path):
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    con.executescript(OLD)
    for st in range(5):
        con.execute("INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                    " message_id, processed) VALUES ('amos','c','1','u','hi',?,?)", (f"m{st}", st))
    con.commit()
    con.close()
    return data, tmp_path / "config"


def info(data):
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(message_queue)")}
        idx = {r[1] for r in con.execute("PRAGMA index_list(message_queue)")}
        rows = con.execute("SELECT message_id, processed, priority, depth, restart_count,"
                           " call_id FROM message_queue ORDER BY id").fetchall()
        return cols, idx, rows
    finally:
        con.close()


def test_step_adds_columns_indexes_and_is_idempotent(tmp_path):
    data, config = make(tmp_path)
    out = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out.append) == 0
    assert any("20_queue" in l for l in out)
    cols, idx, rows = info(data)
    assert {"call_id", "reply_to_agent", "priority", "expires_at", "depth",
            "partial_response", "restart_count", "claimed_by", "owner_agent"} <= cols
    assert {"idx_queue_claim", "idx_queue_call"} <= idx
    assert rows == [(f"m{i}", i, 0, 0, 0, None) for i in range(5)]
    out2 = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out2.append) == 0
    assert any("nothing to do" in l for l in out2)
    ctx = runner.Context(data, config, None, None, None)
    step = next(s for s in runner.load_steps() if s.name == "20_queue")
    assert step.detect(ctx) is False


def test_server_refuses_before_step_has_run(tmp_path):
    from lib.migrate import guard
    data, _ = make(tmp_path)
    ok, msg = guard.check(data)        # 1.x dir, unstamped
    assert not ok and "karakos migrate" in msg
