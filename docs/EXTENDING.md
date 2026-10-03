# Extending Karakos

Guide to customizing agents, adding skills, and growing the system.

## Adding a New Agent

```bash
# Inside the container
bin/create-agent.sh --template primary --model sonnet oracle

# With a Discord bot identity
bin/create-agent.sh --template builder --model sonnet \
  --discord-token "$DISCORD_BOT_TOKEN_BUILDER" builder
```

Templates: `primary`, `relay`, `builder`, `reviewer`.

The agent is hot-registered — no server restart needed.

`create-agent.sh` also drops `agents/<name>/onboarding.md` into every new
agent. The agent server injects it as the first turn's prompt **whenever
`persona/` is empty**, so a brand-new agent interviews you about who you are
instead of starting from nothing. Writing your first `persona/` file is
therefore also what switches onboarding off.

## Customizing Agent Personality

Each agent has a persona directory:

```
agents/{name}/
├── SYSTEM_PROMPT.md    # Core instructions (generated from template)
├── onboarding.md       # First-turn prompt, used only while persona/ is empty
├── persona/
│   └── voice.md        # Voice, tone, behavioral rules
├── inbox/              # Incoming work briefs
└── journal/            # Agent-written logs
```

Edit `persona/voice.md` to customize how the agent communicates. This file is loaded fresh on each session start — no restart needed.

### Example voice.md

```markdown
# Voice

## Tone
Direct and concise. No filler words. Technical when appropriate.

## Addressing Style
Call the owner by first name.

## Boundaries
Never discuss politics or religion. Redirect to practical topics.
```

### Prompt composition and migrated agents

Migrated agents keep their own prompt verbatim (`prompt: {core: false, house_style: false}`); turn `core`/`house_style` on after removing the duplicated sections from the prompt file by hand.

## Adding a Skill

Skills add new tools to the MCP server. They're automatically discovered at startup.

**This is not Claude Code's built-in Agent Skills feature.** Claude Code has
its own native skill system: a `SKILL.md` file with YAML frontmatter,
auto-discovered from `.claude/skills/`. Karakos "skills" are a separate,
package-specific convention — a `tools.json` schema plus a `scripts/`
implementation, discovered by `mcp/tools-server.py` and exposed as MCP
tools. A frontmatter-only `SKILL.md` dropped under `skills/` will not load
here; it needs `tools.json` and `scripts/` as shown below.

### 1. Create the Skill Directory

```bash
cp -r skills/hello-world skills/my-skill
```

### 2. Define Tools

Edit `skills/my-skill/tools.json`:

```json
{
  "skill_name": "my-skill",
  "version": "1.0.0",
  "description": "What this skill does",
  "tools": [
    {
      "name": "my_tool",
      "description": "What this tool does",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "Search query"
          }
        },
        "required": ["query"]
      }
    }
  ]
}
```

### 3. Implement

Create `skills/my-skill/scripts/my_tool.py`:

```python
#!/usr/bin/env python3
import json, os

args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
query = args.get("query", "")

# Do something useful
result = {"answer": f"You asked about: {query}"}

print(json.dumps(result))
```

Scripts receive `TOOL_ARGS` (JSON) and `WORKSPACE_ROOT` via environment. Print
JSON to stdout. Exit code 0 = success.

Three details the discovery code enforces and it is cheap to get wrong:

- **The script filename must match the tool name.** `my_tool` looks for
  `scripts/my_tool.py`, then `scripts/my_tool.sh`, falling back to
  `scripts/main.py` or `scripts/main.sh`. Nothing else is tried.
- **The working directory is the skill directory**, not the workspace root.
  Use `WORKSPACE_ROOT` for anything outside your own skill.
- **60 seconds, hard.** A tool that runs longer is killed and reported as a
  failure.

`skills/README.md` is the fuller authoring guide, including argument
validation and error conventions.

### 4. Test

```bash
python3 mcp/tools-server.py --test-tool my_tool '{"query": "test"}'
```

### 5. Activate

Reset the agent session (dashboard → Agents → Reset, or via API). The MCP server restarts with the agent and discovers the new skill.

## Writing a harness test

`tests/harness/` runs the real `bin/agent-server.py` against a fake `claude`
binary that speaks stream-json, so a test exercises the real spawn path, argv,
queue, cost rows and result parsing with no API spend. CI has no
pytest-asyncio; drive it with `asyncio.run`.

```python
def test_reply(harness):
    h = harness(agents=["a", "b"])          # or Harness(tmp_workspace, agents={"a": {"model": "x"}})

    async def scenario():
        async with h:
            h.script(default={"text": "pong: {{text}}"},
                     rules=[{"match": "slow", "step": {"delay_ms": 300}}])
            await h.send("a", "ping", channel_id="1")
            await h.wait_idle("a", timeout=5)

    asyncio.run(scenario())
    assert h.queue_rows("a")[0]["response"].startswith("pong:")
    assert h.discord[-1]["content"].startswith("pong:")   # post_to_discord is stubbed
```

A step is `{text, tools: [{name, input, usage, message_id, parent_tool_use_id}],
usage, cost, delay_ms, is_error, exit, hang}`; `{{text}}` echoes the input and
`{{env:KARAKOS_AGENT}}` names the agent. `exit` kills the fake mid-turn (respawn
watcher), `hang` stops it reading (pair with `h.interrupt`). Readers
(`h.sent_to`, `h.argv`, `h.queue_rows`, `h.stream_events`) take a shard id,
which equals the agent id until the registry has shards; `h.cost_rows()` is
global. Tests must not read `HOME`, bind real ports or touch Discord;
`tests/test_no_home_access.py` enforces this. The `Harness` signatures are
frozen by `tests/test_harness_api.py`.

## Using the Builder Agent

The builder agent receives specs as markdown files in its inbox and implements them on feature branches.

### Writing a Spec

Create a file in **`inbox/builder/`** at the workspace root. This is not
`agents/builder/inbox/` — the dispatcher only watches the top-level `inbox/`.

```markdown
---
target_branch: main
repo: mcarmody/karakos-package
branch_prefix: builder
requester: 123456789012345678
callback_channel: general
---

# Feature: User Preferences

## Summary
Add a user preferences system that persists settings to a JSON file.

## Requirements
1. Create `data/preferences.json` with default values
2. Add `preferences` tool to MCP server (get/set actions)
3. Primary agent can read and update preferences

## Acceptance Criteria
- [ ] Preferences persist across restarts
- [ ] Default values provided for new installations
```

Only five frontmatter keys are read: `target_branch`, `repo`,
`branch_prefix`, `requester` and `callback_channel`. Anything else is ignored.

**`requester` is the one that matters most.** It is the Discord user ID the
completion notice is sent to. Without it, the build runs to completion and
nobody is told — there is no fallback announcement.

### Triggering a Build

The dispatch adapter watches `inbox/<agent>/`. When it finds a spec:

1. It invokes `bin/invoke-builder.sh` with the spec path
2. The builder reads the spec, creates a feature branch, implements, opens a PR
3. Cost is recorded against the builder agent through the agent server's
   `/cost` endpoint — it is visible on the dashboard's `/costs` page, and it is
   **not** posted to the signals channel
4. `requester` is poked on `callback_channel` (default `general`)
5. Owner reviews and merges

Concurrency is capped: one builder and two reviewers at a time
(`MAX_CONCURRENT_BUILDERS`, `MAX_CONCURRENT_REVIEWERS`). A builder dispatch
times out after 6 hours, a reviewer after 1.

### Using the Reviewer Agent

Send a spec and codebase for adversarial review:

```bash
bin/invoke-reviewer.sh inbox/builder/my-feature.md
```

The spec path is positional. The flags it accepts are `--model`,
`--dispatch-id`, `--output-format`, `--codebase-review` and `--help`; there is
no `--spec`, `--branch` or `--mode`, and passing one silently swallows it as
the spec path instead.

The reviewer returns one of three verdicts: **APPROVE**, **REVISE** or
**RETHINK**.

## Inter-Agent Communication

Agents communicate via `bin/poke.sh`:

`poke.sh` takes **flags**, not positional agent and channel names. Bare words
before the message are silently discarded and the poke goes to the default
agent on the default channel, which is a quiet way to lose a message.

```bash
bin/poke.sh --agent primary --reply-channel general "Status report please"

# Queue it without any Discord post at all (channel_id "0")
bin/poke.sh --agent primary --silent "run the nightly sweep"
```

For file-based dispatch, drop files in `inbox/{agent-name}/`.

### Routing

The relay sends each Discord message to a shard (`lib/routing.py`), in this order:

1. A channel that is not in `channels.json` is never routed, mention or not.
2. An `@mention` of a known agent goes to the shard that owns the channel if it belongs to that agent, else to that agent's first shard.
3. A bot never routes on a channel default (it must `@mention`).
4. Otherwise the shard that lists the channel in `agents.yaml` gets it.
5. A listed channel no shard owns goes to the primary agent's first shard.
6. Otherwise nothing is routed.

Set `"route": false` on a channel in `channels.json` to switch off rule 5 for it (rules 2 and 4 still apply). The relay re-reads `agents.yaml` and `channels.json` when they change, so a shard added by `/reload` needs no relay restart.

## Self-Modification

The system can modify itself through the builder agent:

1. Write a spec describing the change
2. Builder implements on a feature branch
3. Reviewer provides adversarial feedback
4. Owner merges the PR
5. `config/protected-paths.json` decides what the builder may touch

That file has three lists, not one. **Tier 1** is a hard block — `system/`,
`config/`, `.karakos/`, `Dockerfile`, and the four `bin/` scripts that own
process lifecycle. **Tier 2** requires review: the rest of `bin/`,
`agents/templates/`, `mcp/tools-server.py`. **Overrides** are always writable
even though they sit under a protected prefix: `agents/*/persona/`,
`agents/*/journal/`, `agents/*/inbox/` — which is what lets an agent maintain
its own persona and journal without being handed its own process lifecycle.

### What Requires Restart

| Changed File | Restart Needed | How |
|-------------|---------------|-----|
| `persona/voice.md` | None | Loaded fresh each session |
| `skills/*/` | Agent session reset | Dashboard → Reset |
| `config/agents.yaml`, adding an agent | None | `bin/create-agent.sh` hot-registers via POST `/agents/{name}/register` |
| `config/agents.yaml`, changing an existing agent | Agent respawn | POST `/agents/{name}/reload` (keeps context) or `/reset` (drops it) |
| `bin/agent-server.py` | Container restart | `make down && make up` |
| `Dockerfile` | Container rebuild | see [Local development build](#local-development-build) |

## Local Development Build

Production installs pull a prebuilt image from GHCR (`make pull`).
If you are modifying the `Dockerfile` or Python/Node dependencies and need to
test those changes before a release, use the dev compose override:

```bash
docker compose \
  -f config/docker-compose.yml \
  -f config/docker-compose.dev.yml \
  --env-file config/.env \
  up --build -d
```

`config/docker-compose.dev.yml` overlays `build: .` back onto the service so
your local changes are compiled into an image named `karakos-dev:local`.
Bring the stack back down and return to the prebuilt image at any time with:

```bash
docker compose -f config/docker-compose.yml --env-file config/.env down
make up
```

The dev override is intentionally not committed to production flows — it is
only for contributors iterating on the image itself.

## Configuration

`config/.env` holds the environment, but it is not the whole story — four
other files in `config/` carry configuration the wizard generates and you may
want to edit:

| File | Holds |
|---|---|
| `.env` | Secrets, ports, limits — everything below |
| `agents.yaml` | The agent registry: model, `max_turns`, timeout, streaming flags |
| `channels.json` | Which Discord servers and channels are watched, and each channel's `default_agent`, `reply_gate` and `guest_agents` |
| `claude-settings.json` | Hook wiring and the tool permission policy |
| `protected-paths.json` | What a builder agent may and may not commit |

### Environment variables

**Required — the container refuses to start without them:**

| Variable | Description |
|---|---|
| `AGENT_SERVER_TOKEN` | Bearer token every internal API call carries |
| `DASHBOARD_PORT` | Web UI port, and the port published on the host |

**Identity and access:**

| Variable | Description |
|---|---|
| *(Anthropic auth)* | Handled by `claude login` — no API key in env |
| `DISCORD_BOT_TOKEN_PRIMARY` / `DISCORD_BOT_ID_PRIMARY` | The primary agent's bot. Other agents use the same `_<AGENT>` suffix |
| `DISCORD_SERVER_ID` | The guild the relay listens to |
| `DISCORD_CHANNEL_GENERAL` / `DISCORD_CHANNEL_SIGNALS` | Channel IDs |
| `OWNER_DISCORD_ID` | Who counts as the owner. **Unset denies every slash command to everyone** |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Dashboard login, user defaults to `admin` |
| `SESSION_SECRET` | HMAC key for the dashboard session cookie |
| `AGENT_SERVER_PORT` | API port, default 18791, loopback-only on the host |
| `WORKSPACE_ROOT` | `/workspace` in the container |
| `KARAKOS_VERSION` | Image tag to run, default `latest` |
| `TZ` | Container timezone — the scheduler's clock times are in it |

**Cost and capacity:**

| Variable | Description |
|---|---|
| `COST_DAILY_LIMIT` / `COST_MONTHLY_LIMIT` | Spend caps in USD, enforced when a message is queued |
| `COST_WARNING_THRESHOLD` | Fraction of a cap that triggers a warning, default 0.75 |
| `MAX_CONCURRENT_BUILDERS` / `MAX_CONCURRENT_REVIEWERS` | Parallel dispatches |
| `GUEST_TURN_LIMIT` | Turns a bot author may consume, default 12 |
| `DISCORD_POST_MAX_ATTEMPTS` | Retries before a reply is dead-lettered, default 3 |

**Memory, retention and timing:**

| Variable | Description |
|---|---|
| `MEMORY_DECAY_RATE` | Episode importance decay per pass (0–1), applied from `base_importance` |
| `MEMORY_CUTOFF` | Importance below which an episode is eligible to be dropped |
| `MEMORY_PRUNE_GRACE_DAYS` | Days an episode is protected from pruning regardless of score, measured from `inserted_at`; default 7 |
| `MEMORY_MAX_EPISODES` | Episodes kept per day |
| `MEMORY_SCORE_TIMEOUT` / `MEMORY_SCORE_RETRY_TIMEOUT` | Haiku scoring call timeout, first attempt and retry; default 20s / 60s |
| `MESSAGE_RETENTION_DAYS` | JSONL log retention |
| `TOOL_AUDIT_RETENTION_DAYS` | Tool-call audit retention |
| `KARAKOS_RECALL_SOURCE` / `KARAKOS_RECALL_TIMEOUT_S` | Recall injection override and time bound, below |
| `KARAKOS_RECALL_LIMIT` / `KARAKOS_RECALL_MAX_CHARS` / `KARAKOS_RECALL_HOOK_MODE` | Graph recall block size (default 6 results / 2400 chars) and hook mode (`fast`, or `full` to try the model) |
| `SCHEDULER_TICK_SECONDS` | Scheduler loop period, default 15 |
| `ONESHOT_STALE_AFTER_SECONDS` | How late a missed one-off may fire, default 24h |

## Claude Code Hooks

`config/claude-settings.json` is a package-owned Claude Code settings file
passed via `--settings` on every agent's `claude` spawn line
(`bin/agent-server.py`), rather than a `.claude/` directory an installer
would have to scaffold and a user could delete. It wires hook events and
carries the tool permission policy. Shipped hooks:

| Event | Script | Does |
|---|---|---|
| `UserPromptSubmit` | `system/hooks/log-user-prompt.sh` | Appends one line to `logs/hook-events.log` per prompt — proves the hook pipeline is live. |
| `UserPromptSubmit` | `system/hooks/inject-recall.py` | Re-injects a recall block before every user message (see below). |
| `PreToolUse` (`Edit\|Write\|MultiEdit\|Read`) | `system/hooks/resolve-symlink-edit.py` | Rewrites a path through a symlink to its realpath so the harness's "refusing to write through symlink" rejection never reaches the model. |
| `PreToolUse` (`Bash`) | `system/hooks/bash-safety-rails.py` | Denies `pkill -f`, prisma `--accept-data-loss`, `rm -r` on `/`, `$HOME`, the workspace or `.git`, and force-pushes to main/master. Logs denies to `logs/blocked-bash.jsonl`. |
| `PreToolUse` (`Bash`) | `system/hooks/block-bare-ssh.py` | Denies bare `ssh`/`scp`/`rsync` to hosts in `config/hooks.json` `bare_ssh_hosts` (default none) and points at `ssh_wrapper`. |
| `PreToolUse` (`Bash`) | `system/hooks/block-heavy-build.py` | Opt-in (`"heavy_build_block": true` in `config/hooks.json`): denies tsc, next/webpack/vite builds, `npm ci`, bare installs. |
| `Stop` | `system/hooks/stop-deferred-work.py` | Catches "I'll do that shortly"-style deferrals in the final reply and forces the turn to continue, capped at 2 extensions. |

The `hooks` section of `claude-settings.json` is generated by
`bin/hooks-sync.py` from `config/hooks.json` on every boot (and by `setup.sh`).
Entries whose command is under `system/hooks/` are managed; hooks you add
yourself, `permissions` and `env` are preserved.

The git pre-commit also runs `system/check-secrets.py --staged`: forbidden
paths (`.env*` except `.env.template`, `*.pem`, `*.key`, `id_*`, `secrets/`)
and a content scan (gitleaks if installed, else built-in regexes). Allow a
false positive by adding the exact line to `config/secrets-allow.txt`;
`KARAKOS_SECRET_SCAN=off` skips the content scan for one commit.

All hook scripts are fail-safe by construction: a parse error, a missing
field, or an unexpected shape is swallowed and the tool call / turn
proceeds exactly as it would unmodified. None of them ever raise into the
CLI.

### Recall re-injection (`inject-recall.py`)

Without this hook, a long-running session answers every question from
whatever was true when it started. `inject-recall.py` runs on every
`UserPromptSubmit` and folds a recall block into the turn via Claude Code's
`hookSpecificOutput.additionalContext`. It is the only per-prompt recall path.

**Default source: the knowledge graph.** With no override, the hook queries
`data/memory/graph.db` with the prompt text and renders up to
`KARAKOS_RECALL_LIMIT` (default 6) `- [kind] subject: text` lines under
`[ACTIVE RECALL]`, capped at `KARAKOS_RECALL_MAX_CHARS` (default 2400). An
uninitialised or unreadable graph, any error, or a timeout
(`KARAKOS_RECALL_TIMEOUT_S`, default 10s) yields no block and never blocks the
turn. The hook is a fresh process per prompt, so it runs in **fast mode** (no
embedding model; keyword, name and importance signals). Set
`KARAKOS_RECALL_HOOK_MODE=full` to try the model within a 6 s budget, falling
back to fast.

**Operator override.** If `KARAKOS_RECALL_SOURCE` is set, or
`$WORKSPACE_ROOT/config/recall-source` exists, that source is used *instead of*
the graph, never alongside it:

- **Path does not exist** — no-op, not an error.
- **Path is executable** — run with the pending user prompt text on stdin;
  its stdout becomes the recall block. A non-zero exit, a crash, or a
  timeout are all treated as "no recall available."
- **Path is a plain file** — read verbatim, every turn, as a static recall
  block (e.g. a hand-maintained facts file).

Facts also load once per spawn (top graph facts by importance, own header),
because automated turns skip this hook; see ARCHITECTURE.md, "Memory".

Automated traffic — system pokes, heartbeats, and task-complete
notifications sent through `bin/poke.sh` (always `is_bot=1`) — skips
recall entirely, so scheduled/background turns don't pay for it.
`bin/agent-server.py` stamps a `[KARAKOS_AUTOMATED]` sentinel onto the
front of any message batch where every message is bot-originated; the hook
recognizes that same literal string as its skip gate. A batch with even
one human message alongside automated ones is left unmarked, so a human
reply riding along in the same batch still gets a fresh recall block.

### Tool permissions (`permissions.allow` / `permissions.deny`)

`bin/agent-server.py` passes `--dangerously-skip-permissions` on every
agent spawn, which is the CLI's own bypass-all-approval-prompts mode.
`permissions.allow` / `permissions.deny` in `config/claude-settings.json`
sit above that and are **not** overridden by it — verified against the
real CLI:

- A tool named in `permissions.deny` in full (e.g. `"WebFetch"`) is dropped
  from the session's tool list entirely at spawn. The model cannot call it
  — there is nothing to invoke, and no runtime prompt for
  `--dangerously-skip-permissions` to bypass.
- A fine-grained rule (e.g. `"Bash(curl:*)"`) leaves the tool available but
  declines a matching call at request time. That decline shows up in the
  stream-json `result` event's `permission_denials` list, which
  `read_agent_response()` logs as a warning
  (`<agent> permission denied: tool=... input=...`).
- `read_agent_response()` also logs the resolved tool list from the
  session's opening `system`/`init` event, so a full-tool deny is visible
  in `logs/agent-server.log` even though it never produces a runtime
  denial event.

Both `allow` and `deny` default to `[]` (no-op) and apply to every agent,
since `config/claude-settings.json` is currently one file shared across
all agents — there is no per-agent settings file.

### Per-agent environment

`config/claude-settings.json`'s own `env` key is applied by the CLI to its
own process environment, but since that file is shared, it's install-wide,
not per-agent. For environment scoped to a single agent, add an `env`
object to that agent's entry in `config/agents.yaml`:

```yaml
agents:
  researcher:
    name: researcher
    role: custom
    system_prompt: agents/researcher/SYSTEM_PROMPT.md
    env:
      ANTHROPIC_SMALL_FAST_MODEL: claude-haiku-4-5
```

`bin/agent-server.py` layers this onto its own environment (not a
replacement) when spawning that agent's subprocess, so the agent still
inherits `WORKSPACE_ROOT`, API credentials, etc. An agent with no `env` key
spawns exactly as before this feature existed (`env=None`, plain inherit).

### Mid-turn tool activity lines

While a turn is running, the agent posts a subtext line naming each tool
call and what it is working on:

```
-# ⚙ Bash — npm test
-# ⚙ Read — /srv/app/main.py
```

This exists so a four-minute turn is distinguishable from a hung one — the
typing indicator alone cannot tell you the difference. **It is on by
default.** To silence it for an agent, set `tool_streaming` in that agent's
`config/agents.yaml` entry:

```yaml
agents:
  researcher:
    name: researcher
    role: custom
    system_prompt: agents/researcher/SYSTEM_PROMPT.md
    tool_streaming: false
```

The lines are throttled, and the throttle is what makes on-by-default safe:
the first tool call of a turn always posts, then at most one line every
`TOOL_EVENT_MIN_INTERVAL` seconds, with a hard ceiling of
`TOOL_EVENT_MAX_PER_TURN` lines per turn (both in `bin/agent-server.py`). A
turn making fifty rapid tool calls posts one line, not fifty. These are a
liveness signal rather than an audit log — for a complete record of tool
calls, read the agent's log or the `tool_calls` table in
`data/mcp-tools-audit.db`.

Only a known argument is shown (`command`, `file_path`, `pattern`, `url`,
and a few more); an unrecognised tool gets its bare name. Tool inputs carry
file contents, patch bodies and credentials, and this line goes to a Discord
channel, so the summary is an allow-list rather than a best-effort dump.

Agents running with `channel_id` `"0"` (the local/headless lane) post
nothing, as with every other Discord surface.

## Review checklist

Every release/2.0 PR is reviewed against this list. Each item is yes/no; any
"no" on items 1 to 4 is a high finding.

1. Runtime state is keyed by shard id.
2. No data is mutated at boot; every schema change is a `lib/migrate/steps/` step.
3. No household identifiers: `system/check-coupling.sh` prints `coupling: clean`.
4. The tests named in the spec exist and run.
5. Old-install behaviour is unchanged where the spec says so.
6. No new dependency without a pin.

`system/check-coupling.sh` scans git-tracked files against
`system/coupling-denylist.txt` (tab-separated `name`, `regex`, optional
`path-regex`, optional `line-regex`) and honours `system/coupling-allow.txt`
(`path-glob<TAB>regex`). Forks add their own hostnames and names to the
denylist locally. To run it before every push:
`system/install-hooks.sh --install-pre-push` (off by default).

## release/2.0 stability contract

For downstream forks tracking `release/2.0`:

- **Churn expected:** `bin/agent-server.py`, `bin/relay.py`, the `config/`
  layout, `setup.sh`, `dashboard/`.
- **Stable:** the `/message` and `/agents` HTTP shapes, `config/channels.json`,
  hook file names, MCP tool names.
- Small portable fixes submitted by forks are welcome on `release/2.0` now;
  landing them early lets forks rebase once.

## lib/migrate (schema stamp and migrator)

`data/.schema-version` (`{"schema": 2, "package": "2.0.0", "migrated_from": ..., "stamped_at": ...}`)
marks a data directory as 2.0. `bin/entrypoint.sh` and `bin/agent-server.py`
call `lib/migrate/guard.py: require_stamp` before touching any data and exit
78 on an unstamped non-empty directory or an older schema. An empty or absent
data directory is a fresh install and is stamped (`guard.py stamp --fresh`).
`KARAKOS_SKIP_STAMP_CHECK=1` bypasses the check for tests only and is refused
when `KARAKOS_ENV=production`.

**Boot code may only check the stamp, never alter data.** The migrator
(`python3 -m lib.migrate [--dry-run|--auto|--force|--to-backup DIR]`, wrapper
`bin/karakos-migrate`) is the only writer of 1.x data. It detects the version
(read-only fingerprints, `detect.py`), takes a backup (`backup.py`, sqlite
online backup plus a hashed `MANIFEST.json`), runs each applicable step in
`lib/migrate/steps/NN_name.py` order, verifies each, and writes the stamp last.
A failed step leaves the stamp absent and prints the backup path and restore
command. Exit codes: 0 ok, 1 step failed, 2 usage, 3 refused (unknown schema,
no `--force`), 78 guard.

A step module exposes `STEP = Step(name, from_schema, to_schema, detect, apply, verify)`.

| Step | Owns |
|------|------|
| 1.1b | config (agents.json to agents.yaml) |
| 1.2  | queue schema |
| 1.5  | sessions schema |
| 4.4  | memory |
