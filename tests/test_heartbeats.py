import json
import os
import sys

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import heartbeats as hb  # noqa: E402
from job_registry import Job, Every  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
JOB = Job("j", "job", Every(60), "m:f", 300)
COMP = Job("c", "component", None, None, 300)


def test_touch_chains_previous_and_is_atomic(tmp_path):
    a = hb.touch(tmp_path, "j", now=NOW)
    b = hb.touch(tmp_path, "j", ok=False, detail="boom", now=NOW + timedelta(seconds=60))
    assert a["previous_timestamp"] is None
    assert b["previous_timestamp"] == a["timestamp"]
    assert hb.read(tmp_path, "j")["ok"] is False
    assert not list((tmp_path / "data/health/heartbeats").glob("*.tmp"))
    assert set(hb.read_all(tmp_path, [JOB]).keys()) == {"j"}


def test_status_ok_failing_never_stale():
    started = NOW - timedelta(seconds=100)
    ok = {"timestamp": (NOW - timedelta(seconds=10)).isoformat(), "ok": True}
    assert hb.status(JOB, ok, started, NOW) == "ok"
    assert hb.status(JOB, {**ok, "ok": False}, started, NOW) == "failing"
    assert hb.status(JOB, None, started, NOW) == "never"           # boot grace
    assert hb.status(JOB, None, NOW - timedelta(seconds=400), NOW) == "stale"
    old = {"timestamp": (NOW - timedelta(seconds=900)).isoformat(), "ok": True}
    assert hb.status(JOB, old, NOW - timedelta(seconds=1000), NOW) == "stale"
    # a restart inside the window resets the clock
    assert hb.status(JOB, old, NOW - timedelta(seconds=30), NOW) == "ok"


def test_component_timestamps_naive_aware_z(tmp_path):
    d = tmp_path / "data" / "health"
    d.mkdir(parents=True)
    for stamp in (datetime.now().isoformat(),
                  datetime.now(timezone.utc).isoformat(),
                  datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"):
        (d / "c.json").write_text(json.dumps({"timestamp": stamp}))
        rec = hb.read_all(tmp_path, [COMP])["c"]
        assert hb.status(COMP, rec, None, datetime.now(timezone.utc)) == "ok", stamp
        assert hb.check_component_file(d, "c.json", 300)[0] is True


def test_corrupt_file_is_stale_not_exception(tmp_path):
    (tmp_path / "data/health/heartbeats").mkdir(parents=True)
    (tmp_path / "data/health/heartbeats/j.json").write_text("not json")
    rec = hb.read(tmp_path, "j")
    assert hb.status(JOB, rec, NOW, NOW) == "stale"
    assert hb.touch(tmp_path, "j", now=NOW)["previous_timestamp"] is None


@pytest.fixture(autouse=True)
def _isolated_touch_dir(tmp_path, monkeypatch):
    """Keep every test off the real ~/.karakos/heartbeats."""
    monkeypatch.setenv("KARAKOS_HEARTBEAT_DIR", str(tmp_path / "hb"))


def _touch_file(root, name, at):
    d = root / "hb"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text("")
    stamp = at.timestamp()
    os.utime(f, (stamp, stamp))
    return f


def test_touch_file_alone_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_HEARTBEAT_DIR", str(tmp_path / "hb"))
    _touch_file(tmp_path, "j", NOW - timedelta(seconds=10))
    rec = hb.read(tmp_path, "j")
    assert rec["source"] == "touch"
    assert hb.status(JOB, rec, None, NOW) == "ok"


def test_newer_record_wins_both_ways(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_HEARTBEAT_DIR", str(tmp_path / "hb"))
    hb.touch(tmp_path, "j", now=NOW - timedelta(seconds=60))
    f = _touch_file(tmp_path, "j", NOW - timedelta(seconds=10))
    assert hb.read(tmp_path, "j")["source"] == "touch"
    stamp = (NOW - timedelta(seconds=120)).timestamp()
    os.utime(f, (stamp, stamp))
    rec = hb.read(tmp_path, "j")
    assert "source" not in rec and "pid" in rec


def test_corrupt_json_falls_back_to_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_HEARTBEAT_DIR", str(tmp_path / "hb"))
    (tmp_path / "data/health/heartbeats").mkdir(parents=True)
    (tmp_path / "data/health/heartbeats/j.json").write_text("not json")
    _touch_file(tmp_path, "j", NOW - timedelta(seconds=10))
    rec = hb.read(tmp_path, "j")
    assert rec["source"] == "touch"
    assert hb.status(JOB, rec, None, NOW) == "ok"


def test_bad_name_and_missing_are_none(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_HEARTBEAT_DIR", str(tmp_path / "hb"))
    assert hb.read_touch("../x") is None
    assert hb.read_touch("nope") is None
    assert not (tmp_path / "hb").exists()


def test_default_dir_is_home_karakos(tmp_path, monkeypatch):
    monkeypatch.delenv("KARAKOS_HEARTBEAT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hb.touch_dir() == tmp_path / ".karakos" / "heartbeats"
