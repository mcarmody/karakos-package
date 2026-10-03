"""Relay command surface for 6.4: /pause, /resume, /effort, /interrupt <message>.

The agent-server call is stubbed; each test pins the path and body the command
sends and the reply it prints.
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

pytest.importorskip("discord", reason="relay.py imports discord.py")

OWNER = 4242


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_switches_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_switches_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


class Channel:
    def __init__(self, cid=555):
        self.id = cid
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def message(author_id=OWNER, cid=555):
    return SimpleNamespace(
        channel=Channel(cid), author=SimpleNamespace(id=author_id, display_name="Owner"),
        content="")


class Adapter:
    """A DiscordAdapter with the agent-server call stubbed."""

    def __init__(self, relay, responses=None):
        self.a = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
        self.calls = []
        self.responses = responses or {}

        async def post_json(path, payload=None):
            self.calls.append((path, payload))
            for prefix, resp in self.responses.items():
                if path.startswith(prefix):
                    return resp
            return True, "", {}

        self.a.agent_server_post_json = post_json

    def run(self, msg, cmd, args="", mentioned=None, default="a"):
        asyncio.run(self.a.handle_sys_command(msg, cmd, args, mentioned, default))
        return msg.channel.sent


@pytest.fixture
def owner(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"a": {}, "b": {}})


# -- pause / resume -------------------------------------------------------------

def test_pause_posts_minutes_and_replies(relay, owner):
    ad = Adapter(relay, {"/agents/a/pause": (True, "", {"paused": ["a"], "until": 1700000000})})
    out = ad.run(message(), "pause", "30")
    assert ad.calls == [("/agents/a/pause", {"minutes": 30, "by": "Owner"})]
    assert "`a` paused until 22:13 UTC; the turn in progress will finish." in out[0]


def test_pause_without_minutes_is_open_ended(relay, owner):
    ad = Adapter(relay, {"/agents/b/pause": (True, "", {"paused": ["b"], "until": None})})
    out = ad.run(message(), "pause", "", mentioned="b")
    assert ad.calls == [("/agents/b/pause", {"minutes": None, "by": "Owner"})]
    assert "paused until resumed" in out[0]


@pytest.mark.parametrize("bad", ["0", "1441", "abc"])
def test_pause_bad_minutes_makes_no_call(relay, owner, bad):
    ad = Adapter(relay)
    out = ad.run(message(), "pause", bad)
    assert ad.calls == [] and "1 to 1440" in out[0]


def test_resume(relay, owner):
    ad = Adapter(relay)
    out = ad.run(message(), "resume")
    assert ad.calls == [("/agents/a/resume", None)]
    assert "`a` resumed." in out[0]


@pytest.mark.parametrize("cmd", ["pause", "resume"])
def test_non_owner_denied_before_any_call(relay, owner, cmd):
    ad = Adapter(relay)
    out = ad.run(message(author_id=1), cmd)
    assert ad.calls == [] and "Permission denied" in out[0]


def test_slash_args_for_pause(relay):
    assert relay.slash_args("pause", {"minutes": 15, "agent": "a"}) == "15"
    assert relay.slash_args("pause", {}) == ""
    assert "pause" not in relay.SYS_COMMANDS and "resume" not in relay.SYS_COMMANDS


# -- effort -----------------------------------------------------------------------

def test_effort_posts_level_and_reports_applied_and_deferred(relay, owner):
    ad = Adapter(relay, {"/agents/a/effort": (
        True, "", {"effort": "max", "applied": ["a"], "deferred": ["a-2"]})})
    out = ad.run(message(), "effort", "max")
    assert ad.calls == [("/agents/a/effort", {"level": "max"})]
    assert "effort is now max" in out[0]
    assert "Applied now: `a`" in out[0] and "After the current turn: `a-2`" in out[0]


def test_effort_default_and_unknown_level(relay, owner):
    ad = Adapter(relay, {"/agents/b/effort": (True, "", {"effort": None, "applied": ["b"],
                                                         "deferred": []})})
    out = ad.run(message(), "effort", "default", mentioned="b")
    assert ad.calls == [("/agents/b/effort", {"level": "default"})]
    assert "the CLI default" in out[0]
    ad2 = Adapter(relay)
    out = ad2.run(message(), "effort", "extreme")
    assert ad2.calls == [] and "must be one of" in out[0]


def test_effort_failure_and_owner_gate(relay, owner):
    ad = Adapter(relay, {"/agents/a/effort": (False, "agent server returned 400: x", {})})
    assert "effort failed for `a`" in ad.run(message(), "effort", "low")[0]
    ad = Adapter(relay)
    assert "Permission denied" in ad.run(message(author_id=1), "effort", "low")[0]
    assert ad.calls == []


def test_slash_args_for_effort(relay):
    assert relay.slash_args("effort", {"level": "xhigh"}) == "xhigh"
    assert "effort" in relay.SLASH_COMMANDS and "effort" not in relay.SYS_COMMANDS
