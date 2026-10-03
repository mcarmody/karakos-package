"""config/monitor.yaml (optional); defaults are built in."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from findings import make

DEFAULTS = {
    "alerts": {"channel": "signals", "critical_channel": "signals",
               "mention_env": "OWNER_DISCORD_ID", "repeat_critical_s": 1800,
               "max_repeats": 3, "max_posts_per_10min": 6},
    "thresholds": {"stall_s": 120, "tool_stall_s": 900},
    "reap_orphans": False,
}


def load(workspace):
    """-> (config, findings). An invalid file gives defaults and a finding."""
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(workspace) / "config" / "monitor.yaml"
    if not path.is_file():
        return cfg, []
    try:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
        for section in ("alerts", "thresholds"):
            sub = data.get(section) or {}
            if not isinstance(sub, dict):
                raise ValueError(f"{section} must be a mapping")
            for k, v in sub.items():
                if k not in DEFAULTS[section]:
                    raise ValueError(f"unknown key {section}.{k}")
                if type(v) is not type(DEFAULTS[section][k]):
                    raise ValueError(f"{section}.{k} has the wrong type")
                cfg[section][k] = v
        if "reap_orphans" in data:
            if not isinstance(data["reap_orphans"], bool):
                raise ValueError("reap_orphans must be a boolean")
            cfg["reap_orphans"] = data["reap_orphans"]
        return cfg, []
    except Exception as e:
        return copy.deepcopy(DEFAULTS), [make(
            "monitor-config-invalid", "config/monitor.yaml", "warn",
            f"config/monitor.yaml is invalid, using defaults: {e}")]
