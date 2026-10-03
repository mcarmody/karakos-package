"""Kill a process tree without leaking its children, and find strays.

Children of a killed process reparent to init and become unfindable, so the tree is
snapshotted BEFORE the root is signalled. A snapshot records each pid's start time and
`reap` re-checks it before every signal, so a recycled pid is never hit. It signals the
process group when the pid leads one of its own, never its own group, pid 1 or pid 0,
and never raises.
"""
from __future__ import annotations

import os
import signal
import time

import procinfo

INIT_COMMS = ("init", "tini", "dumb-init", "supervisord", "systemd")


def _comm(pid):
    try:
        return (procinfo.PROC / str(pid) / "comm").read_text().strip()
    except OSError:
        return None


def _ancestors():
    """This process's ancestor pids (signalling one would take down our own
    supervisor, user manager or session)."""
    out, p, seen = set(), os.getppid(), set()
    while p and p > 1 and p not in seen:
        seen.add(p)
        out.add(p)
        p = procinfo.ppid(p)
    return out


def snapshot_tree(root_pid):
    """{pid: starttime} for `root_pid` and every descendant. Empty for pid 0 or
    1, a non-integer, an init-like process or one of our own ancestors: the
    tree of pid 1 is the whole machine (2026-10-03: a test's FakeProcess(pid=1)
    reached this and SIGKILLed every process of the user running the suite,
    its systemd --user manager included)."""
    snap = {}
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return snap
    if root_pid <= 1 or _comm(root_pid) in INIT_COMMS or root_pid in _ancestors():
        return snap
    try:
        for p in [int(root_pid)] + procinfo.walk_tree(root_pid):
            st = procinfo.starttime(p)
            if st is not None:
                snap[p] = st
    except Exception:
        pass
    return snap


def _same(pid, st):
    return procinfo.starttime(pid) == st and procinfo.proc_state(pid) not in (None, "Z")


def _protected(pid, own_pgrp, ancestors=frozenset()):
    return (pid in (0, 1, os.getpid()) or pid == own_pgrp or pid in ancestors
            or _comm(pid) in INIT_COMMS)


def _send(pid, sig, own_pgrp):
    """Signal the group when `pid` leads one (and it is not ours, and holds none
    of our ancestors), else the pid."""
    if (procinfo.pgrp(pid) == pid and pid != own_pgrp
            and not any(procinfo.pgrp(a) == pid for a in _ancestors())):
        os.killpg(pid, sig)
    else:
        os.kill(pid, sig)


def reap(snapshot, grace=3.0, sleep=None):
    """-> {"termed": [...], "killed": [...], "survived": [...]}."""
    out = {"termed": [], "killed": [], "survived": []}
    try:
        sleep = sleep or time.sleep
        own = os.getpgrp()
        anc = frozenset(_ancestors())
        live = {}
        for pid, st in dict(snapshot or {}).items():
            if _protected(pid, own, anc) or not _same(pid, st):
                continue
            try:
                _send(pid, signal.SIGTERM, own)
                out["termed"].append(pid)
                live[pid] = st
            except ProcessLookupError:
                pass
            except OSError:
                out["survived"].append(pid)
        waited = 0.0
        while live and waited < grace:
            sleep(0.1)
            waited += 0.1
            live = {p: s for p, s in live.items() if _same(p, s)}
        for pid, st in list(live.items()):
            if not _same(pid, st):
                continue
            try:
                _send(pid, signal.SIGKILL, own)
                out["killed"].append(pid)
            except ProcessLookupError:
                pass
            except OSError:
                out["survived"].append(pid)
        if out["killed"]:
            sleep(0.2)
            for pid in out["killed"]:
                if _same(pid, snapshot[pid]) and pid not in out["survived"]:
                    out["survived"].append(pid)
    except Exception:
        pass
    return out


def _is_init(ppid, extra):
    if ppid in (1, 0) or ppid in extra:
        return True
    try:
        comm = (procinfo.PROC / str(ppid) / "comm").read_text().strip()
    except OSError:
        return False
    return comm in INIT_COMMS


def find_orphans(known_shard_pids, workspace=None, init_pids=()):
    """Processes carrying a KARAKOS_SHARD marker that were reparented to init and are
    not in any live shard's tree. -> {shard: [{"pid", "argv", "age_s", "starttime"}]}.
    `known_shard_pids` is an iterable of live shard process pids."""
    live = set()
    for p in known_shard_pids or ():
        live.add(int(p))
        live.update(procinfo.walk_tree(p))
    me = os.getpid()
    extra = set(init_pids)
    found = {}
    for pid in procinfo.all_pids():
        if pid in live or pid == me or pid == 1:
            continue
        shard = procinfo.environ(pid).get("KARAKOS_SHARD")
        if not shard or procinfo.proc_state(pid) in (None, "Z"):
            continue
        pp = procinfo.ppid(pid)
        if pp is None or not _is_init(pp, extra):
            continue
        found.setdefault(shard, []).append({
            "pid": pid, "argv": procinfo.argv(pid), "age_s": procinfo.age_s(pid),
            "starttime": procinfo.starttime(pid)})
    return found


def reap_orphans(orphans):
    """Reap every orphan root and its descendants; -> merged reap result."""
    total = {"termed": [], "killed": [], "survived": []}
    for items in orphans.values():
        for o in items:
            snap = snapshot_tree(o["pid"])
            if o.get("starttime") is not None and snap.get(o["pid"]) != o["starttime"]:
                continue
            r = reap(snap)
            for k in total:
                total[k].extend(r[k])
    return total
