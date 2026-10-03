"""Pure routing rules (lib/routing.py, step 2.2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import registry  # noqa: E402
from routing import Route, route_message  # noqa: E402

AGENTS = {
    "a": {"name": "a", "role": "primary", "shards": [
        {"id": "a", "channels": ["general"]},
        {"id": "a-2", "channels": ["ops"]}]},
    "b": {"name": "b", "role": "custom", "shards": [
        {"id": "b", "channels": ["b-room"]}]},
    "m": {"name": "m", "role": "monitor"},
}


def reg(agents=AGENTS):
    return registry.parse_registry({"version": 2, "agents": agents})


def r(chan, mention=None, bot=False, opt_out=False, registry_=None):
    return route_message(registry_ or reg(), chan, mention, bot, opt_out)


def test_channel_owner():
    assert r("ops") == Route("a-2", "a", "channel")
    assert r("general") == Route("a", "a", "channel")


def test_mention_rules():
    assert r("ops", "b") == Route("b", "b", "mention")
    assert r("ops", "a") == Route("a-2", "a", "mention")
    assert r("b-room", "a") == Route("a", "a", "mention")


def test_primary_fallback_and_opt_out_and_bot():
    assert r("lobby") == Route("a", "a", "primary_fallback")
    assert r("lobby", opt_out=True) is None
    assert r("lobby", bot=True) is None
    assert r("ops", opt_out=True) == Route("a-2", "a", "channel")


def test_bot_mention_routes():
    assert r("ops", "b", bot=True) == Route("b", "b", "mention")


def test_unknown_mention_falls_through():
    assert r("ops", "zzz") == Route("a-2", "a", "channel")
    assert r("lobby", "zzz") == Route("a", "a", "primary_fallback")


def test_unlisted_channel_b24():
    assert r(None, "b") is None
    assert r(None) is None
    assert r(None, "b", bot=True) is None


def test_no_primary():
    class NoPrimary:
        def ids(self): return ["b"]
        def shard_for_channel(self, n): return None
        def shards_of(self, a): return []
        def primary(self): raise IndexError("no primary")
    assert route_message(NoPrimary(), "lobby", None, False) is None


def test_one_x_registry_without_shards():
    reg1 = reg({"a": {"name": "a", "role": "primary"},
                "b": {"name": "b", "role": "custom"},
                "m": {"name": "m", "role": "monitor"}})
    assert r("lobby", registry_=reg1) == Route("a", "a", "primary_fallback")
    assert r("x", "b", registry_=reg1) == Route("b", "b", "mention")
    assert r("x", "b", registry_=reg1).shard == "b"
