"""lib/usage_governor.py (spec 2.7)."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import aiosqlite

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import usage_governor as ug  # noqa: E402

NOW = 1_800_000_000.0
P = ug.Policy()


def test_decide_table():
    d = ug.decide
    assert d("scheduler", 99, P).reason == "never-gated"
    assert d("scheduler", 99, P).decision == "run"
    assert d("heartbeat", 99, ug.Policy(enabled=False)).reason == "disabled"
    assert d("heartbeat", None, P).reason == "fail-open: usage unreadable"
    assert d("heartbeat", 59, P).decision == "run"           # below 60
    assert d("heartbeat", 60, P).decision == "defer"         # at
    assert d("nightly", 85, P).decision == "defer"           # default 80
    assert d("nightly", 79, P).decision == "run"
    # hysteresis band: a deferred job resumes only below threshold - margin
    assert d("nightly", 77, P, True).decision == "defer"
    assert d("nightly", 75, P, True).decision == "defer"
    assert d("nightly", 74.9, P, True).decision == "run"


def test_glob_matching():
    p = ug.Policy(jobs={"build-queue:*": 50}, never=["health-*"])
    assert ug.decide("build-queue:repo", 50, p).decision == "defer"
    assert ug.decide("health-monitor", 99, p).reason == "never-gated"


def test_policy_load(tmp_path):
    assert ug.Policy.load(tmp_path / "missing.yaml").default == 80
    f = tmp_path / "governor.yaml"
    f.write_text("")
    assert ug.Policy.load(f).jobs == {"heartbeat": 60}
    f.write_text("default: 70\njobs: {nightly: 40}\nnever: [x]\nresume_margin: 2\n")
    p = ug.Policy.load(f)
    assert (p.default, p.jobs, p.never, p.resume_margin) == (70, {"nightly": 40}, ["x"], 2)
    assert p.broken is None


def test_broken_policy_fails_fully_open(tmp_path, caplog):
    f = tmp_path / "governor.yaml"
    for bad in ("a: [unclosed", "- a list", "default: high", "jobs: 3"):
        f.write_text(bad)
        p = ug.Policy.load(f)
        assert p.broken
        assert ug.decide("heartbeat", 99, p).decision == "run"
        assert ug.decide("anything", 99, p).reason == "never-gated"


def row(**kw):
    base = {"is_bot": 1, "server": "local", "channel": "general", "author": "heartbeat",
            "call_id": None}
    base.update(kw)
    return base


def test_is_machine_started():
    assert ug.is_machine_started(row())
    assert not ug.is_machine_started(row(is_bot=0, server="123"))
    assert not ug.is_machine_started(row(is_bot=0))
    assert not ug.is_machine_started(row(channel="hive", server="local"))
    assert not ug.is_machine_started(row(channel="call"))
    assert not ug.is_machine_started(row(channel="handoff"))
    assert ug.job_name(row(author="nightly")) == "nightly"


def test_decision_log_dedups_and_never_raises(tmp_path):
    ug._last_logged.clear()
    path = tmp_path / "logs" / "governor.jsonl"
    d = ug.decide("nightly", 85, P)
    for t in (NOW, NOW + 10, NOW + 70):
        ug.log_decision(path, d, "a", t)
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["decision"] == "defer"
    assert set(lines[0]) == {"ts", "job", "shard", "pct", "threshold", "decision", "reason"}
    ug.log_decision(tmp_path / "logs" / "governor.jsonl" / "nope", d, "a", NOW + 999)


def _queue(tmp_path, rows):
    async def go():
        async with aiosqlite.connect(tmp_path / "q.db") as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "CREATE TABLE message_queue (id INTEGER PRIMARY KEY, agent TEXT, channel TEXT,"
                " server TEXT, author TEXT, is_bot INTEGER, call_id TEXT, processed INTEGER"
                " DEFAULT 0, response TEXT, processed_at TIMESTAMP, created_at TEXT)")
            for r in rows:
                await db.execute(
                    "INSERT INTO message_queue (agent, channel, server, author, is_bot,"
                    " call_id, created_at) VALUES ('a', ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))", r)
            await db.commit()
            out = await ug.age_out(db, "a", P, NOW)
            res = {r["id"]: (r["processed"], r["response"]) for r in
                   await db.execute_fetchall("SELECT * FROM message_queue")}
            return out, res
    return asyncio.run(go())


def test_age_out_expires_and_collapses_duplicate_authors(tmp_path):
    h = 3600
    out, res = _queue(tmp_path, [
        ("g", "local", "heartbeat", 1, None, NOW - 13 * h),   # 1 older than 12h
        ("g", "local", "heartbeat", 1, None, NOW - 3 * h),    # 2 superseded
        ("g", "local", "heartbeat", 1, None, NOW - 1 * h),    # 3 newest, kept
        ("g", "local", "scheduler", 1, None, NOW - 20 * h),   # 4 never gated: untouched
        ("g", "123", "human", 0, None, NOW - 20 * h),         # 5 human: untouched
        ("hive", "local", "x", 1, None, NOW - 20 * h),        # 6 hive: untouched
    ])
    assert out == {"expired": 1, "superseded": 1}
    assert res[1] == (4, "governor-expired")
    assert res[2] == (4, "superseded")
    assert res[3] == (0, None)
    assert all(res[i] == (0, None) for i in (4, 5, 6))


def test_weekly_pct_sync(tmp_path):
    assert ug.weekly_pct_sync(tmp_path / "nope.db", NOW) is None
    db = tmp_path / "agent-server.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rate_limit_state (rate_limit_type TEXT PRIMARY KEY, status TEXT,"
                " resets_at INTEGER, utilization REAL, updated_at TEXT)")
    con.execute("INSERT INTO rate_limit_state VALUES ('seven_day', 'allowed', ?, 73.0, 'x')",
                (int(NOW) + 100,))
    con.commit()
    con.close()
    assert ug.weekly_pct_sync(db, NOW) == 73.0
    assert ug.weekly_pct_sync(db, NOW + 1000) is None  # window rolled over
