"""Ranked recall over the graph, plus legacy_blend for 4.4 parity checks."""
import os
import re

import numpy as np

from lib.graph import embed
from lib.graph.store import norm

DEFAULT_WEIGHTS = {"vec": 0.50, "kw": 0.15, "name": 0.15, "imp": 0.20}
FTS_LIMIT = 50
NAME_OBS_CAP = 500
_TOKEN = re.compile(r"\w+", re.UNICODE)


def scan_limit() -> int:
    try:
        return max(1, int(os.environ.get("KARAKOS_RECALL_SCAN_LIMIT", "2000")))
    except ValueError:
        return 2000


def _embed_budget() -> float:
    try:
        return float(os.environ.get("KARAKOS_RECALL_EMBED_BUDGET", "20"))
    except ValueError:
        return 20.0


def parse_weights(spec, base=None) -> dict:
    w = dict(base or DEFAULT_WEIGHTS)
    if isinstance(spec, dict):
        items = spec.items()
    else:
        items = (p.split("=", 1) for p in (spec or "").split(",") if "=" in p)
    for k, v in items:
        k = str(k).strip()
        if k in w:
            try:
                w[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    return w


def _imp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v) / 10.0))
    except (TypeError, ValueError):
        return 0.5


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(kinds, domain, entity_id, alias="o"):
    sql = [f"{alias}.archived_at IS NULL", f"{alias}.superseded_by IS NULL"]
    args = []
    if kinds:
        sql.append(f"{alias}.kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    if domain:
        sql.append(f"{alias}.domain = ?")
        args.append(domain)
    if entity_id is not None:
        sql.append(f"({alias}.entity_id = ? OR {alias}.id IN "
                   "(SELECT observation_id FROM observation_mentions WHERE entity_id = ?))")
        args += [entity_id, entity_id]
    return " AND ".join(sql), args


def _row(r, signals, score):
    return {"id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
            "content": r["content"], "entity_id": r["entity_id"],
            "importance": r["importance"], "domain": r["domain"], "agent": r["agent"],
            "created_at": r["created_at"], "score": round(score, 4),
            "signals": {k: (None if v is None else round(v, 4)) for k, v in signals.items()}}


def _name_matches(conn, query):
    """{entity_id: signal} from exact 1-4 word spans (1.0) and long-token contains (0.6)."""
    words = norm(query).split()
    spans = {" ".join(words[i:i + n]) for n in range(1, 5) for i in range(len(words) - n + 1)}
    hits = {}
    for s in spans:
        for r in conn.execute("SELECT id FROM entities WHERE name_norm=? AND archived_at IS NULL",
                              (s,)):
            hits[r["id"]] = 1.0
        for r in conn.execute("SELECT a.entity_id id FROM entity_aliases a JOIN entities e "
                              "ON e.id=a.entity_id WHERE a.alias_norm=? AND e.archived_at IS NULL",
                              (s,)):
            hits[r["id"]] = 1.0
    for t in {w for w in words if len(w) >= 4}:
        for r in conn.execute("SELECT id FROM entities WHERE name_norm LIKE ? ESCAPE '\\' "
                              "AND archived_at IS NULL", (_like(t),)):
            hits.setdefault(r["id"], 0.6)
    return hits


def recall(store, query, limit=10, kinds=None, entity=None, domain=None,
           mode="auto", weights=None) -> dict:
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 10
    query = (query or "").strip()
    if isinstance(kinds, str):
        kinds = [kinds]
    wts = parse_weights(weights, parse_weights(os.environ.get("KARAKOS_RECALL_WEIGHTS")))

    with store.read() as conn:
        entity_id = None
        if entity is not None:
            erow = store._find_entity(conn, entity)
            if erow is None:
                return {"mode": "keyword", "reason": "unknown_entity", "results": [],
                        "entities": []}
            entity_id = erow["id"]
        where, fargs = _filters(kinds, domain, entity_id)

        if mode == "recent":
            rows = conn.execute(f"SELECT * FROM observations o WHERE {where} "
                                "ORDER BY created_at DESC, id DESC LIMIT ?",
                                (*fargs, limit)).fetchall()
            return {"mode": "recent",
                    "results": [_row(r, {"imp": _imp01(r["importance"])}, 0.0) for r in rows],
                    "entities": []}

        if not query:
            rows = conn.execute(f"SELECT * FROM observations o WHERE {where} "
                                "ORDER BY importance DESC, id DESC LIMIT ?",
                                (*fargs, limit)).fetchall()
            return {"mode": "keyword", "reason": "empty_query",
                    "results": [_row(r, {"imp": _imp01(r["importance"])},
                                     _imp01(r["importance"])) for r in rows],
                    "entities": []}

        # --- decide vector availability (ask the DB before the model) ---
        reason = None
        qvec = None
        scan_rows = []
        if mode == "keyword":
            reason = "keyword_requested"
        elif os.environ.get("KARAKOS_SEMANTIC_RECALL", "1").strip().lower() in (
                "0", "false", "no", "off", ""):
            reason = "disabled"
        else:
            scan_rows = conn.execute(
                f"SELECT * FROM observations o WHERE {where} AND embedding IS NOT NULL "
                "ORDER BY importance DESC, id DESC LIMIT ?", (*fargs, scan_limit())).fetchall()
            if not scan_rows:
                reason = "no_embeddings"
            elif not embed.embedder_available():
                reason = "embedder_unavailable"
            else:
                vecs = embed.embed_texts([query], budget_s=_embed_budget())
                qv = embed.decode(vecs[0]) if vecs else None
                if qv is None:
                    reason = "embedder_unavailable"
                else:
                    qvec = qv

        cand = {r["id"]: r for r in scan_rows} if qvec is not None else {}

        # --- FTS ---
        kw_raw = {}
        tokens = _TOKEN.findall(query)
        if tokens:
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            try:
                for r in conn.execute(
                        f"SELECT o.*, bm25(observations_fts) AS bm FROM observations_fts "
                        f"JOIN observations o ON o.id = observations_fts.rowid "
                        f"WHERE observations_fts MATCH ? AND {where} ORDER BY bm LIMIT ?",
                        (match, *fargs, FTS_LIMIT)):
                    kw_raw[r["id"]] = -float(r["bm"])
                    cand.setdefault(r["id"], r)
            except Exception:
                kw_raw = {}
        kw = {}
        if kw_raw:
            lo, hi = min(kw_raw.values()), max(kw_raw.values())
            for i, v in kw_raw.items():
                kw[i] = 1.0 if hi == lo else (v - lo) / (hi - lo)
        elif query:
            # substring floor (the 1.x behaviour) when FTS finds nothing
            for r in conn.execute(f"SELECT * FROM observations o WHERE {where} AND "
                                  "content LIKE ? ESCAPE '\\' ORDER BY importance DESC LIMIT ?",
                                  (*fargs, _like(query), FTS_LIMIT)):
                kw[r["id"]] = 0.5
                cand.setdefault(r["id"], r)

        # --- entity names ---
        ent_hits = _name_matches(conn, query)
        name_sig = {}
        for eid, sig in ent_hits.items():
            for r in conn.execute(
                    f"SELECT * FROM observations o WHERE {where} AND (o.entity_id = ? OR o.id IN "
                    "(SELECT observation_id FROM observation_mentions WHERE entity_id = ?)) "
                    "ORDER BY importance DESC LIMIT ?", (*fargs, eid, eid, NAME_OBS_CAP)):
                name_sig[r["id"]] = max(name_sig.get(r["id"], 0.0), sig)
                cand.setdefault(r["id"], r)
        ents = []
        for eid, sig in sorted(ent_hits.items(), key=lambda x: -x[1])[:5]:
            e = conn.execute("SELECT id, name, kind, summary FROM entities WHERE id=?",
                             (eid,)).fetchone()
            ents.append({**dict(e), "signal": sig})

    # --- score ---
    scored = []
    for i, r in cand.items():
        sig = {"vec": None, "kw": kw.get(i, 0.0), "name": name_sig.get(i, 0.0),
               "imp": _imp01(r["importance"])}
        if qvec is not None:
            v = embed.decode(r["embedding"])
            if v is not None:
                sig["vec"] = (embed.cosine(qvec, v) + 1.0) / 2.0
        present = {k: wts[k] for k, v in sig.items() if v is not None}
        total = sum(present.values())
        score = sum(wts[k] * sig[k] for k in present) / total if total > 0 else 0.0
        try:
            imp = float(r["importance"])
        except (TypeError, ValueError):
            imp = 0.0
        scored.append((score, imp, i, _row(r, sig, score)))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    out = {"mode": "hybrid" if qvec is not None else "keyword",
           "results": [t[3] for t in scored[:limit]], "entities": ents}
    if qvec is None:
        out["reason"] = reason or "embedder_unavailable"
    return out


def legacy_blend(store, query, limit=10) -> dict:
    """1.x episode recall: 0.75 * vec01 + 0.25 * importance/10; LIKE fallback."""
    limit = max(1, min(int(limit), 100))
    query = (query or "").strip()
    base = "kind='episode' AND archived_at IS NULL"

    def keyword(conn, reason):
        rows = conn.execute(f"SELECT id, content, importance, created_at FROM observations "
                            f"WHERE {base} AND content LIKE ? ORDER BY importance DESC LIMIT ?",
                            (f"%{query}%", limit)).fetchall()
        return {"mode": "keyword", "reason": reason,
                "episodes": [{"id": r["id"], "summary": r["content"],
                              "importance": r["importance"], "created_at": r["created_at"]}
                             for r in rows]}

    with store.read() as conn:
        if not query:
            return keyword(conn, "empty_query")
        if os.environ.get("KARAKOS_SEMANTIC_RECALL", "1").strip().lower() in (
                "0", "false", "no", "off", ""):
            return keyword(conn, "disabled")
        rows = conn.execute(f"SELECT id, content, importance, created_at, embedding "
                            f"FROM observations WHERE {base} AND embedding IS NOT NULL "
                            "ORDER BY importance DESC LIMIT ?", (scan_limit(),)).fetchall()
        if not rows:
            return keyword(conn, "no_embeddings")
        vecs = embed.embed_texts([query], budget_s=_embed_budget())
        qv = embed.decode(vecs[0]) if vecs else None
        if qv is None:
            return keyword(conn, "embedder_unavailable")
        scored = []
        for r in rows:
            v = embed.decode(r["embedding"])
            if v is None:
                continue
            cos = embed.cosine(qv, v)
            scored.append({"id": r["id"], "summary": r["content"],
                           "importance": r["importance"], "created_at": r["created_at"],
                           "similarity": round(cos, 4),
                           "score": round(0.75 * ((cos + 1.0) / 2.0)
                                          + 0.25 * _imp01(r["importance"]), 4),
                           "match": "semantic"})
        if not scored:
            return keyword(conn, "no_usable_embeddings")
        seen = {e["id"] for e in scored}
        for r in conn.execute(f"SELECT id, content, importance, created_at FROM observations "
                              f"WHERE {base} AND embedding IS NULL AND content LIKE ? "
                              "ORDER BY importance DESC LIMIT ?", (f"%{query}%", limit)):
            if r["id"] in seen:
                continue
            scored.append({"id": r["id"], "summary": r["content"],
                           "importance": r["importance"], "created_at": r["created_at"],
                           "similarity": None,
                           "score": round(0.75 * 0.75 + 0.25 * _imp01(r["importance"]), 4),
                           "match": "keyword"})
        scored.sort(key=lambda e: (e["score"], float(e["importance"] or 0)), reverse=True)
        return {"episodes": scored[:limit], "mode": "semantic", "scanned": len(rows)}
