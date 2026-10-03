"""Message-queue claim, expiry and wake-up (spec 1.2).

Pure functions over an aiosqlite connection; no HTTP. `shard` is the value in
message_queue.agent (the shard id; the default shard carries the agent's id).
Named msgqueue, not queue: lib/ is first on sys.path and a queue.py there would
shadow the stdlib module that logging.handlers and aiosqlite import.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

if sqlite3.sqlite_version_info < (3, 35):
    raise ImportError(
        f"msgqueue needs SQLite 3.35+ for UPDATE ... RETURNING; "
        f"found {sqlite3.sqlite_version}")

STATUS_QUEUED = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETE = 2
STATUS_CRASHED = 3
STATUS_SKIPPED = 4

_ORDER = "priority DESC, created_at ASC, id ASC"
# A reply row (call_id set, reply_to_agent NULL) is read by the waiting caller's
# tool call, never run as a turn (step 2.3).
_NOT_REPLY = "NOT (call_id IS NOT NULL AND reply_to_agent IS NULL)"
_TS = "%Y-%m-%dT%H:%M:%SZ"


def utc_iso(when=None) -> str:
    """UTC ISO string in the one format expires_at is stored and compared in."""
    if when is None:
        when = datetime.now(timezone.utc)
    if isinstance(when, str):
        return when
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, tz=timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime(_TS)


async def expire(db, shard, now=None) -> int:
    """Skip queued rows past expires_at; tell a waiting caller. Returns count."""
    now = utc_iso(now)
    # execute_fetchall runs the statement and drains it in one hop to the
    # connection's thread. With execute() then fetchall() a RETURNING statement
    # stays pending across an await, and another shard's commit() on the shared
    # connection fails with "SQL statements in progress".
    rows = await db.execute_fetchall(
        "UPDATE message_queue SET processed = ?, response = 'expired',"
        " processed_at = CURRENT_TIMESTAMP"
        " WHERE agent = ? AND processed = ? AND expires_at IS NOT NULL"
        " AND expires_at < ?"
        " RETURNING id, call_id, reply_to_agent",
        (STATUS_SKIPPED, shard, STATUS_QUEUED, now))
    for r in rows:
        if r["call_id"] and r["reply_to_agent"]:
            await db.execute(
                "INSERT OR IGNORE INTO message_queue"
                " (agent, channel, channel_id, server, author, author_id, is_bot,"
                "  content, message_id, call_id, owner_agent)"
                " VALUES (?, 'call', '0', 'local', ?, '0', 1, ?, ?, ?, ?)",
                (r["reply_to_agent"], shard,
                 json.dumps({"error": "expired", "call_id": r["call_id"]}),
                 f"expired-{r['call_id']}-{r['id']}", r["call_id"],
                 r["reply_to_agent"]))
    await db.commit()
    if rows:
        for target in {r["reply_to_agent"] for r in rows
                       if r["call_id"] and r["reply_to_agent"]}:
            notify(target)
    return len(rows)


async def claim_batch(db, shard, limit, now=None) -> list:
    """Claim up to `limit` queued rows for `shard`; returns exactly the rows
    this caller now owns, ordered priority DESC, created_at, id.

    One UPDATE ... RETURNING whose candidates come from a subquery, so a row is
    never in two callers' results. A call row (call_id set) is claimed alone;
    ordinary rows never share a batch with one.
    """
    await expire(db, shard, now)
    async with db.execute(
        f"SELECT id, call_id FROM message_queue WHERE agent = ? AND processed = ?"
        f" AND {_NOT_REPLY} ORDER BY {_ORDER} LIMIT 1", (shard, STATUS_QUEUED)) as cur:
        head = await cur.fetchone()
    if head is None:
        return []
    if head["call_id"] is not None:
        where, args = "id = ?", (head["id"],)
    else:
        where = (f"id IN (SELECT id FROM message_queue WHERE agent = ?"
                 f" AND processed = ? AND call_id IS NULL ORDER BY {_ORDER} LIMIT ?)")
        args = (shard, STATUS_QUEUED, limit)
    rows = await db.execute_fetchall(
        "UPDATE message_queue SET processed = ?, claimed_by = ?,"
        " processing_started_at = CURRENT_TIMESTAMP"
        f" WHERE {where} AND processed = ? AND {_NOT_REPLY} RETURNING *",
        (STATUS_IN_PROGRESS, shard, *args, STATUS_QUEUED))
    await db.commit()
    return sorted(rows, key=lambda r: (-(r["priority"] or 0), r["created_at"], r["id"]))


async def reap_hive_rows(db, now=None, ttl=600) -> int:
    """Skip reply rows nobody consumed within `ttl` seconds (step 2.3)."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, tz=timezone.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - ttl, tz=timezone.utc)
    rows = await db.execute_fetchall(
        "UPDATE message_queue SET processed = ?, response = 'stale',"
        " processed_at = CURRENT_TIMESTAMP"
        " WHERE processed = ? AND call_id IS NOT NULL AND reply_to_agent IS NULL"
        " AND created_at <= ? RETURNING id",
        (STATUS_SKIPPED, STATUS_QUEUED, cutoff.strftime("%Y-%m-%d %H:%M:%S")))
    await db.commit()
    return len(rows)


async def release(db, ids) -> int:
    """Return claimed rows to the queue (still-in-progress only)."""
    ids = list(ids)
    if not ids:
        return 0
    cur = await db.execute(
        "UPDATE message_queue SET processed = ?, claimed_by = NULL,"
        " processing_started_at = NULL"
        f" WHERE id IN ({','.join('?' * len(ids))}) AND processed = ?",
        (STATUS_QUEUED, *ids, STATUS_IN_PROGRESS))
    n = cur.rowcount
    await cur.close()
    await db.commit()
    return n


async def set_partial(db, id, text) -> None:
    await db.execute("UPDATE message_queue SET partial_response = ? WHERE id = ?",
                     (text, id))
    await db.commit()


# --- wake-up ---------------------------------------------------------------
# One Condition per (event loop, shard); the loop in the key keeps tests that
# run a fresh loop each from sharing a Condition across loops.
_conds: dict = {}


def _cond(shard) -> asyncio.Condition:
    key = (asyncio.get_running_loop(), shard)
    c = _conds.get(key)
    if c is None:
        c = _conds[key] = asyncio.Condition()
    return c


async def _notify(shard) -> None:
    c = _cond(shard)
    async with c:
        c.notify_all()


def notify(shard) -> None:
    """Wake waiters on `shard`. Sync; call from inside the running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.get_running_loop().create_task(_notify(shard))


async def wait_for_work(shard, timeout) -> bool:
    """Block until `shard` is notified (True) or `timeout` seconds pass (False)."""
    c = _cond(shard)
    async with c:
        try:
            await asyncio.wait_for(c.wait(), timeout)
        except asyncio.TimeoutError:
            return False
    return True
