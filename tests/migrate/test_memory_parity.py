"""Recall parity for 40_memory (spec 4.4): Gate A exact, Gate B logged."""
import json
import sqlite3

import pytest

from _memory_helpers import (STEP, embed, fx, graph, install, make_ctx, mem, migrate,  # noqa: F401
                             migration_meta, _env, fake_embedder)


def jsonl(tmp_path):
    p = tmp_path / "data" / "migration-reports" / "memory-parity.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_gate_a_passes_hybrid(tmp_path, fake_embedder):
    install(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    rows = jsonl(tmp_path)
    assert len(rows) == 50 and all(r["gate_a_ok"] for r in rows)
    assert {r["mode"] for r in rows} == {"hybrid"}
    assert set(rows[0]) >= {"query", "mode", "old_top_k", "new_legacy_top_k",
                            "new_default_top_k", "gate_a_ok", "overlap", "top1_same",
                            "dropped", "added"}
    assert any(r["old_top_k"] for r in rows)
    assert migration_meta(tmp_path)["parity"]["mode"] == "hybrid"


def test_gate_a_passes_keyword_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")
    install(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 0, lines
    rows = jsonl(tmp_path)
    assert len(rows) == 50 and all(r["gate_a_ok"] for r in rows)
    assert {r["mode"] for r in rows} == {"keyword"}


def test_gate_a_passes_when_embedder_missing(tmp_path):
    install(tmp_path)
    rc, _ = migrate(tmp_path)           # no fastembed in this environment
    assert rc == 0 and {r["mode"] for r in jsonl(tmp_path)} == {"keyword"}


def test_corrupted_importance_fails_gate_a_and_cuts_nothing(tmp_path, fake_embedder):
    install(tmp_path)
    ctx = make_ctx(tmp_path)
    mdb = tmp_path / "data/memory/memory.db"
    before = mdb.read_bytes()
    STEP.apply(ctx)
    sp = mem.staging_file(ctx)
    # fidelity would catch this first, so run the parity gate on its own
    con = sqlite3.connect(sp)
    con.execute("UPDATE observations SET importance = 10 - importance WHERE kind='episode'")
    con.commit()
    con.close()
    with pytest.raises(mem.MemoryMigrationError, match="Gate A"):
        mem.run_parity(ctx, ctx._memory["src"], mdb, sp)
    with pytest.raises(mem.MemoryMigrationError):
        STEP.verify(ctx)                 # the full verify refuses too
    assert not (tmp_path / "data/memory/graph.db").exists()
    assert mdb.read_bytes() == before and not sp.exists()


def test_gate_b_differences_logged_not_failing(tmp_path, fake_embedder):
    install(tmp_path)
    rc, lines = migrate(tmp_path)
    assert rc == 0
    rows = jsonl(tmp_path)
    diff = [r for r in rows if r["dropped"] or r["added"] or not r["top1_same"]]
    assert diff, "default weights add the name signal, so some queries must differ"
    assert all(r["gate_a_ok"] for r in diff)
    report = (tmp_path / "data/migration-reports/migration-report.md").read_text()
    assert "Gate B" in report and "queries with differences" in report
    assert report.count("- `") <= 20


def test_queries_deterministic_across_runs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    install(a)
    install(b)
    assert migrate(a)[0] == 0 and migrate(b)[0] == 0
    qa = [r["query"] for r in jsonl(a)]
    assert qa == [r["query"] for r in jsonl(b)] and len(set(qa)) > 20


def test_parity_queries_zero_warns(tmp_path, fake_embedder):
    install(tmp_path)
    rc, lines = migrate(tmp_path, parity_queries=0)
    assert rc == 0
    assert not (tmp_path / "data/migration-reports/memory-parity.jsonl").exists()
    assert "WARNING: recall-parity check disabled" in (
        tmp_path / "data/migration-reports/migration-report.md").read_text()
    assert migration_meta(tmp_path)["parity"]["queries"] == 0


def test_operator_query_file_appended(tmp_path, fake_embedder):
    install(tmp_path)
    (tmp_path / "config" / "memory-parity-queries.txt").write_text(
        "what is the router password\n\n  compost schedule \n")
    rc, _ = migrate(tmp_path, parity_queries=10)
    assert rc == 0
    qs = [r["query"] for r in jsonl(tmp_path)]
    assert len(qs) == 12 and qs[-2:] == ["what is the router password", "compost schedule"]


def test_embedding_record_unchanged_then_changed(tmp_path, monkeypatch, fake_embedder):
    install(tmp_path / "same")
    assert migrate(tmp_path / "same")[0] == 0
    rec = migration_meta(tmp_path / "same")
    assert rec["embedding_model_changed"] is False and rec["reembedded"] == 0
    assert rec["embedding_model_before"] == rec["embedding_model_after"] \
        == "BAAI/bge-small-en-v1.5" and rec["embeddings_carried"] == 30

    monkeypatch.setattr(embed, "MODEL_NAME", "some/future-model")
    install(tmp_path / "new")
    rc, lines = migrate(tmp_path / "new")
    assert rc == 0, lines
    rec = migration_meta(tmp_path / "new")
    assert rec["embedding_model_changed"] is True and rec["reembedded"] == 0
    assert rec["embedding_model_before"] == "BAAI/bge-small-en-v1.5"
    assert rec["embedding_model_after"] == "some/future-model"
    assert rec["embeddings_carried"] == 0
    g = graph(tmp_path / "new")
    assert g.execute("SELECT COUNT(*) FROM observations WHERE embedding IS NOT NULL"
                     ).fetchone()[0] == 0
    assert {r["mode"] for r in jsonl(tmp_path / "new")} == {"keyword"}


def test_embedding_model_before_unknown_without_embeddings(tmp_path):
    install(tmp_path)
    con = sqlite3.connect(tmp_path / "data/memory/memory.db")
    con.execute("UPDATE episodes SET embedding = NULL")
    con.commit()
    con.close()
    assert migrate(tmp_path)[0] == 0
    assert migration_meta(tmp_path)["embedding_model_before"] == "unknown"
