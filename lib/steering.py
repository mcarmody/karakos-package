"""Mid-turn steering and burst coalescing (step 2.5). Pure, stdlib only.

The CLI accepts a user line on stdin at any time and is itself the queue: a line
that arrives while a tool call is in flight is delivered right after that tool's
result, inside the same turn; otherwise it waits and starts the next turn. The
server therefore writes a steerable line at once and keeps a ledger of the lines
it has written that the CLI has not yet replayed (`--replay-user-messages`), so
a row is only marked COMPLETE when the turn that consumed it ends.

Everything here is per shard: `Ledger` lives in `state.steer[shard]`; nothing is
shared across shards.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, List, Optional

EDIT_PREFIX = "edit:"     # message_id prefix of 6.2 edit follow-up rows
DEFAULTS = {"enabled": True, "coalesce_ms": 300, "max_lines_per_turn": 8}
KEYS = tuple(DEFAULTS)

# Priority given to a row inserted by interrupt-with-message: never steerable,
# claimed first by claim_batch.
INTERRUPT_PRIORITY = 100

# The `channel` of the server's own handoff and compact rows (step 2.6): such a
# turn is never steered into, and such a row is never steered.
HANDOFF_CHANNEL = "handoff"

PRIMARY = "primary"   # the line that opens a turn
STEER = "steer"       # a line written mid-turn


@dataclass(frozen=True)
class SteerConfig:
    enabled: bool = True
    coalesce_ms: float = 300
    max_lines_per_turn: int = 8


def steer_config(cfg) -> SteerConfig:
    """From an agent's config dict (`cfg["steering"]`); defaults when absent, so
    an upgraded install gets steering on with a 300 ms window."""
    raw = (cfg or {}).get("steering")
    if not isinstance(raw, dict):
        raw = {}
    return SteerConfig(
        enabled=raw.get("enabled", DEFAULTS["enabled"]) is not False,
        coalesce_ms=raw.get("coalesce_ms", DEFAULTS["coalesce_ms"]),
        max_lines_per_turn=raw.get("max_lines_per_turn", DEFAULTS["max_lines_per_turn"]))


@dataclass
class SteerLine:
    row_ids: List[int]
    text: str
    channel_id: str
    written_at: float
    kind: str                                    # PRIMARY | STEER
    message_ids: List[str] = field(default_factory=list)


class Ledger:
    """Lines written to the CLI's stdin that it has not yet replayed, oldest
    first. One per shard."""

    def __init__(self, shard: str = "", logger=None):
        self.shard = shard
        self.logger = logger
        self.entries: Deque[SteerLine] = deque()
        self.unmatched = 0

    def append(self, line: SteerLine) -> None:
        """Call before the stdin write is awaited, so a replay can never beat
        its entry."""
        self.entries.append(line)

    def remove(self, line: SteerLine) -> None:
        """Take an entry back out (the write raised)."""
        try:
            self.entries.remove(line)
        except ValueError:
            pass

    def match_replay(self, text: str) -> List[SteerLine]:
        """Consume entries from the head while the "\\n"-join of their texts
        equals `text` (both rstripped); return them. No matching head prefix:
        [], one WARNING with the lengths (never the texts), entries stay."""
        want = (text or "").rstrip()
        taken: List[SteerLine] = []
        parts: List[str] = []
        for entry in self.entries:
            parts.append(entry.text)
            taken.append(entry)
            if "\n".join(parts).rstrip() == want:
                for _ in taken:
                    self.entries.popleft()
                return taken
        self.unmatched += 1
        if self.logger is not None:
            self.logger.warning(
                f"steer shard={self.shard} replay matched no pending line "
                f"(replay_len={len(want)} pending={len(self.entries)} "
                f"head_len={len(self.entries[0].text.rstrip()) if self.entries else 0})")
        return []

    def pending(self) -> List[SteerLine]:
        return list(self.entries)

    def drain_all(self) -> List[SteerLine]:
        out = list(self.entries)
        self.entries.clear()
        return out


# -- decisions ---------------------------------------------------------------

def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def paused(state, shard) -> bool:
    """True while the usage gate (breaker, budget, governor) defers the shard.
    A steered write bypasses before_claim, so it must be checked here too."""
    view = getattr(state, "paused_view", None)
    if callable(view):
        return bool(view(shard))
    gate = getattr(state, "usage_gate", None)
    return bool(gate and shard in getattr(gate, "paused", {}))


def steerable(state, shard, row) -> bool:
    """May `row` be written into the turn now in flight on `shard`?"""
    cfg = steer_config(state.cfg(shard))
    if not cfg.enabled:
        return False
    proc = state.agent_processes.get(shard)
    if proc is None or getattr(proc, "returncode", None) is not None:
        return False
    batch = state.active_turns.get(shard)
    if batch is None or getattr(batch, "phase", None) != "streaming":
        return False
    if getattr(batch, "interrupting", False):
        return False
    # A hive call turn is never steered into (the human text would be merged
    # into a reply addressed to another shard); nor an internal handoff turn.
    if getattr(batch, "call_id", None) or any(_get(r, "call_id") for r in batch.rows):
        return False
    if batch.channel_id == HANDOFF_CHANNEL or any(
            _get(r, "channel") == HANDOFF_CHANNEL for r in batch.rows):
        return False
    if _get(row, "call_id") or _get(row, "reply_to_agent") \
            or _get(row, "channel") == HANDOFF_CHANNEL:
        return False
    if (_get(row, "priority", 0) or 0) != 0:
        return False
    # An edit follow-up (6.2) describes a change to a message this turn already
    # read; it runs as the next turn, not as another line into this one.
    if str(_get(row, "message_id", "") or "").startswith(EDIT_PREFIX):
        return False
    # A steered line is answered in the turn's channel.
    if str(_get(row, "channel_id", "")) != str(batch.channel_id):
        return False
    if getattr(batch, "steered_count", 0) >= cfg.max_lines_per_turn:
        return False
    if paused(state, shard):
        return False
    return True


def allowance(state, shard) -> int:
    cfg = steer_config(state.cfg(shard))
    batch = state.active_turns.get(shard)
    used = getattr(batch, "steered_count", 0) if batch is not None else 0
    return max(0, cfg.max_lines_per_turn - used)


_AGE_FMT = "%Y-%m-%d %H:%M:%S"


def _epoch(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.strptime(str(value)[:19].replace("T", " "), _AGE_FMT).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def coalesce_wait_s(oldest_row, now: Optional[float], cfg, queued: int = 0,
                    full_batch: int = 20) -> float:
    """Seconds to wait before claiming so a burst lands in one batch. 0 when
    coalescing is off, when the oldest queued row is a call or priority row, or
    when the queue already holds a full batch. The window is measured from the
    oldest row's age (not sliding)."""
    sc = steer_config(cfg) if not isinstance(cfg, SteerConfig) else cfg
    if not sc.enabled or not sc.coalesce_ms or sc.coalesce_ms <= 0:
        return 0.0
    if _get(oldest_row, "call_id") or (_get(oldest_row, "priority", 0) or 0) > 0:
        return 0.0
    if queued >= full_batch:
        return 0.0
    created = _epoch(_get(oldest_row, "created_at"))
    if now is None:
        now = time.time()
    age = max(0.0, now - created) if created is not None else 0.0
    return max(0.0, sc.coalesce_ms / 1000.0 - age)
