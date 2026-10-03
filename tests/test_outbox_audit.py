"""Outbox audit through the `read_state` adapter: legacy dead-letter file and the 6.1 outbox."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import outbox_audit as oa  # noqa: E402

NOW = time.time()


def kinds(fs):
    return sorted(f.kind for f in fs)


def age(path, seconds):
    os.utime(path, (NOW - seconds, NOW - seconds))


def test_no_files_no_findings(tmp_path):
    st = oa.read_state(tmp_path, NOW)
    assert st.source == "legacy" and st.pending == 0
    assert oa.audit(st, {}, {}) == []


def test_dead_letters_once_per_change(tmp_path):
    p = tmp_path / "data" / "discord-dead-letter.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"a":1}\n{"a":2}\n')
    st = oa.read_state(tmp_path, NOW)
    assert st.dead == 2
    fs = oa.audit(st, {}, {})
    assert kinds(fs) == ["outbox-dead"] and fs[0].severity == "warn"
    prev = oa.prev_of(st)
    assert oa.audit(oa.read_state(tmp_path, NOW + 60), {}, prev) == []   # unchanged: quiet
    p.write_text('{"a":1}\n{"a":2}\n{"a":3}\n')
    assert kinds(oa.audit(oa.read_state(tmp_path, NOW), {}, prev)) == ["outbox-dead"]


def test_old_deferred_message_and_invalid(tmp_path):
    d = tmp_path / "data" / "deferred-messages"
    (d / "invalid").mkdir(parents=True)
    old = d / "m1.json"
    old.write_text("{}")
    age(old, 20 * 60)
    (d / "invalid" / "bad.json").write_text("{}")
    fresh = d / "m2.json"
    fresh.write_text("{}")
    fs = oa.audit(oa.read_state(tmp_path, NOW), {}, {})
    assert kinds(fs) == ["inbound-deferred", "inbound-invalid"]
    assert {f.kind: f.severity for f in fs}["inbound-invalid"] == "info"


def test_young_deferred_is_quiet(tmp_path):
    d = tmp_path / "data" / "deferred-messages"
    d.mkdir(parents=True)
    (d / "m.json").write_text("{}")
    assert oa.audit(oa.read_state(tmp_path, NOW), {}, {}) == []


def test_outbox_db_selects_outbox_source_without_crash(tmp_path):
    (tmp_path / "data" / "outbox").mkdir(parents=True)
    (tmp_path / "data" / "outbox" / "outbox.db").write_text("")
    st = oa.read_state(tmp_path, NOW)
    assert st.source == "outbox"
    assert oa.audit(st, {}, {}) == []


def test_stuck_pending_is_critical():
    st = oa.OutboxState(source="outbox", pending=2, oldest_pending_age_s=700)
    assert [f.severity for f in oa.audit(st, {}, {})] == ["critical"]


# --- 6.1: the outbox branch --------------------------------------------------

def seed_outbox(tmp_path, rows):
    import outbox as ob
    conn = ob.open_store(tmp_path / "data" / "outbox" / "outbox.db")
    for status, created, updated in rows:
        rid, _ = ob.enqueue(conn, "x", "1", "t", now=created)
        conn.execute("UPDATE outbox SET status=?, updated_at=? WHERE id=?", (status, updated, rid))
    conn.close()


def test_outbox_branch_counts_and_ages(tmp_path):
    seed_outbox(tmp_path, [("pending", NOW - 700, NOW - 700), ("sending", NOW - 100, NOW - 100),
                           ("dead", NOW - 5000, NOW - 300), ("dead", NOW - 6000, NOW - 900),
                           ("delivered", NOW - 50, NOW - 50)])
    st = oa.read_state(tmp_path, NOW)
    assert st.source == "outbox" and st.pending == 2 and st.dead == 2
    assert round(st.oldest_pending_age_s) == 700 and round(st.newest_dead_age_s) == 300


def test_outbox_audit_dead_once_per_change_and_stuck(tmp_path):
    seed_outbox(tmp_path, [("pending", NOW - 700, NOW - 700), ("dead", NOW - 5000, NOW - 300)])
    st = oa.read_state(tmp_path, NOW)
    fs = oa.audit(st, {}, {})
    assert kinds(fs) == ["outbox-dead", "outbox-stuck"]       # no info-only finding
    assert [f.severity for f in fs if f.kind == "outbox-stuck"] == ["critical"]
    assert "outbox-dead" not in kinds(oa.audit(oa.read_state(tmp_path, NOW + 60), {}, oa.prev_of(st)))
    seed_outbox(tmp_path, [("dead", NOW - 10, NOW - 10)])
    assert "outbox-dead" in kinds(oa.audit(oa.read_state(tmp_path, NOW), {}, oa.prev_of(st)))


def test_young_pending_is_quiet(tmp_path):
    seed_outbox(tmp_path, [("pending", NOW - 30, NOW - 30)])
    assert oa.audit(oa.read_state(tmp_path, NOW), {}, {}) == []


def test_outbox_wins_over_a_leftover_dead_letter_file_and_spool_still_counted(tmp_path):
    seed_outbox(tmp_path, [("delivered", NOW, NOW)])
    (tmp_path / "data" / "discord-dead-letter.jsonl").write_text('{"a":1}\n')
    d = tmp_path / "data" / "deferred-messages"
    d.mkdir()
    old = d / "m.json"
    old.write_text("{}")
    age(old, 20 * 60)
    st = oa.read_state(tmp_path, NOW)
    assert st.source == "outbox" and st.dead == 0 and st.deferred == 1
    assert kinds(oa.audit(st, {}, {})) == ["inbound-deferred"]


def test_directory_alone_does_not_select_the_outbox(tmp_path):
    (tmp_path / "data" / "outbox").mkdir(parents=True)
    assert oa.read_state(tmp_path, NOW).source == "legacy"


def test_corrupt_outbox_file_is_unavailable_with_zero_counts(tmp_path):
    p = tmp_path / "data" / "outbox" / "outbox.db"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a database at all" * 100)
    st = oa.read_state(tmp_path, NOW)
    assert st.source == "unavailable" and st.pending == 0 and st.dead == 0
    assert oa.audit(st, {}, {}) == []


def test_locked_outbox_file_is_unavailable(tmp_path):
    import sqlite3
    seed_outbox(tmp_path, [("pending", NOW, NOW)])
    db = tmp_path / "data" / "outbox" / "outbox.db"
    lock = sqlite3.connect(str(db), isolation_level=None)
    lock.execute("PRAGMA journal_mode=DELETE")
    lock.execute("BEGIN EXCLUSIVE")
    try:
        st = oa.read_state(tmp_path, NOW)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert st.source == "unavailable" and st.pending == 0
