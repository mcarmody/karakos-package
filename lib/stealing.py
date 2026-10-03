"""Work stealing between idle shards of one agent (step 2.4). Pure, stdlib only.

An idle shard may take rows that are waiting behind a busy sibling. Off by
default. The queue-side predicates live in msgqueue.claim_stolen; this module
holds the config and the "who may steal from whom" decisions.
"""
from dataclasses import dataclass
from typing import Dict, List, Sequence

DEFAULTS = {"enabled": False, "after_s": 5, "max_rows": 5}
KEYS = tuple(DEFAULTS)


@dataclass(frozen=True)
class StealConfig:
    enabled: bool = False
    after_s: float = 5
    max_rows: int = 5


def steal_config(cfg) -> StealConfig:
    """From an agent's config dict (`cfg["work_stealing"]`); defaults when absent."""
    raw = (cfg or {}).get("work_stealing")
    if not isinstance(raw, dict):
        raw = {}
    return StealConfig(
        enabled=bool(raw.get("enabled", DEFAULTS["enabled"])),
        after_s=raw.get("after_s", DEFAULTS["after_s"]),
        max_rows=raw.get("max_rows", DEFAULTS["max_rows"]))


def min_age_s(steal_cfg: StealConfig, cfg) -> float:
    """A row younger than this is never stolen: `after_s`, raised to 2.5's
    coalescing window when steering is on (a floor, never a way around after_s)."""
    coalesce_s = 0.0
    steering = (cfg or {}).get("steering")
    if isinstance(steering, dict) and steering.get("enabled") is not False:
        coalesce_s = float(steering.get("coalesce_ms", 300) or 0) / 1000
    return max(float(steal_cfg.after_s), coalesce_s)


def candidate_victims(specs: Sequence, thief: str, states: Dict[str, str],
                      depths: Dict[str, int]) -> List[str]:
    """Same-agent shards other than the thief that are PROCESSING or
    ERROR_RECOVERY with queued rows; deepest queue first, then plan order. An
    IDLE shard is never a victim: it is about to drain its own queue."""
    agent = next((s.agent for s in specs if s.id == thief), thief)
    mine = [s.id for s in specs if s.agent == agent and s.id != thief
            and states.get(s.id) in ("PROCESSING", "ERROR_RECOVERY")
            and depths.get(s.id, 0) > 0]
    order = {s.id: i for i, s in enumerate(specs)}
    return sorted(mine, key=lambda sid: (-depths.get(sid, 0), order[sid]))


def thief_ready(state_of_thief, held, own_depth) -> bool:
    """IDLE, not held by a usage wall, own queue empty."""
    return state_of_thief == "IDLE" and not held and not own_depth
