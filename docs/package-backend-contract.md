# Package backend contract

What the dashboard (`dashboard/`) may call on the Karakos package backend (`bin/agent-server.py`), and what it reads from disk. Written against `release/2.0` (steps 1.1a/1.1b registry, 1.5 `context_tokens`, 2.1 shard rows, 2.3 hive call log, 2.7 usage sections). Paths below that start with `app/` or `lib/` are relative to `dashboard/`.

The dashboard never talks to a queue broker, ssh, systemd or tmux: every call goes through `agentFetch` to the agent server.

## Connection and auth

| Setting | Meaning | Default |
|---|---|---|
| `AGENT_SERVER_URL` | Base URL of the agent server | `http://localhost:18791` |
| `AGENT_SERVER_TOKEN` | Shared secret | none (all calls 401 without it) |
| `KARAKOS_REGISTRY_PATH` | Path to `config/agents.yaml` (read only) | `$WORKSPACE_ROOT/config/agents.yaml`, `WORKSPACE_ROOT` defaulting to `/workspace` |

Every endpoint below takes `Authorization: Bearer <AGENT_SERVER_TOKEN>`. A missing or wrong token returns `401 {"error": "Unauthorized"}`. Bodies are JSON. All endpoints are served by the one agent-server process; there is no second service.

The `/message` and `/agents` shapes are in the `release/2.0` stability contract (`docs/EXTENDING.md`): they will not change shape without a deprecation. Everything else here may change on `release/2.0`; the dashboard tolerates extra keys.

## Endpoints the dashboard uses

Example bodies are illustrative values with the types the handlers produce.

### `GET /health`

Liveness for the nav header and the roster's per-agent liveness and queue depth.

```json
{
  "status": "healthy",
  "uptime_seconds": 8123,
  "queue_depth": 2,
  "agents": {
    "alpha": {
      "state": "IDLE",
      "alive": true,
      "queue_depth": 2,
      "session_id": "abcd1234",
      "context_tokens": 4200,
      "shards": { "alpha": { "context_tokens": 4200 } }
    }
  },
  "dead_letters": 0,
  "dead_letter_path": "/workspace/data/outbox/outbox.db",
  "outbox": { "pending": 0, "sending": 0, "dead": 0, "oldest_pending_age_s": null }
}
```

`state` is whatever the server holds for the agent: on `release/2.0` that is `IDLE`, `PROCESSING` or `ERROR_RECOVERY`, or `UNKNOWN` for an agent that has not started. Treat it as an open string. `session_id` is truncated to 8 characters. `context_tokens` 0 means unknown. `dead_letters` above 0 means replies were generated and not delivered (outbox rows that gave up). `dead_letter_path` is deprecated and now names the outbox file.

`/health` keeps its per-agent `shards` as a map `{shard_id: {context_tokens}}`; it is not the shard list. The dashboard reads shard rows from `/agents` only and uses `/health` for `alive`, `session_id` and the queue depth of an agent that reports no shard rows.

### `GET /agents`

The roster. Source of the dashboard's agent list, chat picker, `context_tokens` and shard rows.

```json
{
  "agents": [
    {
      "name": "alpha",
      "context_tokens": 4200,
      "shards": [
        {
          "id": "alpha", "is_default": true, "state": "IDLE", "alive": true,
          "pid": 4101, "session_id": "abcd1234", "queue_depth": 0,
          "context_tokens": 4200, "channels": ["general"], "last_channel": "general",
          "paused": null
        },
        {
          "id": "alpha-ops", "is_default": false, "state": "IDLE", "alive": true,
          "pid": 4102, "session_id": "ef567890", "queue_depth": 2,
          "context_tokens": 0, "channels": ["ops"], "last_channel": "ops",
          "paused": { "reason": "budget", "until": 1791020000 }
        }
      ],
      "model": "opus",
      "max_turns": 200,
      "timeout": 10800,
      "state": "IDLE",
      "has_discord_token": true,
      "dashboard_chat": true,
      "label": "Alpha"
    }
  ]
}
```

- `context_tokens`: max over the agent's shards; `0` = unknown (1.5).
- `shards`: a list of rows (2.1), not the 1.5 dict `{shard_id: {context_tokens}}`. `state` is an open string (`IDLE`, `PROCESSING`, `ERROR_RECOVERY`, `UNKNOWN`). `paused` is `null` or `{reason, until}` (2.7): `reason` is `breaker`, `budget` or `governor`, `until` is epoch seconds or `null` (held until usage drops). A shard with no `channels` has none configured. The dashboard also accepts the 1.5 dict from an older server (one row per key, other fields defaulted) and passes unknown row keys through.
- `dashboard_chat: false` marks a relay agent not meant for direct chat. `label` defaults to the agent id.

### `config/agents.yaml` (file, read only)

The dashboard reads the registry the server also reads (`lib/registry.py`, schema `version: 2`) for what `/agents` does not carry: `role`, shard ids with their `channels`, and agents declared but not yet registered with the server. It never writes it and ignores keys it does not know.

```yaml
version: 2
agents:
  alpha:
    name: alpha
    role: primary        # primary | monitor | builder | reviewer | custom
    label: Alpha
    shards:
      - id: alpha
        channels: [general]
      - id: alpha-2
        channels: [ops]
```

An agent with no `shards:` key has one shard named after the agent and no channels.

### `POST /message`

Chat and operator messages into an agent's queue.

```json
{ "agent": "alpha", "content": "hello", "author": "dashboard", "author_id": "0",
  "channel": "dashboard", "channel_id": "0", "server": "local", "message_id": "msg-123" }
```

Only `agent` and `content` (or `attachments`) are required. `202 {"status": "queued", "message_id": "msg-123"}`; a repeated `message_id` returns `202 {"status": "duplicate", ...}`. Errors: `400 {"error": "Invalid agent" | "Empty content"}`, `429 {"error": "Cost limit exceeded", "reason": "..."}` (with `Retry-After`; not applied when `server` is `"local"`), `503 {"error": "Queue full"}`.

### `POST /agents/{name}/interrupt`

Stop the current generation. No body.

```json
{ "status": "interrupted", "interrupted": true }
```

`{"status": "idle", "interrupted": false}` if nothing was running (still `200`). `404 {"error": "Unknown agent"}`.

### `GET /agents/{name}/queue`, `DELETE /agents/{name}/queue/{queue_id}`

What the agent still has to answer, and cancel of one not-yet-started message.

```json
{ "agent": "alpha",
  "messages": [
    { "id": 41, "channel": "general", "author": "sam", "content": "first 200 chars...",
      "content_full_length": 512, "created_at": "2026-10-03 12:00:00", "state": "pending" }
  ] }
```

`state` is `pending` or `processing`. `DELETE` returns `{"status": "cancelled", "cancelled": true, "id": 41}`, or `404 {"error": "No queued message with that id", "cancelled": false}` (unknown, another agent's, or already picked up; use interrupt for in-flight work).

### `GET /cost`

Cost summary for every agent: the dashboard's cost page and limits.

```json
{ "daily": { "alpha": 0.42 }, "monthly": { "alpha": 12.8 },
  "limits": { "daily_limit": 25.0, "monthly_limit": 500.0 } }
```

`daily` is the trailing 24 hours, `monthly` the trailing 30 days, in USD, keyed by agent id. `GET /cost/{agent}` returns `{"agent", "daily", "monthly", "session"}` for one agent.

### `GET /cost/conversations[?agent=alpha]`

Rollup by conversation (one context window = agent + session). Newest first, at most 200.

```json
{ "conversations": [
    { "agent": "alpha", "session_id": "abcd1234", "cost": 0.42, "input_tokens": 1200,
      "output_tokens": 800, "duration_ms": 91000, "turns": 6,
      "started_at": "2026-10-03 11:00:00", "ended_at": "2026-10-03 11:30:00",
      "current": true }
] }
```

Rows from before `session_id` existed show `"session_id": "unknown"`. `current` is true for the live session. The dashboard ignores keys it does not know.

### `GET /usage`

Rate-limit headroom and the pause state behind it. Example values are illustrative; types follow `handle_usage` and `usage_gate.usage_report`.

```json
{
  "agents": { "alpha": {
    "status": "allowed", "rate_limit_type": "five_hour", "resets_at": 1791020000,
    "is_using_overage": false, "overage_status": null,
    "percent_of_window_used": 41.5, "summary": "5h window: 41.5% used",
    "updated_at": "2026-10-03 12:00:00" } },
  "windows": {
    "five_hour": { "status": "allowed", "resets_at": 1791020000, "utilization_pct": 41,
                   "percent_of_window_used": 41.5, "updated_at": "2026-10-03 12:00:00" },
    "seven_day": { "status": "allowed", "resets_at": 1791400000, "utilization_pct": 73,
                   "percent_of_window_used": 12.0, "updated_at": "2026-10-03 12:00:00" }
  },
  "breaker": { "paused": false, "until": null, "types": [] },
  "budgets": { "alpha": { "used": 900000, "budget": 1000000, "paused_since": null, "until": null } },
  "governor": { "weekly_pct": 73, "enabled": true, "policy_broken": false }
}
```

- `agents` is unchanged: per agent, the account's worst window. `summary` is human prose. Fields are `null` when there is no reading yet; `percent_of_window_used: null` is "no reading", never 0.
- `windows` (2.7) is keyed by `rate_limit_type`. `utilization_pct` is the account's consumption of that window (`null` = no reading); `resets_at` is epoch seconds.
- `breaker`: `paused` is true while the account limit is rejecting dispatch; `until` is epoch seconds or `null`; `types` names the windows holding it.
- `budgets`: one entry per agent that has a token budget (agents without one are absent). `used` and `budget` count uncached input plus output tokens in the budget window; `paused_since` and `until` are epoch seconds or `null`.
- `governor`: `weekly_pct` is the weekly window utilisation or `null`; `enabled` is false when the policy is off or broken; `policy_broken` is true when `config/governor.yaml` is invalid.

The dashboard serves this through `GET /api/usage`, trimming `agents` and `budgets` to the account's agent allowlist (`windows`, `breaker` and `governor` are account facts).

### `GET /hive/calls`

The log of hive calls between shards (2.3), newest first. Full field and status tables are in the package's `docs/hive-call-log.md`.

| Query | Meaning |
|---|---|
| `limit` | 1 to 500, default 100 |
| `since` | ISO time; calls created at or after it |
| `shard` | calls where this shard is the caller or the callee |
| `status` | `pending`, `answered`, `expired`, `error`, `timeout` or `abandoned` |

```json
{ "calls": [{
  "call_id": "c-3f9a1b2c4d5e", "from": "alpha", "to": "alpha-ops",
  "from_agent": "alpha", "to_agent": "alpha", "depth": 1, "status": "answered",
  "created_at": "2026-10-03 10:00:00", "started_at": "2026-10-03 10:00:01",
  "answered_at": "2026-10-03 10:00:04", "duration_ms": 3120,
  "question": "first 200 characters of the question",
  "answer": "first 200 characters of the answer", "error": null }] }
```

`from` and `to` are shard ids; `from_agent` and `to_agent` are the agents they belong to. Times are UTC `YYYY-MM-DD HH:MM:SS`. The dashboard serves this through `GET /api/hive/calls`, which validates the four filters (unknown parameters are dropped) and returns only calls whose `from_agent` and `to_agent` are both allowed for the account, because `question` and `answer` carry message text.

### Admin actions (used by the agent detail panel)

All `POST`, no body, `200`/`404 {"error": "Unknown agent"}`:

| Endpoint | Result |
|---|---|
| `/agents/{name}/reset` | `{"status": "reset"}`: drops the session |
| `/agents/{name}/reload` | `{"status": "reloaded"}`: re-reads `agents.yaml`, keeps the session; `400` with `problems` if the file is invalid |
| `/agents/{name}/register` | hot-loads an agent just written to `agents.yaml`; `409` if already running |
| `/agents/{name}/kill` | stops the subprocess without respawning |
| `/agents/{name}/flush` | marks queued messages skipped; `{"status": "flushed", "flushed": <count>}` |

### Graph browse (`GET /graph/*`, step 5.5)

Read-only reads of the memory graph for the `/memory` page. Full reference, with every parameter and error code: the package's `docs/graph-browse-api.md`. Keyword search only (FTS5); the endpoints open the graph read-only and never touch `last_seen_at`. Not part of the `release/2.0` stability contract. The dashboard reaches them through `/api/memory/*` (`lib/memoryBackend.ts`), package profile only, 403 for an agent-restricted account.

| Endpoint | Returns |
|---|---|
| `GET /graph/status` | counts, `embed_model`, `last_consolidation` |
| `GET /graph/observations?q=&kind=&entity=&domain=&agent=&state=&limit=&cursor=` | `{observations: [...], next}` |
| `GET /graph/entities?q=&kind=&state=&limit=&cursor=` | `{entities: [...], next}` |
| `GET /graph/entities/{id}` | `{entity, neighbors, counts}`; 404 `unknown_entity` |

```json
{"observations": [{"id": 1, "kind": "fact", "content": "alpha prefers green tea",
  "entity": {"id": 1, "name": "Alpha", "kind": "thing"}, "mentions": [], "importance": 5.0,
  "archived_at": null, "superseded_by": null}], "next": "b:1"}
```

A missing or too-new graph is `503 {"error": "graph_not_initialised", ...}`; the dashboard passes 400, 404 and 503 through and turns anything else into `502`.

## Gaps (the dashboard needs it, the server does not have it)

| # | Need | Today | Owner |
|---|---|---|---|
| G1 | Shard rows: per-shard state, queue depth, spawn status and channels in `/agents` | **Closed by 2.1** (rows in `/agents`) and consumed by 5.4 (`normalizeShards`, shard table). The adapter still lays the registry's `channels` over rows that report none | - |
| G2 | Hive call log: which agent called which, when, outcome | **Closed by 2.3** (`GET /hive/calls`) and consumed by 5.4 (`/api/hive/calls`, hive call log on `/fleet`) | - |
| G3 | Per-agent last message, `messages_processed`, session age and compaction count for the roster | Not in `/agents` or `/health`; the roster shows blanks/zeros | Unowned; propose adding to `/agents` alongside 2.1 |
| G4 | A cost endpoint the dashboard can use instead of opening the sqlite file | `GET /cost` exists, but `app/api/cost` (and `conversations/metrics`, `chat/history|result|stream`, `history/*`) read `agent-server.db` directly via `AGENT_SERVER_DB_PATH`, which needs the file mounted into the dashboard container and the sqlite drivers | Decided: the dashboard shares the container with the agent server and opens the file read-only (`AGENT_SERVER_DB_PATH` in the image). Chat history has no HTTP endpoint |
| G5 | Server-side session/transcript replay for `chat/stream` | Read from the sqlite file only | Same decision as G4 |
| G6 | Memory browser data | **Closed by 5.5**: `GET /graph/*` (package 5.5a) consumed by `/memory` and `/api/memory/*` | - |

## Contract test

`tests/test_agent_server_routes.py` reads every `agentFetch(...)` path in `dashboard/` and checks it against the routes `create_app()` in `bin/agent-server.py` registers (method and path shape). A dashboard route that calls a path the server does not serve fails that test instead of rendering a 404 body as data.
