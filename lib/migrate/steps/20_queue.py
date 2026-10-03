"""message_queue: schema-2.0 columns and claim indexes (spec 1.2).

Boot never alters 1.x data; this step does. Idempotent: it applies only when a
message_queue table exists and lacks a column or index. Fresh installs get the
same DDL from the server's CREATE TABLE. `agent` holds the shard id.
"""
import sqlite3
from pathlib import Path

from lib.migrate.runner import Step

COLUMNS = (
    ("call_id", "TEXT"),
    ("reply_to_agent", "TEXT"),
    ("priority", "INTEGER DEFAULT 0"),
    ("expires_at", "TEXT"),
    ("depth", "INTEGER DEFAULT 0"),
    ("partial_response", "TEXT"),
    ("restart_count", "INTEGER DEFAULT 0"),
    ("claimed_by", "TEXT"),
    ("owner_agent", "TEXT"),
)
INDEXES = (
    ("idx_queue_claim",
     "CREATE INDEX IF NOT EXISTS idx_queue_claim "
     "ON message_queue(agent, processed, priority DESC, created_at)"),
    ("idx_queue_call",
     "CREATE INDEX IF NOT EXISTS idx_queue_call ON message_queue(call_id)"),
)


def _db(ctx) -> Path:
    data = Path(ctx.data_dir)
    for p in (data / "memory" / "agent-server.db", data / "agent-server.db"):
        if p.is_file():
            return p
    return data / "memory" / "agent-server.db"


def _state(db: Path):
    """(missing columns, missing indexes); None when there is no queue table."""
    if not db.is_file():
        return None
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(message_queue)")}
        idx = {r[1] for r in con.execute("PRAGMA index_list(message_queue)")}
    finally:
        con.close()
    if not cols:
        return None
    return ([c for c, _ in COLUMNS if c not in cols],
            [n for n, _ in INDEXES if n not in idx])


def detect(ctx) -> bool:
    st = _state(_db(ctx))
    return bool(st and (st[0] or st[1]))


def _count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM message_queue").fetchone()[0]
    finally:
        con.close()


def apply(ctx) -> None:
    db = _db(ctx)
    ctx._queue_rows_before = _count(db)
    con = sqlite3.connect(db)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(message_queue)")}
        for name, decl in COLUMNS:
            if name not in have:
                con.execute(f"ALTER TABLE message_queue ADD COLUMN {name} {decl}")
        for _, ddl in INDEXES:
            con.execute(ddl)
        con.commit()
    finally:
        con.close()


def verify(ctx) -> None:
    db = _db(ctx)
    st = _state(db)
    if st and (st[0] or st[1]):
        raise RuntimeError(f"message_queue still lacks: columns={st[0]} indexes={st[1]}")
    before = getattr(ctx, "_queue_rows_before", None)
    if before is not None and _count(db) != before:
        raise RuntimeError("message_queue row count changed during 20_queue")


STEP = Step("20_queue", 1, 2, detect=detect, apply=apply, verify=verify)
