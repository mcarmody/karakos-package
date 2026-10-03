"""Job heartbeats: each scheduled run touches data/health/heartbeats/<name>.json.

Components (another process writes data/health/<name>.json) are read with the
same timestamp rules the 1.x health monitor used: a naive stamp is local time,
aware and `Z` stamps are honoured.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _dir(workspace) -> Path:
    return Path(workspace) / "data" / "health" / "heartbeats"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> Optional[datetime]:
    """Parse an ISO stamp into an aware datetime; None if unusable."""
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()  # naive == local time
    return ts


def read(workspace, name: str) -> Optional[dict]:
    """None when absent; {"corrupt": True} when unreadable."""
    try:
        data = json.loads((_dir(workspace) / f"{name}.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"corrupt": True}
    return data if isinstance(data, dict) else {"corrupt": True}


def touch(workspace, name: str, ok: bool = True, detail: str = "", now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    prev = read(workspace, name)
    prev_ts = prev.get("timestamp") if prev and not prev.get("corrupt") else None
    rec = {"name": name, "timestamp": now.astimezone(timezone.utc).isoformat(), "ok": bool(ok),
           "detail": detail, "previous_timestamp": prev_ts, "pid": os.getpid()}
    d = _dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{name}.json.tmp"
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, d / f"{name}.json")
    return rec


def read_component(workspace, name: str) -> Optional[dict]:
    try:
        data = json.loads((Path(workspace) / "data" / "health" / f"{name}.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"corrupt": True}
    return data if isinstance(data, dict) else {"corrupt": True}


def read_all(workspace, jobs) -> dict:
    """name -> record for every job (heartbeat file) and component (health file)."""
    out = {}
    for j in jobs:
        out[j.name] = (read_component if j.kind == "component" else read)(workspace, j.name)
    return out


def status(job, hb: Optional[dict], scheduler_started_at: Optional[datetime], now: datetime) -> str:
    """ok | failing | stale | never."""
    ts = None
    if hb is not None:
        if hb.get("corrupt"):
            return "stale"
        ts = parse_ts(hb.get("timestamp"))
        if ts is None:
            return "stale"
        if job.kind == "job" and hb.get("ok") is False:
            return "failing"
    base = max(t for t in (ts, scheduler_started_at) if t is not None) if (
        ts or scheduler_started_at) else None
    if base is None:
        return "never" if hb is None else "stale"
    if (now - base).total_seconds() > job.max_age_s:
        return "stale"
    return "ok" if ts is not None else "never"


def check_component_file(health_dir, filename: str, threshold: int, now: Optional[datetime] = None):
    """(healthy, reason) for one health file; same messages as 1.x."""
    path = Path(health_dir) / filename
    if not path.exists():
        return False, f"{filename} health file missing"
    try:
        data = json.loads(path.read_text())
        stamp = data.get("timestamp", "")
        if not stamp:
            return False, f"{filename} has no timestamp"
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.astimezone()
        age = ((now or utcnow()) - ts).total_seconds()
        if age > threshold:
            return False, f"{filename} stale ({age/60:.1f} min, threshold {threshold/60:.1f} min)"
        return True, ""
    except Exception as e:
        return False, f"{filename} error: {e}"
