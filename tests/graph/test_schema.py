import sqlite3

import pytest

from lib.graph.schema import (SCHEMA_VERSION, GraphNotInitialised, check_schema,
                              ensure_schema)
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)


def mk(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    c.row_factory = sqlite3.Row
    return c


def test_ensure_idempotent(tmp_path):
    c = mk(tmp_path)
    ensure_schema(c)
    ensure_schema(c)
    check_schema(c)
    assert c.execute("SELECT value FROM meta WHERE key='graph_schema'").fetchone()[0] \
        == str(SCHEMA_VERSION)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_check_on_empty_file_raises_and_writes_nothing(tmp_path):
    p = tmp_path / "g.db"
    p.write_bytes(b"")
    c = sqlite3.connect(str(p))
    with pytest.raises(GraphNotInitialised):
        check_schema(c)
    c.close()
    assert p.read_bytes() == b""
    assert not list(tmp_path.glob("g.db-*"))


def test_newer_schema_refused(tmp_path):
    c = mk(tmp_path)
    ensure_schema(c)
    c.execute("UPDATE meta SET value=? WHERE key='graph_schema'", (str(SCHEMA_VERSION + 1),))
    c.commit()
    with pytest.raises(GraphNotInitialised):
        check_schema(c)
    with pytest.raises(GraphNotInitialised):
        ensure_schema(c)


def fts(c, table, term):
    return [r[0] for r in c.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (term,))]


def test_fts_triggers(tmp_path):
    c = mk(tmp_path)
    ensure_schema(c)
    c.execute("INSERT INTO observations(kind, content) VALUES ('fact','the quick brown fox')")
    c.execute("INSERT INTO entities(name, name_norm, summary) VALUES ('Alice','alice','gardener')")
    assert fts(c, "observations_fts", "foxes") == [1]  # porter stemming
    assert fts(c, "entities_fts", "gardener") == [1]
    c.execute("UPDATE observations SET content='lazy dog' WHERE id=1")
    c.execute("UPDATE entities SET summary='pilot' WHERE id=1")
    assert fts(c, "observations_fts", "fox") == []
    assert fts(c, "observations_fts", "dog") == [1]
    assert fts(c, "entities_fts", "gardener") == []
    assert fts(c, "entities_fts", "pilot") == [1]
    c.execute("DELETE FROM observations WHERE id=1")
    c.execute("DELETE FROM entities WHERE id=1")
    assert fts(c, "observations_fts", "dog") == []
    assert fts(c, "entities_fts", "pilot") == []


def test_checks_and_cascades(tmp_path):
    c = mk(tmp_path)
    ensure_schema(c)
    c.execute("PRAGMA foreign_keys=ON")
    for n in "ab":
        c.execute("INSERT INTO entities(name, name_norm) VALUES (?,?)", (n, n))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO edges(src_id,dst_id,relation) VALUES (1,1,'x')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO observations(kind, content) VALUES ('bogus','x')")
    c.execute("INSERT INTO edges(src_id,dst_id,relation) VALUES (1,2,'x')")
    c.execute("INSERT INTO entity_aliases VALUES (1,'aa')")
    c.execute("INSERT INTO observations(kind, content, entity_id) VALUES ('fact','x',1)")
    c.execute("DELETE FROM entities WHERE id=1")
    assert c.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0] == 0
    assert c.execute("SELECT entity_id FROM observations").fetchone()[0] is None
