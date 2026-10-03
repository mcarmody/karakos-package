"""Operator pause (spec 6.4): hold an agent's queue without killing anything.

`PauseStore` is the file `data/operator-pause.json`: `{"pauses": {"<shard>":
{since, until, by, reason}}}`, keyed by shard id, written atomically. It is a
new file created on first use; it holds no 1.x data.

`install(state, on_change)` registers `operator_pause_before_claim` once. A
paused shard defers its drain with no state change (the 2.0 contract); a turn
already running finishes normally. A timed pause arms one task per shard that
removes the entry and drains at `until`. `paused_view` is the single read of
"is this shard held, and why" (operator pause first, then the usage gate).

stdlib only; runtime state keyed by shard id.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import msgqueue
import turn_loop

# Patched by tests to move time.
_now = time.time
RESUME_POLL_S = 30.0
MIN_MINUTES = 1
MAX_MINUTES = 1440
REASON = "manual"

_log = logging.getLogger("operator_pause")


class PauseStore:
    def __init__(self, path):
        self.path = Path(path)
        self.pauses: Dict[str, dict] = {}

    def load(self) -> "PauseStore":
        """Read the file. A missing or corrupt file is empty (one warning)."""
        self.pauses = {}
        try:
            raw = json.loads(self.path.read_text())
            entries = raw["pauses"]
            if not isinstance(entries, dict):
                raise ValueError("pauses is not an object")
        except FileNotFoundError:
            return self
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.warning(f"operator pause file unreadable ({e}); treating as empty")
            return self
        for shard, e in entries.items():
            if isinstance(e, dict):
                until = e.get("until")
                self.pauses[str(shard)] = {
                    "since": e.get("since"),
                    "until": until if isinstance(until, (int, float)) else None,
                    "by": e.get("by"), "reason": e.get("reason") or REASON}
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"pauses": self.pauses}, indent=1))
        os.replace(tmp, self.path)

    def active(self, shard: str, now: Optional[float] = None) -> Optional[dict]:
        e = self.pauses.get(shard)
        if e is None:
            return None
        now = _now() if now is None else now
        if e["until"] is not None and e["until"] <= now:
            return None
        return e

    def pause(self, shards: Iterable[str], minutes: Optional[int], by: Optional[str],
              now: Optional[float] = None) -> Optional[float]:
        """Pause every shard; returns `until` (epoch) or None for no end."""
        if minutes is not None and not (MIN_MINUTES <= minutes <= MAX_MINUTES):
            raise ValueError(f"minutes must be {MIN_MINUTES} to {MAX_MINUTES} or null")
        now = _now() if now is None else now
        until = now + minutes * 60 if minutes is not None else None
        for s in shards:
            self.pauses[s] = {"since": now, "until": until, "by": by, "reason": REASON}
        self.save()
        return until

    def resume(self, shards: Iterable[str]) -> List[str]:
        gone = [s for s in shards if self.pauses.pop(s, None) is not None]
        if gone:
            self.save()
        return gone

    def expired(self, now: Optional[float] = None) -> List[str]:
        now = _now() if now is None else now
        return [s for s, e in self.pauses.items()
                if e["until"] is not None and e["until"] <= now]


class PauseState:
    def __init__(self, store: PauseStore, on_change: Optional[Callable] = None):
        self.store = store
        self.on_change = on_change
        self.timers: Dict[str, asyncio.Task] = {}
        self.notified: set = set()      # (shard, since, channel)


def _hhmm(epoch) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M UTC")


def notice_text(until) -> str:
    when = f"until {_hhmm(until)}" if until else "until it is resumed"
    return f"This agent is paused {when} by the owner."


def _srv(state, name, default=None):
    try:
        return getattr(state._server, name)
    except AttributeError:
        return default


def paused_view(state, shard: str) -> Optional[dict]:
    """{reason, until} while the shard is held: an operator pause first
    (reason "manual"), then the usage gate. None when it is not held."""
    op = getattr(state, "operator_pause", None)
    if op is not None:
        e = op.store.active(shard)
        if e is not None:
            return {"reason": e["reason"], "until": e["until"]}
    gate = getattr(state, "usage_gate", None)
    p = gate.paused.get(shard) if gate is not None else None
    return {"reason": p[0], "until": p[1]} if p else None


def _changed(state, shard) -> None:
    cb = state.operator_pause.on_change
    if cb is not None:
        try:
            cb(shard)
        except Exception as e:  # noqa: BLE001 — a beacon must not fail a pause
            state.log.debug(f"pause beacon skipped: {e}")


def _cancel_timer(ps: PauseState, shard: str) -> None:
    t = ps.timers.pop(shard, None)
    if t is not None and t is not asyncio.current_task() and not t.done():
        t.cancel()


def _arm(state, shard: str) -> None:
    """One timer per shard: at `until`, drop the entry and drain."""
    ps = state.operator_pause
    entry = ps.store.pauses.get(shard)
    _cancel_timer(ps, shard)
    if entry is None or entry["until"] is None:
        return
    until = entry["until"]

    async def wake():
        while True:
            remaining = until - _now()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, RESUME_POLL_S))
        cur = ps.store.pauses.get(shard)
        if cur is not None and cur["until"] == until:
            ps.store.resume([shard])
            ps.timers.pop(shard, None)
            _changed(state, shard)
            await turn_loop.drain_shard(state, shard)

    ps.timers[shard] = asyncio.create_task(wake())


async def pause(state, shards: List[str], minutes: Optional[int],
                by: Optional[str] = None) -> Optional[float]:
    """Pause `shards`. Raises ValueError for minutes out of range."""
    ps = state.operator_pause
    until = ps.store.pause(shards, minutes, by)
    for s in shards:
        ps.notified = {k for k in ps.notified if k[0] != s}
        _arm(state, s)
        try:
            await msgqueue.fail_calls(state.db, s, "callee_paused", "manual pause")
        except Exception as e:  # noqa: BLE001
            state.log.debug(f"fail_calls skipped: {e}")
        _changed(state, s)
    return until


async def resume(state, shards: List[str]) -> List[str]:
    ps = state.operator_pause
    gone = ps.store.resume(shards)
    for s in shards:
        _cancel_timer(ps, s)
        ps.notified = {k for k in ps.notified if k[0] != s}
    for s in shards:
        _changed(state, s)
    for s in shards:
        await turn_loop.drain_shard(state, s)
    return gone


async def _before_claim(state, shard: str) -> Optional[str]:
    ps = state.operator_pause
    entry = ps.store.active(shard)
    if entry is None:
        return None
    # A call that landed during the pause is answered now, not at queue expiry.
    try:
        await msgqueue.fail_calls(state.db, shard, "callee_paused", "manual pause")
    except Exception as e:  # noqa: BLE001
        state.log.debug(f"fail_calls skipped: {e}")
    claimable = await msgqueue.peek_claimable(state.db, shard, 20)
    human = next((r for r in claimable if not r["is_bot"]
                  and str(r["channel_id"]) not in ("", "0")), None)
    if human is not None:
        key = (shard, entry["since"], str(human["channel_id"]))
        if key not in ps.notified:
            ps.notified.add(key)
            try:
                await state.post_to_discord(shard, human["channel_id"],
                                            notice_text(entry["until"]))
            except Exception as e:  # noqa: BLE001
                state.log.warning(f"pause notice failed for {shard}: {e}")
    return REASON_DEFER


REASON_DEFER = "paused:manual"


def install(state, on_change: Optional[Callable] = None) -> PauseState:
    """Load the store, re-arm timers, register the gate and the view (once)."""
    ps = getattr(state, "operator_pause", None)
    if isinstance(ps, PauseState):
        return ps
    path = Path(_srv(state, "WORKSPACE_ROOT", ".")) / "data" / "operator-pause.json"
    store = PauseStore(path).load()
    ps = PauseState(store, on_change)
    state.operator_pause = ps
    state.paused_view = lambda shard: paused_view(state, shard)

    # Expired entries are dropped at startup; the rest keep their timers.
    dropped = store.expired()
    if dropped:
        store.resume(dropped)
    state.log.debug(f"operator pause: {len(store.pauses)} active, {len(dropped)} expired")

    async def hook(shard):
        return await _before_claim(state, shard)

    state.hooks.register("before_claim", hook)
    return ps


def rearm_all(state) -> None:
    """Re-arm timers for entries read at startup (needs a running loop)."""
    ps = state.operator_pause
    for s in list(ps.store.pauses):
        _arm(state, s)
