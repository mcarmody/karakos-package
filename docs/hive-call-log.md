# Hive call log (`GET /hive/calls`)

The log of hive calls between shards (step 2.3). It is a view over `message_queue`:
every call row (`call_id` and `reply_to_agent` set) left-joined to its reply row
(same `call_id`, `reply_to_agent` NULL). No table, no schema change. Buzzes are
ordinary rows and are not in it.

## Request

`GET /hive/calls` with `Authorization: Bearer <AGENT_SERVER_TOKEN>`.

| Query | Meaning |
|---|---|
| `limit` | 1 to 500, default 100 |
| `since` | ISO time; calls created at or after it |
| `shard` | calls where this shard is the caller or the callee |
| `status` | one of the statuses below |

## Response

Newest first.

```json
{"calls": [{
  "call_id": "c-3f9a1b2c4d5e", "from": "a", "to": "b",
  "from_agent": "a", "to_agent": "b", "depth": 1, "status": "answered",
  "created_at": "2026-10-03 10:00:00", "started_at": "2026-10-03 10:00:01",
  "answered_at": "2026-10-03 10:00:04", "duration_ms": 3120,
  "question": "first 200 characters of the question",
  "answer": "first 200 characters of the answer", "error": null}]}
```

`from` and `to` are shard ids. `created_at`, `started_at` and `answered_at` are UTC
(`YYYY-MM-DD HH:MM:SS`), `started_at` is null until the callee claims the row.
`duration_ms` comes from the reply body when present, else from the timestamps.
`answer` and `error` are null until there is a reply row.

## Statuses

| Status | When | Example fields |
|---|---|---|
| `pending` | call row queued or in progress and the server still holds it open | `answer: null, error: null` |
| `answered` | reply row with an `answer` | `answer: "42", error: null` |
| `expired` | reply body `{"error":"expired"}` (1.2 expired the queued row) | `answer: null, error: "expired"` |
| `error` | reply body `callee_error`, `callee_failed` or `callee_paused`, or the call row CRASHED | `error: "callee_error"` |
| `timeout` | row skipped as `cancelled` (caller gave up or its turn ended), a `late` reply, or still open past its deadline | `error: "cancelled"` |
| `abandoned` | row skipped as `abandoned` (server restart), or not terminal and not open | `error: "abandoned"` |

Reply bodies are JSON: `{"call_id","answer"}` or `{"call_id","error","detail"?}`
with `error` one of `expired`, `callee_error`, `callee_failed`, `cancelled`,
`callee_paused` (reserved for the budget pause).

## Related routes

- `POST /hive/buzz` `{from, to, message}`: 202 `{"status":"queued","to","message_id"}`.
- `POST /hive/call` `{from, to, question, timeout?}`: 202 `{"call_id","to","depth","deadline"}`.
- `GET /hive/call/{call_id}?wait=<s>`: long poll; `answered`, `expired`, `error`,
  `timeout` or `pending`.
- `POST /hive/call/{call_id}/cancel`.

Refusals carry `{"error": <code>, "detail"?}`: `unknown_caller` 404, `unknown_target`
404, `caller_not_in_turn` 409, `depth_exceeded` 409, `self_call` 409, `deadlock` 409,
`no_available_shard` 409, `callee_paused` 409, `buzz_limit` 429, `queue_full` 503.
