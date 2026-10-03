"""Embedding access: fastembed bge-small, float32 blobs, never raises to callers."""
import importlib.util
import os
import sys
import threading
import time

import numpy as np

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384

_lock = threading.Lock()
_embedder = None
_failed = False


def _enabled() -> bool:
    return os.environ.get("KARAKOS_SEMANTIC_RECALL", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


def _reset() -> None:  # tests
    global _embedder, _failed
    _embedder, _failed = None, False


def get_embedder():
    """Cached TextEmbedding or None. Failure is latched, not re-probed."""
    global _embedder, _failed
    if _embedder is not None:
        return _embedder
    if _failed or not _enabled():
        return None
    with _lock:
        if _embedder is not None:
            return _embedder
        if _failed:
            return None
        try:
            from fastembed import TextEmbedding
            cache = os.environ.get("FASTEMBED_CACHE_PATH")
            kwargs = {"cache_dir": cache} if cache else {}
            _embedder = TextEmbedding(model_name=MODEL_NAME, **kwargs)
        except Exception as e:  # ImportError, missing weights, onnxruntime
            print(f"[graph] embedding model unavailable ({e}); keyword mode",
                  file=sys.stderr)
            _failed = True
            return None
    return _embedder


def embedder_available(probe: bool = False) -> bool:
    """Cheap check; loads the model only when probe=True."""
    if not _enabled() or _failed:
        return False
    if _embedder is not None:
        return True
    if probe:
        return get_embedder() is not None
    if "fastembed" in sys.modules:
        return sys.modules["fastembed"] is not None
    try:
        return importlib.util.find_spec("fastembed") is not None
    except (ImportError, ValueError):
        return False


def embed_texts(texts, budget_s=None):
    """Return one float32 blob per text, or None if unavailable.

    budget_s is a wall-clock cap: the load runs in a worker thread and is
    abandoned (not latched as failed) if it overruns, and batches stop once
    the budget is spent (-> None).
    """
    texts = list(texts)
    if not texts or not _enabled():
        return None
    deadline = None if budget_s is None else time.monotonic() + budget_s
    model = _embedder
    if model is None:
        if _failed:
            return None
        if deadline is None:
            model = get_embedder()
        else:
            box = {}
            t = threading.Thread(target=lambda: box.update(m=get_embedder()), daemon=True)
            t.start()
            t.join(max(0.0, deadline - time.monotonic()))
            model = box.get("m")
        if model is None:
            return None
    out = []
    try:
        for i in range(0, len(texts), 32):
            if deadline is not None and time.monotonic() > deadline:
                return None
            for v in model.embed(texts[i:i + 32]):
                a = np.asarray(v, dtype=np.float32)
                if a.shape != (DIM,):
                    return None
                out.append(a.tobytes())
    except Exception as e:
        print(f"[graph] embed failed ({e})", file=sys.stderr)
        return None
    return out if len(out) == len(texts) else None


def decode(blob):
    """float32 blob -> ndarray(DIM) or None. Never raises."""
    if not blob:
        return None
    try:
        b = bytes(blob)
        if len(b) != DIM * 4:
            return None
        a = np.frombuffer(b, dtype=np.float32)
        return a if np.isfinite(a).all() else None
    except (TypeError, ValueError):
        return None


def cosine(a, b) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
