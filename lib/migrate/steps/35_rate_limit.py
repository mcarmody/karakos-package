"""rate_limit: re-key rate_limit_state by rate_limit_type (spec 2.7).

A rate limit is an account fact, so the table that was one row per agent
becomes one row per window type. Old per-agent rows for a type collapse into
one (newest `updated_at` wins, ties by larger `resets_at`); the agent column is
dropped on purpose. Boot never alters 1.x data; this step does. Idempotent:
applies only when the table exists and its primary key is not
`rate_limit_type`. Fresh installs get the final shape from the server's CREATE.
"""
import sqlite3
from pathlib import Path

from lib.migrate.runner import Step

NEW_DDL = """CREATE TABLE rate_limit_state_new (
    rate_limit_type TEXT PRIMARY KEY,
    status TEXT,
    resets_at INTEGER,
    overage_status TEXT,
    is_using_overage INTEGER DEFAULT 0,
    utilization REAL,
    alerted_for_resets_at INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""


def _db(ctx) -> Path:
    data = Path(ctx.data_dir)
    for p in (data / "memory" / "agent-server.db", data / "agent-server.db"):
        if p.is_file():
            return p
    return data / "memory" / "agent-server.db"


def _pk(con):
    return [r[1] for r in con.execute("PRAGMA table_info(rate_limit_state)") if r[5]]


def _needs(db: Path) -> bool:
    if not db.is_file():
        return False
    con = sqlite3.connect(db)
    try:
        pk = _pk(con)
    finally:
        con.close()
    return bool(pk) and pk != ["rate_limit_type"]


def detect(ctx) -> bool:
    return _needs(_db(ctx))


def _norm(t):
    t = (t or "").strip() if isinstance(t, str) else ""
    return t or "unknown"


def _num(v):
    return v if isinstance(v, (int, float)) else -1


def apply(ctx) -> None:
    con = sqlite3.connect(_db(ctx), isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = con.execute("SELECT * FROM rate_limit_state").fetchall()
            groups = {}
            for r in rows:
                groups.setdefault(_norm(r["rate_limit_type"]), []).append(r)
            keep = {}
            for t, rs in groups.items():
                keep[t] = max(rs, key=lambda r: (str(r["updated_at"] or ""),
                                                 _num(r["resets_at"])))
            ctx.rate_limit_expected = {}
            con.execute("DROP TABLE IF EXISTS rate_limit_state_new")
            con.execute(NEW_DDL)
            for t, k in keep.items():
                alerts = [r["alerted_for_resets_at"] for r in groups[t]
                          if r["resets_at"] == k["resets_at"]
                          and r["alerted_for_resets_at"] is not None]
                alerted = max(alerts) if alerts else None
                con.execute(
                    "INSERT INTO rate_limit_state_new (rate_limit_type, status,"
                    " resets_at, overage_status, is_using_overage, utilization,"
                    " alerted_for_resets_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (t, k["status"], k["resets_at"], k["overage_status"],
                     k["is_using_overage"] or 0, alerted, k["updated_at"]))
                ctx.rate_limit_expected[t] = (k["resets_at"], alerted)
            con.execute("DROP TABLE rate_limit_state")
            con.execute("ALTER TABLE rate_limit_state_new RENAME TO rate_limit_state")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def verify(ctx) -> None:
    db = _db(ctx)
    con = sqlite3.connect(db)
    try:
        if _pk(con) != ["rate_limit_type"]:
            raise RuntimeError("rate_limit_state primary key is not rate_limit_type")
        rows = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT rate_limit_type, resets_at, alerted_for_resets_at"
            " FROM rate_limit_state")}
    finally:
        con.close()
    expected = getattr(ctx, "rate_limit_expected", None)
    if expected is not None:
        if len(rows) != len(expected):
            raise RuntimeError(
                f"rate_limit_state has {len(rows)} rows, expected {len(expected)}")
        for t, (resets, alerted) in expected.items():
            if rows.get(t) != (resets, alerted):
                raise RuntimeError(f"rate_limit_state row {t!r} differs from the kept row")
    if _needs(db):
        raise RuntimeError("rate_limit_state still needs re-keying")


STEP = Step("35_rate_limit", 1, 2, detect=detect, apply=apply, verify=verify)
