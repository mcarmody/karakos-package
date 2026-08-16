"""
Tests for #91's presence half: a bot-wide busy/idle indicator.

The per-message typing indicator (#121) only appears in the channel a turn
is actively draining into, so a long turn looks identical to a hung bot to
anyone not watching that specific channel. This reads bin/agent-server.py's
liveness beacons (the same files bin/wedge-check.py watches) and reflects
PROCESSING as Discord presence, visible from the member list regardless of
channel.
"""

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """Import bin/relay.py fresh, with WORKSPACE_ROOT pointed at a temp tree.

    Function-scoped (unlike the sys-commands suite's module-scoped fixture)
    because these tests write different beacon files into AGENT_BEACON_DIR
    per test and must not see a sibling test's leftovers.
    """
    workspace = tmp_path
    (workspace / "logs").mkdir()
    (workspace / "data" / "health" / "agents").mkdir(parents=True)

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    spec = importlib.util.spec_from_file_location("relay_presence_under_test", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["relay_presence_under_test"] = module
    spec.loader.exec_module(module)
    return module


def make_adapter(relay):
    """A DiscordAdapter with no Discord connection behind it, recording
    change_presence calls instead of touching a real gateway connection."""
    adapter = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
    adapter._presence_busy = None
    adapter.calls = []

    async def change_presence(status=None, activity=None):
        adapter.calls.append((status, activity))

    adapter.change_presence = change_presence
    return adapter


def write_beacon(relay, agent, state):
    relay.AGENT_BEACON_DIR.mkdir(parents=True, exist_ok=True)
    (relay.AGENT_BEACON_DIR / f"{agent}.json").write_text(
        json.dumps({"agent": agent, "state": state})
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# any_agent_processing
# ---------------------------------------------------------------------------

def test_false_when_beacon_dir_missing(relay):
    shutil.rmtree(relay.AGENT_BEACON_DIR)
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is False


def test_false_when_no_beacons(relay):
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is False


def test_false_when_all_idle(relay):
    write_beacon(relay, "amos", "IDLE")
    write_beacon(relay, "herald", "IDLE")
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is False


def test_true_when_one_processing(relay):
    write_beacon(relay, "amos", "IDLE")
    write_beacon(relay, "herald", "PROCESSING")
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is True


def test_corrupt_beacon_does_not_hide_a_real_one(relay):
    """A torn write on one agent's beacon must not mask another's PROCESSING —
    the acceptance test is bot-wide visibility, not per-agent."""
    relay.AGENT_BEACON_DIR.mkdir(parents=True, exist_ok=True)
    (relay.AGENT_BEACON_DIR / "amos.json").write_text("{not valid json")
    write_beacon(relay, "herald", "PROCESSING")
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is True


def test_corrupt_beacon_alone_reads_as_not_processing(relay):
    relay.AGENT_BEACON_DIR.mkdir(parents=True, exist_ok=True)
    (relay.AGENT_BEACON_DIR / "amos.json").write_text("{not valid json")
    adapter = make_adapter(relay)
    assert adapter.any_agent_processing() is False


# ---------------------------------------------------------------------------
# presence_tick — transitions only, and what change_presence is called with
# ---------------------------------------------------------------------------

def test_transition_to_busy_sets_idle_status_with_activity(relay):
    write_beacon(relay, "amos", "PROCESSING")
    adapter = make_adapter(relay)

    run(adapter.presence_tick())

    assert len(adapter.calls) == 1
    status, activity = adapter.calls[0]
    assert status == discord.Status.idle
    assert isinstance(activity, discord.Activity)


def test_transition_to_free_clears_activity(relay):
    adapter = make_adapter(relay)
    adapter._presence_busy = True  # already known busy; no beacon says PROCESSING now

    run(adapter.presence_tick())

    assert adapter.calls == [(discord.Status.online, None)]


def test_no_change_presence_call_without_a_transition(relay):
    """The point of tracking _presence_busy: don't hammer Discord's presence
    rate limit on a turn that polls every 5s for four minutes."""
    adapter = make_adapter(relay)
    adapter._presence_busy = False  # already known idle, and no beacon says otherwise

    run(adapter.presence_tick())

    assert adapter.calls == []


def test_change_presence_failure_does_not_raise(relay):
    """A poll loop that dies on a transient Discord error stops reporting
    presence forever; this must be caught and swallowed like the beacon
    writer's own best-effort contract."""
    write_beacon(relay, "amos", "PROCESSING")
    adapter = make_adapter(relay)

    async def boom(status=None, activity=None):
        raise RuntimeError("simulated Discord API error")

    adapter.change_presence = boom

    run(adapter.presence_tick())  # must not raise

    # The failed transition was not recorded as applied, so the next tick
    # will retry rather than believing presence is already correct.
    assert adapter._presence_busy is None
