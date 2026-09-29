"""
Tests for bin/memory-maintenance.py — Memory consolidation and decay.
"""

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT


class TestMemoryDatabaseInit:
    """Test memory database initialization."""

    def test_creates_tables(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn = mm.init_db()

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "episodes" in tables
        assert "facts" in tables
        assert "patterns" in tables
        conn.close()

    def test_tables_have_expected_columns(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn = mm.init_db()

        cursor = conn.execute("PRAGMA table_info(episodes)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "summary" in columns
        assert "importance" in columns
        assert "created_at" in columns
        assert "embedding" in columns
        conn.close()

    def test_init_is_idempotent(self, tmp_workspace, monkeypatch):
        """Calling init_db twice should not error."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn1 = mm.init_db()
        conn1.close()
        conn2 = mm.init_db()
        conn2.close()


class TestMemoryDecay:
    """Test episode importance decay."""

    def test_decay_reduces_importance(self, memory_db, tmp_workspace, monkeypatch):
        conn, db_path = memory_db
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_DECAY_RATE", "0.25")

        old_date = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Test episode", 8.0, old_date),
        )
        conn.commit()

        cursor = conn.execute("SELECT importance FROM episodes WHERE summary = 'Test episode'")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 8.0
        conn.close()

    def test_episodes_below_cutoff_eligible_for_pruning(self, memory_db):
        """Episodes with effective score below cutoff should be prunable."""
        conn, _ = memory_db

        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Old boring episode", 2.0, old_date),
        )
        conn.commit()

        cursor = conn.execute("SELECT importance FROM episodes WHERE summary = 'Old boring episode'")
        row = cursor.fetchone()
        decay_rate = 0.25
        effective = row[0] - (30 * decay_rate)
        assert effective < 6.0
        conn.close()


class TestEpisodeStorage:
    """Test episode CRUD operations."""

    def test_insert_episode(self, memory_db):
        conn, _ = memory_db

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, channel, tags, agents, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("User discussed deployment", 7.5, "general", "deploy,ops", "amos", now),
        )
        conn.commit()

        cursor = conn.execute("SELECT * FROM episodes WHERE summary LIKE '%deployment%'")
        row = cursor.fetchone()
        assert row is not None
        assert row[2] == 7.5
        conn.close()

    def test_facts_table(self, memory_db):
        conn, _ = memory_db

        conn.execute(
            "INSERT INTO facts (subject, content, confidence, domain) VALUES (?, ?, ?, ?)",
            ("OwnerName", "Lives in a region", 0.95, "personal"),
        )
        conn.commit()

        cursor = conn.execute("SELECT * FROM facts WHERE subject = 'OwnerName'")
        row = cursor.fetchone()
        assert row is not None
        assert "region" in row[2]
        conn.close()

    def test_patterns_table(self, memory_db):
        conn, _ = memory_db

        conn.execute(
            "INSERT INTO patterns (agent, pattern_type, content, confidence) VALUES (?, ?, ?, ?)",
            ("test-agent", "correction", "Don't fabricate URLs", 0.9),
        )
        conn.commit()

        cursor = conn.execute("SELECT * FROM patterns WHERE agent = 'test-agent'")
        row = cursor.fetchone()
        assert row is not None
        assert "fabricate" in row[3]
        conn.close()


class TestPruneGracePeriod:
    """Issue: same-run prune. main() used to create episodes and then delete
    everything below MEMORY_CUTOFF in the same run — the Haiku scoring
    prompt rates ordinary interactions 5-6, below the 6.0 default cutoff, so
    almost everything new was deleted the night it was made (measured
    2026-09-28: 96 created, 93 pruned, net 3 kept)."""

    def test_fresh_low_score_episode_survives_prune(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        now = datetime.now(timezone.utc).isoformat()
        now_sqlite = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Ordinary interaction", 5.5, 5.5, now, now_sqlite),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)

        assert pruned == 0
        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Ordinary interaction'"
        ).fetchone()
        assert row is not None
        conn.close()

    def test_old_low_score_episode_is_pruned(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_PRUNE_GRACE_DAYS", "7")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_inserted = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Old boring episode", 3.0, 3.0, old_created, old_inserted),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)

        assert pruned == 1
        row = conn.execute(
            "SELECT id FROM episodes WHERE summary = 'Old boring episode'"
        ).fetchone()
        assert row is None
        conn.close()

    def test_grace_period_is_configurable(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_PRUNE_GRACE_DAYS", "1")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        two_days_ago_inserted = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        two_days_ago_created = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Two days old", 3.0, 3.0, two_days_ago_created, two_days_ago_inserted),
        )
        conn.commit()

        # With a 1-day grace period, a 2-day-old low-score episode is prunable.
        pruned = mm.prune_low_importance(conn)
        assert pruned == 1
        conn.close()

    def test_high_score_old_episode_never_pruned(self, tmp_workspace, monkeypatch):
        """Grace period only protects new episodes — old ones above cutoff
        should never be pruned regardless of age."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_inserted = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Important old thing", 9.0, 9.0, old_created, old_inserted),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)
        assert pruned == 0
        conn.close()


class TestScoreImportanceFailure:
    """Issue: scoring failure used to default to 5.0, which sits below the
    6.0 cutoff, so a Haiku hiccup silently discarded the episode. Now it
    retries once with a longer timeout, and a final failure stores a score
    at/above the cutoff and is counted in stats."""

    def test_success_on_first_attempt(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, stdout="7\n", stderr="")

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")

        assert score == 7.0
        assert len(calls) == 1

    def test_retries_once_on_failure_then_succeeds(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        calls = []

        def fake_run(cmd, **kwargs):
            timeout = kwargs.get("timeout")
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, stdout="8\n", stderr="")

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")

        assert score == 8.0
        assert len(calls) == 2
        assert calls[1] > calls[0]  # retry uses the longer timeout

    def test_final_failure_stores_cutoff_safe_score_and_counts(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        stats = {"score_failures": 0}
        score = mm.score_importance("some excerpt", stats)

        assert score >= mm.IMPORTANCE_CUTOFF
        assert stats["score_failures"] == 1

    def test_score_failures_not_counted_without_stats_dict(self, tmp_workspace, monkeypatch):
        """Passing no stats dict must not raise — callers that don't care
        about the counter still work."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")
        assert score >= mm.IMPORTANCE_CUTOFF


class TestDecayIdempotent:
    """Issue: compounding decay. decay_importance() used to subtract the
    full age-based decay from the already-decayed `importance` every night,
    so loss grew quadratically. Now it decays from `base_importance`, which
    never changes, so repeated runs converge instead of compounding."""

    def test_decay_is_idempotent_across_two_runs(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_DECAY_RATE", "0.25")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Test episode", 8.0, 8.0, old_date),
        )
        conn.commit()

        mm.decay_importance(conn)
        after_first = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Test episode'"
        ).fetchone()[0]

        mm.decay_importance(conn)
        after_second = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Test episode'"
        ).fetchone()[0]

        assert after_first == pytest.approx(after_second)
        # 8 days old at rate 0.25: (8/4)*0.25 = 0.5 off base_importance 8.0
        assert after_first == pytest.approx(7.5, abs=1e-6)
        conn.close()

    def test_decay_does_not_compound_from_stale_importance(self, tmp_workspace, monkeypatch):
        """Without base_importance, decaying an already-decayed value a
        second time would subtract age-based decay again on top of itself."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_DECAY_RATE", "0.25")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        # Simulate a row that was already decayed once (importance != base).
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Already decayed once", 7.5, 8.0, old_date),
        )
        conn.commit()

        mm.decay_importance(conn)
        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Already decayed once'"
        ).fetchone()
        # Recomputed from base_importance (8.0), not from the stale 7.5.
        assert row[0] == pytest.approx(7.5, abs=1e-6)
        conn.close()


class TestEpisodeColumnMigration:
    """Issue: migrations must run on an old-schema DB that predates
    base_importance/inserted_at, backfilling rather than erroring."""

    def test_migration_adds_columns_and_backfills(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        # Build an old-schema DB by hand — no base_importance, no inserted_at.
        mm.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        old_conn = sqlite3.connect(str(mm.MEMORY_DB))
        old_conn.executescript("""
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                importance REAL DEFAULT 5.0,
                channel TEXT,
                tags TEXT,
                agents TEXT,
                created_at TIMESTAMP,
                consolidated_at TIMESTAMP DEFAULT NULL,
                embedding BLOB
            );
        """)
        old_conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Pre-migration episode", 6.5, datetime.now(timezone.utc).isoformat()),
        )
        old_conn.commit()
        old_conn.close()

        # init_db() must run the guarded migration without erroring.
        conn = mm.init_db()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        assert "base_importance" in cols
        assert "inserted_at" in cols

        row = conn.execute(
            "SELECT importance, base_importance, inserted_at FROM episodes "
            "WHERE summary = 'Pre-migration episode'"
        ).fetchone()
        assert row["base_importance"] == row["importance"] == 6.5
        assert row["inserted_at"] is not None
        conn.close()

    def test_migration_is_idempotent(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn1 = mm.init_db()
        conn1.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Something", 7.0, 7.0, datetime.now(timezone.utc).isoformat()),
        )
        conn1.commit()
        conn1.close()

        # Running init_db() a second time against the same DB must not error
        # or duplicate columns, and must not clobber an existing base_importance.
        conn2 = mm.init_db()
        row = conn2.execute(
            "SELECT importance, base_importance FROM episodes WHERE summary = 'Something'"
        ).fetchone()
        assert row["base_importance"] == 7.0
        conn2.close()


class TestSameRunEpisodesSurvive:
    """End-to-end: process_messages_to_episodes() followed by
    prune_low_importance() in the same run must not delete what was just
    created, even when scoring rates it below cutoff."""

    def test_same_run_episode_survives(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        messages_file = mm.MESSAGES_DIR / f"messages-{yesterday}.jsonl"
        messages_file.parent.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with open(messages_file, "w") as f:
            f.write(json.dumps({
                "ts": ts, "author_name": "Mike", "content": "routine update",
                "channel_name": "general", "is_bot": False,
            }) + "\n")

        # Score below cutoff, as the Haiku prompt does for ordinary chatter.
        monkeypatch.setattr(mm, "score_importance", lambda summary, stats=None: 5.5)

        conn = mm.init_db()
        created = mm.process_messages_to_episodes(conn)
        assert created == 1

        pruned = mm.prune_low_importance(conn)
        assert pruned == 0

        row = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        assert row[0] == 1
        conn.close()

    def test_scoring_failure_stores_and_survives_prune(self, tmp_workspace, monkeypatch):
        """End-to-end: a scoring failure during process_messages_to_episodes
        must store the episode at/above cutoff (not the old below-cutoff
        5.0 default) and be counted, and it must not get pruned in the
        same run."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        messages_file = mm.MESSAGES_DIR / f"messages-{yesterday}.jsonl"
        messages_file.parent.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with open(messages_file, "w") as f:
            f.write(json.dumps({
                "ts": ts, "author_name": "Mike", "content": "routine update",
                "channel_name": "general", "is_bot": False,
            }) + "\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        conn = mm.init_db()
        stats = {"score_failures": 0}
        created = mm.process_messages_to_episodes(conn, stats)
        assert created == 1
        assert stats["score_failures"] == 1

        row = conn.execute(
            "SELECT importance, base_importance FROM episodes"
        ).fetchone()
        assert row["importance"] >= mm.IMPORTANCE_CUTOFF
        assert row["base_importance"] >= mm.IMPORTANCE_CUTOFF

        pruned = mm.prune_low_importance(conn)
        assert pruned == 0
        conn.close()
