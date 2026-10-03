"""Process facts from /proc (stdlib only, Linux). Every reader returns None or an
empty value for a process that is gone; nothing here raises on a vanished pid."""
from __future__ import annotations

import os
import re
from pathlib import Path

PROC = Path("/proc")
try:
    _HZ = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):
    _HZ = 100

_SECRET_NAME = re.compile(r"(token|key|secret|passw|auth|credential)", re.I)
_TOKEN_SHAPE = re.compile(r"^(sk-|ghp_|gho_|xox|AKIA)|^[A-Za-z0-9_\-\.]{24,}$")


def _stat_fields(pid):
    """Fields after the command name: [state, ppid, pgrp, session, ...] or None."""
    try:
        raw = (PROC / str(int(pid)) / "stat").read_text()
    except (OSError, ValueError):
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    return raw[end + 2:].split()


def proc_state(pid):
    """One of R S D T Z (others pass through); None when the pid is gone."""
    f = _stat_fields(pid)
    return f[0] if f else None


def alive(pid):
    s = proc_state(pid)
    return s is not None and s != "Z"


def ppid(pid):
    f = _stat_fields(pid)
    return int(f[1]) if f and len(f) > 1 else None


def pgrp(pid):
    f = _stat_fields(pid)
    return int(f[2]) if f and len(f) > 2 else None


def starttime(pid):
    """Field 22 of /proc/<pid>/stat (clock ticks since boot); None when gone."""
    f = _stat_fields(pid)
    try:
        return int(f[19]) if f else None
    except (IndexError, ValueError):
        return None


def age_s(pid):
    st = starttime(pid)
    if st is None:
        return None
    try:
        up = float((PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return max(up - st / _HZ, 0.0)


def redact_arg(arg: str) -> str:
    if "=" in arg:
        name, _, val = arg.partition("=")
        if val and (_SECRET_NAME.search(name) or _TOKEN_SHAPE.search(val)):
            return f"{name}=***"
        return arg
    if _TOKEN_SHAPE.search(arg) and re.search(r"\d", arg) and re.search(r"[A-Za-z]", arg):
        return "***"
    return arg


def argv(pid, limit=80):
    """The command line, token-shaped arguments redacted, cut to `limit` chars."""
    try:
        raw = (PROC / str(int(pid)) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return ""
    parts = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
    return " ".join(redact_arg(p) for p in parts)[:limit]


def environ(pid):
    try:
        raw = (PROC / str(int(pid)) / "environ").read_bytes()
    except (OSError, ValueError):
        return {}
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def all_pids():
    try:
        return [int(n) for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return []


def _children_map():
    kids = {}
    for p in all_pids():
        pp = ppid(p)
        if pp is not None:
            kids.setdefault(pp, []).append(p)
    return kids


def children(pid):
    """Direct children: [{"pid", "argv", "age_s", "starttime"}]."""
    return [{"pid": c, "argv": argv(c), "age_s": age_s(c), "starttime": starttime(c)}
            for c in sorted(_children_map().get(int(pid), []))]


def walk_tree(pid):
    """Every descendant pid of `pid` (not `pid` itself), breadth first."""
    kids = _children_map()
    out, queue = [], list(kids.get(int(pid), []))
    seen = {int(pid)}
    while queue:
        p = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        queue.extend(kids.get(p, []))
    return out
