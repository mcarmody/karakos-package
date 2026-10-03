"""hookio.py — shared stdin/stdout/deny-log plumbing for the Bash PreToolUse rails."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def read_bash_payload() -> dict | None:
    """Parsed hook payload if this is a Bash tool call with a command, else None."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return payload


def log_block(label: str, command: str, root: str | None = None) -> None:
    """Append one JSONL line to $WORKSPACE_ROOT/logs/blocked-bash.jsonl. Never raises."""
    root = root or os.environ.get("WORKSPACE_ROOT")
    if not root:
        return
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": label, "command": command[:500]}
        with open(os.path.join(logs, "blocked-bash.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def deny(label: str, reason: str, command: str) -> None:
    log_block(label, command)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
