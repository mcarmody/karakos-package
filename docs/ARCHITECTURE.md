# Architecture

Technical reference for the Karakos system. If you only read one section, read
[The two message paths](#the-two-message-paths) — most confusion about this
system comes from assuming replies travel back the way they came in. They
don't.

- [The shape of it](#the-shape-of-it)
- [The two message paths](#the-two-message-paths)
- [Agent server](#agent-server-binagent-serverpy)
- [Relay](#relay-binrelaypy)
- [Scheduler](#scheduler-binschedulerpy)
- [Scheduled one-off work](#scheduled-one-off-work-binoneshotpy)
- [MCP tool servers](#mcp-tool-servers)
- [Dashboard](#dashboard)
- [Agent lifecycle](#agent-lifecycle)
- [Memory](#memory)
- [Protected paths](#protected-paths)
- [Data layout](#data-layout)
- [Known gaps](#known-gaps)

## The shape of it

Everything is **one container**. `tini` is PID 1, it execs `bin/entrypoint.sh`,
and that execs `supervisord`, which starts four long-lived programs. There is
no service mesh and no second container to coordinate with.

```
┌─ container ────────────────────────────────────────────────────────────┐
│  tini (PID 1) → entrypoint.sh → supervisord                            │
│                                                                        │
│   ┌──────────────┐   ┌──────────────┐   ┌───────────┐  ┌────────────┐  │
│   │ agent-server │   │   relay.py   │   │ scheduler │  │ dashboard  │  │
│   │ .py  :18791  │   │              │   │   .py     │  │ next :3000 │  │
│   └──────┬───────┘   └──────┬───────┘   └─────┬─────┘  └──────┬─────┘  │
│          │                  │                 │               │        │
│          │ spawns one per agent               │ pokes         │        │
│          ▼                                    ▼               │        │
│   ┌──────────────┐                     ┌────────────┐         │        │
│   │  claude CLI  │  stdin/stdout       │ bin/*.sh   │         │        │
│   │  stream-json │  ◀───────────────▶  │ bin/*.py   │         │        │
│   └──────┬───────┘                     └────────────┘         │        │
│          │ MCP over stdio                                     │        │
│          ▼                                                    │        │
│   ┌───────────────────────────┐                               │        │
│   │ mcp/tools-server.py       │  ← skills/<name>/tools.json   │        │
│   │ mcp/admin-server.py       │                               │        │
│   └───────────────────────────┘                               │        │
│                                                               │        │
│   ┌───────────────────────────────────────────────────────────▼─────┐  │
│   │  data/ (volume)   logs/ (volume)   inbox/ (volume)              │  │
│   │  config/ agents/ .karakos/  ← bind-mounted from the host        │  │
│   └─────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
        ▲                    ▲                              ▲
   Discord gateway      Discord REST                  Anthropic API
   (relay holds the     (agent-server posts           (via the claude
    one websocket)       replies directly)             CLI subprocess)
```

Supervised programs, from `config/supervisord.conf` — all four `autostart` and
`autorestart`, with **no `priority` set**, so there is no guaranteed start
order:

| Program | Command | `stopwaitsecs` |
|---|---|---|
| `agent-server` | `python3 /workspace/bin/agent-server.py` | 45 |
| `relay` | `python3 /workspace/bin/relay.py` | 10 |
| `dashboard` | `npx next start -p ${DASHBOARD_PORT:-3000}` | 10 |
| `scheduler` | `python3 /workspace/bin/scheduler.py` | 5 |

### Ports

The Dockerfile declares no `EXPOSE`; `config/docker-compose.yml` publishes two:

| Port | Published as | Notes |
|---|---|---|
| `${DASHBOARD_PORT:-3000}` | all host interfaces | The web UI |
| `${AGENT_SERVER_PORT:-18791}` | `127.0.0.1` only | The HTTP API. It binds `0.0.0.0` *inside* the container; the port map is what keeps it off your LAN |

Every agent-server endpoint requires `Authorization: Bearer $AGENT_SERVER_TOKEN`.

### What survives a restart

| Path | Kind |
|---|---|
| `config/`, `agents/`, `.karakos/` | Bind-mounted from your checkout |
| `data/`, `logs/`, `inbox/` | Named Docker volumes |
| `~/.claude`, `~/.claude.json` | Bind-mounted host credentials, read-write so token refresh persists |

Everything else — `bin/`, `mcp/`, `skills/`, `system/`, the built dashboard —
is baked into the image and replaced on upgrade.

## The two message paths

**Discord in is not Discord out.** The relay holds the single gateway
websocket and carries messages *in*. Replies go out over the Discord REST API
straight from the agent server, using that agent's own bot token. The relay
never sees them.

```
IN                                      OUT
Discord ──▶ relay.py                    agent-server ──▶ Discord REST API
              │  POST /message                ▲
              ▼                               │  the same process that
        agent-server ──▶ claude ──────────────┘  read the answer posts it
```

This is why an agent can go quiet in Discord while the dashboard still shows
it working, and why button clicks for `ask_user` come back through the relay
(only it has a gateway connection) while the question itself is posted by the
agent server.

Inbound, in order (`DiscordAdapter.on_message`):

1. The bot's own posts feed the reply gate and stop there.
2. Guilds absent from `config/channels.json` are dropped.
3. The message is captured to `data/messages/messages-YYYY-MM-DD.jsonl`.
4. The target agent is resolved from a bot mention, else the channel's
   `default_agent`.
5. `/clear`, `/reload`, `/status` and `/usage` are handled **inside the relay**
   and never reach an agent.
6. Bot authors pass a guest budget (12 turns by default); humans pass the
   reply gate if the channel sets one.
7. Attachments download (≤25 MB each, ≤10 per message), then `POST /message`.
   On 429, 5xx or a connection failure the payload spools to
   `data/deferred-messages/` and the scheduler retries it every 5 minutes.

Server side, `POST /message` returns **202 immediately** — it queues, it does
not wait. A duplicate `message_id` also returns 202, so a retry is safe. Then
`process_agent_queue` takes the agent's lock, drains up to 20 queued messages
into one turn, writes a single line of stream-json to the subprocess's stdin,
and reads events back off stdout: assistant text accumulates into the queue
row as it arrives, tool events drive the dashboard activity pill and throttled
Discord tool lines (at most 12 a turn, ≥5s apart), and a `result` event closes
the turn with cost and token counts. The reply is posted, the row is marked
complete, and the queue is re-checked before the lock is released.

## Agent server (`bin/agent-server.py`)

The core. Owns the queue, the money, and the Claude subprocesses.

### Subprocess management

One persistent `claude` CLI child per agent, spawned at startup and kept alive:

```
claude -p --input-format stream-json --output-format stream-json
       --model <config.model> --max-turns <config.max_turns|200> --verbose
       --dangerously-skip-permissions --session-id <uuid>
       --system-prompt <text> --settings config/claude-settings.json
       [--append-system-prompt <persona>] [--allowedTools/--disallowedTools]
```

A `respawn_watcher` restarts a subprocess that exits unexpectedly, capped at
**3 respawns per 5 minutes**; past that the agent is left down and a notice is
posted rather than flapping silently.

### Message queue

SQLite table `message_queue` in `data/memory/agent-server.db`:

| `processed` | Meaning |
|---|---|
| `0` | Queued |
| `1` | In progress |
| `2` | Complete |
| `3` | Crashed |
| `4` | Skipped (duplicate, rate-limited, flushed) |

Depth is capped at 50 per agent. Messages arrive from Discord (relay), the
dashboard, `bin/kara`, and `bin/poke.sh`.

On startup, `crash_recovery()` re-marks rows stuck at `1` as `3`, and re-posts
any reply that completed but never made it to Discord.

### HTTP API

All endpoints require the bearer token.

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | System health, agent states, queue depth |
| `/agents` | GET | Agent list with status, model, cost |
| `/message` | POST | Queue a message — returns 202, does not wait |
| `/agents/{name}/reset` | POST | New session; context destroyed |
| `/agents/{name}/reload` | POST | Respawn on the same session; context kept |
| `/agents/{name}/interrupt` | POST | Stop an in-flight turn, discard the partial |
| `/agents/{name}/kill` | POST | Stop and stay down |
| `/agents/{name}/flush` | POST | Drop that agent's queued messages |
| `/agents/{name}/register` | POST | Hot-register a newly created agent |
| `/cost` | POST | Record a cost event |
| `/cost` | GET | Cost across all agents |
| `/cost/{agent}` | GET | Daily and monthly breakdown for one agent |
| `/cost/conversations` | GET | Cost attributed per conversation |
| `/usage` | GET | Rate-limit and token usage |
| `/ask` | POST | Raise an `ask_user` question |
| `/ask/{id}` | GET | Poll its state |
| `/ask/{id}/answer` | POST | Deliver the clicked answer |

There is **no `/status`, no `/queue/*`, and no `/message/{id}/status`** — if
you find a client calling one of those, it is calling a 404.

### Cost control

Every turn's spend is written to `cost_events` per agent. `COST_DAILY_LIMIT`
(default 25.00) and `COST_MONTHLY_LIMIT` (default 500.00) are checked when a
message is **queued**, not when it completes, and `COST_WARNING_THRESHOLD`
(0.75) posts a warning to the signals channel before the cap bites. Messages
from the owner bypass the limits. A separate check warns when the Anthropic
rate-limit headroom runs low.

## Relay (`bin/relay.py`)

The Discord gateway client and the work dispatcher. Two adapters and two
gates:

- **`DiscordAdapter`** — the gateway connection, inbound routing, slash
  commands, attachment download, JSONL capture (a method on this class, not a
  separate adapter).
- **`DispatchAdapter`** — polls `inbox/<agent>/` for work briefs and shells out
  to `bin/invoke-builder.sh` / `bin/invoke-reviewer.sh`. Timeouts: reviewer
  1 hour, builder 6 hours. Concurrency: `MAX_CONCURRENT_BUILDERS=1`,
  `MAX_CONCURRENT_REVIEWERS=2`.
- **`ReplyGate`** — per-channel throttle so agents don't talk over each other.
- **`GuestBudget`** — caps how many turns a *bot* author can consume, 12 by
  default, refilled when a human speaks.

## Scheduler (`bin/scheduler.py`)

Replaces cron, with the container's full environment. The loop ticks every
15 seconds (`SCHEDULER_TICK_SECONDS`) because it also drives the oneshot spool.

| Task | Cadence | Runs |
|---|---|---|
| Heartbeat, primary agent | every 30 min | `bin/heartbeat.sh` → `bin/poke.sh` |
| Heartbeat, relay agent | every 30 min, offset :15 | same |
| Wedge check | every 1 min | `bin/wedge-check.py` |
| Flush deferred messages | every 5 min | `bin/flush-deferred-messages.py` |
| Claude CLI rollback guard | at startup, then hourly | `bin/cli-upgrade-watchdog.sh` |
| Memory maintenance | daily 03:00 | `bin/memory-maintenance.py` |
| Health monitor | daily 04:00 | `bin/health-monitor.py` |
| Data purge | daily 04:30 | `bin/purge-data.py` |
| Update check | Mondays 05:00 | `bin/check-updates.sh` |
| Due one-off work | every tick | `bin/oneshot.py` |

Two of these are deliberately far more frequent than a daily sweep, and it is
worth knowing why.

**Wedge check, every minute.** An agent that is alive but stuck looks healthy
from outside: the process is up, the container is up, and the person waiting
gets nothing. The agent server writes a liveness beacon per agent on every
state change and, throttled to 1/second, on every stream event.
`bin/wedge-check.py` alerts when an agent claims `PROCESSING` while silent for
more than 120 seconds. It alerts through `bin/discord-notify.sh` — the bot
token directly — never through `bin/poke.sh`, because `poke.sh` queues a
message *for an agent*, and the failure being reported is that agents cannot
answer.

**CLI rollback guard, hourly and at startup.** The agent loop runs on the
Claude CLI, which this project does not release and which is replaced under a
running install on every image pull. A bad release installs cleanly, answers
`claude --version`, keeps every health signal green, and simply stops
answering messages. The watchdog notices the installed version has moved away
from the last one this install completed a turn on, runs one probe turn over
the same stream-json wire the agent server uses, and reinstalls the known-good
version if that turn fails. It spends an API call only when the version
actually changed. `bin/upgrade-claude-cli.sh --to 1.2.3` is the same guarantee
for a deliberate upgrade. Both take `--selftest`, which proves the rollback
fires against fake `npm`/`claude` binaries without touching the real install.

## Scheduled one-off work (`bin/oneshot.py`)

The table above is fixed at build time. `bin/oneshot.py` is the primitive that
lets an agent schedule *arbitrary* future work at runtime — so "I'll check back
in ten minutes" is a mechanism rather than a sentence. Agents reach it through
the `schedule` MCP tool; humans and scripts through the CLI.

```
oneshot.py schedule --label check-logs --when 10m --message "check the logs"
oneshot.py list
oneshot.py cancel check-logs
```

Each item is one JSON file in `data/oneshot-spool/` holding the **absolute**
epoch second it is due, never the relative span the caller typed. `data/` is a
volume, so the spool outlives the container that wrote it, and the scheduler
replays it at startup before entering its loop.

There is no systemd in the image, so there are no transient timers to re-arm:
being in the spool *is* being armed. A deadline that passed while the
container was down fires immediately, unless it is more than
`ONESHOT_STALE_AFTER_SECONDS` late (default 24h), in which case it is dropped
with a log line rather than arriving days after it was useful.

## MCP tool servers

`.mcp.json` registers two stdio JSON-RPC servers, started by the Claude CLI as
its own children:

- **`system-tools`** → `mcp/tools-server.py`
- **`karakos-admin`** → `mcp/admin-server.py`

### Tools

| Tool | Does |
|---|---|
| `workspace` | System config, agent registry |
| `session` | Finalize / load session summaries |
| `memory` | Query episodes and facts |
| `schedule` | Schedule, list, cancel future work (see `bin/oneshot.py`) |
| `discord` | Read-only Discord access — channels, history |
| `taskboard` | Task tracking, in `data/taskboard.json` |
| `vault` | Git-backed knowledge store |
| `ask_user` | Put a multiple-choice question to a human and block on the answer |

### Asking the user a question (`bin/ask_handler.py`)

Claude Code's built-in `AskUserQuestion` tool does not exist over this
transport: agents run as `claude -p --input-format stream-json`, and in that
mode the CLI leaves the tool out of the session's tool list entirely, even
with `--allowedTools AskUserQuestion`. There is nothing to intercept in the
output stream, so the bridge is a replacement tool rather than an adapter.

```
agent → ask_user (MCP)  ──POST /ask──▶  agent server ──▶ Discord embed + buttons
              ▲                              ▲                      │
              │                              │                   click
        poll GET /ask/{id}          POST /ask/{id}/answer ◀── relay (gateway)
```

- `bin/ask_handler.py` owns the payload shape and the registry state machine.
  It does no I/O, so all three processes can share it.
- The question is posted under the **relay's** bot token: a component
  interaction is delivered only to the application that sent the message, and
  the relay holds the one gateway connection.
- Only the people whose messages started the turn, plus the owner, can answer.
- While a question is outstanding the agent's beacon reads `AWAITING_USER`,
  which `bin/wedge-check.py` does not treat as an active turn — a person taking
  four minutes to decide is not a wedged agent. It flips back to `PROCESSING`
  the moment the question resolves or expires.

### Skill discovery

`mcp/tools-server.py` scans `skills/*/tools.json` at startup; each skill
supplies tool definitions and scripts, dispatched by subprocess with a
`TOOL_ARGS` environment variable. This is **Karakos's own convention**, not
Claude Code's `SKILL.md` frontmatter feature — a frontmatter-only file under
`skills/` will not load. See [EXTENDING.md](EXTENDING.md).

### Audit trail

Every tool call is logged to `data/mcp-tools-audit.db` with timestamp, tool
name, duration and outcome.

## Dashboard

Next.js, served by `npx next start` on port 3000.

| Page | Description |
|---|---|
| `/` | Agent status cards, uptime, queue depth |
| `/agents` | Per-agent detail — status, cost, model, session reset |
| `/chat` | Direct chat with an agent, streamed |
| `/conversations` | Message feed with channel / human / tool-use filters |
| `/costs` | Spend by agent and by conversation |
| `/system` | Server health, component status |
| `/settings` | Configuration viewer |
| `/login` | Sign-in |

### Authentication

A login form, not HTTP Basic. Credentials are `DASHBOARD_USER` /
`DASHBOARD_PASSWORD`; success sets `karakos_session`, a base64 HMAC-SHA256
cookie with a 24-hour expiry, verified by every API route before it proxies
anything.

### Chat flow

```
Browser ──POST /api/chat──▶ agent-server POST /message   (channel_id "0")
Browser ◀── EventSource /api/chat/stream ── polls the SQLite queue row
```

The stream route is an SSE *response* over a **200 ms poll of
`data/memory/agent-server.db`** — it reads the `response`, `processed` and
`activity` columns of the message's row and emits the string delta. It calls
no agent-server endpoint. It gives up after 5 minutes. `/api/chat/history`
likewise reads the database file directly.

**`channel_id "0"` means headless: do not post to Discord.** Every outbound
Discord path short-circuits on it — the reply, the typing indicator, tool
activity lines, crash notices — and `ask_user` refuses to run. It is also the
default when `/message` omits a channel id, which is what `bin/poke.sh
--silent` and `bin/kara` rely on.

## Agent lifecycle

**Start.** The server initialises the database, sets every agent `IDLE`, runs
crash recovery, then spawns one subprocess per agent. Each gets:
`agents/<agent>/SYSTEM_PROMPT.md`, every file in `agents/<agent>/persona/`
concatenated, and — if the persona directory is empty —
`agents/<agent>/onboarding.md`, so a brand-new agent interviews you instead of
starting blank. If `data/last-session-summary-<agent>.md` exists and is under
24 hours old, it is prepended as `[SESSION RESET]`.

**Stopping a turn, and starting over.** These four are different and are
routinely confused:

| Action | Subprocess | Session id | Context |
|---|---|---|---|
| `reset` | killed, respawned | **new** | destroyed |
| `reload` | killed, respawned | same | preserved |
| `interrupt` | killed, respawned | same | preserved, partial reply discarded |
| `kill` | killed, stays down | same | preserved |

`interrupt` exists because stream-json has no "stop" message: the only way to
end an in-flight turn is to kill the process to force EOF. The agent is
flagged so the partial text is thrown away rather than posted.

**Shutdown.** `SIGTERM` triggers `graceful_shutdown()`, which runs
`bin/summarize-session.py` per agent with a 25-second budget, which is why
`stop_grace_period` is 45 seconds.

## Memory

`data/memory/memory.db`, maintained daily at 03:00 by
`bin/memory-maintenance.py`.

| Table | Columns of note |
|---|---|
| `episodes` | `summary`, `importance` (default 5.0), `channel`, `tags`, `agents`, `embedding` |
| `facts` | `subject`, `content`, `confidence` (0.8), `domain` |
| `patterns` | `agent`, `pattern_type`, `content`, `confidence`, `reinforcement_count` |

The daily pass reads yesterday's message JSONL, scores importance with a Haiku
call, writes episodes, decays importance by `MEMORY_DECAY_RATE` (0.25), drops
anything below `MEMORY_CUTOFF` (6.0), and keeps at most
`MEMORY_MAX_EPISODES` (15) per day.

Embeddings are generated with **`BAAI/bge-small-en-v1.5` via `fastembed`**, 50
episodes a batch, stored as float32 blobs. The model is hardcoded, not
configurable. If `fastembed` is missing the step is skipped rather than
failing the run.

**Recall today is a `LIKE` scan over `summary`, ordered by importance** — the
embeddings are written but nothing reads them back. There is no vector index
and no similarity search. See [Known gaps](#known-gaps).

Separately, `system/hooks/inject-recall.py` runs on `UserPromptSubmit` and
injects a block from `KARAKOS_RECALL_SOURCE` (default `config/recall-source`,
absent unless you create it; a plain file is read verbatim, an executable one
is run with the prompt on stdin). It skips any prompt carrying the
`[KARAKOS_AUTOMATED]` sentinel, and it does not read `memory.db`.

## Protected paths

`config/protected-paths.json`, enforced by a pre-commit hook that
`bin/entrypoint.sh` installs at every start.

**Tier 1 — hard block.** A builder agent cannot commit these at all:

```
system/   config/   .karakos/   Dockerfile
bin/agent-server.py   bin/relay.py   bin/entrypoint.sh   bin/scheduler.py
config/protected-paths.json
```

**Tier 2 — review required:** `bin/`, `agents/templates/`,
`mcp/tools-server.py`.

**Overrides — always writable**, even though they sit under a protected
prefix: `agents/*/persona/`, `agents/*/journal/`, `agents/*/inbox/`. That
carve-out is what lets an agent maintain its own persona and journal without
being handed the keys to its own process lifecycle.

## Data layout

```
data/                                  # named volume
├── memory/
│   ├── agent-server.db                # message_queue, sessions, cost_events,
│   │                                  #   rate_limit_state
│   └── memory.db                      # episodes, facts, patterns, embeddings
├── mcp-tools-audit.db                 # tool_calls
├── messages/
│   └── messages-YYYY-MM-DD.jsonl      # daily capture
├── attachments/<discord_message_id>/  # downloaded files
├── deferred-messages/                 # spooled while the server was down
│   ├── stale/                         # too old to re-fire
│   └── invalid/                       # unparseable
├── oneshot-spool/*.oneshot.json       # agent-scheduled future work
├── health/
│   ├── agents/<agent>.json            # liveness beacons
│   ├── relay.json  scheduler.json
│   ├── mcp-tools.json  memory-maintenance.json
│   ├── claude-cli.json                # known-good CLI version
│   └── wedge-check-state.json
├── taskboard.json
├── discord-dead-letter.jsonl          # replies Discord refused
├── last-session-summary-<agent>.md
└── stop-hook-extensions.json

logs/                                  # named volume
├── agent-server.log  relay.log  scheduler.log  supervisord.log
├── health-alerts.log
├── summarizer-audit.jsonl  git-events.jsonl  hook-events.log
└── session-summaries/<agent>-<ts>.md

inbox/<agent>/                         # named volume — builder/reviewer briefs
```

Note `data/memory/agent-server.db` — the queue database lives under
`memory/`, not at the top of `data/`.

## Known gaps

Documented so you don't spend an evening deciding whether it's your install.
Each is a real defect in the code, not a configuration mistake.

- **Session summaries are not actually produced.** `bin/summarize-session.py`
  reads `logs/agent-streams/`, and nothing writes to that directory. It exits
  non-zero every time, so `data/last-session-summary-<agent>.md` is never
  written and the `[SESSION RESET]` re-injection above never fires in
  practice.
- **`memory.recall` does not use the embeddings** it spends time computing;
  it is a `LIKE` match.
- **Tool-audit retention is a no-op** — `bin/purge-data.py` purges
  `mcp/tool-audit.db`, while the real database is `data/mcp-tools-audit.db`.
- **`bin/capture.py --backfill` reads the wrong path**, `data/agent-server.db`
  rather than `data/memory/agent-server.db`.
- **Four dashboard API routes call endpoints the agent server does not have**
  (`/status`, `/interrupt` at the wrong path, `/queue/*`), and
  `/api/agents/[name]/open-terminal` shells `osascript`, which cannot work in
  a Linux container.
- **The weekly update check does nothing.** `bin/check-updates.sh` reads a
  `package.json` at the workspace root that does not exist, so it exits before
  it reaches the GitHub API; even if it ran, it only writes to stdout, which
  the scheduler discards. Watch the releases page instead.
- **There is no context-budget compaction.** The `sessions` table carries
  `input_tokens`, `compaction_count` and `last_compacted`, and the tokens are
  written after every turn, but nothing reads them back or compares them to a
  threshold. Only the monetary caps are enforced.
