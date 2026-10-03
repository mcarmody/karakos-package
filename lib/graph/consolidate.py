"""Nightly graph consolidation: episodes from messages, decay, archive, dedup,
entity upkeep, embedding backfill.

`run(store, messages_dir=...)` runs the passes in order. Each pass is
independent: one that raises is recorded under stats["errors"] and the rest
still run. Normal runs commit per pass; a dry run does everything inside one
transaction that is rolled back and never calls a model.
"""
import hashlib
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from lib.graph import embed as _embed
from lib.graph.store import norm

_log = logging.getLogger("graph-consolidate")

EMBED_BATCH = 50
META_KEY = "last_consolidation"

SCORE_PROMPT = """Score the importance of this conversation excerpt on a scale of 1-10.

Consider:
- 9-10: Major decisions, critical events, important personal information
- 7-8: Meaningful conversations, useful information, preferences
- 5-6: Normal interactions, routine tasks
- 3-4: Minor updates, simple acknowledgments
- 1-2: Trivial chatter, noise

Excerpt: {summary}

Respond with ONLY a number 1-10."""


def _cfg() -> dict:
    env = os.environ.get
    cutoff = float(env("MEMORY_CUTOFF", "6.0"))
    return {
        "decay_rate": float(env("MEMORY_DECAY_RATE", "0.25")),
        "cutoff": cutoff,
        "max_episodes": int(env("MEMORY_MAX_EPISODES", "15")),
        "grace_days": float(env("MEMORY_PRUNE_GRACE_DAYS", "7")),
        "retention_days": float(env("MEMORY_ARCHIVE_RETENTION_DAYS", "30")),
        "dedup_cosine": float(env("MEMORY_DEDUP_COSINE", "0.95")),
        "dedup_max": int(env("MEMORY_DEDUP_MAX_PER_RUN", "200")),
        "entity_stale_days": float(env("MEMORY_ENTITY_STALE_DAYS", "180")),
        "embed_max": int(env("MEMORY_EMBED_MAX_PER_RUN", "500")),
        "score_timeout": float(env("MEMORY_SCORE_TIMEOUT", "20")),
        "score_retry_timeout": float(env("MEMORY_SCORE_RETRY_TIMEOUT", "60")),
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    """ISO or `YYYY-MM-DD HH:MM:SS` -> aware UTC datetime, None if unusable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# -- scoring (ported unchanged from the 1.x script) ------------------------

def _haiku_once(prompt: str, timeout: float):
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku", "--max-turns", "1"],
            capture_output=True, text=True, timeout=timeout)
        return max(1.0, min(10.0, float(result.stdout.strip())))
    except Exception:
        return None


def haiku_score(summary: str, cfg=None, log=_log):
    """Haiku importance 1-10, one retry with a longer timeout; None on failure."""
    cfg = cfg or _cfg()
    prompt = SCORE_PROMPT.format(summary=summary)
    score = _haiku_once(prompt, cfg["score_timeout"])
    if score is not None:
        return score
    log.warning("Failed to score importance on first attempt, retrying with longer timeout")
    return _haiku_once(prompt, cfg["score_retry_timeout"])


# -- messages -> episodes --------------------------------------------------

def read_previous_day_messages(messages_dir, now, log=_log) -> list:
    date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    path = Path(messages_dir) / f"messages-{date_str}.jsonl"
    if not path.exists():
        log.info(f"No messages file for {date_str}")
        return []
    out = []
    with open(path, "r") as f:
        for line in f:
            try:
                out.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    log.info(f"Read {len(out)} messages from {date_str}")
    return out


def segment_messages_into_episodes(messages: list) -> list:
    """Group messages with gaps of at most 5 minutes."""
    episodes, current, last_ts = [], [], None
    for msg in messages:
        try:
            ts = datetime.fromisoformat(msg["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if last_ts and (ts - last_ts).total_seconds() > 300 and current:
            episodes.append(current)
            current = []
        current.append(msg)
        last_ts = ts
    if current:
        episodes.append(current)
    return episodes


def create_episode_summary(messages: list) -> str:
    texts = []
    for msg in messages[:10]:
        content = msg.get("content", "")
        if content and not msg.get("is_bot", False):
            texts.append(f"{msg.get('author_name', 'User')}: {content}")
    return " | ".join(texts)[:500]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pass_episodes(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    messages = read_previous_day_messages(messages_dir, now, log)
    segments = [s for s in segment_messages_into_episodes(messages) if s]
    drafts = []
    for seg in segments:
        summary = create_episode_summary(seg)
        if summary:
            drafts.append((seg, summary))
    skipped_empty = len(segments) - len(drafts)
    capped = 0
    if len(drafts) > cfg["max_episodes"]:
        # Keep the fullest conversations; chronological order is preserved and
        # the choice is deterministic, so a re-run selects the same ones.
        keep = sorted(sorted(range(len(drafts)), key=lambda i: (-len(drafts[i][1]), i))
                      [:cfg["max_episodes"]])
        capped = len(drafts) - len(keep)
        drafts = [drafts[i] for i in keep]
    created = skipped = 0
    ts_now = _iso(now)
    for seg, summary in drafts:
        channel = seg[0].get("channel_name", "unknown")
        created_at = seg[0].get("ts") or ts_now
        digest = _digest(summary)
        with tx.write() as conn:
            dupe = any(_digest(r["content"]) == digest for r in conn.execute(
                "SELECT content FROM observations WHERE kind='episode' AND channel=? "
                "AND created_at=?", (channel, created_at)))
            if dupe:
                skipped += 1
                continue
        try:
            importance = score_fn(summary)
        except Exception as e:
            log.warning(f"score_fn raised: {e}")
            importance = None
        if importance is None:
            importance = cfg["cutoff"] + 1.0  # cutoff-safe: kept, not pruned
            if stats.get("_count_failures", True):
                stats["score_failures"] += 1
        importance = max(1.0, min(10.0, float(importance)))
        with tx.write() as conn:
            cur = conn.execute(
                "INSERT INTO observations(kind, content, importance, base_importance, "
                "channel, source, created_at, inserted_at, updated_at) "
                "VALUES ('episode', ?, ?, ?, ?, 'nightly', ?, ?, ?)",
                (summary, importance, importance, channel, created_at, ts_now, ts_now))
            names = {m.get("author_name") for m in seg
                     if m.get("is_bot") and m.get("author_name")}
            for name in sorted(names):
                eid, _ = store._entity(conn, name, "agent")
                conn.execute("INSERT OR IGNORE INTO observation_mentions"
                             "(observation_id, entity_id) VALUES (?,?)", (cur.lastrowid, eid))
        created += 1
    return {"created": created, "skipped_existing": skipped,
            "skipped_empty": skipped_empty, "over_cap": capped}


# -- decay / archive -------------------------------------------------------

def _pass_decay(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    """importance = base_importance - days/4 * rate, idempotent. Episodes only."""
    decayed = 0
    with tx.write() as conn:
        rows = conn.execute(
            "SELECT id, importance, base_importance, created_at FROM observations "
            "WHERE kind='episode' AND archived_at IS NULL").fetchall()
        for r in rows:
            created = _parse(r["created_at"])
            if created is None:
                continue
            base = r["base_importance"] if r["base_importance"] is not None else r["importance"]
            days = (now - created).total_seconds() / 86400
            new = max(0.0, base - (days / 4.0) * cfg["decay_rate"])
            if r["importance"] is None or abs(new - r["importance"]) > 1e-9:
                conn.execute("UPDATE observations SET importance=? WHERE id=?", (new, r["id"]))
                decayed += 1
    return {"decayed": decayed}


def _pass_prune(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    grace_cutoff = now - timedelta(days=cfg["grace_days"])
    keep_until = now - timedelta(days=cfg["retention_days"])
    archived = deleted = 0
    with tx.write() as conn:
        for r in conn.execute(
                "SELECT id, inserted_at FROM observations WHERE kind='episode' "
                "AND archived_at IS NULL AND importance < ?", (cfg["cutoff"],)).fetchall():
            ins = _parse(r["inserted_at"])
            if ins is None or ins > grace_cutoff:
                continue  # unknown age: leave it; inside grace: never touched
            conn.execute("UPDATE observations SET archived_at=? WHERE id=?",
                         (_iso(now), r["id"]))
            archived += 1
        # Episodes only: this pass archived them. Any other kind with archived_at
        # set was archived by something else and is not this job's to delete.
        for r in conn.execute("SELECT id, archived_at FROM observations "
                              "WHERE kind='episode' AND archived_at IS NOT NULL").fetchall():
            at = _parse(r["archived_at"])
            if at is None or at > keep_until:
                continue
            if conn.execute("SELECT 1 FROM observations WHERE superseded_by=? LIMIT 1",
                            (r["id"],)).fetchone():
                continue  # a merge keeper still referenced by its losers
            conn.execute("DELETE FROM observations WHERE id=?", (r["id"],))
            deleted += 1
    return {"archived": archived, "deleted": deleted}


# -- dedup -----------------------------------------------------------------

def _winner(a, b):
    """Higher importance wins; tie goes to the older (created_at, then id)."""
    ia, ib = a["importance"] or 0.0, b["importance"] or 0.0
    if ia != ib:
        return (a, b) if ia > ib else (b, a)
    ka, kb = (a["created_at"] or "", a["id"]), (b["created_at"] or "", b["id"])
    return (a, b) if ka <= kb else (b, a)


def _candidate_pairs(rows, threshold):
    """(i, j) index pairs, i < j, that look like duplicates, in a stable order."""
    pairs = set()
    by_text = {}
    for i, r in enumerate(rows):
        by_text.setdefault(norm(r["content"]), []).append(i)
    for idx in by_text.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                pairs.add((idx[x], idx[y]))
    vec_idx = []
    vecs = []
    for i, r in enumerate(rows):
        if r["embed_model"] != _embed.MODEL_NAME:
            continue
        v = _embed.decode(r["embedding"])
        if v is None:
            continue
        n = float(np.linalg.norm(v))
        if n <= 0.0:
            continue
        vec_idx.append(i)
        vecs.append(v / n)
    if len(vecs) > 1:
        m = np.vstack(vecs)
        for start in range(0, len(vecs), 512):
            sims = m[start:start + 512] @ m.T
            rr, cc = np.nonzero(sims >= threshold)
            for r_, c_ in zip(rr, cc):
                a, b = vec_idx[start + int(r_)], vec_idx[int(c_)]
                if a < b:
                    pairs.add((a, b))
    return sorted(pairs)


def _pass_dedup(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    merged = 0
    capped = False
    with tx.write() as conn:
        rows = conn.execute(
            "SELECT id, kind, entity_id, content, importance, base_importance, "
            "reinforcement_count, created_at, embedding, embed_model FROM observations "
            "WHERE archived_at IS NULL AND superseded_by IS NULL ORDER BY id").fetchall()
        groups = {}
        for r in rows:
            groups.setdefault((r["kind"], r["entity_id"]), []).append(r)
        for key in sorted(groups, key=lambda k: (k[0], -1 if k[1] is None else k[1])):
            grp = groups[key]
            if len(grp) < 2:
                continue
            state = {r["id"]: dict(r) for r in grp}
            gone = set()
            for i, j in _candidate_pairs(grp, cfg["dedup_cosine"]):
                a, b = grp[i]["id"], grp[j]["id"]
                if a in gone or b in gone:
                    continue
                if merged >= cfg["dedup_max"]:
                    capped = True
                    break
                win, lose = _winner(state[a], state[b])
                imp = max(win["importance"] or 0.0, lose["importance"] or 0.0)
                win["importance"] = imp
                win["reinforcement_count"] = (win["reinforcement_count"] or 1) + \
                    (lose["reinforcement_count"] or 1)
                if win["kind"] == "episode":
                    win["base_importance"] = max(win["base_importance"] or 0.0,
                                                 lose["base_importance"] or 0.0)
                conn.execute("UPDATE observations SET superseded_by=? WHERE id=?",
                             (win["id"], lose["id"]))
                conn.execute(
                    "UPDATE observations SET importance=?, base_importance=?, "
                    "reinforcement_count=?, updated_at=? WHERE id=?",
                    (win["importance"], win["base_importance"], win["reinforcement_count"],
                     _iso(now), win["id"]))
                gone.add(lose["id"])
                merged += 1
            if capped:
                break
    return {"merged": merged, "capped": capped}


# -- entity upkeep ---------------------------------------------------------

def _pass_entities(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    stale_before = now - timedelta(days=cfg["entity_stale_days"])
    archived = recomputed = 0
    with tx.write() as conn:
        for e in conn.execute("SELECT id, importance, last_seen_at, created_at FROM entities "
                              "WHERE archived_at IS NULL").fetchall():
            live = conn.execute(
                "SELECT MAX(importance) AS m, COUNT(*) AS n FROM observations "
                "WHERE entity_id=? AND archived_at IS NULL AND superseded_by IS NULL",
                (e["id"],)).fetchone()
            if live["n"]:
                new = live["m"] if live["m"] is not None else 5.0
                if e["importance"] is None or abs(new - e["importance"]) > 1e-9:
                    conn.execute("UPDATE entities SET importance=?, updated_at=? WHERE id=?",
                                 (new, _iso(now), e["id"]))
                    recomputed += 1
                continue
            if conn.execute("SELECT 1 FROM edges WHERE src_id=? OR dst_id=? LIMIT 1",
                            (e["id"], e["id"])).fetchone():
                continue
            seen = _parse(e["last_seen_at"]) or _parse(e["created_at"])
            if seen is not None and seen < stale_before:
                conn.execute("UPDATE entities SET archived_at=?, updated_at=? WHERE id=?",
                             (_iso(now), _iso(now), e["id"]))
                archived += 1
    return {"archived": archived, "recomputed": recomputed}


# -- embedding backfill ----------------------------------------------------

def _foreign(conn, table, limit):
    """Rows holding an embedding from another model or of the wrong length."""
    want = _embed.DIM * 4
    text = ("content" if table == "observations" else "name || '. ' || COALESCE(summary, '')")
    return [(table, r["id"], r["t"].strip()) for r in conn.execute(
        f"SELECT id, {text} AS t FROM {table} WHERE embedding IS NOT NULL "
        "AND archived_at IS NULL AND (embed_model IS NOT ? OR length(embedding) != ?) "
        "ORDER BY id LIMIT ?", (_embed.MODEL_NAME, want, limit))]


def _pass_embed(tx, store, now, messages_dir, score_fn, cfg, stats, log):
    cap = cfg["embed_max"]
    if stats["dry_run"]:
        with tx.write() as conn:
            missing = sum(conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE embedding IS NULL AND archived_at IS NULL"
            ).fetchone()[0] for t in ("observations", "entities"))
            foreign = sum(len(_foreign(conn, t, cap)) for t in ("observations", "entities"))
        return {"would_embed": min(cap, missing), "would_reembed": min(cap, foreign)}
    if not _embed.embedder_available():
        reason = "no embedding model available"
        log.info(f"embedding backfill skipped: {reason}")
        return {"embedded": 0, "reembedded": 0, "skipped": reason}
    out = {"embedded": 0, "reembedded": 0}

    def flush(items, key):
        texts = [t or " " for _, _, t in items]
        blobs = _embed.embed_texts(texts)
        if not blobs:
            return False
        for (table, rid, _), blob in zip(items, blobs):
            store.set_embedding(table, rid, blob)
        out[key] += len(items)
        return True

    done = 0
    while done < cap:
        batch = store.iter_unembedded(min(EMBED_BATCH, cap - done))
        if not batch:
            break
        if not flush(batch, "embedded"):
            out["skipped"] = "embedding model produced no vectors"
            log.info("embedding backfill stopped: model unavailable")
            return out
        done += len(batch)
    for table in ("observations", "entities"):
        while done < cap:
            with store.read() as conn:
                batch = _foreign(conn, table, min(EMBED_BATCH, cap - done))
            if not batch:
                break
            if not flush(batch, "reembedded"):
                out["skipped"] = "embedding model produced no vectors"
                return out
            done += len(batch)
    return out


# -- driver ----------------------------------------------------------------

class _DryTx:
    """One transaction for the whole run, rolled back at the end."""

    def __init__(self, store):
        self.conn = store.connect()
        self.conn.execute("BEGIN IMMEDIATE")
        self._n = 0

    @contextmanager
    def write(self):
        self._n += 1
        sp = f"dry{self._n}"
        self.conn.execute(f"SAVEPOINT {sp}")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {sp}")
            self.conn.execute(f"RELEASE {sp}")
            raise
        self.conn.execute(f"RELEASE {sp}")

    def finish(self):
        try:
            self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()


class _Tx:
    def __init__(self, store):
        self.write = store.write


PASSES = (("episodes", _pass_episodes), ("decay", _pass_decay), ("prune", _pass_prune),
          ("dedup", _pass_dedup), ("entities", _pass_entities), ("embed", _pass_embed))


def run(store, *, now=None, messages_dir, score_fn=None, log=None, dry_run=False) -> dict:
    """Run every pass; returns the stats dict (also stored in meta.last_consolidation).

    `score_fn(summary) -> float | None` replaces the Haiku scorer (None counts
    as a scoring failure). A dry run never calls a model and leaves the file
    untouched.
    """
    log = log or _log
    now = now or datetime.now(timezone.utc)
    cfg = _cfg()
    stats = {"started": _iso(now), "dry_run": bool(dry_run), "score_failures": 0,
             "errors": {}}
    if dry_run:
        stats["_count_failures"] = False
        score_fn = lambda summary: None  # noqa: E731  cutoff-safe score, no model
    elif score_fn is None:
        score_fn = lambda summary: haiku_score(summary, cfg, log)  # noqa: E731
    tx = _DryTx(store) if dry_run else _Tx(store)
    try:
        for name, fn in PASSES:
            try:
                stats[name] = fn(tx, store, now, messages_dir, score_fn, cfg, stats, log)
            except Exception as e:
                log.error(f"consolidation pass {name} failed: {e}")
                stats["errors"][name] = str(e)
        try:
            with tx.write() as conn:
                row = conn.execute("SELECT MAX(inserted_at) FROM observations "
                                   "WHERE kind='episode'").fetchone()
                stats["newest_inserted_at"] = row[0]
                stats["finished"] = _iso(datetime.now(timezone.utc))
                public = {k: v for k, v in stats.items() if not k.startswith("_")}
                if not dry_run:
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                 (META_KEY, json.dumps(public)))
        except Exception as e:
            log.error(f"could not record {META_KEY}: {e}")
            stats["errors"]["meta"] = str(e)
    finally:
        if dry_run:
            tx.finish()
    stats.pop("_count_failures", None)
    return stats
