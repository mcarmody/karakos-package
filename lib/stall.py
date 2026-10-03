"""Stall diagnosis: say why a shard is silent, not only that it is.

`diagnose` is pure over a beacon, a procinfo-shaped view (`ctx["proc"]`) and the
other shards' beacons; first match wins. Per-shard facts are keyed by shard id.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import procinfo
from findings import make

ACTIVE_STATES = frozenset({"PROCESSING", "ERROR_RECOVERY"})
DEFAULT_CONFIG = {"stall_s": 120, "tool_stall_s": 900}


@dataclass
class Diagnosis:
    cause: str
    why: str
    severity: str
    evidence: dict = field(default_factory=dict)


def _mins(seconds):
    return f"{seconds / 60:.1f} min"


def _silent(beacon, now):
    if beacon.get("silent_for") is not None:
        return float(beacon["silent_for"])
    ts = beacon.get("last_activity")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return 0.0
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.astimezone()
        return max(now - ts.timestamp(), 0.0)
    return 0.0


def diagnose(beacon, ctx, config=None, _depth=0) -> Diagnosis:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    stall_s, tool_s = cfg["stall_s"], cfg["tool_stall_s"]
    proc = ctx.get("proc") or procinfo
    now = ctx.get("now")
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    silent = _silent(beacon, now)
    pid = beacon.get("proc_pid")
    ev = {"shard": beacon.get("shard") or beacon.get("agent"), "silent_s": round(silent),
          "phase": beacon.get("phase"), "last_event_type": beacon.get("last_event_type")}

    if beacon.get("state") == "PROCESSING" and pid and not proc.alive(pid):
        return Diagnosis("process_dead", f"the claude process (pid {pid}) is gone; "
                         "the respawn watcher should restart it", "critical", {**ev, "proc_pid": pid})
    if pid and proc.proc_state(pid) == "T":
        return Diagnosis("process_stopped", f"the claude process (pid {pid}) is stopped "
                         "(SIGSTOP), it will not move until continued", "critical", {**ev, "proc_pid": pid})
    paused = beacon.get("paused")
    if paused:
        return Diagnosis("paused_by_gate", f"paused by the usage gate ({paused.get('reason')}), "
                         f"resumes at {paused.get('until')}", "info", {**ev, "paused": paused})
    held = beacon.get("held_until")
    if held and held > now:
        return Diagnosis("held_by_wall", f"held at the usage wall for another "
                         f"{_mins(held - now)}", "info", {**ev, "held_until": held})
    if beacon.get("ask_pending"):
        return Diagnosis("waiting_on_user", "waiting for the user to answer a question",
                         "info", ev)
    blocked = beacon.get("blocked_on")
    if blocked:
        callee = blocked.get("callee")
        sev = "warn" if silent > tool_s else "info"
        why = f"blocked in a hive call to {callee}"
        cb = (ctx.get("beacons") or {}).get(callee)
        if cb and _depth == 0 and cb.get("state") in ACTIVE_STATES and _silent(cb, now) > stall_s:
            sub = diagnose(cb, ctx, cfg, _depth=1)
            why += f", which is itself stalled: {sub.why}"
        return Diagnosis("blocked_in_hive_call", why, sev, {**ev, "blocked_on": blocked})
    kids = proc.children(pid) if pid else []
    if kids:
        k = max(kids, key=lambda c: c.get("age_s") or 0)
        age = k.get("age_s") or 0.0
        sev = "warn" if age > tool_s else "info"
        return Diagnosis("tool_running",
                         f'waiting on a tool: "{k.get("argv")}" has run for {_mins(age)} '
                         f'(child pid {k.get("pid")})', sev,
                         {**ev, "child_pid": k.get("pid"), "child_age_s": round(age)})
    if silent > stall_s:
        sev = "critical" if silent > 5 * stall_s else "warn"
        return Diagnosis("model_silent", f"the model has sent nothing for {_mins(silent)} "
                         "and no tool is running", sev, ev)
    return Diagnosis("unknown", "silent but no cause found", "warn", ev)


def read_beacons(workspace) -> dict:
    """shard -> beacon dict (last_activity as epoch) for every readable beacon."""
    out = {}
    d = Path(workspace) / "data" / "health" / "agents"
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            b = json.loads(p.read_text())
            ts = datetime.fromisoformat(b["last_activity"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(b, dict):
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        b["last_activity_epoch"] = ts.timestamp()
        out[b.get("shard") or b.get("agent") or p.stem] = b
    return out


def stall_stage(workspace, now, config, proc=None):
    """Findings `stall:<shard>` for every shard claiming a turn and silent past stall_s."""
    cfg = {**DEFAULT_CONFIG, **(config or {}).get("thresholds", {})}
    beacons = read_beacons(workspace)
    t = now.timestamp()
    ctx = {"proc": proc or procinfo, "now": t, "beacons": beacons}
    out = []
    for shard, b in beacons.items():
        if b.get("state") not in ACTIVE_STATES:
            continue
        silent = t - b["last_activity_epoch"]
        if silent <= cfg["stall_s"]:
            continue
        b = {**b, "silent_for": silent}
        d = diagnose(b, ctx, cfg)
        out.append(make("stall", shard, d.severity,
                         f"silent {_mins(silent)}. Why: {d.why}",
                         datetime.fromtimestamp(b["last_activity_epoch"], timezone.utc).isoformat(),
                         {"cause": d.cause, **d.evidence}))
    return out
