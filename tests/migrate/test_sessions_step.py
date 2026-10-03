"""Migrator step 30_sessions (spec 1.5): context columns on sessions."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.migrate import guard, runner  # noqa: E402

OLD = """CREATE TABLE sessions (agent TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  input_tokens INTEGER DEFAULT 0, compaction_count INTEGER DEFAULT 0,
  last_compacted TIMESTAMP);
INSERT INTO sessions (agent, session_id, input_tokens) VALUES ('a', 's1', 77);"""


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
    con.commit()
    con.close()
    return data, tmp_path / "config"


def cols(data):
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    finally:
        con.close()


def test_columns_added_and_rerun_is_noop(tmp_path):
    data, config = make(tmp_path)
    assert "context_tokens" not in cols(data)
    out = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out.append) == 0
    assert any("30_sessions" in l for l in out)
    assert {"context_tokens", "context_updated_at"} <= cols(data)
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    assert con.execute("SELECT input_tokens, context_tokens FROM sessions").fetchall() == [(77, 0)]
    con.close()
    # second run: nothing to do, and the step itself no longer applies
    out2 = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out2.append) == 0
    assert any("nothing to do" in l for l in out2)
    ctx = runner.Context(data, config, None, None, None)
    step = next(s for s in runner.load_steps() if s.name == "30_sessions")
    assert step.detect(ctx) is False
    step.apply(ctx)  # idempotent even if forced
    assert {"context_tokens", "context_updated_at"} <= cols(data)


def test_no_sessions_table_is_not_applicable(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    ctx = runner.Context(data, tmp_path, None, None, None)
    step = next(s for s in runner.load_steps() if s.name == "30_sessions")
    assert step.detect(ctx) is False


def test_server_refuses_boot_before_step(tmp_path):
    data, _ = make(tmp_path)
    ok, msg = guard.check(data)  # unstamped 1.x data: the boot guard refuses
    assert not ok and "karakos migrate" in msg
    with pytest.raises(SystemExit) as e:
        guard.require_stamp(data)
    assert e.value.code == 78
    assert "context_tokens" not in cols(data)  # boot guard wrote nothing
