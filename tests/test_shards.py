"""lib/shards.py: pure shard planning (step 2.1)."""

import sys
from types import SimpleNamespace as NS

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import shards as sh  # noqa: E402
from shards import ShardSpec  # noqa: E402


class Reg:
    def __init__(self, layout):
        self.layout = layout  # {agent: [(shard id, channels)] or []}

    def ids(self):
        return list(self.layout)

    def shards_of(self, agent):
        return [NS(id=i, agent=agent, channels=tuple(c)) for i, c in self.layout[agent]]


def specs(*pairs):
    return [ShardSpec(i, a, (), i == a) for a, i in pairs]


FIXTURE = specs(("a", "a"), ("a", "a-2"), ("b", "b"))


def test_plan_order_and_defaults():
    reg = Reg({"a": [("a", []), ("a-2", ["x"])], "b": [], "c": [("c-main", [])]})
    plan = sh.plan_shards(reg)
    assert [(s.id, s.agent, s.is_default) for s in plan] == [
        ("a", "a", True), ("a-2", "a", False), ("b", "b", True), ("c-main", "c", False)]
    assert plan[1].channels == ("x",)


def test_diff():
    old = specs(("a", "a"), ("a", "a-2"))
    new = [ShardSpec("a", "a", ("x",), True), ShardSpec("a-3", "a", (), False)]
    d = sh.diff_shards(old, new)
    assert [s.id for s in d.added] == ["a-3"]
    assert [s.id for s in d.removed] == ["a-2"]
    assert [s.id for s in d.kept] == ["a"]  # channel-only change is still kept
    assert d.kept[0].channels == ("x",)


def test_resolve_targets():
    r = lambda *a, **k: sh.resolve_targets(FIXTURE, *a, **k)
    assert r("a-2") == ["a-2"]
    assert r("a") == ["a", "a-2"]  # agent id equal to its default shard id: all
    assert r("a", shard="a") == ["a"]
    assert r("a", shard="a-2") == ["a-2"]
    assert r("a-2", shard="a-2") == ["a-2"]
    assert r("a", shard="b") == []  # foreign shard
    assert r("a", shard="nope") == []
    assert r("b") == ["b"]
    assert r("nope") == []


def test_first_shard():
    assert sh.first_shard(FIXTURE, "a") == "a"
    assert sh.first_shard(FIXTURE, "a-2") == "a-2"
    assert sh.first_shard(FIXTURE, "zz") is None
    only = specs(("c", "c-main"))
    assert sh.first_shard(only, "c") == "c-main"


def test_aggregate_state():
    A = sh.aggregate_state
    assert A(["IDLE"]) == "IDLE"
    assert A(["PROCESSING"]) == "PROCESSING"
    assert A(["ERROR_RECOVERY"]) == "ERROR_RECOVERY"
    assert A(["UNKNOWN"]) == "UNKNOWN"
    assert A(["IDLE", "PROCESSING"]) == "PROCESSING"
    assert A(["ERROR_RECOVERY", "PROCESSING"]) == "PROCESSING"
    assert A(["IDLE", "ERROR_RECOVERY"]) == "ERROR_RECOVERY"
    assert A(["IDLE", "IDLE"]) == "IDLE"
    assert A(["IDLE", "UNKNOWN"]) == "UNKNOWN"
    assert A(["UNKNOWN", "UNKNOWN"]) == "UNKNOWN"


def test_shard_label():
    assert sh.shard_label(FIXTURE[0]) == "a"
    assert sh.shard_label(FIXTURE[1]) == "a (a-2)"


def test_orphan_key_warnings():
    only_main = specs(("a", "a-main"), ("b", "b"))
    w = sh.orphan_key_warnings(only_main, ["a"], [])
    assert w == ["agent a has data under key a but no shard with that id; "
                 "its history will not be used"]
    assert sh.orphan_key_warnings(only_main, [], ["a"]) == w
    assert sh.orphan_key_warnings(FIXTURE, ["a", "a-2", "b"], ["a"]) == []
    assert sh.orphan_key_warnings(only_main, ["a-main"], []) == []
