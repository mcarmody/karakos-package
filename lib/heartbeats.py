"""Job heartbeats: each scheduled run touches data/health/heartbeats/<name>.json.

The touch file `<heartbeat_dir>/<job>` is the required signal (contract 07); its mtime is the last success.
`heartbeat_dir` is `$KARAKOS_HEARTBEAT_DIR`, default `~/.karakos/heartbeats`.
The JSON file `data/health/heartbeats/<job>.json` is optional detail; when both exist, the newer timestamp wins.
Readers never create or write the touch file.

Components (another process writes data/health/<name>.json) are read with the
same timestamp rules the 1.x health monitor used: a naive stamp is local time,
aware and `Z` stamps are honoured.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TOUCH_DIR_ENV = "KARAKOS_HEARTBEAT_DIR"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _dir(workspace) -> Path:
    return Path(workspace) / "data" / "health" / "heartbeats"


def touch_dir() -> Path:
    return Path(os.environ.get(TOUCH_DIR_ENV) or Path.home() / ".karakos" / "heartbeats")


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


def read_touch(name: str) -> Optional[dict]:
    """Record from the touch file's mtime; None when absent; {"corrupt": True} when unreadable. Never writes."""
    if not NAME_RE.fullmatch(name):
        return None
    p = touch_dir() / name
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return {"corrupt": True}
    if not stat.S_ISREG(st.st_mode):
        return {"corrupt": True}
    return {"name": name, "timestamp": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "ok": True, "detail": "", "source": "touch"}


def read_json(workspace, name: str) -> Optional[dict]:
    """None when absent; {"corrupt": True} when unreadable."""
    try:
        data = json.loads((_dir(workspace) / f"{name}.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"corrupt": True}
    return data if isinstance(data, dict) else {"corrupt": True}


def read(workspace, name: str) -> Optional[dict]:
    """Newest of the touch file and the JSON file; see the module docstring."""
    j = read_json(workspace, name)
    t = read_touch(name)
    if j is None:
        return t
    if t is None:
        return j
    if j.get("corrupt"):
        return {"corrupt": True} if t.get("corrupt") else t
    if t.get("corrupt"):
        return j
    tj, tt = parse_ts(j.get("timestamp")), parse_ts(t.get("timestamp"))
    if tt is None:
        return j
    if tj is None:
        return t
    return t if tt > tj else j


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
