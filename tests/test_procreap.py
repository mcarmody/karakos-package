"""Process reap and orphan sweep, against real processes."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import procinfo  # noqa: E402
import procreap  # noqa: E402

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


def wait_pid_file(path, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("child did not report")


def gone(pid):
    return not procinfo.alive(pid)


def settle(pids, timeout=5):
    end = time.time() + timeout
    while time.time() < end and not all(gone(p) for p in pids):
        time.sleep(0.05)


@pytest.fixture
def tree(tmp_path):
    """parent -> child, a setsid grandchild, and a nohup sleeper."""
    f = tmp_path / "pids"
    script = (
        "setsid sleep 300 & echo $! >> {f}; "
        "nohup sleep 300 >/dev/null 2>&1 & echo $! >> {f}; sleep 300"
    ).format(f=f)
    root = subprocess.Popen(["sh", "-c", script], stdout=subprocess.DEVNULL)
    end = time.time() + 5
    while time.time() < end and (not f.exists() or len(f.read_text().split()) < 2):
        time.sleep(0.05)
    pids = [int(x) for x in f.read_text().split()]
    yield root, pids
    for p in [root.pid] + pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    root.wait()


def test_snapshot_then_kill_root_then_reap_everything(tree):
    root, kids = tree
    snap = procreap.snapshot_tree(root.pid)
    assert root.pid in snap and all(k in snap for k in kids)
    root.kill()
    root.wait()
    res = procreap.reap(snap, grace=1.0)
    settle(kids)
    assert all(gone(k) for k in kids)
    assert set(kids) <= set(res["termed"]) and res["survived"] == []


def test_recycled_pid_is_not_signalled(tree):
    root, kids = tree
    snap = procreap.snapshot_tree(root.pid)
    snap = {p: st + 1 for p, st in snap.items()}   # start times no longer match
    res = procreap.reap(snap, grace=0.2)
    assert res == {"termed": [], "killed": [], "survived": []}
    assert all(procinfo.alive(k) for k in kids)


def test_never_signals_own_group_or_init(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda *a: sent.append(a))
    monkeypatch.setattr(os, "killpg", lambda *a: sent.append(a))
    me = os.getpid()
    snap = {1: procinfo.starttime(1) or 0, 0: 0, me: procinfo.starttime(me),
            os.getpgrp(): procinfo.starttime(os.getpgrp()) or 0}
    res = procreap.reap(snap, grace=0.1, sleep=lambda s: None)
    assert sent == [] and res["termed"] == []


def test_sigterm_ignorer_is_killed_after_grace(tmp_path):
    f = tmp_path / "ready"
    p = subprocess.Popen(["sh", "-c", f"trap '' TERM; echo 1 > {f}; while :; do sleep 1; done"])
    end = time.time() + 5
    while time.time() < end and not f.exists():
        time.sleep(0.05)
    snap = procreap.snapshot_tree(p.pid)
    res = procreap.reap(snap, grace=0.5)
    p.wait(timeout=5)
    assert p.pid in res["killed"] and p.pid in res["termed"]


def test_empty_and_dead_snapshots_are_quiet():
    assert procreap.reap({}) == {"termed": [], "killed": [], "survived": []}
    p = subprocess.Popen(["true"])
    p.wait()
    assert procreap.reap({p.pid: 1}) == {"termed": [], "killed": [], "survived": []}


def test_oserror_is_recorded_in_survived(tree, monkeypatch):
    root, kids = tree
    snap = procreap.snapshot_tree(root.pid)
    def boom(*a):
        raise PermissionError(1, "no")
    monkeypatch.setattr(os, "killpg", boom)
    monkeypatch.setattr(os, "kill", boom)
    res = procreap.reap(snap, grace=0.1, sleep=lambda s: None)
    assert set(res["survived"]) == set(snap) and res["termed"] == []


def test_orphan_sweep_reports_ghost_once_and_reaps_only_when_asked(tmp_path):
    env = {**os.environ, "KARAKOS_SHARD": "ghost"}
    out = tmp_path / "pid"
    subprocess.run(["sh", "-c", f"sleep 300 & echo $! > {out}"], env=env, check=True)
    pid = wait_pid_file(out)
    try:
        ppid = procinfo.ppid(pid)
        found = procreap.find_orphans([], tmp_path, init_pids={ppid})
        assert [o["pid"] for o in found["ghost"]] == [pid]
        # a live shard's tree is not orphaned
        assert procreap.find_orphans([pid], tmp_path, init_pids={ppid}) == {}
        assert procinfo.alive(pid)                      # detection alone kills nothing
        procreap.reap_orphans(found)
        settle([pid])
        assert gone(pid)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_child_under_live_shard_is_not_an_orphan(tmp_path):
    env = {**os.environ, "KARAKOS_SHARD": "live"}
    p = subprocess.Popen(["sleep", "30"], env=env)
    try:
        assert procreap.find_orphans([os.getpid()], tmp_path) == {}
    finally:
        p.kill()
        p.wait()


def test_tree_of_pid_1_or_an_ancestor_is_never_snapshotted():
    """Regression, 2026-10-03: kill_agent_subprocess snapshotted a test double's
    pid 1, and reap SIGKILLed the user's whole session."""
    assert procreap.snapshot_tree(1) == {}
    assert procreap.snapshot_tree(0) == {}
    assert procreap.snapshot_tree("x") == {}
    assert procreap.snapshot_tree(os.getppid()) == {}


def test_reap_never_signals_an_ancestor_or_init_like(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda *a: sent.append(a))
    monkeypatch.setattr(os, "killpg", lambda *a: sent.append(a))
    snap = {os.getppid(): procinfo.starttime(os.getppid())}
    res = procreap.reap(snap, grace=0.1, sleep=lambda s: None)
    assert sent == [] and res["termed"] == [] and res["killed"] == []
