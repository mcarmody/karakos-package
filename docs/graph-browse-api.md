# Graph browse API (`GET /graph/*`)

Read-only HTTP read path for the memory graph (`data/memory/graph.db`), served by the
agent server for the dashboard's memory browser (step 5.5). All four routes are `GET`
and need `Authorization: Bearer <AGENT_SERVER_TOKEN>`.

**Read-only.** The handlers open the database with `mode=ro` and `PRAGMA query_only=ON`,
never create a schema, never touch `last_seen_at`, and never select the `embedding`
columns. They run in the server's executor, so a slow query does not block other routes.

**Not part of the `release/2.0` stability contract.** These endpoints may change on
`release/2.0`; 7.2 documents them in EXTENDING.

**Keyword search only.** `q` uses FTS5 (`observations_fts`), built the way `recall`
builds it: word tokens, each double-quoted, joined with `OR`, ranked by `bm25`. The
semantic half of `recall` needs the embedding model in-process and stays available to
agents through the `memory` tool.

## Errors

| Status | `error` | When |
|---|---|---|
| 401 | `Unauthorized` | missing or wrong bearer token (the server's usual 401 body) |
| 400 | `bad_request` | bad `kind`, `state`, `entity`, `limit`, `cursor`, or an over-long `q`/`domain`/`agent` |
| 404 | `unknown_entity` | `/graph/entities/{id}` with no such entity |
| 503 | `graph_not_initialised` | no graph file, wrong schema, or schema newer than this code. `detail` is `memory graph is not initialised; run: karakos migrate` |

Error body: `{"error": "<code>", "detail": "<text>"}`.

## `GET /graph/status`

Counts for a page header. `observations` counts active rows by kind; `archived` counts
rows with `archived_at`; `superseded` counts rows with `superseded_by` and no
`archived_at`. `last_consolidation` is the stored stats object of the last consolidation
run (opaque; `started` and `finished` are ISO times) or `null`.

```json
{
  "schema": 1,
  "entities": 2,
  "edges": 1,
  "observations": {"fact": 1, "episode": 0, "pattern": 0},
  "archived": 0,
  "superseded": 0,
  "embed_model": "BAAI/bge-small-en-v1.5",
  "last_consolidation": {"started": "2026-10-03T03:00:00Z", "finished": "2026-10-03T03:00:09Z"}
}
```

## `GET /graph/observations`

| Query | Meaning |
|---|---|
| `q` | keyword search, at most 200 characters; no word tokens gives an empty list |
| `kind` | `fact`, `episode` or `pattern` |
| `entity` | integer entity id: matches `entity_id` or a mention. An id that matches nothing gives an empty list |
| `domain`, `agent` | exact match, at most 80 characters |
| `state` | `active` (default: not archived, not superseded), `archived`, `superseded` (superseded and not archived), `all` |
| `limit` | 1 to 100, default 25. Above 100 clamps to 100; 0 or a non-integer is `bad_request` |
| `cursor` | the previous response's `next` |

**Cursors.** Without `q`: newest first (`id` descending), cursor `b:<id>` (rows with an
id below it); `next` is `b:<last id>` while more rows exist, else `null`. With `q`:
ranked, cursor `o:<offset>`; `offset + limit` may not exceed 200, so `next` is `null` at
that cap. A cursor of the wrong form for the request is `bad_request`.

```json
{
  "observations": [{
    "id": 1,
    "kind": "fact",
    "subkind": null,
    "content": "alpha prefers green tea",
    "entity": {"id": 1, "name": "Alpha", "kind": "thing"},
    "mentions": [{"id": 2, "name": "Beta"}],
    "importance": 5.0,
    "confidence": 0.8,
    "domain": "home",
    "agent": "a",
    "channel": null,
    "tags": ["pref"],
    "reinforcement_count": 1,
    "source": "write",
    "created_at": "2026-10-03T12:29:56Z",
    "updated_at": "2026-10-03T12:29:56Z",
    "consolidated_at": null,
    "archived_at": null,
    "superseded_by": null
  }],
  "next": "b:1"
}
```

`entity` is `null` for an observation with no primary entity. `tags` is always a list.

## `GET /graph/entities`

| Query | Meaning |
|---|---|
| `q` | case-folded substring of the name or an alias, at most 200 characters |
| `kind` | exact entity kind |
| `state` | `active` (default), `archived`, `all` |
| `limit`, `cursor` | as above; entities order by name and use `o:<offset>` cursors only |

```json
{
  "entities": [{
    "id": 1,
    "name": "Alpha",
    "kind": "thing",
    "summary": null,
    "importance": 5.0,
    "last_seen_at": "2026-10-03T12:29:56Z",
    "archived_at": null,
    "observation_count": 1,
    "edge_count": 1
  }],
  "next": null
}
```

`observation_count` counts active observations linked by `entity_id` or a mention.

## `GET /graph/entities/{id}`

One entity, up to 50 neighbours (strongest edges first; `direction` is `out` when the
entity is the source), and active observation counts by kind. The entity's observations
come from `/graph/observations?entity=<id>`. `aliases` are the stored normalised forms.

```json
{
  "entity": {
    "id": 1,
    "name": "Alpha",
    "kind": "thing",
    "summary": null,
    "importance": 5.0,
    "last_seen_at": "2026-10-03T12:29:56Z",
    "archived_at": null,
    "created_at": "2026-10-03T12:29:56Z",
    "updated_at": "2026-10-03T12:29:56Z",
    "aliases": ["al"]
  },
  "neighbors": [{"id": 2, "name": "Beta", "kind": "thing", "relation": "knows",
                 "weight": 1.0, "direction": "out"}],
  "counts": {"fact": 1, "episode": 0, "pattern": 0}
}
```
