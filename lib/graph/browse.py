"""Read-only browse functions over the graph (step 5.5a).

Pure functions over a GraphStore. Every SELECT goes through store.read_ro(), names
its columns, and leaves the vector columns alone. Nothing here writes, creates a
schema, or calls recall/tool code (which touches last_seen_at).
"""
import json
import re

from lib.graph.schema import GraphNotInitialised
from lib.graph.store import norm, open_graph

KINDS = ("fact", "episode", "pattern")
OBS_STATES = ("active", "archived", "superseded", "all")
ENT_STATES = ("active", "archived", "all")
SEARCH_CAP = 200
NEIGHBOR_LIMIT = 50
TEXT_MAX = 200
FIELD_MAX = 80
NOT_INITIALISED = "memory graph is not initialised; run: karakos migrate"
_TOKEN = re.compile(r"\w+", re.UNICODE)
_CURSOR = re.compile(r"^(b|o):(\d{1,12})$")

OBS_COLS = ("o.id, o.kind, o.subkind, o.content, o.entity_id, o.importance, o.confidence, "
            "o.domain, o.agent, o.channel, o.tags, o.reinforcement_count, o.source, "
            "o.created_at, o.updated_at, o.consolidated_at, o.archived_at, o.superseded_by")
ENT_COLS = ("e.id, e.name, e.kind, e.summary, e.importance, e.last_seen_at, e.archived_at")


class BrowseError(Exception):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _bad(detail):
    return BrowseError(400, "bad_request", detail)


# -- parameters -----------------------------------------------------------------

def parse_limit(v, default=25, lo=1, hi=100) -> int:
    """Non-integer or below `lo` is bad_request; above `hi` clamps to `hi`."""
    if v is None or v == "":
        return default
    try:
        n = int(str(v).strip())
    except ValueError:
        raise _bad("limit must be an integer")
    if n < lo:
        raise _bad(f"limit must be at least {lo}")
    return min(n, hi)


def parse_cursor(v):
    """None -> None; 'b:<id>' / 'o:<offset>' -> (form, int); else bad_request."""
    if v is None or v == "":
        return None
    m = _CURSOR.match(str(v))
    if not m:
        raise _bad("cursor must look like b:<id> or o:<offset>")
    return m.group(1), int(m.group(2))


def _enum(v, allowed, name, default=None):
    if v is None or v == "":
        return default
    if v not in allowed:
        raise _bad(f"{name} must be one of: {', '.join(allowed)}")
    return v


def _text(v, name, maxlen):
    if v is None or v == "":
        return None
    v = str(v)
    if len(v) > maxlen:
        raise _bad(f"{name} is too long (max {maxlen} characters)")
    return v


def _int_id(v, name):
    if v is None or v == "":
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        raise _bad(f"{name} must be an integer")


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _tags(raw):
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(t) for t in v] if isinstance(v, list) else []


def _meta_json(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


# -- status ---------------------------------------------------------------------

def status(store) -> dict:
    with store.read_ro() as conn:
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM observations WHERE archived_at IS NULL "
            "AND superseded_by IS NULL GROUP BY kind")}
        return {
            "schema": int(meta.get("graph_schema", 0)),
            "entities": one("SELECT COUNT(*) FROM entities WHERE archived_at IS NULL"),
            "edges": one("SELECT COUNT(*) FROM edges"),
            "observations": {k: by_kind.get(k, 0) for k in KINDS},
            "archived": one("SELECT COUNT(*) FROM observations WHERE archived_at IS NOT NULL"),
            "superseded": one("SELECT COUNT(*) FROM observations WHERE superseded_by IS NOT NULL "
                              "AND archived_at IS NULL"),
            "embed_model": meta.get("embed_model"),
            "last_consolidation": _meta_json(meta.get("last_consolidation")),
        }


# -- observations ---------------------------------------------------------------

_STATE_SQL = {
    "active": "o.archived_at IS NULL AND o.superseded_by IS NULL",
    "archived": "o.archived_at IS NOT NULL",
    "superseded": "o.superseded_by IS NOT NULL AND o.archived_at IS NULL",
    "all": "1=1",
}


def _obs_row(r, mentions):
    return {
        "id": r["id"], "kind": r["kind"], "subkind": r["subkind"], "content": r["content"],
        "entity": ({"id": r["entity_id"], "name": r["ename"], "kind": r["ekind"]}
                   if r["entity_id"] is not None and r["ename"] is not None else None),
        "mentions": mentions.get(r["id"], []),
        "importance": r["importance"], "confidence": r["confidence"],
        "domain": r["domain"], "agent": r["agent"], "channel": r["channel"],
        "tags": _tags(r["tags"]), "reinforcement_count": r["reinforcement_count"],
        "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"],
        "consolidated_at": r["consolidated_at"], "archived_at": r["archived_at"],
        "superseded_by": r["superseded_by"],
    }


def list_observations(store, q=None, kind=None, entity=None, domain=None, agent=None,
                      state="active", limit=25, cursor=None) -> dict:
    kind = _enum(kind, KINDS, "kind")
    state = _enum(state, OBS_STATES, "state", "active")
    entity = _int_id(entity, "entity")
    domain = _text(domain, "domain", FIELD_MAX)
    agent = _text(agent, "agent", FIELD_MAX)
    q = _text(q, "q", TEXT_MAX)
    q = q.strip() if q else None
    limit = parse_limit(limit)
    cur = parse_cursor(cursor)

    where, args = [_STATE_SQL[state]], []
    if kind:
        where.append("o.kind = ?")
        args.append(kind)
    if domain:
        where.append("o.domain = ?")
        args.append(domain)
    if agent:
        where.append("o.agent = ?")
        args.append(agent)
    if entity is not None:
        where.append("(o.entity_id = ? OR o.id IN "
                     "(SELECT observation_id FROM observation_mentions WHERE entity_id = ?))")
        args += [entity, entity]
    base = (f"FROM observations o LEFT JOIN entities en ON en.id = o.entity_id")
    sel = f"SELECT {OBS_COLS}, en.name AS ename, en.kind AS ekind "
    nxt = None

    with store.read_ro() as conn:
        if q:
            if cur is not None and cur[0] != "o":
                raise _bad("cursor for a search must look like o:<offset>")
            offset = cur[1] if cur else 0
            tokens = _TOKEN.findall(q)
            room = SEARCH_CAP - offset
            if not tokens or room <= 0:
                return {"observations": [], "next": None}
            take = min(limit, room)
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            try:
                rows = conn.execute(
                    sel + base + " JOIN observations_fts ON observations_fts.rowid = o.id "
                    f"WHERE observations_fts MATCH ? AND {' AND '.join(where)} "
                    "ORDER BY bm25(observations_fts), o.id DESC LIMIT ? OFFSET ?",
                    (match, *args, take + 1, offset)).fetchall()
            except Exception:
                return {"observations": [], "next": None}
            if len(rows) > take:
                rows = rows[:take]
                if offset + take < SEARCH_CAP:
                    nxt = f"o:{offset + take}"
        else:
            if cur is not None:
                if cur[0] != "b":
                    raise _bad("cursor for a list must look like b:<id>")
                where.append("o.id < ?")
                args.append(cur[1])
            rows = conn.execute(
                sel + base + f" WHERE {' AND '.join(where)} ORDER BY o.id DESC LIMIT ?",
                (*args, limit + 1)).fetchall()
            if len(rows) > limit:
                rows = rows[:limit]
                nxt = f"b:{rows[-1]['id']}"
        mentions = {}
        if rows:
            ids = [r["id"] for r in rows]
            for m in conn.execute(
                    "SELECT m.observation_id oid, e.id, e.name FROM observation_mentions m "
                    "JOIN entities e ON e.id = m.entity_id WHERE m.observation_id IN "
                    f"({','.join('?' * len(ids))}) ORDER BY e.name_norm, e.id", ids):
                mentions.setdefault(m["oid"], []).append({"id": m["id"], "name": m["name"]})
        return {"observations": [_obs_row(r, mentions) for r in rows], "next": nxt}


# -- entities -------------------------------------------------------------------

_ENT_STATE_SQL = {"active": "e.archived_at IS NULL", "archived": "e.archived_at IS NOT NULL",
                  "all": "1=1"}


def list_entities(store, q=None, kind=None, state="active", limit=25, cursor=None) -> dict:
    state = _enum(state, ENT_STATES, "state", "active")
    kind = _text(kind, "kind", FIELD_MAX)
    q = _text(q, "q", TEXT_MAX)
    limit = parse_limit(limit)
    cur = parse_cursor(cursor)
    if cur is not None and cur[0] != "o":
        raise _bad("cursor for entities must look like o:<offset>")
    offset = cur[1] if cur else 0

    where, args = [_ENT_STATE_SQL[state]], []
    if kind:
        where.append("e.kind = ?")
        args.append(kind)
    n = norm(q) if q else ""
    if n:
        pat = _like(n)
        where.append("(e.name_norm LIKE ? ESCAPE '\\' OR e.id IN (SELECT entity_id FROM "
                     "entity_aliases WHERE alias_norm LIKE ? ESCAPE '\\'))")
        args += [pat, pat]
    with store.read_ro() as conn:
        rows = conn.execute(
            f"SELECT {ENT_COLS}, "
            "(SELECT COUNT(*) FROM observations o WHERE o.archived_at IS NULL AND "
            " o.superseded_by IS NULL AND (o.entity_id = e.id OR o.id IN "
            " (SELECT observation_id FROM observation_mentions WHERE entity_id = e.id))) "
            " AS observation_count, "
            "(SELECT COUNT(*) FROM edges WHERE src_id = e.id OR dst_id = e.id) AS edge_count "
            f"FROM entities e WHERE {' AND '.join(where)} "
            "ORDER BY e.name_norm, e.id LIMIT ? OFFSET ?",
            (*args, limit + 1, offset)).fetchall()
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = f"o:{offset + limit}"
    return {"entities": [{k: r[k] for k in r.keys()} for r in rows], "next": nxt}


def get_entity(store, entity_id) -> dict:
    eid = _int_id(entity_id, "id")
    if eid is None:
        raise _bad("id must be an integer")
    with store.read_ro() as conn:
        row = conn.execute(
            f"SELECT {ENT_COLS}, e.created_at, e.updated_at FROM entities e WHERE e.id = ?",
            (eid,)).fetchone()
        if row is None:
            raise BrowseError(404, "unknown_entity", f"no entity with id {eid}")
        ent = {k: row[k] for k in row.keys()}
        ent["aliases"] = [r[0] for r in conn.execute(
            "SELECT alias_norm FROM entity_aliases WHERE entity_id=? ORDER BY alias_norm",
            (eid,))]
        neighbors = [dict(r) for r in conn.execute(
            "SELECT e.id, e.name, e.kind, ed.relation, ed.weight, 'out' AS direction "
            "FROM edges ed JOIN entities e ON e.id=ed.dst_id WHERE ed.src_id=? "
            "UNION ALL "
            "SELECT e.id, e.name, e.kind, ed.relation, ed.weight, 'in' "
            "FROM edges ed JOIN entities e ON e.id=ed.src_id WHERE ed.dst_id=? "
            "ORDER BY weight DESC LIMIT ?", (eid, eid, NEIGHBOR_LIMIT))]
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT o.kind, COUNT(*) n FROM observations o WHERE o.archived_at IS NULL "
            "AND o.superseded_by IS NULL AND (o.entity_id = ? OR o.id IN "
            "(SELECT observation_id FROM observation_mentions WHERE entity_id = ?)) "
            "GROUP BY o.kind", (eid, eid))}
    return {"entity": ent, "neighbors": neighbors,
            "counts": {k: by_kind.get(k, 0) for k in KINDS}}


# -- entry point ----------------------------------------------------------------

def handle(route, query, data_dir, entity_id=None):
    """(status, body) for one request. `route`: status | observations | entities |
    entity (with `entity_id`, or pass ("entity", id))."""
    if isinstance(route, (tuple, list)):
        route, entity_id = route[0], route[1]
    q = query if query is not None else {}
    try:
        store = open_graph(data_dir, create=False)
        if route == "status":
            return 200, status(store)
        if route == "observations":
            return 200, list_observations(
                store, q=q.get("q"), kind=q.get("kind"), entity=q.get("entity"),
                domain=q.get("domain"), agent=q.get("agent"), state=q.get("state") or "active",
                limit=q.get("limit"), cursor=q.get("cursor"))
        if route == "entities":
            return 200, list_entities(
                store, q=q.get("q"), kind=q.get("kind"), state=q.get("state") or "active",
                limit=q.get("limit"), cursor=q.get("cursor"))
        if route == "entity":
            return 200, get_entity(store, entity_id)
        raise BrowseError(404, "not_found", f"unknown route {route!r}")
    except GraphNotInitialised:
        return 503, {"error": "graph_not_initialised", "detail": NOT_INITIALISED}
    except BrowseError as e:
        return e.status, {"error": e.code, "detail": e.detail}
