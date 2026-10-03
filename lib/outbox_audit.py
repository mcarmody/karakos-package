"""Audit message delivery through one adapter (`read_state`) that 6.1 swaps.

Reads only: never deletes or retries (the flusher and the outbox own that)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from findings import make

STUCK_S = 600
DEFERRED_S = 900


@dataclass
class OutboxState:
    source: str = "legacy"
    pending: int = 0
    oldest_pending_age_s: Optional[float] = None
    dead: int = 0
    newest_dead_age_s: Optional[float] = None
    deferred: int = 0
    oldest_deferred_age_s: Optional[float] = None
    invalid: int = 0
    newest_dead_at: Optional[float] = None   # additive: lets audit see an mtime change


def read_state(workspace, now=None) -> OutboxState:
    ws = Path(workspace)
    now = time.time() if now is None else now
    if (ws / "data" / "outbox" / "outbox.db").exists():
        # 6.1 fills this branch with its own outbox layout.
        return OutboxState(source="outbox")
    st = OutboxState()
    dead = ws / "data" / "discord-dead-letter.jsonl"
    try:
        mt = dead.stat().st_mtime
        with open(dead, "rb") as f:
            st.dead = sum(1 for line in f if line.strip())
        if st.dead:
            st.newest_dead_at, st.newest_dead_age_s = mt, max(now - mt, 0.0)
    except OSError:
        pass
    ddir = ws / "data" / "deferred-messages"
    ages = []
    for p in ddir.glob("*.json") if ddir.is_dir() else []:
        try:
            ages.append(max(now - p.stat().st_mtime, 0.0))
        except OSError:
            pass
    st.deferred = len(ages)
    st.oldest_deferred_age_s = max(ages) if ages else None
    for sub in ("invalid", "stale"):
        d = ddir / sub
        if d.is_dir():
            st.invalid += sum(1 for p in d.iterdir() if p.is_file())
    return st


def audit(state: OutboxState, config, prev) -> list:
    prev = prev or {}
    out = []
    if state.dead and (state.dead != prev.get("dead")
                       or state.newest_dead_at != prev.get("newest_dead_at")):
        out.append(make("outbox-dead", "discord", "warn",
                        f"{state.dead} undelivered Discord message(s) in the dead-letter file",
                        detail={"dead": state.dead}))
    if state.pending and (state.oldest_pending_age_s or 0) > STUCK_S:
        out.append(make("outbox-stuck", "discord", "critical",
                        f"{state.pending} outbound message(s) pending, oldest "
                        f"{state.oldest_pending_age_s / 60:.0f} min"))
    if state.deferred and (state.oldest_deferred_age_s or 0) > DEFERRED_S:
        out.append(make("inbound-deferred", "discord", "warn",
                        f"{state.deferred} inbound message(s) deferred, oldest "
                        f"{state.oldest_deferred_age_s / 60:.0f} min: the server was down or refusing"))
    if state.invalid:
        out.append(make("inbound-invalid", "discord", "info",
                        f"{state.invalid} deferred inbound message(s) set aside as invalid or stale"))
    return out


def prev_path(workspace) -> Path:
    return Path(workspace) / "data" / "health" / "outbox-audit.json"


def load_prev(workspace):
    try:
        d = json.loads(prev_path(workspace).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def prev_of(state: OutboxState) -> dict:
    return {"dead": state.dead, "newest_dead_at": state.newest_dead_at}
