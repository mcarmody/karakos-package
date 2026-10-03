"""Fork and customised-install policy: name what the migrator does not know.

Known sets come from docs/migration-inventory.md (the union over the nine 1.x
tags). Anything outside them is reported; the runner refuses (exit 3) unless
--force, and a forced run lists it in migration-report.md. memory.db is covered
by the memory step's own preflight.
"""
import json
from pathlib import Path

from lib.migrate.detect import _tables

AGENT_DB_COLUMNS = {
    "message_queue": {"id", "agent", "channel", "channel_id", "server", "author",
                      "author_id", "is_bot", "content", "message_id", "mentions_agent",
                      "attachments", "processed", "response", "discord_response_id",
                      "created_at", "processing_started_at", "processed_at"},
    "sessions": {"agent", "session_id", "input_tokens", "compaction_count",
                 "last_compacted"},
    "cost_events": {"id", "agent", "cost_delta", "session_total", "input_tokens",
                    "output_tokens", "duration_ms", "timestamp", "session_id"},
    "turn_events": {"id", "message_id", "seq", "kind", "content", "created_at"},
    "rate_limit_state": {"agent", "status", "rate_limit_type", "resets_at",
                         "overage_status", "is_using_overage", "alerted_for_resets_at",
                         "updated_at"},
}
SQLITE_INTERNAL_PREFIXES = ("sqlite_",)

# agents.json agent keys the registry carries (lib/registry.py) plus the two
# discord keys it folds into `discord:`
AGENT_KEYS = {"system_prompt", "prompt", "model", "max_turns", "timeout", "tool_streaming",
              "stream_to_channel", "dashboard_chat", "allowed_tools", "disallowed_tools",
              "env", "label", "token_budget_4h", "token_budget_min_pause_s",
              "work_stealing", "discord_bot_token_env", "discord_bot_id_env", "role"}
AGENTS_TOP_KEYS = {"agents"}
CHANNELS_TOP_KEYS = {"server_id", "channels"}
CHANNEL_KEYS = {"id", "default_agent"}


def _agent_db(data: Path):
    for p in (data / "memory" / "agent-server.db", data / "agent.db"):
        if p.is_file():
            return p
    return None


def _load(path: Path):
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def check(data_dir, config_dir) -> list:
    """Lines naming unknown tables, columns and config keys. Read-only."""
    data, cfg = Path(data_dir), Path(config_dir)
    out = []
    db = _agent_db(data)
    schema = _tables(db, []) if db else None
    for t in sorted(schema or {}):
        if t.startswith(SQLITE_INTERNAL_PREFIXES):
            continue
        if t not in AGENT_DB_COLUMNS:
            out.append(f"{db.name}: unknown table {t} ({len(schema[t])} columns)")
            continue
        for c in sorted(schema[t] - AGENT_DB_COLUMNS[t]):
            out.append(f"{db.name}: unknown column {t}.{c}")
    agents = _load(cfg / "agents.json")
    if agents is not None:
        for k in sorted(set(agents) - AGENTS_TOP_KEYS):
            out.append(f"agents.json: unknown top-level key {k}")
        for name, a in sorted((agents.get("agents") or {}).items()):
            for k in sorted(set(a if isinstance(a, dict) else {}) - AGENT_KEYS):
                out.append(f"agents.json: agent {name}: unknown key {k}")
    chans = _load(cfg / "channels.json")
    if chans is not None:
        for k in sorted(set(chans) - CHANNELS_TOP_KEYS):
            out.append(f"channels.json: unknown top-level key {k}")
        for name, c in sorted((chans.get("channels") or {}).items()):
            for k in sorted(set(c if isinstance(c, dict) else {}) - CHANNEL_KEYS):
                out.append(f"channels.json: channel {name}: unknown key {k}")
    return out
