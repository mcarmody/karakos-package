"""Migrator step 50_build_queue (spec 3.3): the queue DB and its off-by-default config."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

from lib.migrate import guard, runner  # noqa: E402

STEP = {s.name: s for s in runner.load_steps()}["50_build_queue"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("KARAKOS_SKIP_STAMP_CHECK", raising=False)


def make(tmp_path):
    (tmp_path / "data" / "memory").mkdir(parents=True)
    con = sqlite3.connect(tmp_path / "data" / "memory" / "agent-server.db")   # a 1.x data dir
    con.executescript("CREATE TABLE message_queue (id INTEGER PRIMARY KEY, agent TEXT NOT NULL,"
                      " channel TEXT NOT NULL, channel_id TEXT NOT NULL, author TEXT NOT NULL,"
                      " content TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL);")
    con.commit()
    con.close()
    (tmp_path / "config").mkdir()
    return tmp_path / "data", tmp_path / "config"


def migrate(tmp_path, steps=None):
    data, cfg = tmp_path / "data", tmp_path / "config"
    guard.stamp_path(data).unlink(missing_ok=True)
    lines = []
    rc = runner.run(data, cfg, tmp_path / "backups", steps=steps or [STEP], force=True,
                    out=lines.append)
    return rc, lines


def tables(db):
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_chain_lists_the_step_in_order():
    names = [s.name for s in runner.load_steps()]
    assert "50_build_queue" in names
    assert names == sorted(names)
    assert names.index("40_memory") < names.index("50_build_queue")
    assert names[-1] == "50_build_queue" or names[-1] > "50"
    assert STEP.from_schema == 2 and STEP.to_schema == 2     # SCHEMA_VERSION unchanged


def test_creates_tables_and_a_disabled_config(tmp_path):
    data, cfg = make(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    assert {"build_queue", "build_queue_events"} <= tables(data / "build-queue.db")
    doc = yaml.safe_load((cfg / "build-queue.yaml").read_text())
    assert doc["enabled"] is False
    assert "remote" in (cfg / "build-queue.yaml").read_text()          # the commented example
    import build_hosts
    c = build_hosts.load_config(cfg / "build-queue.yaml")
    assert c.invalid is None and c.enabled is False and not c.warnings


def test_columns_match_the_spec(tmp_path):
    data, _ = make(tmp_path)
    migrate(tmp_path)
    c = sqlite3.connect(data / "build-queue.db")
    cols = [r[1] for r in c.execute("PRAGMA table_info(build_queue)")]
    assert cols == ["id", "kind", "status", "reason", "repo", "target_branch", "brief", "requester",
                    "callback_channel", "origin", "source", "priority", "host", "exec_host",
                    "attempts", "source_ref", "not_before", "run_ref", "result", "created_at",
                    "started_at", "finished_at"]
    assert [r[1] for r in c.execute("PRAGMA table_info(build_queue_events)")] == \
        ["id", "queue_id", "ts", "event", "detail"]
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    idx = [r[1] for r in c.execute("PRAGMA index_list(build_queue_events)")]
    assert idx


def test_existing_config_is_not_overwritten(tmp_path):
    data, cfg = make(tmp_path)
    (cfg / "build-queue.yaml").write_text("enabled: true\n")
    migrate(tmp_path)
    assert (cfg / "build-queue.yaml").read_text() == "enabled: true\n"
    assert (data / "build-queue.db").exists()


def test_second_run_is_a_noop(tmp_path):
    data, cfg = make(tmp_path)
    migrate(tmp_path)
    c = sqlite3.connect(data / "build-queue.db")
    c.execute("INSERT INTO build_queue(id, kind, status, brief) VALUES ('bq-1','build','queued','b')")
    c.commit()
    c.close()
    text = (cfg / "build-queue.yaml").read_text()

    class Ctx:
        data_dir, config_dir = data, cfg
    assert STEP.detect(Ctx) is False
    rc, lines = migrate(tmp_path)
    assert rc == 0 and any("plan: no data steps" in l for l in lines)
    c = sqlite3.connect(data / "build-queue.db")
    assert c.execute("SELECT COUNT(*) FROM build_queue").fetchone()[0] == 1
    assert (cfg / "build-queue.yaml").read_text() == text


def test_dry_run_writes_nothing(tmp_path):
    data, cfg = make(tmp_path)
    guard.stamp_path(data).unlink(missing_ok=True)
    lines = []
    assert runner.run(data, cfg, tmp_path / "b", steps=[STEP], dry_run=True,
                      force=True, out=lines.append) == 0
    assert not (data / "build-queue.db").exists() and not (cfg / "build-queue.yaml").exists()
    assert any("build queue" in l for l in lines), lines


def test_partial_db_is_completed(tmp_path):
    data, _ = make(tmp_path)
    c = sqlite3.connect(data / "build-queue.db")
    c.execute("CREATE TABLE build_queue (id TEXT PRIMARY KEY)")
    c.commit()
    c.close()
    migrate(tmp_path)        # table exists with a different shape: the second table is what is missing
    assert "build_queue_events" in tables(data / "build-queue.db")


def test_boot_never_creates_the_database(tmp_path):
    """The dispatcher only checks; with `enabled: true` and no tables it exits."""
    import build_dispatcher as bd
    import build_hosts
    ws = tmp_path
    (ws / "data").mkdir()
    (ws / "inbox").mkdir()
    cfg = build_hosts.parse_config({"enabled": True})
    d = bd.QueueDispatcher(ws / "data" / "build-queue.db", cfg, {}, workspace=ws)
    with pytest.raises(SystemExit) as ei:
        d.open()
    assert "run karakos migrate" in str(ei.value)
    assert not (ws / "data" / "build-queue.db").exists()


def test_setup_creates_the_same_schema(tmp_path):
    import subprocess
    db = tmp_path / "data" / "build-queue.db"
    subprocess.run([sys.executable, str(ROOT / "lib" / "buildq.py"), "init", "--db", str(db)],
                   check=True)
    assert {"build_queue", "build_queue_events"} <= tables(db)
    assert 'lib/buildq.py" init' in (ROOT / "setup.sh").read_text()
