"""lib/registry.py: schema, validation, legacy_view, write_agent, CLI."""
import copy
import dataclasses
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import registry  # noqa: E402
from registry import RegistryError, Shard, load_registry, parse_registry  # noqa: E402

CHANNELS = {"general", "signals", "ops"}

VALID = {
    "version": 2,
    "agents": {
        "amos": {"name": "Amos", "role": "primary", "model": "opus", "effort": "high",
                 "shards": [{"id": "amos", "channels": ["general"]},
                            {"id": "amos-ops", "channels": ["ops"]}]},
        "relay": {"name": "Relay", "role": "monitor"},
        "helper": {"name": "Helper", "role": "custom"},
    },
}


def mutated(fn):
    d = copy.deepcopy(VALID)
    fn(d)
    return d


def problems(data, channels=CHANNELS):
    with pytest.raises(RegistryError) as e:
        parse_registry(data, channels)
    return e.value.problems


def write_ws(tmp_path, data, channels=("general", "signals", "ops")):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "agents.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (tmp_path / "config" / "channels.json").write_text(
        json.dumps({"channels": {c: "1" for c in channels}}))
    return tmp_path


def test_valid_round_trip(tmp_path):
    reg = load_registry(write_ws(tmp_path, VALID))
    assert reg.ids() == ["amos", "relay", "helper"]
    assert reg.primary().id == "amos" and reg.monitor().id == "relay"
    assert reg.agent("amos").get("model") == "opus"
    assert reg.agent("relay").get("model") == "sonnet"          # default
    assert reg.agent("relay").get("max_turns") == 200
    assert [a.id for a in reg.by_role("custom")] == ["helper"]
    assert reg.shard_for_channel("ops").id == "amos-ops"
    assert reg.shard_for_channel("signals") is None


def test_shards_shape():
    reg = parse_registry(VALID, CHANNELS)
    shards = reg.shards()
    assert [s.id for s in shards] == ["amos", "amos-ops", "relay", "helper"]
    assert shards[1].agent == "amos" and list(shards[1].channels) == ["ops"]
    assert shards[2].channels == () and shards[2].agent == "relay"   # default shard
    assert [s.id for s in reg.shards_of("amos")] == ["amos", "amos-ops"]
    assert isinstance(shards[0], Shard)
    with pytest.raises(dataclasses.FrozenInstanceError):
        shards[0].id = "x"


def test_each_violation_distinct():
    cases = {
        "two primaries": mutated(lambda d: d["agents"]["helper"].update(role="primary")),
        "no primary": mutated(lambda d: d["agents"]["amos"].update(role="custom")),
        "two monitors": mutated(lambda d: d["agents"]["helper"].update(role="monitor")),
        "bad id": mutated(lambda d: d["agents"].update({"Bad_Id": {"name": "x", "role": "custom"}})),
        "no name": mutated(lambda d: d["agents"]["helper"].pop("name")),
        "bad role": mutated(lambda d: d["agents"]["helper"].update(role="boss")),
        "bad effort": mutated(lambda d: d["agents"]["helper"].update(effort="ludicrous")),
        "bad type": mutated(lambda d: d["agents"]["helper"].update(max_turns="many")),
        "bad bool": mutated(lambda d: d["agents"]["helper"].update(handoff_on_reset="yes")),
        "unknown channel": mutated(lambda d: d["agents"]["helper"].update(
            shards=[{"id": "helper", "channels": ["nope"]}])),
        "channel twice": mutated(lambda d: d["agents"]["helper"].update(
            shards=[{"id": "helper", "channels": ["general"]}])),
        "dup shard id": mutated(lambda d: d["agents"]["helper"].update(
            shards=[{"id": "amos-ops"}])),
        "bad version": mutated(lambda d: d.update(version=1)),
    }
    seen = {}
    for label, data in cases.items():
        probs = problems(data)
        assert probs, label
        seen[label] = "\n".join(probs)
    assert len(set(seen.values())) == len(seen)


def test_no_monitor_is_error():
    probs = problems(mutated(lambda d: d["agents"].pop("relay")))
    assert any("'monitor'" in p for p in probs)


def test_custom_role_accepted():
    reg = parse_registry(mutated(lambda d: d["agents"]["helper"].update(role="custom")), CHANNELS)
    assert reg.agent("helper").role == "custom"


def test_shard_id_equal_to_other_agent_rejected():
    data = mutated(lambda d: d["agents"]["helper"].update(shards=[{"id": "relay"}]))
    probs = problems(data)
    assert any("equals another agent's id" in p for p in probs)


def test_all_errors_reported_together():
    def bad(d):
        d["agents"]["helper"].update(role="primary", effort="zzz")
        d["agents"]["relay"].pop("name")
        d["agents"]["amos"]["shards"] = [{"id": "amos", "channels": ["nope"]}]
    probs = problems(mutated(bad))
    assert len(probs) >= 4


def test_unknown_keys_warn_not_error():
    reg = parse_registry(mutated(lambda d: d["agents"]["helper"].update(wat=1)), CHANNELS)
    assert any("wat" in w for w in reg.warnings)


def test_legacy_view_equals_conftest_dict(legacy_workspace):
    tmp_workspace = legacy_workspace
    old = json.loads((tmp_workspace / "config" / "agents.json").read_text())
    data = {"version": 2, "agents": {
        "test-agent": {"name": "Test", "role": "primary",
                       "system_prompt": "agents/test-agent/SYSTEM_PROMPT.md",
                       "discord": {"token_env": "DISCORD_BOT_TOKEN_TEST"}},
        "monitor": {"name": "monitor", "role": "monitor"}}}
    reg = parse_registry(data, {"general", "signals"})
    view = reg.legacy_view()
    # handoff_on_reset is always written (effective value, 2.6); a primary
    # that never set it gets the carry-over.
    got = dict(view["agents"]["test-agent"])
    assert got.pop("handoff_on_reset") is True
    assert got == old["agents"]["test-agent"]


def test_legacy_view_full_keys():
    data = mutated(lambda d: d["agents"]["helper"].update(
        model="haiku", max_turns=5, allowed_tools=["Read"], env={"A": "b"},
        discord={"token_env": "T", "bot_id_env": "I"}))
    h = parse_registry(data, CHANNELS).legacy_view()["agents"]["helper"]
    assert h == {"model": "haiku", "max_turns": 5, "allowed_tools": ["Read"],
                 "env": {"A": "b"}, "handoff_on_reset": True,
                 "discord_bot_token_env": "T", "discord_bot_id_env": "I"}


NEW = {"name": "New", "role": "builder", "model": "haiku"}


def test_write_agent_appends_text_and_keeps_lines(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ruamel", None)       # force the text path
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    ws = tmp_path
    (ws / "config").mkdir()
    (ws / "config" / "channels.json").write_text(json.dumps({"channels": {"general": "1"}}))
    original = textwrap.dedent("""\
        # hand-edited, keep me
        version: 2
        agents:
          amos:   # the boss
            name: Amos
            role: primary
          relay:
            {name: Relay, role: monitor}
        """)
    (ws / "config" / "agents.yaml").write_text(original)
    registry.write_agent(ws, "newbie", NEW)
    text = (ws / "config" / "agents.yaml").read_text()
    assert text.startswith(original)                       # existing lines untouched
    reg = load_registry(ws)
    assert reg.agent("newbie").role == "builder"
    assert reg.agent("newbie").get("model") == "haiku"


def test_write_agent_ruamel_keeps_comments(tmp_path):
    pytest.importorskip("ruamel.yaml")
    ws = tmp_path
    (ws / "config").mkdir()
    (ws / "config" / "channels.json").write_text(json.dumps({"channels": {}}))
    (ws / "config" / "agents.yaml").write_text(
        "# top\nversion: 2\nagents:\n  amos:  # boss\n    name: Amos\n    role: primary\n"
        "  relay: {name: R, role: monitor}\n")
    registry.write_agent(ws, "newbie", NEW)
    text = (ws / "config" / "agents.yaml").read_text()
    assert "# top" in text and "# boss" in text
    assert load_registry(ws).agent("newbie")


def test_write_agent_refuses_invalid(tmp_path):
    ws = write_ws(tmp_path, VALID)
    path = ws / "config" / "agents.yaml"
    before = path.read_text()
    with pytest.raises(RegistryError):
        registry.write_agent(ws, "dupe", {"name": "D", "role": "primary"})   # 2nd primary
    with pytest.raises(RegistryError):
        registry.write_agent(ws, "helper", NEW)                              # exists
    with pytest.raises(RegistryError):
        registry.write_agent(ws, "BAD", NEW)                                 # bad id
    assert path.read_text() == before


def run_cli(ws, *args):
    return subprocess.run([sys.executable, str(ROOT / "lib" / "registry.py"),
                           "--workspace", str(ws), *args],
                          capture_output=True, text=True)


def test_cli(tmp_path):
    ws = write_ws(tmp_path, VALID)
    r = run_cli(ws, "ids")
    assert (r.returncode, r.stdout.split()) == (0, ["amos", "relay", "helper"])
    r = run_cli(ws, "role", "monitor")
    assert (r.returncode, r.stdout.strip()) == (0, "relay")
    r = run_cli(ws, "field", "amos", "model")
    assert (r.returncode, r.stdout.strip()) == (0, "opus")
    assert run_cli(ws, "field", "amos", "handoff_on_reset").stdout.strip() == "true"
    assert run_cli(ws, "validate").returncode == 0
    assert run_cli(ws, "field", "ghost", "model").returncode == 1
    assert run_cli(ws, "field", "amos", "bogus").returncode == 1
    assert run_cli(ws, "role", "nonsense").returncode == 2
    assert run_cli(ws).returncode == 2
    bad = mutated(lambda d: d["agents"].pop("relay"))
    write_ws(tmp_path, bad)
    r = run_cli(ws, "validate")
    assert r.returncode == 1 and "monitor" in r.stderr


# -- 2.7: token budget keys ----------------------------------------------------

def _with(agent, **kw):
    d = copy.deepcopy(VALID)
    d["agents"][agent].update(kw)
    return d


@pytest.mark.parametrize("key,val,ok", [
    ("token_budget_4h", None, True), ("token_budget_4h", 1000, True),
    ("token_budget_4h", 999, False), ("token_budget_4h", "5000", False),
    ("token_budget_4h", True, False),
    ("token_budget_min_pause_s", 60, True), ("token_budget_min_pause_s", 21600, True),
    ("token_budget_min_pause_s", 59, False), ("token_budget_min_pause_s", 21601, False)])
def test_token_budget_validation(key, val, ok):
    data = _with("helper", **{key: val})
    if ok:
        parse_registry(data)
    else:
        with pytest.raises(RegistryError):
            parse_registry(data)


def test_monitor_cannot_have_a_budget_and_legacy_view_copies_both():
    with pytest.raises(RegistryError):
        parse_registry(_with("relay", token_budget_4h=5000))
    reg = parse_registry(_with("helper", token_budget_4h=5000, token_budget_min_pause_s=600))
    legacy = reg.legacy_view()["agents"]["helper"]
    assert legacy["token_budget_4h"] == 5000 and legacy["token_budget_min_pause_s"] == 600
    assert "token_budget_4h" not in reg.legacy_view()["agents"]["amos"]


# -- step 2.6: context budget, reset mode, handoff default by role ------------

def _with26(agent, **kw):
    return mutated(lambda d: d["agents"][agent].update(**kw))


def test_handoff_on_reset_effective_default_by_role():
    reg = parse_registry(VALID, CHANNELS)
    assert reg.agent("amos").get("handoff_on_reset") is True      # primary
    assert reg.agent("helper").get("handoff_on_reset") is True    # custom
    assert reg.agent("relay").get("handoff_on_reset") is False    # monitor
    data = copy.deepcopy(VALID)
    data["agents"]["bld"] = {"name": "Bld", "role": "builder"}
    data["agents"]["rev"] = {"name": "Rev", "role": "reviewer"}
    reg = parse_registry(data, CHANNELS)
    assert reg.agent("bld").get("handoff_on_reset") is False
    assert reg.agent("rev").get("handoff_on_reset") is False


def test_handoff_on_reset_explicit_wins():
    reg = parse_registry(_with26("amos", handoff_on_reset=False), CHANNELS)
    assert reg.agent("amos").get("handoff_on_reset") is False
    assert reg.legacy_view()["agents"]["amos"]["handoff_on_reset"] is False
    data = copy.deepcopy(VALID)
    data["agents"]["bld"] = {"name": "Bld", "role": "builder", "handoff_on_reset": True}
    assert parse_registry(data, CHANNELS).legacy_view()["agents"]["bld"]["handoff_on_reset"] is True


def test_context_budget_validation():
    for bad in (19999, 0, -5, "big", True, 1.5):
        assert problems(_with26("amos", context_budget_tokens=bad)), bad
    assert parse_registry(_with26("amos", context_budget_tokens=20000), CHANNELS)
    assert parse_registry(_with26("amos", context_budget_tokens=None), CHANNELS)
    reg = parse_registry(_with26("amos", context_budget_tokens=2000000), CHANNELS)
    assert any("context_budget_tokens" in w for w in reg.warnings)
    reg = parse_registry(_with26("amos", context_budget_tokens=500000), CHANNELS)
    assert not any("context_budget_tokens" in w for w in reg.warnings)


def test_reset_mode_enum():
    assert parse_registry(VALID, CHANNELS).agent("amos").get("reset_mode") == "reset"
    assert parse_registry(_with26("amos", reset_mode="compact"), CHANNELS)
    assert problems(_with26("amos", reset_mode="nuke"))


def test_legacy_view_carries_budget_mode_and_handoff():
    reg = parse_registry(_with26("amos", context_budget_tokens=50000,
                                 reset_mode="compact", handoff_on_reset=False), CHANNELS)
    a = reg.legacy_view()["agents"]["amos"]
    assert (a["context_budget_tokens"], a["reset_mode"], a["handoff_on_reset"]) == \
        (50000, "compact", False)
    plain = parse_registry(VALID, CHANNELS).legacy_view()["agents"]["amos"]
    assert "context_budget_tokens" not in plain and "reset_mode" not in plain
