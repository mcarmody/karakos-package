"""Per-agent token budget with minimum-pause hysteresis (spec 2.7).

Usage is input plus output tokens summed over a fixed trailing four hours and
over *all of the agent's shards* (cost_events rows stay keyed by shard id). A
pause covers every shard of the agent. stdlib only.
"""
import logging
import time
from typing import Dict, Iterable, Optional, Tuple

TOKEN_BUDGET_WINDOW_S = 4 * 3600
TOKEN_BUDGET_MIN_PAUSE_S = 1800
TOKEN_BUDGET_NOTICE_COOLDOWN_S = 3600
TOKEN_USAGE_CACHE_S = 60

_log = logging.getLogger("token_budget")


def pause_state(now, usage, budget, paused_since,
                min_pause_s=TOKEN_BUDGET_MIN_PAUSE_S) -> Tuple[bool, Optional[float]]:
    """Not paused and usage at or over budget pauses (recording `now`). Paused
    stays paused while inside the minimum pause **or** still over budget, and
    keeps its original `paused_since`; otherwise it resumes."""
    if paused_since is None:
        if usage >= budget:
            return True, now
        return False, None
    if now - paused_since < min_pause_s or usage >= budget:
        return True, paused_since
    return False, None


def should_announce_pause(now, last_notice,
                          cooldown_s=TOKEN_BUDGET_NOTICE_COOLDOWN_S) -> bool:
    return last_notice is None or now - last_notice >= cooldown_s


# agent key -> (cached_at, value). Keyed by the sorted shard tuple so a
# changed shard set never reads a stale sum.
_cache: Dict[tuple, Tuple[float, int]] = {}


async def usage_in_window(db, shard_ids: Iterable[str],
                          window_s=TOKEN_BUDGET_WINDOW_S, now=None,
                          use_cache=True) -> int:
    """Tokens used by `shard_ids` in the trailing window. Any error returns 0
    (fails open: an unreadable database never mutes an agent). Cached for
    TOKEN_USAGE_CACHE_S unless `use_cache` is false (resume evaluation)."""
    ids = tuple(sorted(shard_ids))
    if not ids:
        return 0
    now = time.time() if now is None else now
    key = (ids, window_s)
    hit = _cache.get(key)
    if use_cache and hit and 0 <= now - hit[0] < TOKEN_USAGE_CACHE_S:
        return hit[1]
    try:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS used"
            f" FROM cost_events WHERE agent IN ({','.join('?' * len(ids))})"
            " AND timestamp >= datetime(?, 'unixepoch')",
            (*ids, now - window_s))
        used = int(rows[0][0] or 0)
    except Exception as e:  # noqa: BLE001 — fail open by design
        _log.debug(f"token usage unreadable: {type(e).__name__}: {e}")
        return 0
    _cache[key] = (now, used)
    return used
