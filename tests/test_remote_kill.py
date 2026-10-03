"""REMOTE_KILL (lib/build_run.py) compares the process start time in run.pid before
it signals, so a recycled pid/pgid is never signalled.

SAFETY: every process signalled or merely named here was started by the test
itself (a `sleep` in its own session) and is reaped by the test. No test sends a
signal to a pid it did not start; the "recycled" cases only write a run.pid whose
start time is wrong for a live process of ours, and assert it is left alone.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import build_run  # noqa: E402
import procinfo  # noqa: E402

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


@pytest.fixture
def sandbox(tmp_path):
    procs = []
    sigs = tmp_path / "signals.log"
    hook = tmp_path / "hook.sh"
    # log every kill invocation the script makes, then let the builtin run
    hook.write_text(f'kill() {{ echo "$*" >> {sigs}; builtin kill "$@"; }}\n')
    rdir = tmp_path / "run"
    rdir.mkdir()

    def spawn():
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)   # pid == pgid
        procs.append(p)
        return p

    def kill_script(grace=2):
        r = subprocess.run(["bash", "-s", "--", str(rdir), str(grace)], input=build_run.REMOTE_KILL,
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "BASH_ENV": str(hook)})
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr

    def signals():
        return sigs.read_text().splitlines() if sigs.exists() else []

    yield spawn, rdir, kill_script, signals
    for p in procs:
        p.kill()            # our own children only
        p.wait()


def _write(rdir, p, start):
    lines = [str(p.pid), str(p.pid)] + ([str(start)] if start is not None else [])
    (rdir / "run.pid").write_text("\n".join(lines) + "\n")


def _alive(p):
    return p.poll() is None


def test_matching_start_time_is_signalled(sandbox):
    spawn, rdir, kill_script, signals = sandbox
    p = spawn()
    _write(rdir, p, procinfo.starttime(p.pid))
    threading.Thread(target=p.wait, daemon=True).start()   # reap, as init would; a zombie keeps its group
    assert kill_script() == "termed"
    assert p.wait(timeout=10) is not None
    assert any("-TERM" in s for s in signals())


def test_recycled_pid_is_not_signalled(sandbox):
    spawn, rdir, kill_script, signals = sandbox
    p = spawn()
    _write(rdir, p, procinfo.starttime(p.pid) + 1)      # a different process held this pid
    assert kill_script() == "gone"
    time.sleep(0.2)
    assert _alive(p)
    assert not any(("-TERM" in s or "-KILL" in s) for s in signals())


def test_run_pid_without_a_start_time_is_refused(sandbox):
    spawn, rdir, kill_script, signals = sandbox
    p = spawn()
    _write(rdir, p, None)                               # a pre-check 2-line run.pid
    assert kill_script() == "failed"
    assert _alive(p)
    assert not any(("-TERM" in s or "-KILL" in s) for s in signals())


def test_dead_leader_pid_is_not_signalled(sandbox):
    spawn, rdir, kill_script, signals = sandbox
    p = spawn()
    st = procinfo.starttime(p.pid)
    p.kill()
    p.wait()                                            # reaped: pid is free
    _write(rdir, p, st)
    assert kill_script() == "gone"
    assert not any(("-TERM" in s or "-KILL" in s) for s in signals())


def test_runner_writes_the_start_time():
    src = (ROOT / "bin" / "build-runner.sh").read_text()
    assert '"$CPID" "$CPG" "$CSTART" > "$DIR/run.pid"' in src
