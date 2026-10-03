import sys
import types

import numpy as np
import pytest

from lib.graph import embed


def vec(*head, dim=embed.DIM):
    v = np.zeros(dim, dtype=np.float32)
    v[:len(head)] = head
    return v


def blob(*head):
    return vec(*head).tobytes()


class FakeTextEmbedding:
    constructed = []
    vectors = {}
    kwargs = []

    def __init__(self, model_name=None, **kwargs):
        FakeTextEmbedding.constructed.append(model_name)
        FakeTextEmbedding.kwargs.append(kwargs)

    def embed(self, texts, **kw):
        for t in texts:
            yield vec(*FakeTextEmbedding.vectors.get(t, [1.0]))


class ExplodingTextEmbedding:
    constructed = []

    def __init__(self, model_name=None, **kwargs):
        ExplodingTextEmbedding.constructed.append(model_name)
        raise RuntimeError("model weights not found")


def install_fastembed(monkeypatch, cls, vectors=None):
    cls.constructed = []
    if vectors is not None:
        cls.vectors = vectors
    m = types.ModuleType("fastembed")
    m.TextEmbedding = cls
    monkeypatch.setitem(sys.modules, "fastembed", m)
    return cls


@pytest.fixture(autouse=True)
def _fresh_embedder(monkeypatch):
    for k in ("KARAKOS_SEMANTIC_RECALL", "KARAKOS_RECALL_WEIGHTS", "FASTEMBED_CACHE_PATH",
              "KARAKOS_RECALL_SCAN_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    embed._reset()
    yield
    embed._reset()


@pytest.fixture
def store(tmp_path):
    from lib.graph.store import open_graph
    return open_graph(tmp_path, create=True)
