"""Build queue state (spec 3.3): pure functions over a sqlite3 connection.

Rows are keyed by `id` (`bq-<12 hex>`), never by shard. Stdlib only. Nothing
here signals a process: `recover` hands the rows it fails to a caller-supplied
cleanup callable.
"""
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

KINDS = ("build", "review")
STATUSES = ("queued", "running", "done", "failed", "cancelled")
ORIGINS = ("human", "machine")
ROLE_OF_KIND = {"build": "builder", "review": "reviewer"}
DEFER_EVENT_EVERY_S = 3600

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
URL_RE = re.compile(r"https://[^\s\"'<>)\]]+/pull/\d+")

DDL = (
    """CREATE TABLE IF NOT EXISTS build_queue (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
        repo TEXT, target_branch TEXT, brief TEXT NOT NULL, requester TEXT,
        callback_channel TEXT, origin TEXT NOT NULL DEFAULT 'human', source TEXT,
        priority INTEGER DEFAULT 0, host TEXT, exec_host TEXT,
        attempts INTEGER DEFAULT 0, source_ref TEXT UNIQUE, not_before INTEGER,
        run_ref TEXT, result TEXT, created_at TEXT, started_at TEXT, finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS build_queue_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id TEXT, ts TEXT, event TEXT,
        detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_build_queue_events_queue ON build_queue_events(queue_id)",
)


class BriefError(ValueError):
    """A brief that cannot be queued; the message is the reason shown to the requester."""


def init_schema(conn) -> None:
    """Create both tables (additive; used by the migrator step and by setup)."""
    for ddl in DDL:
        conn.execute(ddl)
    conn.commit()


def tables_present(conn) -> bool:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"build_queue", "build_queue_events"} <= have


def connect(path, timeout: float = 30.0):
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return conn


def iso(now) -> str:
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(now), timezone.utc).isoformat()


def _ts(now) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    return now.timestamp() if isinstance(now, datetime) else float(now)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def parse_frontmatter(text: str) -> dict:
    """Same semantics as the relay's reader: a `---` fenced block of `key: value`
    lines (not YAML)."""
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    out_lines, inside = [], False
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            inside = True
            continue
        if inside:
            if line.strip() == "---":
                break
            out_lines.append(line)
    meta = {}
    for line in out_lines:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def _bad_ref(v: str) -> bool:
    return (" " in v or v.startswith("-") or ".." in v or "\n" in v)


def validate_brief(kind, meta, registry, body: str = "") -> None:
    """Raise BriefError with a reason, else return None."""
    if kind not in KINDS:
        raise BriefError(f"unknown kind {kind!r}")
    repo = (meta.get("repo") or "").strip()
    branch = (meta.get("target_branch") or "").strip()
    if kind == "build":
        if not repo:
            raise BriefError("a build needs `repo: owner/name`")
        if not branch:
            raise BriefError("a build needs an explicit `target_branch`")
    elif not repo and not body.strip():
        raise BriefError("a review needs `repo` or a spec in the body")
    if repo and (_bad_ref(repo) or not REPO_RE.match(repo)):
        raise BriefError(f"invalid repo {repo!r}: expected owner/name")
    if branch and (_bad_ref(branch) or not BRANCH_RE.match(branch)):
        raise BriefError(f"invalid target_branch {branch!r}")
    prefix = (meta.get("branch_prefix") or "").strip()
    if prefix and (_bad_ref(prefix) or not PREFIX_RE.match(prefix)):
        raise BriefError(f"invalid branch_prefix {prefix!r}")
    for key in ("requester",):
        v = (meta.get(key) or "").strip()
        if v and not AGENT_RE.match(v):
            raise BriefError(f"invalid {key} {v!r}")
    role = ROLE_OF_KIND[kind]
    try:
        have = list(registry.by_role(role)) if registry is not None else []
    except Exception:  # noqa: BLE001
        have = []
    if not have:
        raise BriefError(f"no {role} agent in the registry")


def new_id() -> str:
    return "bq-" + secrets.token_hex(6)


def add_event(conn, queue_id, event, detail="", now=None) -> None:
    conn.execute("INSERT INTO build_queue_events(queue_id, ts, event, detail) VALUES (?,?,?,?)",
                 (queue_id, iso(now), event, detail))
    conn.commit()


def add_event_once(conn, queue_id, event, detail, now=None, every=DEFER_EVENT_EVERY_S) -> bool:
    """Log a deferral at most once per `every` seconds per row and reason (the event
    name is the reason: host-busy, governor, host-unreachable)."""
    ts = _ts(now)
    row = conn.execute(
        "SELECT ts FROM build_queue_events WHERE queue_id=? AND event=? "
        "ORDER BY id DESC LIMIT 1", (queue_id, event)).fetchone()
    if row:
        try:
            if ts - datetime.fromisoformat(row[0]).timestamp() < every:
                return False
        except ValueError:
            pass
    add_event(conn, queue_id, event, detail, now)
    return True


def enqueue(conn, kind, brief, repo=None, target_branch=None, requester=None,
            callback_channel=None, origin="human", source=None, priority=0, host=None,
            source_ref=None, now=None, registry=None, validate=True) -> str:
    if origin not in ORIGINS:
        raise BriefError(f"invalid origin {origin!r}")
    meta = parse_frontmatter(brief)
    meta.setdefault("repo", repo or "")
    meta.setdefault("target_branch", target_branch or "")
    if repo:
        meta["repo"] = repo
    if target_branch:
        meta["target_branch"] = target_branch
    if validate:
        validate_brief(kind, meta, registry, body=brief)
    if source_ref:
        dup = conn.execute("SELECT id FROM build_queue WHERE source_ref=?", (source_ref,)).fetchone()
        if dup:
            return dup[0]
    qid = new_id()
    try:
        conn.execute(
            "INSERT INTO build_queue(id, kind, status, repo, target_branch, brief, requester, "
            "callback_channel, origin, source, priority, host, source_ref, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, kind, "queued", meta.get("repo") or None, meta.get("target_branch") or None,
             brief, requester or meta.get("requester") or None,
             callback_channel or meta.get("callback_channel") or "general",
             origin, source, int(priority or 0), host or None, source_ref, iso(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        dup = conn.execute("SELECT id FROM build_queue WHERE source_ref=?", (source_ref,)).fetchone()
        if dup:
            return dup[0]
        raise
    add_event(conn, qid, "queued", f"origin={origin} kind={kind}", now)
    return qid


def resolve_host(row, default_host="local") -> str:
    return row["host"] or default_host


def claim_next(conn, caps, now, gate: Optional[Callable] = None, default_host="local"):
    """Claim the best claimable queued row, or None.

    `caps` maps host -> free slots. A candidate whose host is full is skipped, so
    a free host is never idle behind a full one. `gate(row) -> (ok, reason)` runs
    per candidate before the claim; a rejected row stays queued (its reason is an
    event, once an hour). The claim is a single guarded UPDATE, so two claimers
    can never take the same row."""
    ts = _ts(now)
    cands = conn.execute(
        "SELECT * FROM build_queue WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
        "ORDER BY priority DESC, created_at, id", (int(ts),)).fetchall()
    for row in cands:
        host = resolve_host(row, default_host)
        if caps.get(host, 0) <= 0:
            continue
        if gate is not None:
            ok, reason = gate(row)
            if not ok:
                name = (reason or "deferred").split(":")[0].strip() or "deferred"
                add_event_once(conn, row["id"], name, reason, now)
                continue
        cur = conn.execute(
            "UPDATE build_queue SET status='running', exec_host=?, attempts=attempts+1, "
            "started_at=?, reason=NULL WHERE id=? AND status='queued' RETURNING *",
            (host, iso(now), row["id"]))
        claimed = cur.fetchone()
        conn.commit()
        if claimed is not None:
            add_event(conn, claimed["id"], "running", f"host={host}", now)
            return claimed
    return None


def finish(conn, qid, status, reason, result, now=None) -> None:
    conn.execute("UPDATE build_queue SET status=?, reason=?, result=?, finished_at=? WHERE id=?",
                 (status, reason, json.dumps(result) if result is not None else None, iso(now), qid))
    conn.commit()
    add_event(conn, qid, status, reason or "", now)


def get(conn, qid):
    return conn.execute("SELECT * FROM build_queue WHERE id=?", (qid,)).fetchone()


def set_run_ref(conn, qid, ref) -> None:
    conn.execute("UPDATE build_queue SET run_ref=? WHERE id=?", (json.dumps(ref), qid))
    conn.commit()


def cancel(conn, qid, now=None):
    """-> previous status (None when unknown). A queued row is cancelled at once; a
    running row is marked cancelled and the dispatcher's poll stops the work."""
    row = get(conn, qid)
    if row is None:
        return None
    prev = row["status"]
    if prev in ("queued", "running"):
        conn.execute("UPDATE build_queue SET status='cancelled', reason='cancelled', finished_at=? "
                     "WHERE id=? AND status IN ('queued','running')", (iso(now), qid))
        conn.commit()
        add_event(conn, qid, "cancel-requested", f"was {prev}", now)
    return prev


def requeue(conn, qid, now=None) -> Optional[str]:
    """A NEW row copying the brief with attempts=0; the old id is never reused."""
    row = get(conn, qid)
    if row is None:
        return None
    new = new_id()
    conn.execute(
        "INSERT INTO build_queue(id, kind, status, repo, target_branch, brief, requester, "
        "callback_channel, origin, source, priority, host, created_at, attempts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (new, row["kind"], "queued", row["repo"], row["target_branch"], row["brief"],
         row["requester"], row["callback_channel"], row["origin"], row["source"],
         row["priority"], row["host"], iso(now)))
    conn.commit()
    add_event(conn, new, "queued", f"requeue of {qid}", now)
    return new


def list_rows(conn, status=None, limit=100):
    if status:
        return conn.execute("SELECT * FROM build_queue WHERE status=? ORDER BY created_at DESC, id "
                            "LIMIT ?", (status, int(limit))).fetchall()
    return conn.execute("SELECT * FROM build_queue ORDER BY created_at DESC, id LIMIT ?",
                        (int(limit),)).fetchall()


def events(conn, qid):
    return conn.execute("SELECT * FROM build_queue_events WHERE queue_id=? ORDER BY id",
                        (qid,)).fetchall()


def recover(conn, now=None, cleanup: Optional[Callable] = None, max_attempts=1):
    """Every `running` row at dispatcher start: failed `dispatcher-restart`, its
    run ref passed to `cleanup(row)`. Retried (re-queued in place) only when
    `max_attempts` allows more attempts than it has had; never by default, since
    re-running a build can open a second PR. -> ids recovered."""
    out = []
    for row in conn.execute("SELECT * FROM build_queue WHERE status='running'").fetchall():
        if cleanup is not None:
            try:
                cleanup(row)
            except Exception as e:  # noqa: BLE001
                add_event(conn, row["id"], "cleanup-error", repr(e), now)
        if (row["attempts"] or 0) < int(max_attempts):
            conn.execute("UPDATE build_queue SET status='queued', reason='dispatcher-restart', "
                         "run_ref=NULL WHERE id=?", (row["id"],))
            conn.commit()
            add_event(conn, row["id"], "requeued", "dispatcher-restart", now)
        else:
            finish(conn, row["id"], "failed", "dispatcher-restart", None, now)
        out.append(row["id"])
    return out


def verify_outcome(kind, exit_code, result_text, report=None):
    """-> (status, reason). A build is done only when it exited 0 AND a PR URL is
    present (the runner's report line, else a URL in the final result text)."""
    if exit_code == "timeout":
        return "failed", "timeout"
    if exit_code != 0:
        return "failed", f"exit-{exit_code}"
    if kind == "review":
        return ("done", "") if (result_text or "").strip() else ("failed", "empty-review")
    pr = (report or {}).get("pr_url") or ""
    if not pr:
        m = URL_RE.search(result_text or "")
        pr = m.group(0) if m else ""
    return ("done", "") if pr else ("failed", "no-pr")


def pr_url_of(result_text, report=None) -> str:
    pr = (report or {}).get("pr_url") or ""
    if not pr:
        m = URL_RE.search(result_text or "")
        pr = m.group(0) if m else ""
    return pr


def main(argv=None) -> int:
    """`python3 lib/buildq.py init --db PATH`: create the schema (setup uses this)."""
    import argparse
    ap = argparse.ArgumentParser(prog="buildq.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = connect(args.db)
    try:
        init_schema(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
