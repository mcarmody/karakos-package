"""Busy/idle bot presence in bin/relay.py: debounce, idle hold, reconnect."""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

RELAY_PATH = Path(__file__).parent.parent / "bin" / "relay.py"
discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    ws = tmp_path_factory.mktemp("workspace")
    (ws / "logs").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("relay_presence_under_test", RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_presence_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def tracker(relay, clock, interval=15, hold=10):
    return relay.PresenceTracker(min_interval=interval, idle_hold=hold, clock=clock)


def test_agents_busy(relay):
    assert relay.agents_busy({"agents": {"a": {"state": "PROCESSING"}}})
    assert not relay.agents_busy({"agents": {"a": {"state": "IDLE"}, "b": {"state": "ERROR_RECOVERY"}}})
    assert not relay.agents_busy({})
    assert relay.agents_busy({"agents": {"a": {"state": "IDLE"}, "b": {"state": "PROCESSING"}}})


def test_first_observation_sets_presence(relay):
    c = Clock()
    assert tracker(relay, c).observe(False) == "idle"
    assert tracker(relay, c).observe(True) == "busy"


def test_no_repeat_updates(relay):
    c, t = Clock(), None
    t = tracker(relay, c)
    assert t.observe(True) == "busy"
    for _ in range(5):
        c.t += 20
        assert t.observe(True) is None


def test_idle_requires_hold(relay):
    c = Clock()
    t = tracker(relay, c)
    assert t.observe(True) == "busy"
    c.t += 20
    assert t.observe(False) is None      # idle just started
    c.t += 5
    assert t.observe(False) is None      # still inside hold
    c.t += 6
    assert t.observe(False) == "idle"


def test_brief_idle_gap_does_not_flicker(relay):
    c = Clock()
    t = tracker(relay, c)
    t.observe(True)
    c.t += 20
    assert t.observe(False) is None
    c.t += 3
    assert t.observe(True) is None       # still "busy", nothing to send
    assert t.applied == "busy"


def test_rate_limit_defers_flip(relay):
    c = Clock()
    t = tracker(relay, c, interval=15, hold=0)
    assert t.observe(False) == "idle"
    c.t += 3
    assert t.observe(True) is None       # too soon
    c.t += 13
    assert t.observe(True) == "busy"


def test_reset_resends_current_state(relay):
    c = Clock()
    t = tracker(relay, c)
    assert t.observe(True) == "busy"
    t.reset()                            # reconnect
    c.t += 1
    assert t.observe(True) == "busy"     # re-applied immediately


def test_presence_for(relay):
    st, act = relay.presence_for("busy")
    assert st == discord.Status.dnd and act is not None
    st, act = relay.presence_for("idle")
    assert st == discord.Status.online and act is None


def test_tick_sends_and_recovers_from_failure(relay):
    class Fake(relay.DiscordAdapter):
        def __init__(self):
            self.presence = relay.PresenceTracker(min_interval=0, idle_hold=0)
            self.calls, self.fail, self.busy = [], False, True

        def is_closed(self):
            return False

        async def fetch_agents_busy(self):
            return self.busy

        async def change_presence(self, status=None, activity=None):
            if self.fail:
                raise RuntimeError("gateway down")
            self.calls.append(status)

    f = Fake()
    f.fail = True
    asyncio.run(f.presence_tick())
    assert f.calls == []
    f.fail = False
    asyncio.run(f.presence_tick())       # retried after failure
    assert f.calls == [discord.Status.dnd]
    asyncio.run(f.presence_tick())       # unchanged: no update
    assert len(f.calls) == 1
    f.busy = None                        # server unreachable: keep state
    asyncio.run(f.presence_tick())
    assert len(f.calls) == 1
