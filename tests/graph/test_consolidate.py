"""Graph consolidation. The 1.x cases of tests/test_memory.py map to:
decay_reduces/idempotent/no-compounding -> test_decay_*; prune grace + same-run
survival + grace configurable + high score never pruned -> test_prune_*,
test_same_run_*; score failure cases -> test_scoring_failure_*; column
migration -> dropped (the graph schema owns its columns)."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.graph import consolidate as C
from lib.graph import embed
from tests.graph.helpers import (FakeTextEmbedding, _fresh_embedder, blob,  # noqa: F401
                                 install_fastembed, store)

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("MEMORY_DECAY_RATE", "MEMORY_CUTOFF", "MEMORY_MAX_EPISODES", "MEMORY_PRUNE_GRACE_DAYS",
              "MEMORY_ARCHIVE_RETENTION_DAYS", "MEMORY_DEDUP_COSINE", "MEMORY_DEDUP_MAX_PER_RUN",
              "MEMORY_ENTITY_STALE_DAYS", "MEMORY_EMBED_MAX_PER_RUN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")  # no model unless a test opts in


def go(store, tmp_path, **kw):
    kw.setdefault("score_fn", lambda s: 5.0)
    kw.setdefault("now", NOW)
    return C.run(store, messages_dir=tmp_path / "messages", **kw)


def add_episode(store, content="ep", importance=5.0, base=None, age_days=0.0, inserted_days=None,
                channel="c", now=NOW, **extra):
    created = iso(now - timedelta(days=age_days))
    ins = iso(now - timedelta(days=age_days if inserted_days is None else inserted_days))
    with store.write() as conn:
        cur = conn.execute(
            "INSERT INTO observations(kind, content, importance, base_importance, channel, source,"
            " created_at, inserted_at, updated_at) VALUES ('episode',?,?,?,?, 'nightly',?,?,?)",
            (content, importance, importance if base is None else base, channel, created, ins, ins))
        return cur.lastrowid


def row(store, oid):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM observations WHERE id=?", (oid,)).fetchone())


def write_messages(tmp_path, msgs, day=None):
    d = tmp_path / "messages"
    d.mkdir(exist_ok=True)
    day = day or (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    (d / f"messages-{day}.jsonl").write_text("\n".join(json.dumps(m) for m in msgs) + "\n")


def msg(minute, text, author="ann", bot=False, channel="general", hour=10):
    return {"ts": f"2026-10-02T{hour:02d}:{minute:02d}:00Z", "author_name": author,
            "content": text, "is_bot": bot, "channel_name": channel}


# -- episodes ---------------------------------------------------------------

def test_episodes_created_with_sources_and_agent_mentions(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello"), msg(1, "reply", author="robo", bot=True),
                              msg(30, "later thing")])
    stats = go(store, tmp_path, score_fn=lambda s: 8.0)
    assert stats["episodes"]["created"] == 2
    with store.read() as conn:
        rows = conn.execute("SELECT * FROM observations WHERE kind='episode' "
                            "ORDER BY created_at").fetchall()
        assert [r["source"] for r in rows] == ["nightly", "nightly"]
        assert rows[0]["content"] == "ann: hello" and rows[0]["importance"] == pytest.approx(8.0, abs=0.1)
        assert rows[0]["base_importance"] == 8.0 and rows[0]["channel"] == "general"
        ents = conn.execute("SELECT e.name, e.kind FROM observation_mentions m JOIN entities e "
                            "ON e.id=m.entity_id WHERE m.observation_id=?", (rows[0]["id"],)).fetchall()
        assert [(e["name"], e["kind"]) for e in ents] == [("robo", "agent")]


def test_episode_creation_is_idempotent_on_rerun(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello"), msg(30, "again")])
    assert go(store, tmp_path)["episodes"]["created"] == 2
    again = go(store, tmp_path)
    assert again["episodes"]["created"] == 0 and again["episodes"]["skipped_existing"] == 2
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_idempotence_key_includes_content_hash(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello")])
    go(store, tmp_path)
    write_messages(tmp_path, [msg(0, "different words")])
    assert go(store, tmp_path)["episodes"]["created"] == 1


def test_per_day_cap_at_creation(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_MAX_EPISODES", "3")
    write_messages(tmp_path, [msg(0, f"m{h}", hour=h) for h in range(8)])
    stats = go(store, tmp_path)
    assert stats["episodes"]["created"] == 3 and stats["episodes"]["over_cap"] == 5
    assert go(store, tmp_path)["episodes"]["created"] == 0  # same selection on re-run


def test_scoring_failure_keeps_episode_above_cutoff(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello"), msg(30, "again")])
    stats = go(store, tmp_path, score_fn=lambda s: None)
    assert stats["score_failures"] == 2
    with store.read() as conn:
        rows = conn.execute("SELECT importance, base_importance FROM observations").fetchall()
    assert [r["base_importance"] for r in rows] == [7.0, 7.0]  # cutoff + 1
    assert all(r["importance"] >= 6.0 for r in rows)  # nightly decay on a day-old row is tiny


def test_scoring_exception_counts_as_failure(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello")])

    def boom(s):
        raise RuntimeError("model down")
    stats = go(store, tmp_path, score_fn=boom)
    assert stats["score_failures"] == 1 and stats["episodes"]["created"] == 1


def test_haiku_scorer_retries_once_then_gives_up(monkeypatch):
    calls = []

    def once(prompt, timeout):
        calls.append(timeout)
        return None
    monkeypatch.setattr(C, "_haiku_once", once)
    assert C.haiku_score("x") is None
    assert calls == [20.0, 60.0]
    monkeypatch.setattr(C, "_haiku_once", lambda p, t: 6.5)
    assert C.haiku_score("x") == 6.5


# -- decay ------------------------------------------------------------------

def test_decay_reduces_from_base_and_is_idempotent(store, tmp_path):
    a = add_episode(store, "old", importance=8.0, age_days=16)
    fact = store.add_observation("a fact", kind="fact", importance=9.0, embed=False)[0]
    first = go(store, tmp_path)
    assert row(store, a)["importance"] == pytest.approx(8.0 - 16 / 4 * 0.25)
    assert first["decay"]["decayed"] == 1
    again = go(store, tmp_path)
    assert again["decay"]["decayed"] == 0
    assert row(store, a)["importance"] == pytest.approx(7.0)
    assert row(store, fact)["importance"] == 9.0  # facts and patterns are not decayed


def test_decay_does_not_compound_across_nightly_runs(store, tmp_path):
    a = add_episode(store, "old", importance=9.0, age_days=8)
    for n in range(5):
        go(store, tmp_path, now=NOW + timedelta(days=n * 0.0))
    assert row(store, a)["importance"] == pytest.approx(9.0 - 8 / 4 * 0.25)


def test_decay_rate_env(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DECAY_RATE", "1.0")
    a = add_episode(store, "old", importance=8.0, age_days=4)
    go(store, tmp_path)
    assert row(store, a)["importance"] == pytest.approx(7.0)


# -- prune / archive ---------------------------------------------------------

def test_fresh_low_score_episode_survives_in_grace_window(store, tmp_path):
    a = add_episode(store, "fresh", importance=2.0, age_days=0.5)
    go(store, tmp_path)
    assert row(store, a)["archived_at"] is None


def test_old_low_score_episode_is_archived_not_deleted(store, tmp_path):
    a = add_episode(store, "old", importance=2.0, age_days=10)
    stats = go(store, tmp_path)
    assert stats["prune"]["archived"] == 1 and stats["prune"]["deleted"] == 0
    assert row(store, a)["archived_at"] is not None


def test_same_run_episode_survives(store, tmp_path):
    write_messages(tmp_path, [msg(0, "routine task")])
    go(store, tmp_path, score_fn=lambda s: 3.0)
    with store.read() as conn:
        assert conn.execute("SELECT archived_at FROM observations").fetchone()[0] is None


def test_grace_period_is_from_inserted_at_and_configurable(store, tmp_path, monkeypatch):
    # created long ago but inserted yesterday: inside grace
    a = add_episode(store, "backfill", importance=1.0, age_days=60, inserted_days=1)
    go(store, tmp_path)
    assert row(store, a)["archived_at"] is None
    monkeypatch.setenv("MEMORY_PRUNE_GRACE_DAYS", "0.5")
    go(store, tmp_path)
    assert row(store, a)["archived_at"] is not None


def test_high_score_old_episode_never_archived(store, tmp_path):
    a = add_episode(store, "important", importance=9.9, base=9.9, age_days=20)
    go(store, tmp_path)
    assert row(store, a)["archived_at"] is None


def test_archive_then_retention_delete(store, tmp_path):
    a = add_episode(store, "old", importance=2.0, age_days=10)
    go(store, tmp_path)
    assert row(store, a)["archived_at"]
    go(store, tmp_path, now=NOW + timedelta(days=29))
    assert row(store, a)
    stats = go(store, tmp_path, now=NOW + timedelta(days=31))
    assert stats["prune"]["deleted"] == 1
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE id=?", (a,)).fetchone()[0] == 0



def test_retention_never_deletes_an_archived_fact(store, tmp_path):
    """Only episodes this job archived are hard-deleted; a fact archived by
    anything else stays."""
    f = store.add_observation("the boiler code is 4471", kind="fact", importance=5.0,
                              embed=False)[0]
    with store.write() as conn:
        conn.execute("UPDATE observations SET archived_at=? WHERE id=?",
                     ((NOW - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S+00:00"), f))
    stats = go(store, tmp_path, now=NOW + timedelta(days=31))
    assert stats["prune"]["deleted"] == 0
    assert row(store, f)

def test_recall_ignores_archived_rows(store, tmp_path):
    a = add_episode(store, "zebra stampede notes", importance=2.0, age_days=10)
    go(store, tmp_path)
    assert row(store, a)["archived_at"]
    res = store.recall("zebra stampede")
    assert "zebra" not in json.dumps(res, default=str)


# -- dedup -------------------------------------------------------------------

def fact(store, text, imp=5.0, entity=None, kind="fact", vec=None, count=1):
    oid = store.add_observation(text, kind=kind, entity=entity, importance=imp, embed=False)[0]
    with store.write() as conn:
        conn.execute("UPDATE observations SET reinforcement_count=? WHERE id=?", (count, oid))
        if vec is not None:
            conn.execute("UPDATE observations SET embedding=?, embed_model=? WHERE id=?",
                         (blob(*vec), embed.MODEL_NAME, oid))
    return oid


def insert_dup(store, text, **kw):
    """add_observation de-dupes exact text on write; make the dupe by hand."""
    oid = fact(store, text + "\x00", **kw)
    with store.write() as conn:
        conn.execute("UPDATE observations SET content=? WHERE id=?", (text, oid))
    return oid


def test_dedup_exact_merges_keeps_higher_importance_and_sums_reinforcement(store, tmp_path):
    a = fact(store, "Likes tea", imp=4.0, count=2)
    b = insert_dup(store, "likes  TEA", imp=7.0, count=3)
    stats = go(store, tmp_path)
    assert stats["dedup"]["merged"] == 1
    assert row(store, a)["superseded_by"] == b
    keeper = row(store, b)
    assert keeper["superseded_by"] is None and keeper["reinforcement_count"] == 5
    assert keeper["importance"] == 7.0


def test_dedup_tie_keeps_older(store, tmp_path):
    a = fact(store, "same words", imp=5.0)
    b = insert_dup(store, "same words", imp=5.0)
    go(store, tmp_path)
    assert row(store, b)["superseded_by"] == a and row(store, a)["superseded_by"] is None


def test_dedup_near_duplicates_by_cosine(store, tmp_path):
    a = fact(store, "the cat sat", imp=5.0, vec=(1.0, 0.0))
    b = fact(store, "a cat was sitting", imp=6.0, vec=(1.0, 0.05))
    c = fact(store, "unrelated", imp=5.0, vec=(0.0, 1.0))
    stats = go(store, tmp_path)
    assert stats["dedup"]["merged"] == 1
    assert row(store, a)["superseded_by"] == b and row(store, c)["superseded_by"] is None


def test_dedup_threshold_env(store, tmp_path, monkeypatch):
    fact(store, "one", vec=(1.0, 0.0))
    fact(store, "two", vec=(1.0, 0.5))  # cos ~0.894
    assert go(store, tmp_path)["dedup"]["merged"] == 0
    monkeypatch.setenv("MEMORY_DEDUP_COSINE", "0.85")
    assert go(store, tmp_path)["dedup"]["merged"] == 1


def test_dedup_never_crosses_kind_or_entity(store, tmp_path):
    fact(store, "same text", entity="alice")
    insert_dup(store, "same text", entity="bob")
    insert_dup(store, "same text", kind="pattern", entity="alice")
    fact(store, "vec", vec=(1.0, 0.0), entity="alice")
    fact(store, "vec2", vec=(1.0, 0.0), kind="pattern", entity="alice")
    assert go(store, tmp_path)["dedup"]["merged"] == 0


def test_dedup_exact_only_without_embeddings(store, tmp_path):
    fact(store, "alpha one")
    fact(store, "alpha two")
    assert go(store, tmp_path)["dedup"]["merged"] == 0


def test_dedup_ignores_foreign_embeddings(store, tmp_path):
    a = fact(store, "x", vec=(1.0, 0.0))
    b = fact(store, "y", vec=(1.0, 0.0))
    with store.write() as conn:
        conn.execute("UPDATE observations SET embed_model='other' WHERE id=?", (b,))
    assert go(store, tmp_path)["dedup"]["merged"] == 0


def test_dedup_honours_per_run_cap(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DEDUP_MAX_PER_RUN", "2")
    for i in range(4):
        fact(store, f"pair {i}")
        insert_dup(store, f"pair {i}")
    stats = go(store, tmp_path)
    assert stats["dedup"] == {"merged": 2, "capped": True}
    assert go(store, tmp_path)["dedup"]["merged"] == 2  # the rest on the next night


def test_dedup_leaves_archived_and_superseded_alone(store, tmp_path):
    a = fact(store, "dup text")
    b = insert_dup(store, "dup text")
    with store.write() as conn:
        conn.execute("UPDATE observations SET archived_at=? WHERE id=?", (iso(NOW), b))
    assert go(store, tmp_path)["dedup"]["merged"] == 0


# -- entities -----------------------------------------------------------------

def set_seen(store, eid, dt):
    with store.write() as conn:
        conn.execute("UPDATE entities SET last_seen_at=? WHERE id=?", (iso(dt), eid))


def test_entity_archive_and_importance_recompute(store, tmp_path):
    stale, _ = store.add_entity("ghost")
    kept_edge, _ = store.add_entity("linked")
    store.add_edge("linked", "other", "knows")
    live, _ = store.add_entity("busy")
    store.add_observation("busy fact", entity="busy", importance=3.0, embed=False)
    store.add_observation("busy fact two", entity="busy", importance=8.0, embed=False)
    old = NOW - timedelta(days=400)
    for e in (stale, kept_edge, live):
        set_seen(store, e, old)
    stats = go(store, tmp_path)
    assert stats["entities"]["archived"] == 1
    with store.read() as conn:
        by = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM entities")}
    assert by["ghost"]["archived_at"] and not by["linked"]["archived_at"]
    assert not by["busy"]["archived_at"] and by["busy"]["importance"] == 8.0


def test_recent_entity_is_not_archived(store, tmp_path):
    store.add_entity("fresh")
    assert go(store, tmp_path)["entities"]["archived"] == 0


# -- embedding backfill ---------------------------------------------------------

def test_backfill_respects_per_run_cap(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "1")
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    for i in range(7):
        store.add_observation(f"obs {i}", embed=False)
    monkeypatch.setenv("MEMORY_EMBED_MAX_PER_RUN", "5")
    stats = go(store, tmp_path)
    assert stats["embed"]["embedded"] == 5
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE embedding IS NULL").fetchone()[0] == 2
    assert go(store, tmp_path)["embed"]["embedded"] == 2


def test_backfill_covers_entities(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "1")
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    store.add_entity("thing one")
    assert go(store, tmp_path)["embed"]["embedded"] == 1


def test_backfill_skips_quietly_without_a_model(store, tmp_path, monkeypatch):
    # Force "no model" whatever the host has installed (CI and dev hosts carry one).
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")
    embed._reset()
    store.add_observation("no model here", embed=False)
    stats = go(store, tmp_path)
    assert stats["embed"]["embedded"] == 0 and "no embedding model" in stats["embed"]["skipped"]
    assert not stats["errors"]


def test_backfill_reembeds_foreign_length_and_model(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "1")
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    short = store.add_observation("short blob", embed=False)[0]
    other = store.add_observation("other model", embed=False)[0]
    good = store.add_observation("good", embed=False)[0]
    with store.write() as conn:
        conn.execute("UPDATE observations SET embedding=?, embed_model=? WHERE id=?",
                     (b"\x00" * 16, embed.MODEL_NAME, short))
        conn.execute("UPDATE observations SET embedding=?, embed_model='foo' WHERE id=?",
                     (blob(1.0), other))
        conn.execute("UPDATE observations SET embedding=?, embed_model=? WHERE id=?",
                     (blob(1.0), embed.MODEL_NAME, good))
    stats = go(store, tmp_path)
    assert stats["embed"]["reembedded"] == 2
    with store.read() as conn:
        for oid in (short, other):
            r = conn.execute("SELECT length(embedding) n, embed_model m FROM observations WHERE id=?",
                             (oid,)).fetchone()
            assert (r["n"], r["m"]) == (embed.DIM * 4, embed.MODEL_NAME)


# -- meta, dry run, isolation ------------------------------------------------------

def test_meta_last_consolidation_written(store, tmp_path):
    write_messages(tmp_path, [msg(0, "hello")])
    stats = go(store, tmp_path)
    with store.read() as conn:
        meta = json.loads(conn.execute("SELECT value FROM meta WHERE key='last_consolidation'")
                          .fetchone()[0])
    assert meta["started"] == "2026-10-03T03:00:00Z" and meta["finished"]
    assert meta["episodes"]["created"] == 1 and meta["newest_inserted_at"] == stats["newest_inserted_at"]
    assert meta["score_failures"] == 0


def test_dry_run_leaves_file_byte_identical_and_calls_no_model(store, tmp_path, monkeypatch):
    write_messages(tmp_path, [msg(0, "hello")])
    add_episode(store, "old", importance=2.0, age_days=10)
    fact(store, "dup text")
    insert_dup(store, "dup text")
    store.add_entity("ghost")
    set_seen(store, 1, NOW - timedelta(days=400))
    monkeypatch.setattr(C, "_haiku_once", lambda *a: pytest.fail("model called in dry run"))
    before = store.path.read_bytes()
    stats = C.run(store, now=NOW, messages_dir=tmp_path / "messages", dry_run=True)
    assert store.path.read_bytes() == before
    assert stats["dry_run"] is True and stats["score_failures"] == 0
    assert stats["episodes"]["created"] == 1 and stats["prune"]["archived"] == 1
    assert stats["dedup"]["merged"] == 1 and "would_embed" in stats["embed"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key='last_consolidation'").fetchone()[0] == 0


def test_one_failing_pass_does_not_stop_the_others(store, tmp_path, monkeypatch):
    a = add_episode(store, "old", importance=8.0, age_days=16)

    def boom(*a, **k):
        raise RuntimeError("decay exploded")
    monkeypatch.setattr(C, "PASSES", tuple(
        (n, boom if n == "decay" else f) for n, f in C.PASSES))
    write_messages(tmp_path, [msg(0, "hello")])
    stats = go(store, tmp_path)
    assert stats["errors"] == {"decay": "decay exploded"}
    assert stats["episodes"]["created"] == 1 and "prune" in stats and "dedup" in stats
    assert "entities" in stats and "embed" in stats


def test_failed_pass_rolls_back_its_own_writes(store, tmp_path, monkeypatch):
    def half(tx, *a, **k):
        with tx.write() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES ('half', '1')")
            raise RuntimeError("late failure")
    monkeypatch.setattr(C, "PASSES", (("decay", half),) + C.PASSES[2:])
    stats = go(store, tmp_path)
    assert "decay" in stats["errors"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key='half'").fetchone()[0] == 0
