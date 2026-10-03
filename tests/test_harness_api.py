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


EMPTY = inspect.Parameter.empty


def test_constructor():
    assert params("__init__") == [("tmp_workspace", EMPTY), ("agents", ["a", "b"])]


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
    assert params("cost_rows") == []
    assert not inspect.iscoroutinefunction(Harness.cost_rows)


def test_discord_recorder_attribute(tmp_workspace):
    assert Harness(tmp_workspace).discord == []
