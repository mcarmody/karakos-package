# Package backend contract

What the dashboard may call on the Karakos package backend (`bin/agent-server.py`) when it runs with `KARAKOS_PROFILE=package`, and what it reads from disk. Written against `mcarmody/karakos-package` `release/2.0` (steps 1.1a/1.1b registry, 1.5 `context_tokens` and `shards`). The package copy of this file lives in that repo's `docs/`; this repo's copy is the source.

The dashboard never talks to a queue broker, `shards.json`, ssh, systemd or tmux under this profile. Those are household-only (see `docs/package-profile-inventory.md`).

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
  "dead_letter_path": "/workspace/data/discord-dead-letter.jsonl"
}
```

`state` is whatever the server holds for the agent: on `release/2.0` that is `IDLE`, `PROCESSING` or `ERROR_RECOVERY`, or `UNKNOWN` for an agent that has not started. Treat it as an open string. `session_id` is truncated to 8 characters. `context_tokens` 0 means unknown. `dead_letters` above 0 means replies were generated and not delivered.

### `GET /agents`

The roster. Source of the dashboard's agent list, chat picker and `context_tokens`.

```json
{
  "agents": [
    {
      "name": "alpha",
      "context_tokens": 4200,
      "shards": { "alpha": { "context_tokens": 4200 } },
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
- `shards`: shard id -> `{context_tokens}` (1.5). Until 2.1 the only shard is the agent itself.
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

Rate-limit headroom per agent. Example values are illustrative; types follow `handle_usage`. (On `release/2.0` the rows are still keyed by agent; the plan keys `rate_limit_state` by window type, so expect this shape to be revisited.)

```json
{ "agents": { "alpha": {
    "status": "allowed", "rate_limit_type": "five_hour", "resets_at": 1791020000,
    "is_using_overage": false, "overage_status": null,
    "percent_of_window_used": 41.5, "summary": "5h window: 41.5% used",
    "updated_at": "2026-10-03 12:00:00" } } }
```

`summary` is human prose. Fields are `null` when there is no reading yet; `percent_of_window_used: null` is "no reading", never 0.

### Admin actions (used by the agent detail panel)

All `POST`, no body, `200`/`404 {"error": "Unknown agent"}`:

| Endpoint | Result |
|---|---|
| `/agents/{name}/reset` | `{"status": "reset"}`: drops the session |
| `/agents/{name}/reload` | `{"status": "reloaded"}`: re-reads `agents.yaml`, keeps the session; `400` with `problems` if the file is invalid |
| `/agents/{name}/register` | hot-loads an agent just written to `agents.yaml`; `409` if already running |
| `/agents/{name}/kill` | stops the subprocess without respawning |
| `/agents/{name}/flush` | marks queued messages skipped; `{"status": "flushed", "flushed": <count>}` |

## Gaps (the dashboard needs it, the server does not have it)

| # | Need | Today | Owner |
|---|---|---|---|
| G1 | Shard rows: per-shard state, queue depth, spawn status and channels in `/agents` | `shards` is `{shard_id: {context_tokens}}` only; the adapter lays the registry's `channels` over it | 2.1 (shards spawn). The adapter passes unknown shard keys through, so no dashboard change is needed when 2.1 adds them |
| G2 | Hive call log: which agent called which, when, outcome | No endpoint | 2.3. Needed by the 5.4 fleet/hive page |
| G3 | Per-agent last message, `messages_processed`, session age and compaction count for the roster | Not in `/agents` or `/health`; the roster shows blanks/zeros under this profile | Unowned; propose adding to `/agents` alongside 2.1 |
| G4 | A cost endpoint the dashboard can use instead of opening the sqlite file | `GET /cost` exists, but `app/api/cost` (and `finance/usage-timeseries`, `conversations/metrics`, `chat/history|result|stream`, `history/*`) read `agent-server.db` directly via `AGENT_SERVER_DB_PATH`, which needs the file mounted into the dashboard container and the sqlite drivers | 5.3 (image build) decides: mount the DB, or 5.1 re-points these routes at HTTP. Time series and chat history have no HTTP endpoint at all |
| G5 | Server-side session/transcript replay for `chat/stream` | Read from the sqlite file only | Same decision as G4 |
| G6 | Memory browser data | No endpoint | 5.5 |

## Existing dashboard calls that do not match this server

These already exist in the household dashboard and are not changed by 5.0. They are household-era paths the package server does not serve; 5.1 either gates the route or moves it to the path above.

| Dashboard calls | Package server has |
|---|---|
| `POST /interrupt` with `{agent, reason}` (`app/api/agents/[name]/interrupt`) | `POST /agents/{name}/interrupt` |
| `GET /queue/{name}`, `DELETE /queue/{name}/{id}` (`app/api/agents/[name]/queue`) | `GET /agents/{name}/queue`, `DELETE /agents/{name}/queue/{queue_id}` |
| `GET /status` (`app/api/sys`) | `/health` and `/agents` |
| `POST /flush` (`app/api/sys`) | `POST /agents/{name}/flush` |
| `GET /reviews` (`app/api/reviews`) | none |
