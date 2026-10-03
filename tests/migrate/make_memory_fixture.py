"""Deterministic (seed 0) legacy memory.db generator for the 4.4 tests.

No model, no network: embeddings are hash-derived 384-dim float32 vectors, and
HashTextEmbedding is the matching stand-in for fastembed.TextEmbedding.
"""
import hashlib
import random
import sqlite3
import sys
import types
from pathlib import Path

import numpy as np

DIM = 384
VOCAB = ("garden kitchen budget recipe thermostat calendar printer router backup "
         "invoice dentist school bicycle compost solar pantry freezer laundry "
         "garage orchard fence gutter roof vacation passport insurance tax "
         "receipt warranty battery filter sensor schedule reminder grocery "
         "furnace window paint shelf tool drill ladder").split()
AGENTS = ("alpha", "beta", "gamma")

OLD_EPISODES = """CREATE TABLE episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL, importance REAL DEFAULT 5.0,
  channel TEXT, tags TEXT, agents TEXT, created_at TIMESTAMP,
  consolidated_at TIMESTAMP DEFAULT NULL, embedding BLOB);"""
CURRENT_EPISODES = """CREATE TABLE episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL, importance REAL DEFAULT 5.0,
  base_importance REAL, channel TEXT, tags TEXT, agents TEXT, created_at TIMESTAMP,
  inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, consolidated_at TIMESTAMP DEFAULT NULL,
  embedding BLOB);"""
REST = """
CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
  content TEXT NOT NULL, confidence REAL DEFAULT 0.8, domain TEXT DEFAULT 'general',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
  pattern_type TEXT NOT NULL, content TEXT NOT NULL, confidence REAL DEFAULT 0.7,
  reinforcement_count INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP);
CREATE INDEX idx_episodes_importance ON episodes(importance DESC);
"""

# what the fixture holds, for assertions
COUNTS = {"episodes": 41, "episodes_ok": 40, "facts": 11, "facts_ok": 10,
          "patterns": 6, "patterns_ok": 5, "embedded": 30, "foreign": 5, "none": 5,
          "malformed": 3, "candidates_lines": 4, "candidates_new": 3}


def fake_vec(text) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(str(text).encode()).digest()[:8], "big")
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def fake_blob(text) -> bytes:
    return fake_vec(text).tobytes()


class HashTextEmbedding:
    def __init__(self, model_name=None, **kwargs):
        pass

    def embed(self, texts, **kw):
        for t in texts:
            yield fake_vec(t)


def install_fake_fastembed(monkeypatch):
    m = types.ModuleType("fastembed")
    m.TextEmbedding = HashTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", m)


def _sentence(rng, n=None):
    return " ".join(rng.choice(VOCAB) for _ in range(n or rng.randint(7, 12)))


def make_memory_db(path, variant="current", seed=0) -> dict:
    """Create a legacy memory.db at `path`. variant 'old' lacks base_importance
    and inserted_at on episodes."""
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript((OLD_EPISODES if variant == "old" else CURRENT_EPISODES) + REST)
    texts = []
    for i in range(1, 41):
        text = f"{_sentence(rng)} ep{i}"
        texts.append(text)
        imp = round(rng.uniform(3, 9), 1)
        if i <= 30:
            emb = fake_blob(text)
        elif i <= 35:
            emb = b"\x00\x01\x02\x03\x04\x05\x06"          # foreign length
        else:
            emb = None
        agents = [None, "alpha", "alpha,beta", '["beta", "gamma"]'][i % 4]
        ts = f"2026-01-{(i % 28) + 1:02d}T10:00:00Z"
        if variant == "old":
            con.execute("INSERT INTO episodes(summary, importance, channel, tags, agents, "
                        "created_at, embedding) VALUES (?,?,?,?,?,?,?)",
                        (text, imp, "general", '["t"]', agents, ts, emb))
        else:
            con.execute("INSERT INTO episodes(summary, importance, base_importance, channel, "
                        "tags, agents, created_at, inserted_at, embedding) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (text, imp, imp + 0.5, "general", '["t"]', agents, ts, ts, emb))
    # malformed: blank summary (with an embedding), blank fact content, blank pattern
    if variant == "old":
        con.execute("INSERT INTO episodes(summary, importance, embedding) VALUES (?,?,?)",
                    ("   ", 9.0, fake_blob("blank")))
    else:
        con.execute("INSERT INTO episodes(summary, importance, base_importance, embedding) "
                    "VALUES (?,?,?,?)", ("   ", 9.0, 9.0, fake_blob("blank")))
    subjects = ["Garden", "garden", "Budget", "Router", "Printer", "Pantry", "Solar",
                "Passport", "Furnace", "Dentist"]
    for i, subj in enumerate(subjects):
        conf = 0.8 if i % 3 else 0.6
        con.execute("INSERT INTO facts(subject, content, confidence, domain, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?)",
                    (subj, f"{_sentence(rng, 8)} fact{i}", conf, "home" if i % 2 else "general",
                     "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"))
    con.execute("INSERT INTO facts(subject, content) VALUES ('Blank', '')")
    for i in range(5):
        con.execute("INSERT INTO patterns(agent, pattern_type, content, confidence, "
                    "reinforcement_count, created_at) VALUES (?,?,?,?,?,?)",
                    (AGENTS[i % 3], ["preference", "habit"][i % 2],
                     f"{_sentence(rng, 8)} pat{i}", 0.7, i + 1, "2026-03-01T00:00:00Z"))
    con.execute("INSERT INTO patterns(agent, pattern_type, content) VALUES ('alpha','habit','')")
    con.commit()
    dup = con.execute("SELECT subject, content FROM facts WHERE id=3").fetchone()
    con.close()
    return {"fact_dup": dup, "texts": texts}


def make_install(root, variant="current", seed=0, candidates=True) -> dict:
    """<root>/data/memory/memory.db, <root>/config/, candidates dir."""
    root = Path(root)
    (root / "config").mkdir(parents=True, exist_ok=True)
    # a recognisable 1.x install (the detector needs more than memory.db)
    (root / "config" / "agents.json").write_text('{"agents": {"alpha": {}}}\n')
    info = make_memory_db(root / "data" / "memory" / "memory.db", variant, seed)
    if candidates:
        d = root / "data" / "memory-candidates"
        d.mkdir(parents=True, exist_ok=True)
        s, c = info["fact_dup"]
        (d / "2026-04-01.md").write_text(
            "# Candidates\n"
            f"- **{s}:** {c}\n"                       # duplicate of a migrated fact
            "- **Compost:** turn the pile every second week\n"
            "- **Gutter:** clear before the first rain\n"
            "- **Orchard [home]:** prune in late winter\n")
    return info
