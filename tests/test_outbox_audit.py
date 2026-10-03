"""Outbox audit through the legacy adapter."""
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
