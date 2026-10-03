"""kill_agent_subprocess treats an already-dead process as dead, not as an error."""

import asyncio
import errno
import importlib.util
import os
import sys
from pathlib import Path

import pytest

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"


@pytest.fixture
def ags(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("ags_kill_under_test", AGENT_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["ags_kill_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return m


class FakeProc:
    pid = 4242

    def __init__(self, terminate_exc=None, kill_exc=None, hang=False):
        self.terminate_exc = terminate_exc
        self.kill_exc = kill_exc
        self.hang = hang
        self.killed = False

    def terminate(self):
        if self.terminate_exc:
            raise self.terminate_exc

    def kill(self):
        self.killed = True
        if self.kill_exc:
            raise self.kill_exc

    async def wait(self):
        if self.hang and not self.killed:
            await asyncio.sleep(60)
        return 0


def _kill(ags, proc):
    ags.agent_processes["amos"] = proc
    asyncio.run(ags.kill_agent_subprocess("amos"))


@pytest.mark.parametrize("exc", [
    ProcessLookupError("gone"),
    OSError(errno.ESRCH, "No such process"),
])
def test_terminate_on_a_dead_process_clears_state(ags, exc):
    _kill(ags, FakeProc(terminate_exc=exc))
    assert "amos" not in ags.agent_processes


def test_kill_on_a_dead_process_after_timeout_clears_state(ags, monkeypatch):
    real_wait_for = asyncio.wait_for

    async def fast(coro, timeout):
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(ags.asyncio, "wait_for", fast)
    _kill(ags, FakeProc(hang=True, kill_exc=ProcessLookupError("gone")))
    assert "amos" not in ags.agent_processes


def test_other_oserror_still_propagates(ags):
    with pytest.raises(OSError):
        _kill(ags, FakeProc(terminate_exc=OSError(errno.EPERM, "nope")))
