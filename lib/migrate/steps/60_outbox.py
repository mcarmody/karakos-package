"""Import the 1.x Discord dead-letter file into the outbox as `dead` rows (spec 6.1).

Boot never imports it; this step does. Each valid JSON line becomes a `dead`
row (reason `legacy dead letter: <reason>`); malformed lines are skipped and
counted. The file is then renamed to `discord-dead-letter.jsonl.migrated`, kept,
never deleted, so a second run finds nothing to do.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from lib import outbox
from lib.migrate.runner import Step

LEGACY = "discord-dead-letter.jsonl"


def _file(ctx) -> Path:
    return Path(ctx.data_dir) / LEGACY


def _db(ctx) -> Path:
    return Path(ctx.data_dir) / "outbox" / "outbox.db"


def _parse(path: Path):
    """(valid records, malformed line count)."""
    good, bad = [], 0
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict) or not rec.get("channel_id") or not isinstance(rec.get("content"), str):
            bad += 1
            continue
        good.append(rec)
    return good, bad


def _ts(rec) -> float:
    try:
        return datetime.fromisoformat(str(rec.get("ts"))).timestamp()
    except ValueError:
        return 0.0


def detect(ctx) -> bool:
    f = _file(ctx)
    try:
        return f.is_file() and f.stat().st_size > 0
    except OSError:
        return False


def plan(ctx):
    good, bad = _parse(_file(ctx))
    return [f"60_outbox: import {len(good)} dead-letter record(s) into the outbox; "
            f"{bad} malformed line(s) skipped; file kept as {LEGACY}.migrated"]


def apply(ctx) -> None:
    good, bad = _parse(_file(ctx))
    ctx._outbox_valid = len(good)
    conn = outbox.open_store(_db(ctx))
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A run interrupted between the insert and the rename must not import twice.
        seen = {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT channel_id, content_sha, created_at FROM outbox WHERE dead_reason LIKE 'legacy%'")}
        for rec in good:
            created = _ts(rec)
            key = (str(rec["channel_id"]), outbox.content_sha(rec["content"]), created)
            if key in seen:
                continue
            seen.add(key)
            outbox.insert_dead(conn, str(rec.get("agent") or ""), rec["channel_id"], rec["content"],
                               f"legacy dead letter: {rec.get('reason') or 'unknown'}", created,
                               int(rec.get("attempts") or 0))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    f = _file(ctx)
    f.rename(f.with_name(LEGACY + ".migrated"))
    ctx.report["Outbox"] = [f"- imported {len(good)} dead-letter record(s); {bad} malformed line(s) skipped"]


def verify(ctx) -> None:
    conn = sqlite3.connect(str(_db(ctx)))
    try:
        n = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead' "
                         "AND dead_reason LIKE 'legacy%'").fetchone()[0]
    finally:
        conn.close()
    want = getattr(ctx, "_outbox_valid", None)
    if want is not None and n != want:
        raise RuntimeError(f"outbox has {n} legacy dead rows, expected {want}")
    if _file(ctx).exists():
        raise RuntimeError("dead-letter file was not renamed")


STEP = Step("60_outbox", 1, 2, detect=detect, apply=apply, verify=verify, plan=plan)
