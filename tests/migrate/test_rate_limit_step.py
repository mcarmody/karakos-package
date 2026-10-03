"""Migrator step 35_rate_limit (spec 2.7): rate_limit_state re-keyed by type."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.migrate import guard, runner  # noqa: E402

OLD = """CREATE TABLE rate_limit_state (agent TEXT PRIMARY KEY, status TEXT,
  rate_limit_type TEXT, resets_at INTEGER, overage_status TEXT,
  is_using_overage INTEGER DEFAULT 0, alerted_for_resets_at INTEGER,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO rate_limit_state VALUES
 ('a',   'allowed',  'five_hour', 1000, 'rejected', 0, NULL, '2026-10-03 01:00:00'),
 ('a-2', 'rejected', 'five_hour', 2000, NULL,       1, 2000, '2026-10-03 02:00:00'),
 ('b',   'allowed',  'five_hour', 2000, NULL,       0, 1500, '2026-10-03 02:00:00'),
 ('c',   'allowed',  'seven_day', 9000, NULL,       0, NULL, '2026-10-03 00:30:00'),
 ('d',   'allowed',  NULL,        50,   NULL,       0, NULL, '2026-10-03 00:00:00'),
 ('e',   'allowed',  '  ',        60,   NULL,       0, 60,   '2026-10-03 00:10:00');"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("KARAKOS_SKIP_STAMP_CHECK", raising=False)
    monkeypatch.delenv("KARAKOS_ENV", raising=False)


def make(tmp_path, ddl=OLD):
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    con.executescript(ddl)
    con.commit()
    con.close()
    return data, tmp_path / "config"


def table(data):
    con = sqlite3.connect(data / "memory" / "agent-server.db")
    con.row_factory = sqlite3.Row
    try:
        pk = [r[1] for r in con.execute("PRAGMA table_info(rate_limit_state)") if r[5]]
        rows = {r["rate_limit_type"]: dict(r) for r in
                con.execute("SELECT * FROM rate_limit_state")}
        return pk, rows
    finally:
        con.close()


def step():
    return next(s for s in runner.load_steps() if s.name == "35_rate_limit")


def ctx(data, config):
    return runner.Context(data, config, None, None, None)


def test_collapses_to_one_row_per_type_and_second_run_is_noop(tmp_path):
    data, config = make(tmp_path)
    assert step().detect(ctx(data, config)) is True
    out = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out.append) == 0
    assert any("35_rate_limit" in l for l in out)
    pk, rows = table(data)
    assert pk == ["rate_limit_type"]
    assert set(rows) == {"five_hour", "seven_day", "unknown"}
    five = rows["five_hour"]
    assert (five["status"], five["resets_at"]) == ("rejected", 2000)   # newest, larger reset on tie
    assert five["alerted_for_resets_at"] == 2000                       # max over the kept reset
    assert five["utilization"] is None
    assert rows["unknown"]["resets_at"] == 60                           # null and blank folded;
    assert rows["unknown"]["alerted_for_resets_at"] == 60               # newest of the two kept
    assert "agent" not in five
    out2 = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out2.append) == 0
    assert any("nothing to do" in l for l in out2)
    assert step().detect(ctx(data, config)) is False


def test_no_table_is_a_noop(tmp_path):
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    sqlite3.connect(data / "memory" / "agent-server.db").close()
    assert step().detect(ctx(data, tmp_path / "config")) is False


def test_already_rekeyed_is_a_noop(tmp_path):
    data, config = make(
        tmp_path, "CREATE TABLE rate_limit_state (rate_limit_type TEXT PRIMARY KEY, status TEXT)")
    assert step().detect(ctx(data, config)) is False


def test_alert_marker_prevents_a_repeat_alert(tmp_path):
    data, config = make(tmp_path)
    runner.run(data, config, backup_root=tmp_path / "bk", out=lambda *_: None)
    _, rows = table(data)
    # the server's rule: alert once per resets_at, kept in alerted_for_resets_at
    five = rows["five_hour"]
    assert five["alerted_for_resets_at"] == five["resets_at"]


def test_server_refuses_before_step_and_boots_after(tmp_path, monkeypatch):
    import asyncio
    import importlib.util
    import os
    data, config = make(tmp_path)
    ws = tmp_path
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    (ws / "logs").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "ags_rl_step", ROOT / "bin" / "agent-server.py")
    ags = importlib.util.module_from_spec(spec)
    sys.modules["ags_rl_step"] = ags
    spec.loader.exec_module(ags)
    with pytest.raises(SystemExit):
        asyncio.run(ags.init_db())
    # the refused boot above already created 2.0-shaped sibling tables: force past
    # the fork policy, which is not what this test is about
    runner.run(data, config, backup_root=tmp_path / "bk", out=lambda *_: None, force=True)

    async def boot():
        # the other 1.x tables are absent in this fixture, so init_db creates them
        await ags.init_db()
        await ags.db.close()
    try:
        asyncio.run(boot())
    except SystemExit as e:  # an unrelated schema guard (message_queue) is not ours
        assert "rate_limit_state" not in str(e)
