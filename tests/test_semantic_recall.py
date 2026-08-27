"""
Tests for issue #149 — the memory tool's `recall` action must actually use
the embeddings bin/memory-maintenance.py writes.

Before this, maintenance embedded 50 episodes a night with
BAAI/bge-small-en-v1.5 and stored float32 blobs on episodes.embedding, and
`recall` was `summary LIKE '%query%'`. Nothing in the repo ever read a blob
back, so the install paid for the fastembed dependency and the nightly
compute and got substring matching.

Nothing here loads the real model. fastembed is stubbed into sys.modules in
every test that reaches the embedder, so the vectors are chosen by the test
and the assertions are about ranking, not about what bge-small happens to
think two sentences mean. The blobs themselves are written with numpy
exactly the way memory-maintenance.py writes them, so the decode path is
tested against the real byte format rather than a convenient one.
"""

import sqlite3
import sys

import numpy as np
import pytest

from conftest import import_script, PACKAGE_ROOT


# --- helpers ---------------------------------------------------------------


def blob(vector):
    """Encode a vector the way bin/memory-maintenance.py does."""
    return np.array(vector, dtype=np.float32).tobytes()


class FakeTextEmbedding:
    """Stand-in for fastembed.TextEmbedding with test-chosen vectors."""

    def __init__(self, model_name=None, **kwargs):
        self.model_name = model_name
        FakeTextEmbedding.constructed.append(model_name)

    def embed(self, texts, **kwargs):
        for text in texts:
            yield np.array(FakeTextEmbedding.vectors[text], dtype=np.float32)


class ExplodingTextEmbedding:
    """A model that cannot be loaded — a missing weight file, a dead ONNX
    runtime, no network on first use."""

    def __init__(self, model_name=None, **kwargs):
        ExplodingTextEmbedding.constructed.append(model_name)
        raise RuntimeError("model weights not found")


def install_fastembed(monkeypatch, cls, vectors=None):
    """Put a fake `fastembed` module in sys.modules and return the class."""
    import types

    cls.constructed = []
    if vectors is not None:
        cls.vectors = vectors
    module = types.ModuleType("fastembed")
    module.TextEmbedding = cls
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return cls


def remove_fastembed(monkeypatch):
    """Make `from fastembed import TextEmbedding` raise ImportError, the way
    it does on an install that never got the optional dependency."""
    monkeypatch.setitem(sys.modules, "fastembed", None)


@pytest.fixture
def server(memory_db, tmp_workspace, monkeypatch):
    """tools-server imported against a scratch workspace holding memory.db."""
    conn, _ = memory_db
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    monkeypatch.delenv("KARAKOS_SEMANTIC_RECALL", raising=False)
    monkeypatch.delenv("MEMORY_IMPORTANCE_WEIGHT", raising=False)
    mod = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    yield mod, conn
    conn.close()


def add_episode(conn, summary, importance, embedding=None, created_at="2026-01-01T00:00:00+00:00"):
    cur = conn.execute(
        "INSERT INTO episodes (summary, importance, created_at, embedding) "
        "VALUES (?, ?, ?, ?)",
        (summary, importance, created_at, blob(embedding) if embedding else None),
    )
    conn.commit()
    return cur.lastrowid


def like_ids(conn, query):
    """What the old keyword-only recall would have returned, in its order."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT id FROM episodes WHERE summary LIKE ? ORDER BY importance DESC",
            (f"%{query}%",),
        ).fetchall()
    ]


# --- the actual bug --------------------------------------------------------


def test_semantic_ranking_beats_keyword_where_they_disagree(server, monkeypatch):
    """The paraphrase must win over the literal match.

    `paraphrase` never contains the string "deploy", so the old LIKE scan
    could not return it at all; `aside` contains it and is about something
    else entirely. Same importance on both, so the only thing separating
    them is the embedding.
    """
    mod, conn = server
    paraphrase = add_episode(
        conn, "Shipped the dashboard to production and restarted the service",
        7.0, embedding=[1.0, 0.0, 0.0],
    )
    aside = add_episode(
        conn, "Someone said the word deploy while talking about the fence",
        7.0, embedding=[-0.2, 1.0, 0.0],
    )

    # Establish that the two rankings genuinely disagree: keyword sees only
    # the aside, and cannot see the paraphrase at any limit.
    assert like_ids(conn, "deploy") == [aside]

    install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "semantic"
    ids = [e["id"] for e in result["episodes"]]
    assert ids[0] == paraphrase, "the semantically closest episode must rank first"
    assert aside in ids, "the keyword match is demoted, not dropped"
    assert result["episodes"][0]["similarity"] == pytest.approx(1.0, abs=1e-4)
    assert result["episodes"][0]["match"] == "semantic"


def test_recall_is_not_a_like_scan_anymore(server, monkeypatch):
    """A query that matches no episode literally still returns the right one."""
    mod, conn = server
    wanted = add_episode(conn, "Renewed the TLS certificate for the hub", 6.0,
                         embedding=[0.0, 1.0, 0.0])
    add_episode(conn, "Reheated leftovers", 6.0, embedding=[1.0, 0.0, 0.0])

    assert like_ids(conn, "https expiry") == []

    install_fastembed(monkeypatch, FakeTextEmbedding, {"https expiry": [0.0, 1.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "https expiry"})

    assert result["mode"] == "semantic"
    assert result["episodes"][0]["id"] == wanted


# --- importance stays in the ranking ---------------------------------------


def test_importance_flips_a_near_tie(server, monkeypatch):
    """An ancient trivial episode that happens to sit close to the query must
    not outrank a more important one that is only slightly further away.

    trivial: cosine 0.80, importance 3 -> 0.75*0.900 + 0.25*0.3 = 0.7500
    important: cosine 0.70, importance 9 -> 0.75*0.850 + 0.25*0.9 = 0.8625
    """
    mod, conn = server
    trivial = add_episode(conn, "Trivial chatter", 3.0, embedding=[0.8, 0.6, 0.0])
    important = add_episode(conn, "Major decision", 9.0, embedding=[0.7, 0.714143, 0.0])

    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "q"})

    ids = [e["id"] for e in result["episodes"]]
    assert ids == [important, trivial]


def test_importance_cannot_rescue_a_bad_match(server, monkeypatch):
    """The blend is 75% similarity, so importance tunes ties — it does not
    override relevance."""
    mod, conn = server
    close = add_episode(conn, "Close but unimportant", 1.0, embedding=[1.0, 0.0, 0.0])
    far = add_episode(conn, "Important but unrelated", 10.0, embedding=[-1.0, 0.0, 0.0])

    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "q"})

    ids = [e["id"] for e in result["episodes"]]
    assert ids == [close, far]


def test_importance_weight_is_tunable(server, monkeypatch):
    """MEMORY_IMPORTANCE_WEIGHT=1.0 collapses the blend back to importance."""
    mod, conn = server
    monkeypatch.setattr(mod, "MEMORY_IMPORTANCE_WEIGHT", 1.0)
    close = add_episode(conn, "Close but unimportant", 1.0, embedding=[1.0, 0.0, 0.0])
    far = add_episode(conn, "Unrelated but important", 10.0, embedding=[-1.0, 0.0, 0.0])

    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "q"})

    assert [e["id"] for e in result["episodes"]] == [far, close]


# --- graceful degradation --------------------------------------------------


def test_falls_back_to_keyword_when_nothing_is_embedded(server, monkeypatch):
    """A database maintenance has never embedded (or one where fastembed was
    never installed, so generate_embeddings() skipped) behaves exactly as it
    did before — and must not pay to load a model to find that out."""
    mod, conn = server
    hit = add_episode(conn, "We talked about the deploy", 7.0)
    add_episode(conn, "Unrelated", 9.0)

    fake = install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "keyword"
    assert result["reason"] == "no_embeddings"
    assert [e["id"] for e in result["episodes"]] == [hit]
    assert fake.constructed == [], "must not load the model when there is nothing to compare"


def test_falls_back_to_keyword_when_fastembed_is_absent(server, monkeypatch):
    """The dependency is optional in memory-maintenance.py, so it has to be
    optional here too: a missing fastembed is keyword search, not an error."""
    mod, conn = server
    hit = add_episode(conn, "We talked about the deploy", 7.0, embedding=[1.0, 0.0, 0.0])
    add_episode(conn, "Unrelated", 9.0, embedding=[0.0, 1.0, 0.0])

    remove_fastembed(monkeypatch)
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "keyword"
    assert result["reason"] == "embedder_unavailable"
    assert [e["id"] for e in result["episodes"]] == [hit]
    assert "error" not in result


def test_falls_back_when_the_model_will_not_load(server, monkeypatch, capsys):
    """fastembed present but the model itself unusable — same outcome."""
    mod, conn = server
    hit = add_episode(conn, "We talked about the deploy", 7.0, embedding=[1.0, 0.0, 0.0])

    install_fastembed(monkeypatch, ExplodingTextEmbedding)
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "keyword"
    assert result["reason"] == "embedder_unavailable"
    assert [e["id"] for e in result["episodes"]] == [hit]
    assert "falling back to keyword search" in capsys.readouterr().err


def test_a_broken_model_is_not_re_probed_every_call(server, monkeypatch):
    """Retrying a load that takes seconds to fail, on every single call, is
    how a 60s tool budget gets eaten. The failure is latched."""
    mod, conn = server
    add_episode(conn, "deploy", 7.0, embedding=[1.0, 0.0, 0.0])

    broken = install_fastembed(monkeypatch, ExplodingTextEmbedding)
    for _ in range(3):
        mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert len(broken.constructed) == 1


def test_empty_query_does_not_load_the_model(server, monkeypatch):
    """`recall` with no query is 'the most important episodes', which the
    LIKE '%%' scan already answers. Embedding the empty string does not."""
    mod, conn = server
    top = add_episode(conn, "Important", 9.0, embedding=[1.0, 0.0, 0.0])
    add_episode(conn, "Less important", 4.0, embedding=[1.0, 0.0, 0.0])

    fake = install_fastembed(monkeypatch, FakeTextEmbedding, {})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": ""})

    assert result["mode"] == "keyword"
    assert result["reason"] == "empty_query"
    assert result["episodes"][0]["id"] == top
    assert fake.constructed == []


def test_kill_switch_forces_keyword_mode(memory_db, tmp_workspace, monkeypatch):
    """A host that cannot afford an ONNX model inside a tool call can opt out."""
    conn, _ = memory_db
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")
    mod = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")

    hit = add_episode(conn, "We talked about the deploy", 7.0, embedding=[1.0, 0.0, 0.0])
    fake = install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})

    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "keyword"
    assert result["reason"] == "disabled"
    assert [e["id"] for e in result["episodes"]] == [hit]
    assert fake.constructed == []
    conn.close()


# --- mixed databases -------------------------------------------------------


def test_mixed_database_ranks_both_kinds(server, monkeypatch):
    """Episodes predating this change have embedding IS NULL. They must still
    be reachable by literal match, folded into the same ranking rather than
    dropped or automatically preferred."""
    mod, conn = server
    embedded = add_episode(conn, "Pushed the new build to the hub", 7.0,
                           embedding=[1.0, 0.0, 0.0])
    legacy = add_episode(conn, "Old note that mentions deploy", 7.0)
    add_episode(conn, "Old note about nothing relevant", 7.0)

    install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "semantic"
    by_id = {e["id"]: e for e in result["episodes"]}
    assert embedded in by_id and legacy in by_id
    assert by_id[embedded]["match"] == "semantic"
    assert by_id[legacy]["match"] == "keyword"
    assert by_id[legacy]["similarity"] is None
    # The perfect semantic match outranks the literal one.
    assert result["episodes"][0]["id"] == embedded


def test_unembedded_non_matches_are_not_invented(server, monkeypatch):
    """An un-embedded episode with no literal match has nothing to rank on
    and must not appear."""
    mod, conn = server
    add_episode(conn, "Pushed the new build", 7.0, embedding=[1.0, 0.0, 0.0])
    ghost = add_episode(conn, "Completely unrelated old note", 9.0)

    install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert ghost not in [e["id"] for e in result["episodes"]]


def test_limit_is_respected(server, monkeypatch):
    mod, conn = server
    for i in range(8):
        add_episode(conn, f"Episode {i}", 5.0 + i * 0.1, embedding=[1.0, 0.0, 0.0])

    install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0, 0.0]})
    result = mod.handle_core_tool("memory", {"action": "recall", "query": "q", "limit": 3})

    assert len(result["episodes"]) == 3


# --- blob decoding ---------------------------------------------------------


def test_decode_embedding_round_trips_the_maintenance_format(server):
    """The reader must agree with numpy's float32 tobytes(), which is what
    bin/memory-maintenance.py writes."""
    mod, _ = server
    vector = [0.1, -0.25, 3.5, 0.0]

    decoded = mod.decode_embedding(blob(vector))

    assert decoded == pytest.approx(vector, abs=1e-6)
    assert len(decoded) == 4


def test_decode_embedding_survives_junk(server):
    mod, _ = server
    assert mod.decode_embedding(None) is None
    assert mod.decode_embedding(b"") is None
    assert mod.decode_embedding(b"abc") is None  # not a whole number of floats


def test_wrong_dimension_blobs_are_skipped_not_compared(server, monkeypatch):
    """A blob from a different model does not share a vector space with the
    query, so comparing them is meaningless. If that is all there is, recall
    degrades to keyword rather than ranking nonsense."""
    mod, conn = server
    hit = add_episode(conn, "mentions deploy", 7.0, embedding=[1.0, 2.0])  # 2-dim
    install_fastembed(monkeypatch, FakeTextEmbedding, {"deploy": [1.0, 0.0, 0.0]})

    result = mod.handle_core_tool("memory", {"action": "recall", "query": "deploy"})

    assert result["mode"] == "keyword"
    assert result["reason"] == "no_usable_embeddings"
    assert [e["id"] for e in result["episodes"]] == [hit]


def test_cosine_similarity_edges(server):
    mod, _ = server
    assert mod.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert mod.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert mod.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert mod.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# --- cost ------------------------------------------------------------------


def test_model_is_loaded_once_per_process(server, monkeypatch):
    """The load is the expensive part and this server is long-lived, so only
    the first recall of a session may pay it."""
    mod, conn = server
    add_episode(conn, "one", 7.0, embedding=[1.0, 0.0, 0.0])

    fake = install_fastembed(monkeypatch, FakeTextEmbedding, {"q": [1.0, 0.0, 0.0]})
    for _ in range(4):
        mod.handle_core_tool("memory", {"action": "recall", "query": "q"})

    assert fake.constructed == [mod.EMBED_MODEL_NAME]


def test_the_pinned_model_is_the_one_maintenance_writes(server, monkeypatch):
    """Query and stored vectors must come from the same model or the cosine
    is meaningless."""
    mod, _ = server
    source = (PACKAGE_ROOT / "bin" / "memory-maintenance.py").read_text()
    assert mod.EMBED_MODEL_NAME == "BAAI/bge-small-en-v1.5"
    assert f'model_name="{mod.EMBED_MODEL_NAME}"' in source


# --- the leaked connection -------------------------------------------------


@pytest.mark.parametrize("args", [
    {"action": "recent"},
    {"action": "recall", "query": "x"},
    {"action": "facts", "query": "x"},
    {"action": "nonsense"},
])
def test_memory_tool_closes_its_connection(memory_db, tmp_workspace, monkeypatch, args):
    """The `conn.close()` at the foot of the memory branch was unreachable —
    every action above it returns — so every memory call leaked a sqlite
    connection. Every action must now close, including the unknown-action
    path."""
    conn, _ = memory_db
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    mod = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    add_episode(conn, "something", 7.0)

    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **kw):
        opened.append(real_connect(*a, **kw))
        return opened[-1]

    monkeypatch.setattr(mod.sqlite3, "connect", tracking_connect)
    remove_fastembed(monkeypatch)

    mod.handle_core_tool("memory", args)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    conn.close()


def test_unknown_memory_action_still_errors(server, monkeypatch):
    mod, _ = server
    result = mod.handle_core_tool("memory", {"action": "nonsense"})
    assert "error" in result
