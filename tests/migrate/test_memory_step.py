"""Migrator step 40_memory (spec 4.4): backup, dry run, apply, verify, cutover."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from _memory_helpers import (STEP, fx, graph, install, make_ctx, mem, migrate,  # noqa: F401
                             migration_meta, tree_hash, _env, fake_embedder)
from lib.migrate import backup as backup_mod, guard


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_dry_run_writes_nothing_and_reports_counts(tmp_path):
    install(tmp_path)
    before = tree_hash(tmp_path)
    rc, lines = migrate(tmp_path, dry_run=True)
    assert rc == 0 and tree_hash(tmp_path) == before
    text = "\n".join(lines)
    assert "memory: 41 episodes, 11 facts, 6 patterns, 3 candidate lines" in text
    assert "30 carried, 5 none, 5 foreign" in text
    assert "skipped as malformed (empty text): 3" in text
    assert "12 fact subjects" in text and "3 agents" in text
    assert "memory migration is one way; restore from the backup to go back" in text


def test_apply_exact_counts_and_mapping(tmp_path, fake_embedder):
    install(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    g = graph(tmp_path)
    by_kind = {r["kind"]: r["n"] for r in g.execute(
        "SELECT kind, COUNT(*) n FROM observations GROUP BY kind")}
    assert by_kind == {"episode": 40, "fact": 10 + 3, "pattern": 5}
    assert g.execute("SELECT COUNT(*) FROM observations WHERE legacy_ref IS NULL").fetchone()[0] == 0

    # fact: importance = confidence * 10, subject entity (kind topic)
    f = g.execute("SELECT o.*, e.name, e.kind ekind FROM observations o JOIN entities e "
                  "ON e.id=o.entity_id WHERE o.legacy_ref='facts:1'").fetchone()
    assert f["importance"] == pytest.approx(f["confidence"] * 10) == pytest.approx(6.0)
    assert f["base_importance"] == f["importance"]
    assert (f["name"], f["ekind"], f["source"]) == ("Garden", "topic", "migrated")
    # facts 1 and 2 differ only by case: one entity
    assert g.execute("SELECT COUNT(*) FROM entities WHERE name_norm='garden'").fetchone()[0] == 1

    # pattern: subkind, agent entity, importance formula
    p = g.execute("SELECT o.*, e.name, e.kind ekind FROM observations o JOIN entities e "
                  "ON e.id=o.entity_id WHERE o.legacy_ref='patterns:3'").fetchone()
    assert p["subkind"] == "preference" and (p["name"], p["ekind"]) == ("gamma", "agent")
    assert p["reinforcement_count"] == 3
    assert p["importance"] == pytest.approx(min(10, 0.7 * 10 + 1.5849625007))

    # episode: copied unchanged, agents -> first agent + mentions
    e = g.execute("SELECT * FROM observations WHERE legacy_ref='episodes:3'").fetchone()  # '["beta","gamma"]'
    assert e["agent"] == "beta" and e["channel"] == "general"
    ments = {r[0] for r in g.execute(
        "SELECT en.name FROM observation_mentions m JOIN entities en ON en.id=m.entity_id "
        "WHERE m.observation_id=?", (e["id"],))}
    assert ments == {"beta", "gamma"}
    src = sqlite3.connect(tmp_path / "data" / "memory" / "memory.db.migrated")
    imp, base, summ = src.execute(
        "SELECT importance, base_importance, summary FROM episodes WHERE id=3").fetchone()
    assert (e["importance"], e["base_importance"], e["content"]) == (imp, base, summ)

    rec = migration_meta(tmp_path)
    assert rec["episodes"] == 41 and rec["facts"] == 11 and rec["patterns"] == 6
    assert rec["candidates"] == 3 and rec["embeddings_carried"] == 30
    assert rec["foreign_embeddings"] == 5 and rec["source_sha256"]
    assert guard.read_stamp(tmp_path / "data") is not None
    assert "## Memory" in (tmp_path / "data" / "migration-reports"
                           / "migration-report.md").read_text()


def test_embeddings_carried_byte_identical_foreign_null(tmp_path, fake_embedder):
    install(tmp_path)
    assert migrate(tmp_path)[0] == 0
    g = graph(tmp_path)
    for i in range(1, 31):
        r = g.execute("SELECT embedding, embed_model FROM observations WHERE legacy_ref=?",
                      (f"episodes:{i}",)).fetchone()
        text = sqlite3.connect(tmp_path / "data/memory/memory.db.migrated").execute(
            "SELECT summary FROM episodes WHERE id=?", (i,)).fetchone()[0]
        assert bytes(r["embedding"]) == fx.fake_blob(text)
        assert r["embed_model"] == "BAAI/bge-small-en-v1.5"
    for i in range(31, 41):   # 5 foreign + 5 none
        r = g.execute("SELECT embedding, embed_model FROM observations WHERE legacy_ref=?",
                      (f"episodes:{i}",)).fetchone()
        assert r["embedding"] is None and r["embed_model"] is None
    assert migration_meta(tmp_path)["foreign_embeddings"] == 5


def test_candidates_imported_deduped_files_left(tmp_path, fake_embedder):
    install(tmp_path)
    cfile = tmp_path / "data" / "memory-candidates" / "2026-04-01.md"
    text = cfile.read_text()
    assert migrate(tmp_path)[0] == 0
    assert cfile.read_text() == text
    g = graph(tmp_path)
    rows = g.execute("SELECT o.*, e.name FROM observations o JOIN entities e ON e.id=o.entity_id "
                     "WHERE o.source='candidate' ORDER BY o.id").fetchall()
    assert len(rows) == 3
    assert all(r["confidence"] == 0.5 and r["importance"] == 5.0 and r["kind"] == "fact"
               for r in rows)
    assert rows[0]["legacy_ref"] == "candidates:2026-04-01.md:3" and rows[0]["name"] == "Compost"
    assert g.execute("SELECT COUNT(*) FROM observations WHERE content LIKE '%fact2'"
                     ).fetchone()[0] == 1       # the duplicate was not re-added


def test_memory_db_renamed_and_no_open_handle(tmp_path, fake_embedder):
    install(tmp_path)
    mdb = tmp_path / "data" / "memory" / "memory.db"
    assert migrate(tmp_path)[0] == 0
    assert not mdb.exists() and not Path(str(mdb) + "-wal").exists()
    assert (tmp_path / "data/memory/memory.db.migrated").is_file()
    assert (tmp_path / "data/memory/graph.db").is_file()
    assert not (tmp_path / "data/memory/graph.db.tmp").exists()
    if os.path.isdir("/proc/self/fd"):
        open_files = [os.readlink(f"/proc/self/fd/{fd}") for fd in os.listdir("/proc/self/fd")
                      if os.path.exists(f"/proc/self/fd/{fd}")]
        assert not [f for f in open_files if "memory.db" in f or "graph.db" in f]


def test_failure_at_verify_leaves_memory_db_untouched(tmp_path, monkeypatch, fake_embedder):
    install(tmp_path)
    mdb = tmp_path / "data" / "memory" / "memory.db"
    before = mdb.read_bytes()

    def boom(*a, **k):
        raise mem.MemoryMigrationError("injected: counts do not match 1 != 2")
    monkeypatch.setattr(mem, "verify_fidelity", boom)
    rc, lines = migrate(tmp_path)
    assert rc == 1
    text = "\n".join(lines)
    assert "injected" in text and "--to-backup" in text and "pre-2.0-" in text
    assert mdb.read_bytes() == before
    assert not (tmp_path / "data/memory/graph.db").exists()
    assert not (tmp_path / "data/memory/graph.db.tmp").exists()
    assert guard.read_stamp(tmp_path / "data") is None


def test_kill_between_swap_and_rename_then_rerun_completes(tmp_path, monkeypatch, fake_embedder):
    install(tmp_path)
    real = mem._rename_legacy

    def die(lp):
        raise RuntimeError("killed after swap")
    monkeypatch.setattr(mem, "_rename_legacy", die)
    rc, _ = migrate(tmp_path)
    assert rc == 1
    assert (tmp_path / "data/memory/graph.db").exists()
    assert (tmp_path / "data/memory/memory.db").exists()
    assert not (tmp_path / "data/memory/graph.db.tmp").exists()

    monkeypatch.setattr(mem, "_rename_legacy", real)
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    assert not (tmp_path / "data/memory/memory.db").exists()
    assert (tmp_path / "data/memory/memory.db.migrated").exists()
    assert migration_meta(tmp_path)["episodes"] == 41


def test_second_run_is_noop(tmp_path, fake_embedder):
    install(tmp_path)
    assert migrate(tmp_path)[0] == 0
    h = tree_hash(tmp_path / "data")
    ctx = make_ctx(tmp_path, with_backup=False)
    assert STEP.detect(ctx) is False
    rc, lines = migrate(tmp_path)
    assert rc == 0 and any("already at schema" in l for l in lines)
    assert tree_hash(tmp_path / "data") == h


def test_no_memory_db_creates_empty_graph(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.json").write_text('{"agents": {"alpha": {}}}')
    (tmp_path / "data").mkdir()
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    g = graph(tmp_path)
    assert g.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    from lib.graph.store import open_graph
    assert open_graph(tmp_path / "data", create=False).status()["observations"] == 0
    assert STEP.detect(make_ctx(tmp_path, with_backup=False)) is False


def test_precondition_manifest_must_list_memory_db(tmp_path):
    install(tmp_path)
    ctx = make_ctx(tmp_path)
    mf = Path(ctx.backup_dir) / backup_mod.MANIFEST
    m = json.loads(mf.read_text())
    m["files"] = [e for e in m["files"] if not e["path"].endswith("memory.db")]
    mf.write_text(json.dumps(m))
    mdb = tmp_path / "data" / "memory" / "memory.db"
    before = mdb.read_bytes()
    with pytest.raises(mem.MemoryMigrationError, match="manifest does not list"):
        STEP.apply(ctx)
    assert mdb.read_bytes() == before
    assert not (tmp_path / "data/memory/graph.db.tmp").exists()
    ctx.backup_dir = None
    with pytest.raises(mem.MemoryMigrationError, match="no backup"):
        STEP.apply(ctx)


def test_old_schema_variant_migrates(tmp_path, fake_embedder):
    install(tmp_path, variant="old")
    con = sqlite3.connect(tmp_path / "data/memory/memory.db")
    assert "base_importance" not in {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
    con.close()
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    g = graph(tmp_path)
    r = g.execute("SELECT importance, base_importance, inserted_at FROM observations "
                  "WHERE legacy_ref='episodes:5'").fetchone()
    assert r["base_importance"] == r["importance"] and r["inserted_at"]
    assert g.execute("SELECT COUNT(*) FROM observations WHERE kind='episode'").fetchone()[0] == 40


def test_fork_extra_table_refused_without_force_reported_with(tmp_path, fake_embedder):
    install(tmp_path)
    mdb = tmp_path / "data/memory/memory.db"
    con = sqlite3.connect(mdb)
    con.execute("CREATE TABLE custom_notes (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute("ALTER TABLE facts ADD COLUMN weird TEXT")
    con.commit()
    con.close()
    before = tree_hash(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 3 and tree_hash(tmp_path) == before
    assert "extra table custom_notes" in "\n".join(lines)
    assert "extra column facts.weird" in "\n".join(lines)

    rc, lines = migrate(tmp_path, force=True)
    assert rc == 0, lines
    report = (tmp_path / "data/migration-reports/migration-report.md").read_text()
    assert "## Left behind" in report and "custom_notes" in report
    kept = sqlite3.connect(tmp_path / "data/memory/memory.db.migrated")
    assert kept.execute("SELECT COUNT(*) FROM custom_notes").fetchone()[0] == 0
