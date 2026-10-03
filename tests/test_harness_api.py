"""API freeze for tests/harness.Harness (review L6).

Parallel builds (1.2, 1.5, phase 2) code against these signatures. Changing
one must fail here, not silently in someone else's branch.
"""

import inspect

from harness import Harness


def sig(name):
    return inspect.signature(getattr(Harness, name))


def params(name):
    return [(p.name, p.default) for p in sig(name).parameters.values()
            if p.name != "self"]


def prefix(name, original):
    """The original parameters and defaults, in order, as a prefix; later
    parameters are allowed if they have defaults (additive growth, 2.1)."""
    got = params(name)
    assert got[:len(original)] == original, got
    assert all(d is not EMPTY for _, d in got[len(original):]), got


EMPTY = inspect.Parameter.empty


def test_constructor():
    prefix("__init__", [("tmp_workspace", EMPTY), ("agents", ["a", "b"])])
    assert dict(params("__init__"))["shards"] is None


def test_send():
    assert params("send") == [("agent", EMPTY), ("text", EMPTY), ("channel_id", "1")]
    assert inspect.iscoroutinefunction(Harness.send)


def test_wait_idle():
    assert params("wait_idle") == [("agent", EMPTY), ("timeout", 5)]
    assert inspect.iscoroutinefunction(Harness.wait_idle)


def test_shard_keyed_readers_are_sync():
    for name in ("sent_to", "argv", "queue_rows"):
        assert params(name) == [("shard", EMPTY)], name
        assert not inspect.iscoroutinefunction(getattr(Harness, name)), name
    prefix("cost_rows", [])
    assert dict(params("cost_rows"))["shard"] is None
    assert not inspect.iscoroutinefunction(Harness.cost_rows)


def test_discord_recorder_attribute(tmp_workspace):
    assert Harness(tmp_workspace).discord == []
