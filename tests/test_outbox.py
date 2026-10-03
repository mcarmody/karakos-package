"""lib/outbox.py: store, policy and audit trail, with an injected clock."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import outbox as ob  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture
def conn(tmp_path):
    return ob.open_store(tmp_path / "outbox" / "outbox.db")


def events(conn, rid):
    return [r["event"] for r in conn.execute("SELECT event FROM outbox_events WHERE outbox_id=? ORDER BY id", (rid,))]


def test_store_modes_and_pragmas(tmp_path):
    p = tmp_path / "outbox" / "outbox.db"
    c = ob.open_store(p)
    assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_classify():
    assert [ob.classify(s) for s in (200, 201, 429, 500, 503, None, 400, 401, 403, 404, 422)] == [
        "ok", "ok", "ratelimit", "retry", "retry", "retry", "permanent", "permanent", "permanent",
        "permanent", "permanent"]


def test_backoff_sequence_and_jitter():
    assert [ob.backoff_s(n, jitter=False) for n in range(1, 9)] == [5, 15, 45, 135, 405, 1215, 3600, 3600]
    for _ in range(50):
        assert 4.5 <= ob.backoff_s(1) <= 5.5


def test_enqueue_claim_ordering_per_channel(conn):
    a, ca = ob.enqueue(conn, "x", "1", "first", now=T0, claimed=True)
    b, cb = ob.enqueue(conn, "x", "1", "second", now=T0 + 1, claimed=True)
    c, cc = ob.enqueue(conn, "x", "2", "other", now=T0 + 2)
    assert (ca, cb, cc) == (True, False, False)
    # a is sending, so b is blocked behind it; the other channel is free
    assert [r["id"] for r in ob.claim_due(conn, T0 + 10)] == [c]
    assert ob.record_failure(conn, a, "retry", 500, "HTTP 500", None, T0 + 10) == "pending"
    # a is pending in backoff (not due): b must still wait
    assert ob.claim_due(conn, T0 + 11) == []
    assert [r["id"] for r in ob.claim_due(conn, T0 + 100)] == [a]


def test_retry_policy_to_dead_with_growing_backoff(conn, monkeypatch):
    monkeypatch.setattr(ob, "backoff_s", lambda n, jitter=True: float(5 * 3 ** (n - 1)))
    monkeypatch.setenv("DISCORD_OUTBOX_MAX_ATTEMPTS", "4")
    rid, _ = ob.enqueue(conn, "x", "1", "hi", now=T0)
    now, waits = T0, []
    for n in range(1, 5):
        assert [r["id"] for r in ob.claim_due(conn, now)] == [rid]
        st = ob.record_failure(conn, rid, "retry", 500, "HTTP 500", None, now)
        row = ob.get(conn, rid)
        if st == "pending":
            waits.append(row["next_attempt_at"] - now)
            now = row["next_attempt_at"]
    assert waits == [5, 15, 45] and st == "dead"
    assert "max attempts" in row["dead_reason"] and row["last_status"] == 500 and row["content"] == "hi"


def test_permanent_dead_at_once_and_ratelimit_schedule(conn):
    a, _ = ob.enqueue(conn, "x", "1", "a", now=T0)
    b, _ = ob.enqueue(conn, "x", "2", "b", now=T0)
    ob.claim_due(conn, T0)
    assert ob.record_failure(conn, a, "permanent", 403, "HTTP 403", None, T0) == "dead"
    assert "403" in ob.get(conn, a)["dead_reason"]
    assert ob.record_failure(conn, b, "ratelimit", 429, "HTTP 429", 42.0, T0) == "pending"
    assert ob.get(conn, b)["next_attempt_at"] == T0 + 42
    ob.claim_due(conn, T0 + 50)
    ob.record_failure(conn, b, "ratelimit", 429, "HTTP 429", 9999, T0 + 50)
    assert ob.get(conn, b)["next_attempt_at"] == T0 + 50 + 300 and ob.get(conn, b)["attempts"] == 2


def test_max_age_makes_dead(conn, monkeypatch):
    monkeypatch.setenv("DISCORD_OUTBOX_MAX_AGE_S", "100")
    rid, _ = ob.enqueue(conn, "x", "1", "hi", now=T0)
    ob.claim_due(conn, T0)
    assert ob.record_failure(conn, rid, "retry", 500, "HTTP 500", None, T0 + 101) == "dead"
    assert "max age" in ob.get(conn, rid)["dead_reason"]


def test_chunks_recorded_and_delivered(conn):
    rid, _ = ob.enqueue(conn, "x", "1", "hi", claimed=True, now=T0, chunks_total=2)
    assert ob.record_chunk(conn, rid, "m1", T0) and ob.record_chunk(conn, rid, "m2", T0)
    assert ob.mark_delivered(conn, rid, T0)
    row = ob.get(conn, rid)
    assert json.loads(row["message_ids"]) == ["m1", "m2"] and row["chunks_done"] == 2
    assert row["status"] == "delivered"
    assert events(conn, rid) == ["enqueued", "chunk_delivered", "chunk_delivered", "delivered"]


def test_recover_sending(conn):
    rid, _ = ob.enqueue(conn, "x", "1", "hi", claimed=True, now=T0)
    assert ob.recover_sending(conn, T0 + 5) == 1
    assert ob.get(conn, rid)["status"] == "pending" and "recovered" in events(conn, rid)
    assert ob.recover_sending(conn, T0 + 6) == 0


def test_stats_and_find_for_reply(conn):
    a, _ = ob.enqueue(conn, "x", "1", "hi", now=T0, content_sha=ob.content_sha("raw"))
    ob.enqueue(conn, "x", "2", "yo", now=T0)
    s = ob.stats(conn, T0 + 60)
    assert s["pending"] == 2 and s["sending"] == 0 and s["dead"] == 0 and s["oldest_pending_age_s"] == 60
    assert ob.find_for_reply(conn, "x", "1", ob.content_sha("raw"), T0 + 100)["id"] == a
    assert ob.find_for_reply(conn, "x", "1", ob.content_sha("raw"), T0 + 500) is None
    assert ob.find_for_reply(conn, "y", "1", ob.content_sha("raw"), T0) is None


def test_operator_verbs_and_events(conn):
    rid, _ = ob.enqueue(conn, "x", "1", "hi", now=T0)
    ob.claim_due(conn, T0)
    ob.record_failure(conn, rid, "permanent", 403, "HTTP 403", None, T0)
    assert ob.retry(conn, rid, T0 + 1)
    row = ob.get(conn, rid)
    assert row["status"] == "pending" and row["attempts"] == 0 and row["dead_reason"] is None
    assert not ob.retry(conn, rid, T0 + 2)            # pending rows cannot be "retried"
    assert ob.discard(conn, rid, T0 + 3)
    assert ob.claim_due(conn, T0 + 99) == [] and ob.stats(conn, T0 + 99)["pending"] == 0
    assert ob.retry(conn, rid, T0 + 4)                # discarded can be revived
    assert events(conn, rid)[-3:] == ["retried_manually", "discarded", "retried_manually"]


def test_events_never_carry_content(conn):
    secret = "ZEBRA-secret-content-9981"
    rid, _ = ob.enqueue(conn, "x", "1", secret, claimed=True, now=T0)
    ob.record_failure(conn, rid, "retry", 500, "HTTP 500 " + "x" * 400, None, T0)
    rows = conn.execute("SELECT detail FROM outbox_events").fetchall()
    assert all(secret not in (r[0] or "") and len(r[0] or "") <= 200 for r in rows)


def test_purge_keeps_dead_and_removes_old_delivered(conn):
    d, _ = ob.enqueue(conn, "x", "1", "old", claimed=True, now=T0)
    ob.mark_delivered(conn, d, T0)
    x, _ = ob.enqueue(conn, "x", "2", "dead", now=T0)
    ob.claim_due(conn, T0)
    ob.record_failure(conn, x, "permanent", 403, "HTTP 403", None, T0)
    new, _ = ob.enqueue(conn, "x", "3", "new", claimed=True, now=T0 + 8 * 86400)
    ob.mark_delivered(conn, new, T0 + 8 * 86400)
    res = ob.purge(conn, T0 + 8 * 86400 + 1)
    assert res["delivered"] == 1 and ob.get(conn, d) is None and ob.get(conn, x) and ob.get(conn, new)
    assert not events(conn, d)
    assert ob.purge(conn, T0 + 100 * 86400)["dead"] == 1


def test_list_rows_hides_content_unless_dead_and_asked(conn):
    rid, _ = ob.enqueue(conn, "x", "1", "secret", now=T0)
    assert "content" not in ob.list_rows(conn)[0]
    ob.claim_due(conn, T0)
    ob.record_failure(conn, rid, "permanent", 403, "HTTP 403", None, T0)
    assert "content" not in ob.list_rows(conn, "dead")[0]
    assert ob.list_rows(conn, "dead", include_content=True)[0]["content"] == "secret"
    assert ob.show(conn, rid)["events"]
