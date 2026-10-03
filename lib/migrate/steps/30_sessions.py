"""sessions: add context_tokens / context_updated_at (spec 1.5).

Boot never alters 1.x data; this step does. Idempotent: it applies only when
a sessions table exists and lacks either column, so a second run is a no-op.
Fresh installs get the columns from the server's CREATE TABLE.
"""
import sqlite3
from pathlib import Path

from lib.migrate.runner import Step

COLUMNS = (("context_tokens", "INTEGER DEFAULT 0"),
           ("context_updated_at", "TIMESTAMP"))


def _db(ctx) -> Path:
    data = Path(ctx.data_dir)
    for p in (data / "memory" / "agent-server.db", data / "agent-server.db"):
        if p.is_file():
            return p
    return data / "memory" / "agent-server.db"


def _missing(db: Path):
    """Columns still to add; None when there is no sessions table."""
    if not db.is_file():
        return None
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    finally:
        con.close()
    if not cols:
        return None
    return [c for c, _ in COLUMNS if c not in cols]


def detect(ctx) -> bool:
    return bool(_missing(_db(ctx)))


def apply(ctx) -> None:
    db = _db(ctx)
    con = sqlite3.connect(db)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        for name, decl in COLUMNS:
            if name not in have:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
        con.commit()
    finally:
        con.close()


def verify(ctx) -> None:
    left = _missing(_db(ctx))
    if left:
        raise RuntimeError(f"sessions still lacks columns: {left}")


STEP = Step("30_sessions", 1, 2, detect=detect, apply=apply, verify=verify)
