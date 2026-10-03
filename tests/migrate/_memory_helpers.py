"""Shared helpers for the 4.4 memory migrator tests."""
import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import make_memory_fixture as fx  # noqa: E402
from lib.graph import embed  # noqa: E402
from lib.migrate import backup as backup_mod  # noqa: E402
from lib.migrate import runner  # noqa: E402

mem = importlib.import_module("lib.migrate.steps.40_memory")
STEP = mem.STEP


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # never load a real embedding model (network, HOME cache), even where CI has
    # fastembed installed; tests wanting an embedder add the fake on top
    monkeypatch.setitem(sys.modules, "fastembed", None)
    for k in ("KARAKOS_SKIP_STAMP_CHECK", "KARAKOS_ENV", "KARAKOS_SEMANTIC_RECALL",
              "KARAKOS_RECALL_WEIGHTS", "KARAKOS_RECALL_SCAN_LIMIT", "FASTEMBED_CACHE_PATH"):
        monkeypatch.delenv(k, raising=False)
    embed._reset()
    yield
    embed._reset()


@pytest.fixture
def fake_embedder(monkeypatch):
    fx.install_fake_fastembed(monkeypatch)
    embed._reset()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def install(tmp_path, variant="current", **kw):
    fx.make_install(tmp_path, variant=variant, **kw)
    return tmp_path / "data", tmp_path / "config"


def migrate(tmp_path, **kw):
    """Run the chain restricted to the memory step. Returns (rc, output lines)."""
    lines = []
    rc = runner.run(tmp_path / "data", tmp_path / "config", tmp_path / "backups",
                    steps=[STEP], out=lines.append, **kw)
    return rc, lines


def make_ctx(tmp_path, with_backup=True, parity_queries=50, force=False):
    data, config = tmp_path / "data", tmp_path / "config"
    detected = runner.detect_version(data, config)
    ctx = runner.Context(data, config, None, detected, __import__("logging").getLogger("t"),
                         force=force, parity_queries=parity_queries)
    if with_backup:
        ctx.backup_dir = backup_mod.backup(data, config, tmp_path / "backups")
    return ctx


def graph(tmp_path):
    con = sqlite3.connect(tmp_path / "data" / "memory" / "graph.db")
    con.row_factory = sqlite3.Row
    return con


def migration_meta(tmp_path):
    con = graph(tmp_path)
    try:
        return json.loads(con.execute("SELECT value FROM meta WHERE key='migration'")
                          .fetchone()[0])
    finally:
        con.close()


@pytest.fixture
def legacy_memory_db(tmp_workspace):
    """A 1.x memory.db (episodes/facts/patterns DDL). Migrator tests only."""
    db_path = tmp_workspace / "data" / "memory" / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 5.0,
            base_importance REAL,
            channel TEXT,
            tags TEXT,
            agents TEXT,
            created_at TIMESTAMP,
            inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consolidated_at TIMESTAMP DEFAULT NULL,
            embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.8,
            domain TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            reinforcement_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
    """)
    conn.commit()
    return conn, db_path
