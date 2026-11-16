"""Tests for bin/backup-databases.py: the nightly local backup of an install's SQLite databases."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT, import_script

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import job_registry as jr  # noqa: E402


@pytest.fixture
def bd():
    return import_script("backup-databases")


def make_db(path: Path, rows=3, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    return con


def count(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        con.close()


def test_job_is_registered_nightly_with_a_command():
    job = {j.name: j for j in jr.BUILTIN}["db-backup"]
    assert job.kind == "job" and isinstance(job.schedule, jr.Daily)
    assert job.run == ["python3", "bin/backup-databases.py"]
    assert job.max_age_s == 48 * 3600


def test_backs_up_every_database_keeping_relative_paths(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "memory" / "graph.db").close()
    make_db(data / "outbox" / "outbox.db", rows=5).close()
    (data / "notes.txt").write_text("not a database")
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    day = dest / "2026-11-14"
    assert count(day / "memory" / "graph.db") == 3
    assert count(day / "outbox" / "outbox.db") == 5
    assert not (day / "notes.txt").exists()
    manifest = json.loads((day / "manifest.json").read_text())
    assert sorted(f["path"] for f in manifest["files"]) == ["memory/graph.db", "outbox/outbox.db"]
    assert manifest["failed"] == []
    assert not list(day.rglob("*.partial"))


def test_consistent_with_a_live_wal_writer(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    con = make_db(data / "agent.db", rows=4, wal=True)
    con.execute("INSERT INTO t VALUES (99)")   # committed below, still in the WAL file
    con.commit()
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    assert count(dest / "2026-11-14" / "agent.db") == 5
    con.close()


def test_keeps_only_the_newest_seven_days(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "a.db").close()
    (dest / "keep-me").mkdir(parents=True)
    for d in range(1, 10):
        assert bd.run_backup(data, dest, keep=7, today=f"2026-11-{d:02d}") == 0
    days = sorted(p.name for p in dest.iterdir() if p.is_dir())
    assert days == ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07",
                    "2026-11-08", "2026-11-09", "keep-me"]


def test_rerun_on_the_same_day_replaces_that_days_copy(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    con = make_db(data / "a.db", rows=1)
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    con.execute("INSERT INTO t VALUES (2)")
    con.commit()
    con.close()
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    assert count(dest / "2026-11-14" / "a.db") == 2


def test_a_corrupt_database_is_reported_and_the_others_are_still_saved(bd, tmp_path, capsys):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "good.db").close()
    (data / "bad.db").write_bytes(b"this is not a sqlite file" * 100)
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 1
    assert count(dest / "2026-11-14" / "good.db") == 3
    assert not (dest / "2026-11-14" / "bad.db").exists()
    assert "FAILED bad.db" in capsys.readouterr().err
    manifest = json.loads((dest / "2026-11-14" / "manifest.json").read_text())
    assert len(manifest["failed"]) == 1


def test_a_backup_directory_inside_data_is_not_backed_up_again(bd, tmp_path):
    data = tmp_path / "data"
    dest = data / "backups"
    make_db(data / "a.db").close()
    assert bd.run_backup(data, dest, keep=7, today="2026-11-13") == 0
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    assert sorted(p.name for p in (dest / "2026-11-14").iterdir()) == ["a.db", "manifest.json"]


def test_restore_puts_the_saved_database_back_and_drops_stale_journals(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "a.db", rows=3).close()
    assert bd.run_backup(data, dest, keep=7, today="2026-11-14") == 0
    con = sqlite3.connect(data / "a.db")
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    (data / "a.db-wal").write_bytes(b"stale")
    (data / "a.db-shm").write_bytes(b"stale")
    assert bd.run_restore(data, dest, "2026-11-14") == 0
    assert count(data / "a.db") == 3
    assert not (data / "a.db-wal").exists() and not (data / "a.db-shm").exists()


def test_restore_of_an_unknown_day_changes_nothing(bd, tmp_path):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "a.db").close()
    dest.mkdir()
    assert bd.run_restore(data, dest, "2026-01-01") == 1
    assert bd.run_restore(data, dest, "../etc") == 1
    assert count(data / "a.db") == 3


def test_main_uses_the_environment_and_lists(bd, tmp_path, monkeypatch, capsys):
    data, dest = tmp_path / "data", tmp_path / "bk"
    make_db(data / "a.db").close()
    monkeypatch.setenv("KARAKOS_HOME", str(tmp_path))
    monkeypatch.setenv("KARAKOS_BACKUP_DIR", str(dest))
    monkeypatch.setenv("KARAKOS_BACKUP_KEEP", "2")
    assert bd.main([]) == 0
    capsys.readouterr()
    assert bd.main(["--list"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    monkeypatch.setenv("KARAKOS_BACKUP_KEEP", "0")
    assert bd.main([]) == 2
