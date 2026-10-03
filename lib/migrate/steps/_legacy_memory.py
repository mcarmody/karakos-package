"""Frozen, read-only copy of the 1.x memory recall (mcp/tools-server.py, 1.5).

This is the only place that logic survives. 40_memory runs it over the old
memory.db and compares it with lib.graph.recall.legacy_blend over the migrated
graph (Gate A). Deliberate, documented deviations from the 1.x source, each
there so the comparison measures data arrival and not incidental behaviour:

* rows the migrator skips as malformed (NULL or blank summary) are ignored;
* an embedding blob that is not DIM * 4 bytes is treated as NULL (the graph
  stores it NULL), so such a row matches only literally, as in the graph;
* ties are broken by id ascending, explicitly;
* vector maths uses lib.graph.embed (same decode and cosine as the graph side).

Nothing here writes. Connections are the caller's and must be read-only.
"""
import re

from lib.graph import embed

KEYWORD_MATCH_SIMILARITY = 0.75
IMPORTANCE_WEIGHT = 0.25
SCAN_LIMIT = 2000
_EMBED_BYTES = embed.DIM * 4


def usable_text(v) -> bool:
    return v is not None and str(v).strip() != ""


def _imp01(importance) -> float:
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 5.0
    return max(0.0, min(1.0, importance / 10.0))


def blended_score(similarity01, importance) -> float:
    return (1.0 - IMPORTANCE_WEIGHT) * similarity01 + IMPORTANCE_WEIGHT * _imp01(importance)


def _like(query):
    return f"%{query}%"


def _episodes(conn, where, args):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
    emb = "embedding" if "embedding" in cols else "NULL AS embedding"
    rows = conn.execute(
        f"SELECT id, summary, importance, created_at, {emb} FROM episodes WHERE {where} "
        "ORDER BY importance DESC, id ASC", args).fetchall()
    return [r for r in rows if usable_text(r[1])]


def _effective_null(blob) -> bool:
    return blob is None or len(bytes(blob)) != _EMBED_BYTES


def keyword_recall(conn, query, limit, reason) -> dict:
    rows = _episodes(conn, "summary LIKE ?", (_like(query),))[:limit]
    return {"episodes": [{"id": r[0], "summary": r[1], "importance": r[2],
                          "created_at": r[3]} for r in rows],
            "mode": "keyword", "reason": reason}


def recall_episodes(conn, query, limit, semantic=True) -> dict:
    """1.x recall_episodes over an old memory.db. `semantic=False` forces the
    keyword floor (used when the migrated graph carries no embeddings)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    query = (query or "").strip()
    if not query:
        return keyword_recall(conn, "", limit, "empty_query")
    if not semantic or not embed._enabled():
        return keyword_recall(conn, query, limit, "disabled")

    embedded = [r for r in _episodes(conn, "embedding IS NOT NULL", ())
                if not _effective_null(r[4])][:SCAN_LIMIT]
    if not embedded:
        return keyword_recall(conn, query, limit, "no_embeddings")
    if not embed.embedder_available():
        return keyword_recall(conn, query, limit, "embedder_unavailable")
    vecs = embed.embed_texts([query])
    qv = embed.decode(vecs[0]) if vecs else None
    if qv is None:
        return keyword_recall(conn, query, limit, "embedder_unavailable")

    scored = []
    for r in embedded:
        v = embed.decode(r[4])
        if v is None:
            continue
        cos = embed.cosine(qv, v)
        scored.append({"id": r[0], "summary": r[1], "importance": r[2],
                       "created_at": r[3], "similarity": round(cos, 4),
                       "score": round(blended_score((cos + 1.0) / 2.0, r[2]), 4),
                       "match": "semantic"})
    if not scored:
        return keyword_recall(conn, query, limit, "no_usable_embeddings")

    seen = {e["id"] for e in scored}
    literal = [r for r in _episodes(conn, "summary LIKE ?", (_like(query),))
               if _effective_null(r[4])][:limit]
    for r in literal:
        if r[0] in seen:
            continue
        scored.append({"id": r[0], "summary": r[1], "importance": r[2],
                       "created_at": r[3], "similarity": None,
                       "score": round(blended_score(KEYWORD_MATCH_SIMILARITY, r[2]), 4),
                       "match": "keyword"})

    def key(e):
        try:
            imp = float(e["importance"])
        except (TypeError, ValueError):
            imp = 0.0
        return (e["score"], imp)

    scored.sort(key=key, reverse=True)
    return {"episodes": scored[:limit], "mode": "semantic", "scanned": len(embedded)}


def facts_like(conn, query) -> list:
    """1.x `facts` action: content or subject LIKE the query (no limit here:
    the comparison is of result sets)."""
    q = _like((query or "").strip())
    rows = conn.execute("SELECT id, subject, content FROM facts "
                        "WHERE content LIKE ? OR subject LIKE ? ORDER BY id",
                        (q, q)).fetchall()
    return [r[0] for r in rows if usable_text(r[2])]


_WORD = re.compile(r"\S+")


def query_from(text, rng, words=6) -> str:
    toks = _WORD.findall(str(text))
    if len(toks) <= words:
        return " ".join(toks)
    start = rng.randrange(0, len(toks) - words + 1)
    return " ".join(toks[start:start + words])
