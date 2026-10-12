#!/usr/bin/env python3
"""Answers file for ``./setup.sh --answers <file.json>`` (unattended install).

The wizard asks for a fixed set of fields. This module reads the same fields
from a JSON file, resolves secrets named by environment variable, applies the
wizard's defaults and the wizard's agent-name rules, and reports *every*
problem at once. It prints the normalised answers as JSON for setup.sh to load.

    python3 lib/setup_answers.py answers.json          # JSON on stdout, exit 0
                                                       # problems on stderr, exit 2

Any field may be given literally (``"discord_bot_token": "..."``) or by the
name of an environment variable (``"discord_bot_token_env": "DISCORD_BOT_TOKEN"``).
Literal wins when both are present. Unknown keys are an error, so a typo does
not silently fall back to a default.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402  (slugify_id and the reserved monitor ids live there)

# field -> (required, default)
FIELDS = {
    "system_name": (True, None),
    "owner_name": (True, None),
    "primary_agent_name": (False, None),          # default: system_name
    "monitor_agent_name": (False, "monitor"),
    "discord_bot_token": (True, None),
    "discord_bot_id": (True, None),
    "discord_server_id": (True, None),
    "channel_general": (True, None),
    "channel_signals": (True, None),
    "channel_staff": (False, ""),
    "owner_discord_id": (True, None),
    "cost_daily_limit": (False, "25.00"),
    "cost_monthly_limit": (False, "500.00"),
    # Not asked by the wizard (it runs `claude login` in a browser). Optional here:
    # written to config/.env as CLAUDE_CODE_OAUTH_TOKEN when given.
    "claude_oauth_token": (False, ""),
}
SECRET_FIELDS = {"discord_bot_token", "claude_oauth_token"}
SNOWFLAKES = ("discord_bot_id", "discord_server_id", "channel_general",
              "channel_signals", "channel_staff", "owner_discord_id")
MONEY = ("cost_daily_limit", "cost_monthly_limit")


def _env_key(field):
    return f"{field}_env"


def resolve(raw, env):
    """-> (values, errors). Fills defaults; does not validate formats."""
    errors, values = [], {}
    if not isinstance(raw, dict):
        return {}, ["the answers file must be a JSON object"]
    known = set(FIELDS) | {_env_key(f) for f in FIELDS}
    for key in sorted(set(raw) - known):
        if key.startswith("_"):      # "_comment" style keys are allowed
            continue
        errors.append(f"unknown field '{key}'")
    for field, (required, default) in FIELDS.items():
        literal, ref = raw.get(field), raw.get(_env_key(field))
        value = None
        if literal not in (None, ""):
            if isinstance(literal, bool) or not isinstance(literal, (str, int, float)):
                errors.append(f"'{field}' must be a string or number")
                continue
            value = str(literal)
        elif ref not in (None, ""):
            if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
                errors.append(f"'{_env_key(field)}' must be an environment variable name")
                continue
            value = env.get(ref)
            if value in (None, ""):
                errors.append(f"'{field}': environment variable {ref} is not set")
                continue
        if value is None:
            if required:
                errors.append(f"missing required field '{field}'"
                              + (f" (or '{_env_key(field)}')" if field in SECRET_FIELDS else ""))
                continue
            value = default
        values[field] = value if value is None else str(value).strip()
    return values, errors


def validate(values):
    """Format and agent-name rules. Returns a list of problems."""
    errors = []
    for field, value in values.items():
        if value is not None and re.search(r"[\x00-\x1f\x7f]", value):
            # these values are written into config/.env: a newline would inject a variable
            errors.append(f"'{field}' contains a control character or newline")
    for field in SNOWFLAKES:
        v = values.get(field)
        if v and not re.fullmatch(r"\d{15,25}", v):
            errors.append(f"'{field}' must be a Discord ID (15 to 25 digits), got {v!r}")
    for field in MONEY:
        v = values.get(field)
        try:
            ok = v is not None and re.fullmatch(r"\d+(\.\d+)?", v) and float(v) > 0
        except ValueError:
            ok = False
        if not ok:
            errors.append(f"'{field}' must be a positive number of USD, got {v!r}")
    if not errors and float(values["cost_daily_limit"]) > float(values["cost_monthly_limit"]):
        errors.append("'cost_daily_limit' is larger than 'cost_monthly_limit'")
    # Same rules as the wizard: ids are slugs; the monitor may not name a system
    # component or equal the primary.
    primary = values.get("primary_agent_name") or values.get("system_name")
    monitor = values.get("monitor_agent_name")
    if primary and monitor:
        pid, mid = registry.slugify_id(primary), registry.slugify_id(monitor)
        if mid in registry.RESERVED_MONITOR_IDS:
            errors.append(f"'monitor_agent_name' {monitor!r} is reserved (names a system "
                          f"component); choose another")
        elif mid == pid:
            errors.append("'monitor_agent_name' must differ from the primary agent's name")
    return errors


def load(path, env=None):
    """Read, resolve and validate. -> (answers, errors); answers is {} on error."""
    env = os.environ if env is None else env
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}, [f"answers file not found: {path}"]
    except (OSError, ValueError) as exc:
        return {}, [f"cannot read {path} as JSON: {exc}"]
    values, errors = resolve(raw, env)
    errors += validate(values)
    if errors:
        return {}, errors
    if not values.get("primary_agent_name"):
        values["primary_agent_name"] = values["system_name"]
    return values, []


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    answers, errors = load(argv[0])
    if errors:
        print(f"{argv[0]}: {len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    json.dump(answers, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
