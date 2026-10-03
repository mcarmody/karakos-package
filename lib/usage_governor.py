"""Weekly-usage governor for machine-started work (spec 2.7).

Machine-started work (heartbeats, scheduled pokes, queued builds) yields when
the account's seven-day usage is high. Human messages never reach `decide`.
Every failure mode (unreadable reading, broken policy) fails open.

stdlib only (PyYAML for the policy file, as the registry already requires it).
"""
import fnmatch
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import msgqueue
import rate_limits

_log = logging.getLogger("usage_governor")

DEFAULT_THRESHOLD = 80
DEFAULT_JOBS = {"heartbeat": 60}
DEFAULT_NEVER = ["scheduler", "health-monitor", "wedge-check", "monitor",
                 "build-queue-human"]
NON_GOVERNED_CHANNELS = ("hive", "call", "handoff")
DECISION_LOG_DEDUP_S = 60


@dataclass
class Policy:
    enabled: bool = True
    default: float = DEFAULT_THRESHOLD
    jobs: dict = field(default_factory=lambda: dict(DEFAULT_JOBS))
    never: List[str] = field(default_factory=lambda: list(DEFAULT_NEVER))
    resume_margin: float = 5
    max_defer_hours: float = 12
    broken: Optional[str] = None  # reason, when the file was unusable

    @classmethod
    def broken_policy(cls, reason: str) -> "Policy":
        """Fully open: every job is never-gated."""
        return cls(never=["*"], broken=reason)

    @classmethod
    def load(cls, path) -> "Policy":
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            import yaml
            data = yaml.safe_load(path.read_text())
            if data is None:
                return cls()
            if not isinstance(data, dict):
                raise ValueError("governor.yaml must be a mapping")
            p = cls()
            if "enabled" in data:
                if not isinstance(data["enabled"], bool):
                    raise ValueError("'enabled' must be true or false")
                p.enabled = data["enabled"]
            for key in ("default", "resume_margin", "max_defer_hours"):
                if key in data:
                    v = data[key]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                        raise ValueError(f"'{key}' must be a non-negative number")
                    setattr(p, key, v)
            if "jobs" in data:
                jobs = data["jobs"]
                if not isinstance(jobs, dict) or not all(
                        isinstance(k, str) and isinstance(v, (int, float))
                        and not isinstance(v, bool) for k, v in jobs.items()):
                    raise ValueError("'jobs' must map job globs to numbers")
                p.jobs = dict(jobs)
            if "never" in data:
                nv = data["never"]
                if not isinstance(nv, list) or not all(isinstance(x, str) for x in nv):
                    raise ValueError("'never' must be a list of globs")
                p.never = list(nv)
            return p
        except Exception as e:  # noqa: BLE001 — fail fully open
            reason = f"{type(e).__name__}: {e}"
            _warn_once(f"{path}: {reason}")
            return cls.broken_policy(reason)


_warned = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        _log.warning(f"governor policy unusable, failing open: {msg}")


@dataclass(frozen=True)
class Decision:
    job: str
    pct: Optional[float]
    threshold: Optional[float]
    decision: str  # "run" | "defer"
    reason: str


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def job_name(row) -> str:
    """The poke `--source` label (the row's author). A build-queue claim is
    named `build-queue:<repo>` by 3.3, which puts that name in `author`."""
    return str(_get(row, "author", "") or "")


def is_machine_started(row) -> bool:
    return (bool(_get(row, "is_bot", 0))
            and _get(row, "server", "") == "local"
            and _get(row, "channel", "") not in NON_GOVERNED_CHANNELS
            and not _get(row, "call_id"))


def _matches(job: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(job, g) for g in globs)


def threshold_for(job: str, policy: Policy) -> float:
    for glob, value in policy.jobs.items():
        if fnmatch.fnmatchcase(job, glob):
            return value
    return policy.default


def is_governed(job: str, policy: Policy) -> bool:
    return policy.enabled and not _matches(job, policy.never)


def decide(job, pct, policy, deferred_before=False) -> Decision:
    if _matches(job, policy.never):
        return Decision(job, pct, None, "run", "never-gated")
    if not policy.enabled:
        return Decision(job, pct, None, "run", "disabled")
    if pct is None:
        return Decision(job, pct, None, "run", "fail-open: usage unreadable")
    threshold = threshold_for(job, policy)
    if deferred_before:
        if pct < threshold - policy.resume_margin:
            return Decision(job, pct, threshold, "run", "below resume threshold")
        return Decision(job, pct, threshold, "defer", "above resume threshold")
    if pct >= threshold:
        return Decision(job, pct, threshold, "defer", "weekly usage at or over threshold")
    return Decision(job, pct, threshold, "run", "below threshold")


_last_logged: dict = {}


def log_decision(path, decision: Decision, shard: str, now=None) -> None:
    """Append one JSON line; an unchanged decision for the same job is logged at
    most once a minute. Never raises."""
    now = time.time() if now is None else now
    try:
        key = decision.job
        prev = _last_logged.get(key)
        if prev and prev[0] == (decision.decision, decision.reason) \
                and 0 <= now - prev[1] < DECISION_LOG_DEDUP_S:
            return
        _last_logged[key] = ((decision.decision, decision.reason), now)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({
                "ts": now, "job": decision.job, "shard": shard, "pct": decision.pct,
                "threshold": decision.threshold, "decision": decision.decision,
                "reason": decision.reason}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def weekly_pct_sync(db_path, now=None) -> Optional[float]:
    """The governor's reading for a process that is not the agent server: opens
    the database read-only. Any error is None."""
    now = time.time() if now is None else now
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM rate_limit_state").fetchall()
        finally:
            con.close()
        return rate_limits.weekly_utilization(rows, now)
    except Exception:  # noqa: BLE001
        return None


async def age_out(db, shard, policy, now=None) -> dict:
    """At a deferral: skip governed machine rows queued longer than
    `max_defer_hours` (`governor-expired`), and where several queued machine rows
    share an author keep the newest and skip the older (`superseded`)."""
    now = time.time() if now is None else now
    rows = await db.execute_fetchall(
        "SELECT id, author, is_bot, server, channel, call_id, created_at"
        " FROM message_queue WHERE agent = ? AND processed = ?"
        " ORDER BY created_at, id", (shard, msgqueue.STATUS_QUEUED))
    machine = [r for r in rows
               if is_machine_started(r) and is_governed(job_name(r), policy)]
    expired, superseded = [], []
    survivors = []
    cutoff = now - policy.max_defer_hours * 3600
    for r in machine:
        created = rate_limits._epoch(r["created_at"])
        if created is not None and created < cutoff:
            expired.append(r["id"])
        else:
            survivors.append(r)
    newest = {}
    for r in survivors:  # ascending, so the last per author wins
        newest[r["author"]] = r["id"]
    for r in survivors:
        if newest[r["author"]] != r["id"]:
            superseded.append(r["id"])
    for ids, why in ((expired, "governor-expired"), (superseded, "superseded")):
        if ids:
            await db.execute(
                "UPDATE message_queue SET processed = ?, response = ?,"
                " processed_at = CURRENT_TIMESTAMP"
                f" WHERE id IN ({','.join('?' * len(ids))}) AND processed = ?",
                (msgqueue.STATUS_SKIPPED, why, *ids, msgqueue.STATUS_QUEUED))
    if expired or superseded:
        await db.commit()
    return {"expired": len(expired), "superseded": len(superseded)}
