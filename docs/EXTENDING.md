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
| `config/agents.json`, adding an agent | None | `bin/create-agent.sh` hot-registers via POST `/agents/{name}/register` |
| `config/agents.json`, changing an existing agent | Agent respawn | POST `/agents/{name}/reload` (keeps context) or `/reset` (drops it) |
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
| `agents.json` | The agent registry: model, `max_turns`, timeout, streaming flags |
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
| `MEMORY_DECAY_RATE` | Episode importance decay per pass (0–1) |
| `MEMORY_CUTOFF` | Importance below which an episode is dropped |
| `MEMORY_MAX_EPISODES` | Episodes kept per day |
| `MESSAGE_RETENTION_DAYS` | JSONL log retention |
| `TOOL_AUDIT_RETENTION_DAYS` | Tool-call audit retention |
| `KARAKOS_RECALL_SOURCE` / `KARAKOS_RECALL_TIMEOUT_S` | Recall injection, below |
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
| `PreToolUse` (`Bash`) | `system/hooks/rewrite-sleep-poll.py` | Rewrites a leading `sleep N` into `wait-for.sh --sleep N` so the sandbox's blocked-foreground-sleep rejection never reaches the model. |
| `Stop` | `system/hooks/stop-deferred-work.py` | Catches "I'll do that shortly"-style deferrals in the final reply and forces the turn to continue, capped at 2 extensions. |

All hook scripts are fail-safe by construction: a parse error, a missing
field, or an unexpected shape is swallowed and the tool call / turn
proceeds exactly as it would unmodified. None of them ever raise into the
CLI.

### Recall re-injection (`inject-recall.py`)

Without this hook, everything an agent knows enters once, at spawn, via
`--append-system-prompt`, and a long-running session answers every question
from whatever was true when it started. `inject-recall.py` re-reads a
recall source on every `UserPromptSubmit` and folds it into the turn via
Claude Code's `hookSpecificOutput.additionalContext`.

The package ships no memory store of its own — the recall source is a
documented, swappable interface, resolved from `KARAKOS_RECALL_SOURCE`
(default `$WORKSPACE_ROOT/config/recall-source`):

- **Path does not exist** — no-op. Not an error; a fresh install with no
  recall source configured behaves exactly as before this hook existed.
- **Path is executable** — run with the pending user prompt text on stdin;
  its stdout becomes the recall block. A non-zero exit, a crash, or a
  timeout (`KARAKOS_RECALL_TIMEOUT_S`, default 10s) are all treated as "no
  recall available," never as an error that blocks the turn.
- **Path is a plain file** — read verbatim, every turn, as a static recall
  block (e.g. a hand-maintained facts file).

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
object to that agent's entry in `config/agents.json`:

```json
{
  "agents": {
    "researcher": {
      "system_prompt": "agents/researcher/SYSTEM_PROMPT.md",
      "env": { "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5" }
    }
  }
}
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
`config/agents.json` entry:

```json
{
  "agents": {
    "researcher": {
      "system_prompt": "agents/researcher/SYSTEM_PROMPT.md",
      "tool_streaming": false
    }
  }
}
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
