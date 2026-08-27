#!/usr/bin/env python3
"""
Karakos MCP Tool Server — JSON-RPC 2.0 over stdin/stdout

Discovers tools from skills/*/tools.json, validates calls,
dispatches to skill scripts, maintains audit trail.
"""

import array
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
SKILLS_DIR = WORKSPACE / "skills"
HEALTH_FILE = WORKSPACE / "data" / "health" / "mcp-tools.json"
AUDIT_DB_PATH = WORKSPACE / "data" / "mcp-tools-audit.db"

# Maximum payload size for tool arguments
MAX_ARGS_SIZE = 65536

# Agent server, for the ask_user round trip (#101). KARAKOS_AGENT is set by
# bin/agent-server.py on the subprocess this tool server is a child of; it is
# how a question knows whose conversation to appear in.
AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}")
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
KARAKOS_AGENT = os.environ.get("KARAKOS_AGENT", "")

# Poll cadence while a question is on screen. Short enough that the agent
# resumes promptly after a click, long enough not to spin.
ASK_POLL_INTERVAL_SEC = float(os.environ.get("ASK_POLL_INTERVAL_SEC", "2.0"))
ASK_DEFAULT_TIMEOUT_SEC = 300

# =============================================================================
# Core Tools (ship by default)
# =============================================================================

CORE_TOOLS = [
    {
        "name": "workspace",
        "description": "System config, agent registry, version info. Actions: status (show workspace info), agents (list agents), config (show system config).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "agents", "config"],
                    "description": "The action to perform"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "session",
        "description": "Session lifecycle management. Actions: finalize (generate summary), load_last (retrieve checkpoint).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["finalize", "load_last"],
                    "description": "The action to perform"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "schedule",
        "description": (
            "Schedule your own future work. This is how a promise like "
            "\"I'll check back in 10 minutes\" becomes a mechanism instead of a "
            "sentence. Actions: create (schedule a message or command for later), "
            "list (show what is pending), cancel (drop a pending item by label). "
            "Scheduled items survive a restart of the container."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "cancel"],
                    "description": "The action to perform"
                },
                "when": {
                    "type": "string",
                    "description": "When to fire: a relative span ('10m', '+2h', '1h30m') or an absolute time ('2026-08-09 09:00')"
                },
                "message": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Message to deliver to yourself at that time (the usual case for a reminder)"
                },
                "command": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Shell command to run at that time, instead of a message"
                },
                "label": {
                    "type": "string",
                    "description": "Short name for this item; required for create and cancel"
                },
                "agent": {
                    "type": "string",
                    "description": "Agent to deliver the message to (default: primary agent)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "memory",
        "description": "Query episodic memory. Actions: recall (semantic search over episodes by embedding similarity, blended with importance; falls back to keyword matching when no embeddings are available), facts (search facts), recent (recent episodes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["recall", "facts", "recent"],
                    "description": "The action to perform"
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for recall and facts)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "discord",
        "description": "Discord server read-only access. Actions: history (view messages), channels (list channels), online (list members).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["history", "channels", "online"],
                    "description": "The action to perform"
                },
                "channel": {
                    "type": "string",
                    "description": "Channel name or ID (required for history)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of messages (default 20, max 50)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "taskboard",
        "description": "Task and todo tracking. Actions: list (show tasks), add (create task), update (modify task), complete (mark done).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "update", "complete"],
                    "description": "The action to perform"
                },
                "title": {
                    "type": "string",
                    "description": "Task title (for add)"
                },
                "id": {
                    "type": "string",
                    "description": "Task ID (for update/complete)"
                },
                "status": {
                    "type": "string",
                    "description": "New status (for update)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "vault",
        "description": "Git-backed knowledge vault. Actions: pull (sync from remote), push (sync to remote), status (show git status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pull", "push", "status"],
                    "description": "The action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (for push)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "ask_user",
        "description": (
            "Put a multiple-choice question to the user and wait for their "
            "answer. Renders in Discord as an embed with one button per "
            "option; returns the option they clicked. This is the only way "
            "to ask a blocking question over this transport — the built-in "
            "AskUserQuestion tool is not available to agents running "
            "headless. Use it for decisions you cannot make yourself; do not "
            "use it for questions you could simply write out in your reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to put to the user",
                    "path_mode": "none"
                },
                "options": {
                    "type": "array",
                    "description": "2-10 choices. Each is a string, or an object with 'label' and optional 'description'.",
                    "items": {"type": ["string", "object"]}
                },
                "header": {
                    "type": "string",
                    "description": "Short title for the embed (optional)",
                    "path_mode": "none"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for an answer (default 300, max 3600)"
                }
            },
            "required": ["question", "options"]
        }
    },
]


# =============================================================================
# Audit Database
# =============================================================================

def init_audit_db():
    """Initialize audit trail database."""
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUDIT_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT,
            result_size_bytes INTEGER,
            duration_ms REAL,
            success INTEGER,
            error_msg TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name)")
    conn.commit()
    return conn


def log_audit(conn, tool_name, args_json, result_size, duration_ms, success, error_msg=None):
    """Record tool call in audit trail."""
    try:
        conn.execute(
            "INSERT INTO tool_calls (timestamp, tool_name, args_json, result_size_bytes, "
            "duration_ms, success, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                args_json[:1024] if args_json else None,
                result_size,
                duration_ms,
                1 if success else 0,
                error_msg,
            )
        )
        conn.commit()
    except Exception:
        pass


def write_health():
    """Write health heartbeat."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy",
    }))


# =============================================================================
# Skill Discovery
# =============================================================================

def discover_skills() -> list[dict]:
    """Scan skills/*/tools.json for tool definitions.

    This package's skill convention is tools.json + scripts/ (see
    docs/EXTENDING.md). A skill directory that ships only a frontmatter-style
    SKILL.md (the Anthropic Agent Skills convention) has no tools.json for
    this server to read, so it is skipped — but skipped loudly, on stderr,
    naming the file, per issue #84.
    """
    tools = []
    if not SKILLS_DIR.exists():
        return tools

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        tools_file = skill_dir / "tools.json"
        if not tools_file.exists():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                sys.stderr.write(
                    f"Skipping {skill_md}: no tools.json in {skill_dir}. "
                    "This server only loads the tools.json + scripts/ skill "
                    "convention (docs/EXTENDING.md); frontmatter-only "
                    "SKILL.md files are not discovered.\n"
                )
            continue
        try:
            data = json.loads(tools_file.read_text())
            for tool in data.get("tools", []):
                tool["_skill_dir"] = str(skill_dir)
                tools.append(tool)
        except Exception as e:
            sys.stderr.write(f"Error loading {tools_file}: {e}\n")

    return tools


# =============================================================================
# Input Validation
# =============================================================================

def validate_args(args: dict, schema: dict) -> str | None:
    """Basic JSON Schema validation. Returns error message or None."""
    if schema.get("type") != "object":
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in args:
            return f"Missing required field: {field}"

    for key, value in args.items():
        if key not in properties:
            continue
        prop_schema = properties[key]

        # Type check
        expected_type = prop_schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"Field '{key}' must be a string"
        elif expected_type == "integer" and not isinstance(value, int):
            return f"Field '{key}' must be an integer"
        elif expected_type == "number" and not isinstance(value, (int, float)):
            return f"Field '{key}' must be a number"
        elif expected_type == "boolean" and not isinstance(value, bool):
            return f"Field '{key}' must be a boolean"

        # Enum check
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            return f"Field '{key}' must be one of: {prop_schema['enum']}"

        # Path safety (reject traversal unless explicitly allowed).
        # `path_mode: "none"` marks a field as prose or a shell command rather
        # than a path: an ellipsis in "Ship it, or wait...?" or "remind me to
        # check the logs..." contains ".." and would otherwise be rejected as
        # traversal, which makes any free-text field unusable.
        #
        # #132 and #101 hit this same wall independently and landed on two
        # different spellings — a `free_text: True` boolean and this
        # `path_mode: "none"`. They are one axis, not two, so they are unified
        # here on path_mode, which main already used for "absolute". Keeping
        # both would mean every future free-text field has to guess which flag
        # this predicate actually reads.
        if isinstance(value, str) and ".." in value:
            if prop_schema.get("path_mode") not in ("absolute", "none"):
                return f"Field '{key}' contains path traversal"

    return None


# =============================================================================
# Tool Dispatch
# =============================================================================

def agent_server_request(method: str, path: str, payload: dict = None,
                         timeout: float = 15.0) -> tuple[int, dict]:
    """One request to the local agent server. Returns (status, body).

    Uses urllib rather than aiohttp on purpose: this server is a synchronous
    stdio JSON-RPC loop with no event loop of its own, and adding an async
    HTTP client here would mean adding a dependency to the one process that
    currently has none beyond the standard library.
    """
    url = f"{AGENT_SERVER_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {AGENT_SERVER_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except (ValueError, OSError):
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def ask_user(args: dict, sleep=time.sleep, monotonic=time.monotonic) -> dict:
    """Put a question to the user and block until they answer it.

    The blocking is the feature. The agent's turn is suspended inside this
    tool call, so the answer arrives as a tool result in the same turn rather
    than as a fresh message the agent has to correlate itself.

    `sleep`/`monotonic` are injectable so the poll loop is testable without a
    real clock — the test drives the same loop the agent drives, and the
    deadline is real rather than a fixed sleep.
    """
    agent = args.get("agent") or KARAKOS_AGENT
    if not agent:
        return {
            "status": "error",
            "error": "No agent identity (KARAKOS_AGENT unset); cannot route the question",
        }

    timeout = args.get("timeout") or ASK_DEFAULT_TIMEOUT_SEC
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = ASK_DEFAULT_TIMEOUT_SEC

    status, body = agent_server_request("POST", "/ask", {
        "agent": agent,
        "question": args.get("question", ""),
        "options": args.get("options"),
        "header": args.get("header"),
        "timeout": timeout,
    })
    if status != 201:
        return {
            "status": "error",
            "error": body.get("error") or f"agent server returned {status}",
        }

    ask_id = body.get("ask_id")
    deadline = monotonic() + timeout + ASK_POLL_INTERVAL_SEC * 2

    while True:
        poll_status, poll_body = agent_server_request("GET", f"/ask/{ask_id}")
        state = poll_body.get("status")
        if poll_status == 200 and state == "answered":
            return {
                "status": "answered",
                "answer": poll_body.get("answer"),
                "answer_index": poll_body.get("answer_index"),
                "answered_by": poll_body.get("answered_by"),
                "question": poll_body.get("question"),
            }
        if poll_status == 404 or state == "expired":
            # 404 also covers "the agent server restarted while we waited" —
            # either way there is no answer coming and the agent should stop
            # holding the turn open.
            return {
                "status": "timeout",
                "error": "The user did not answer in time; decide without them or ask again.",
            }
        if monotonic() >= deadline:
            return {
                "status": "timeout",
                "error": "Timed out waiting for an answer.",
            }
        sleep(ASK_POLL_INTERVAL_SEC)


# =============================================================================
# Semantic recall for the memory tool (#149)
# =============================================================================
#
# bin/memory-maintenance.py has always written a float32 embedding
# (BAAI/bge-small-en-v1.5, via fastembed) onto every episode, and nothing ever
# read one back: `recall` was a `summary LIKE '%query%'` scan. The install paid
# for the fastembed dependency — including the aarch64 build-essential dance in
# the Dockerfile — and the nightly embedding compute, and got substring
# matching. These helpers close that loop.
#
# Everything below is best-effort on purpose. fastembed is an optional import
# in memory-maintenance.py (missing => embedding generation is skipped), and
# every episode written before this change has `embedding IS NULL`. So recall
# has to keep working with no model, no vectors, or only some vectors: every
# failure path lands on the original LIKE query, never on an error or an empty
# result.

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# How much an episode's own importance counts versus how close it sits to the
# query. 0.0 = pure cosine, 1.0 = pure importance. Rationale for 0.25 is on
# blended_score().
MEMORY_IMPORTANCE_WEIGHT = float(os.environ.get("MEMORY_IMPORTANCE_WEIGHT", "0.25"))

# An episode with no embedding can still be matched literally, and a literal
# hit has to be rankable against a scored one. It is scored as a fair-but-not-
# great semantic match (0.75 on the 0..1 scale below, i.e. cosine 0.5) so a
# substring hit on a not-yet-embedded episode interleaves with the semantic
# hits instead of being dropped or always winning.
KEYWORD_MATCH_SIMILARITY = 0.75

# Ceiling on how many embedded episodes are pulled into memory to score,
# highest importance first. 2000 x 384 float32 is ~3 MB of blobs and ~0.6s to
# decode and score on a Raspberry Pi 4; maintenance prunes the table by
# importance nightly, so a real database rarely gets near this.
MEMORY_RECALL_SCAN_LIMIT = int(os.environ.get("MEMORY_RECALL_SCAN_LIMIT", "2000"))

# Kill switch for a host where loading an ONNX model inside a 60s tool call is
# not affordable. Anything falsy forces the original keyword path.
SEMANTIC_RECALL_ENABLED = os.environ.get(
    "KARAKOS_SEMANTIC_RECALL", "1"
).strip().lower() not in ("0", "false", "no", "off", "")

# Loading BAAI/bge-small-en-v1.5 costs ~6s and ~230 MB RSS on a Raspberry Pi 4
# with the weights already on disk — a real bite out of a 60s tool-call budget,
# and out of the box. This server is long-lived (one process per agent), so the
# model is loaded once and reused: only the first recall of a session pays for
# it, and later ones cost ~0.1-0.3s. Nothing here downloads the weights; that
# is the nightly bin/memory-maintenance.py run's job, and until it has run
# there are no embeddings and recall_episodes() never reaches the model.
_embedder = None
_embedder_failed = False


def get_embedder():
    """Return a cached fastembed TextEmbedding, or None if unavailable.

    A missing fastembed, a missing model file, or an onnxruntime that will not
    start are all "no semantic recall today", never an error to the caller.
    The failure is latched so a broken install is not re-probed — and
    re-timed-out — on every subsequent call.
    """
    global _embedder, _embedder_failed

    if _embedder is not None:
        return _embedder
    if _embedder_failed:
        return None

    try:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)
    except Exception as e:  # ImportError, missing model, onnxruntime, ...
        print(f"[tools-server] semantic recall unavailable ({e}); "
              f"falling back to keyword search", file=sys.stderr)
        _embedder_failed = True
        return None

    return _embedder


def embed_query(text: str):
    """Embed one query string with the same model maintenance used.

    Returns a list of floats, or None if the query could not be embedded.
    """
    model = get_embedder()
    if model is None:
        return None
    try:
        vectors = list(model.embed([text]))
    except Exception as e:
        print(f"[tools-server] could not embed query ({e}); "
              f"falling back to keyword search", file=sys.stderr)
        return None
    if not vectors:
        return None
    return [float(x) for x in vectors[0]]


def decode_embedding(blob):
    """Decode one float32 blob written by bin/memory-maintenance.py.

    That writer uses numpy's `tobytes()` — native-endian, unpadded, which is
    exactly what `array('f')` reads back, so this needs no numpy of its own.
    NULL, empty, or truncated blobs return None rather than raising: one bad
    row must not take out the whole recall.
    """
    if not blob:
        return None
    try:
        vector = array.array("f")
        vector.frombytes(bytes(blob))
    except (ValueError, TypeError):
        return None
    return list(vector) if len(vector) else None


def cosine_similarity(a, b) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is zero."""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a * norm_b) ** 0.5)


def blended_score(similarity01: float, importance) -> float:
    """Blend a 0..1 similarity with the episode's importance.

    Keeping importance in the ranking is the whole point of the blend: on pure
    cosine, an ancient throwaway line that happens to land near the query in
    vector space outranks a genuinely important episode that is only slightly
    further away.

    Importance carries recency for free, so nothing here has to re-derive age
    from created_at: bin/memory-maintenance.py decays importance by age every
    night (decay_importance(), 0.25 points per 4 days by default) and prunes
    below a cutoff, so a stale episode has already been marked down by the
    time it reaches this function.

    0.25 keeps similarity dominant — a genuinely bad match cannot be rescued
    by being important — while letting a ~3-point importance gap flip a
    near-tie in similarity.
    """
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 5.0
    importance01 = max(0.0, min(1.0, importance / 10.0))
    weight = max(0.0, min(1.0, MEMORY_IMPORTANCE_WEIGHT))
    return (1.0 - weight) * similarity01 + weight * importance01


def keyword_recall(conn, query: str, limit: int, reason: str) -> dict:
    """The original substring scan — the floor every failure path lands on."""
    rows = conn.execute(
        "SELECT id, summary, importance, created_at FROM episodes "
        "WHERE summary LIKE ? ORDER BY importance DESC LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    return {
        "episodes": [dict(r) for r in rows],
        "mode": "keyword",
        "reason": reason,
    }


def recall_episodes(conn, query: str, limit: int) -> dict:
    """Rank episodes against `query` by embedding similarity plus importance.

    Falls back to keyword_recall() whenever semantic ranking is not possible:
    an empty query, the kill switch, no embedded episodes yet, or no usable
    embedding model. A *partially* embedded database is handled rather than
    fallen back on — embedded rows are scored, and un-embedded rows that match
    literally are folded into the same ranking.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))

    query = (query or "").strip()
    if not query:
        # `LIKE '%%'` matches everything, which is the documented way to ask
        # for "the most important episodes". Embedding the empty string is not.
        return keyword_recall(conn, "", limit, "empty_query")

    if not SEMANTIC_RECALL_ENABLED:
        return keyword_recall(conn, query, limit, "disabled")

    # Ask the database before the model. If maintenance has never embedded
    # anything there is nothing to compare against, and loading an ONNX model
    # to find that out would burn seconds of a 60s tool call for nothing.
    embedded_rows = conn.execute(
        "SELECT id, summary, importance, created_at, embedding FROM episodes "
        "WHERE embedding IS NOT NULL ORDER BY importance DESC LIMIT ?",
        (MEMORY_RECALL_SCAN_LIMIT,)
    ).fetchall()
    if not embedded_rows:
        return keyword_recall(conn, query, limit, "no_embeddings")

    query_vector = embed_query(query)
    if not query_vector:
        return keyword_recall(conn, query, limit, "embedder_unavailable")

    scored = []
    for row in embedded_rows:
        vector = decode_embedding(row["embedding"])
        if not vector or len(vector) != len(query_vector):
            # A different length means a different model wrote it; skip rather
            # than compare two vectors that do not share a space.
            continue
        # bge cosine is [-1, 1]; map to [0, 1] so it blends with importance.
        cosine = cosine_similarity(query_vector, vector)
        similarity01 = (cosine + 1.0) / 2.0
        scored.append({
            "id": row["id"],
            "summary": row["summary"],
            "importance": row["importance"],
            "created_at": row["created_at"],
            "similarity": round(cosine, 4),
            "score": round(blended_score(similarity01, row["importance"]), 4),
            "match": "semantic",
        })

    if not scored:
        # Every stored blob was unreadable, or came from another model.
        return keyword_recall(conn, query, limit, "no_usable_embeddings")

    # Mixed database: episodes maintenance has not reached yet can still be
    # matched literally, and are folded into the same ranking so they are
    # neither dropped nor automatically preferred.
    seen = {episode["id"] for episode in scored}
    for row in conn.execute(
        "SELECT id, summary, importance, created_at FROM episodes "
        "WHERE embedding IS NULL AND summary LIKE ? "
        "ORDER BY importance DESC LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall():
        if row["id"] in seen:
            continue
        scored.append({
            "id": row["id"],
            "summary": row["summary"],
            "importance": row["importance"],
            "created_at": row["created_at"],
            "similarity": None,
            "score": round(
                blended_score(KEYWORD_MATCH_SIMILARITY, row["importance"]), 4
            ),
            "match": "keyword",
        })

    def sort_key(episode):
        try:
            importance = float(episode["importance"])
        except (TypeError, ValueError):
            importance = 0.0
        return (episode["score"], importance)

    scored.sort(key=sort_key, reverse=True)
    return {
        "episodes": scored[:limit],
        "mode": "semantic",
        "scanned": len(embedded_rows),
    }


def handle_core_tool(tool_name: str, args: dict) -> dict:
    """Handle built-in core tools."""

    if tool_name == "ask_user":
        return ask_user(args)

    if tool_name == "workspace":
        action = args.get("action", "status")
        if action == "status":
            config_path = WORKSPACE / ".karakos" / "config.json"
            config = {}
            if config_path.exists():
                config = json.loads(config_path.read_text())
            return {
                "system_name": config.get("system_name", os.environ.get("SYSTEM_NAME", "Karakos")),
                "version": config.get("version", "1.0.0"),
                "owner": config.get("owner_name", os.environ.get("OWNER_NAME", "User")),
                "workspace": str(WORKSPACE),
            }
        elif action == "agents":
            agents_path = WORKSPACE / "config" / "agents.json"
            if agents_path.exists():
                return json.loads(agents_path.read_text())
            return {"agents": {}}
        elif action == "config":
            config_path = WORKSPACE / ".karakos" / "config.json"
            if config_path.exists():
                return json.loads(config_path.read_text())
            return {}

    elif tool_name == "session":
        action = args.get("action", "load_last")
        if action == "finalize":
            try:
                result = subprocess.run(
                    ["python3", str(WORKSPACE / "bin" / "summarize-session.py")],
                    capture_output=True, text=True, timeout=30, cwd=str(WORKSPACE)
                )
                return {"status": "ok" if result.returncode == 0 else "error",
                        "output": result.stdout.strip()}
            except Exception as e:
                return {"error": str(e)}
        elif action == "load_last":
            # Check for session summary files
            data_dir = WORKSPACE / "data"
            summaries = sorted(data_dir.glob("last-session-summary-*.md"))
            if summaries:
                latest = summaries[-1]
                age_hours = (time.time() - latest.stat().st_mtime) / 3600
                return {
                    "status": "success",
                    "summary": latest.read_text(),
                    "age_hours": round(age_hours, 1),
                    "path": str(latest),
                }
            return {"status": "not_found"}

    elif tool_name == "schedule":
        # Shells out to bin/oneshot.py rather than importing it, for the same
        # reason skill tools are subprocesses: this server must not inherit a
        # scheduling bug as an unhandled exception in its JSON-RPC loop.
        action = args.get("action", "list")
        oneshot_bin = WORKSPACE / "bin" / "oneshot.py"
        if not oneshot_bin.exists():
            return {"error": f"Scheduler primitive not found: {oneshot_bin}"}

        if action == "create":
            label = args.get("label", "").strip()
            when = args.get("when", "").strip()
            if not label:
                return {"error": "A label is required to create a scheduled item"}
            if not when:
                return {"error": "A 'when' is required (e.g. '10m' or '2026-08-09 09:00')"}
            if not args.get("message") and not args.get("command"):
                return {"error": "Provide a message to deliver or a command to run"}
            cmd = ["python3", str(oneshot_bin), "--json", "schedule",
                   "--label", label, "--when", when]
            if args.get("message"):
                cmd += ["--message", args["message"]]
            if args.get("command"):
                cmd += ["--command", args["command"]]
            if args.get("agent"):
                cmd += ["--agent", args["agent"]]
        elif action == "list":
            cmd = ["python3", str(oneshot_bin), "--json", "list"]
        elif action == "cancel":
            label = args.get("label", "").strip()
            if not label:
                return {"error": "A label is required to cancel a scheduled item"}
            cmd = ["python3", str(oneshot_bin), "--json", "cancel", label]
        else:
            return {"error": f"Unknown schedule action: {action}"}

        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=30, cwd=str(WORKSPACE))
        except subprocess.TimeoutExpired:
            return {"error": "Scheduler timed out"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"error": result.stderr.strip() or "Scheduler produced no output"}

    elif tool_name == "memory":
        action = args.get("action", "recent")
        memory_db = WORKSPACE / "data" / "memory" / "memory.db"
        if not memory_db.exists():
            return {"error": "Memory database not found"}

        conn = sqlite3.connect(str(memory_db))
        conn.row_factory = sqlite3.Row
        limit = args.get("limit", 10)

        # try/finally, because the `conn.close()` that used to sit at the foot
        # of this branch was unreachable — every action above it returns — so
        # the connection leaked on every single memory call.
        try:
            if action == "recent":
                rows = conn.execute(
                    "SELECT id, summary, importance, created_at FROM episodes "
                    "ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return {"episodes": [dict(r) for r in rows]}

            elif action == "recall":
                return recall_episodes(conn, args.get("query", ""), limit)

            elif action == "facts":
                query = args.get("query", "")
                rows = conn.execute(
                    "SELECT id, subject, content, confidence, domain FROM facts "
                    "WHERE content LIKE ? OR subject LIKE ? LIMIT ?",
                    (f"%{query}%", f"%{query}%", limit)
                ).fetchall()
                return {"facts": [dict(r) for r in rows]}

            return {"error": f"Unknown tool or action: {tool_name}"}
        finally:
            conn.close()

    elif tool_name == "discord":
        action = args.get("action", "channels")
        if action == "channels":
            channels_path = WORKSPACE / "config" / "channels.json"
            if channels_path.exists():
                return json.loads(channels_path.read_text())
            return {"channels": {}}
        elif action == "history":
            channel = args.get("channel", "general")
            limit = min(args.get("limit", 20), 50)
            # Read from JSONL capture
            today = datetime.now().strftime("%Y-%m-%d")
            log_path = WORKSPACE / "data" / "messages" / f"messages-{today}.jsonl"
            if not log_path.exists():
                return {"messages": [], "channel": channel}
            messages = []
            for line in log_path.read_text().strip().split("\n"):
                try:
                    msg = json.loads(line)
                    if msg.get("channel_name") == channel:
                        messages.append({
                            "ts": msg.get("ts", ""),
                            "author": msg.get("author_name", ""),
                            "content": msg.get("content", "")[:500],
                            "is_bot": msg.get("is_bot", False),
                        })
                except json.JSONDecodeError:
                    continue
            return {"messages": messages[-limit:], "channel": channel}
        elif action == "online":
            return {"error": "Online member list requires Discord API access"}

    elif tool_name == "taskboard":
        # Simple file-based task tracking
        tasks_file = WORKSPACE / "data" / "taskboard.json"
        action = args.get("action", "list")

        tasks = []
        if tasks_file.exists():
            tasks = json.loads(tasks_file.read_text()).get("tasks", [])

        if action == "list":
            return {"tasks": tasks}
        elif action == "add":
            task = {
                "id": f"task-{int(time.time())}",
                "title": args.get("title", "Untitled"),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            tasks.append(task)
            tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
            return {"task": task}
        elif action == "complete":
            task_id = args.get("id", "")
            for task in tasks:
                if task["id"] == task_id:
                    task["status"] = "done"
                    task["completed_at"] = datetime.now(timezone.utc).isoformat()
                    tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
                    return {"task": task}
            return {"error": f"Task not found: {task_id}"}

    elif tool_name == "vault":
        action = args.get("action", "status")
        vault_dir = WORKSPACE / "vault"
        if not vault_dir.exists():
            return {"error": "Vault directory not found. Create it with: git clone <repo> vault/"}

        if action == "status":
            result = subprocess.run(
                ["git", "status", "--short"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"status": result.stdout.strip(), "clean": not result.stdout.strip()}
        elif action == "pull":
            result = subprocess.run(
                ["git", "pull"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}
        elif action == "push":
            msg = args.get("message", "Auto-commit from vault tool")
            subprocess.run(["git", "add", "-A"], cwd=str(vault_dir))
            subprocess.run(["git", "commit", "-m", msg], cwd=str(vault_dir))
            result = subprocess.run(
                ["git", "push"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}

    return {"error": f"Unknown tool or action: {tool_name}"}


def handle_skill_tool(tool: dict, args: dict) -> dict:
    """Dispatch to a skill script."""
    skill_dir = Path(tool.get("_skill_dir", ""))
    scripts_dir = skill_dir / "scripts"

    # Find the implementation script
    script = None
    for ext in [".py", ".sh"]:
        candidate = scripts_dir / f"{tool['name']}{ext}"
        if candidate.exists():
            script = candidate
            break
    # Also check for a main script
    if not script:
        for ext in [".py", ".sh"]:
            candidate = scripts_dir / f"main{ext}"
            if candidate.exists():
                script = candidate
                break

    if not script:
        return {"error": f"No implementation script found for tool '{tool['name']}'"}

    # Execute skill script
    try:
        env = os.environ.copy()
        env["WORKSPACE_ROOT"] = str(WORKSPACE)
        env["TOOL_ARGS"] = json.dumps(args)

        if script.suffix == ".py":
            cmd = ["python3", str(script)]
        else:
            cmd = ["bash", str(script)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=str(skill_dir), env=env
        )

        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"Script exited with code {result.returncode}"}

        # Try to parse as JSON
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"output": result.stdout.strip()}

    except subprocess.TimeoutExpired:
        return {"error": "Skill script timed out (60s limit)"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# JSON-RPC Server
# =============================================================================

def main():
    """Run MCP tool server (JSON-RPC 2.0 over stdin/stdout)."""
    audit_conn = init_audit_db()

    # Discover all tools
    skill_tools = discover_skills()
    all_tools = CORE_TOOLS + skill_tools

    # Build tool registry
    tool_registry = {}
    for tool in all_tools:
        tool_registry[tool["name"]] = tool

    # Test mode
    if len(sys.argv) > 1 and sys.argv[1] == "--test-tool":
        if len(sys.argv) < 3:
            print("Usage: tools-server.py --test-tool <name> [args_json]")
            sys.exit(1)
        tool_name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        if tool_name not in tool_registry:
            print(f"Unknown tool: {tool_name}")
            print(f"Available: {list(tool_registry.keys())}")
            sys.exit(1)
        result = handle_core_tool(tool_name, args) if tool_name in [t["name"] for t in CORE_TOOLS] else handle_skill_tool(tool_registry[tool_name], args)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    # Main JSON-RPC loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "tools/list":
            # Return all registered tools
            tools_list = []
            for tool in all_tools:
                tools_list.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                })
            response = {
                "jsonrpc": "2.0",
                "result": {"tools": tools_list},
                "id": req_id,
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            args_json = json.dumps(args)

            if len(args_json) > MAX_ARGS_SIZE:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": f"Arguments too large ({len(args_json)} > {MAX_ARGS_SIZE})"},
                    "id": req_id,
                }
            elif tool_name not in tool_registry:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    "id": req_id,
                }
            else:
                tool = tool_registry[tool_name]

                # Validate args
                validation_error = validate_args(args, tool.get("inputSchema", {}))
                if validation_error:
                    response = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32602, "message": validation_error},
                        "id": req_id,
                    }
                else:
                    start = time.time()
                    try:
                        if tool_name in [t["name"] for t in CORE_TOOLS]:
                            result = handle_core_tool(tool_name, args)
                        else:
                            result = handle_skill_tool(tool, args)

                        duration_ms = (time.time() - start) * 1000
                        result_json = json.dumps(result)
                        log_audit(audit_conn, tool_name, args_json, len(result_json), duration_ms, True)
                        write_health()

                        response = {
                            "jsonrpc": "2.0",
                            "result": {"content": [{"type": "text", "text": result_json}]},
                            "id": req_id,
                        }
                    except Exception as e:
                        duration_ms = (time.time() - start) * 1000
                        log_audit(audit_conn, tool_name, args_json, 0, duration_ms, False, str(e))

                        response = {
                            "jsonrpc": "2.0",
                            "error": {"code": -32603, "message": str(e)},
                            "id": req_id,
                        }
        else:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
                "id": req_id,
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
