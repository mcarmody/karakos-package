import sys

import numpy as np

from lib.graph import embed
from tests.graph.helpers import (ExplodingTextEmbedding, FakeTextEmbedding, blob,
                                  install_fastembed)
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)


def test_model_loaded_once_and_cache_dir(monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/x/cache")
    FakeTextEmbedding.kwargs = []
    assert len(embed.embed_texts(["a", "b"])) == 2
    embed.embed_texts(["c"])
    assert FakeTextEmbedding.constructed == [embed.MODEL_NAME]
    assert FakeTextEmbedding.kwargs == [{"cache_dir": "/x/cache"}]


def test_failure_latched(monkeypatch):
    install_fastembed(monkeypatch, ExplodingTextEmbedding)
    assert embed.embed_texts(["a"]) is None
    assert embed.embed_texts(["a"]) is None
    assert not embed.embedder_available()
    assert len(ExplodingTextEmbedding.constructed) == 1


def test_missing_fastembed(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    assert embed.embed_texts(["a"]) is None
    assert embed.embed_texts(["a"]) is None


def test_kill_switch(monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")
    assert embed.embed_texts(["a"]) is None
    assert not embed.embedder_available()
    assert FakeTextEmbedding.constructed == []


def test_budget_respected(monkeypatch):
    import time

    class Slow:
        def __init__(self, model_name=None, **kw):
            time.sleep(0.4)

        def embed(self, texts, **kw):
            return iter([])

    install_fastembed(monkeypatch, Slow)
    t = time.monotonic()
    assert embed.embed_texts(["a"], budget_s=0.1) is None
    assert time.monotonic() - t < 0.3
    time.sleep(0.6)  # let the abandoned loader finish before the autouse reset


def test_availability_without_load(monkeypatch):
    install_fastembed(monkeypatch, FakeTextEmbedding, {})
    assert embed.embedder_available()
    assert FakeTextEmbedding.constructed == []
    assert embed.embedder_available(probe=True)
    assert FakeTextEmbedding.constructed == [embed.MODEL_NAME]


def test_decode_junk():
    assert embed.decode(None) is None
    assert embed.decode(b"") is None
    assert embed.decode(b"abc") is None
    assert embed.decode(np.zeros(3, dtype=np.float32).tobytes()) is None
    assert embed.decode(np.full(embed.DIM, np.nan, dtype=np.float32).tobytes()) is None
    assert embed.decode(blob(1.0)).shape == (embed.DIM,)
    assert embed.cosine(np.zeros(3), np.ones(3)) == 0.0


def test_pinned_model_matches_requirements_and_dockerfile():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert "fastembed==0.3.6" in (root / "requirements.txt").read_text()
    assert embed.MODEL_NAME == "BAAI/bge-small-en-v1.5" and embed.DIM == 384
    assert embed.MODEL_NAME in (root / "Dockerfile").read_text()
