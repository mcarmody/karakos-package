"""Prompt composition: core + section + shard + house style (step 1.3)."""

import asyncio
import logging
import sys

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import prompt_compose as pc  # noqa: E402
from prompt_compose import compose_system_prompt  # noqa: E402

sys.path.insert(0, str(PACKAGE_ROOT / "tests"))
from harness import Harness  # noqa: E402

SPLICE = "<!-- core:insert -->"


def put(ws, rel, text):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def ws(tmp_workspace, monkeypatch):
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("SYSTEM_NAME", raising=False)
    return tmp_workspace


def test_no_files_is_byte_identical(ws):
    text = "You are x.\n  {{x}} and {{ unknown }} stay.\n\n"
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", text)
    assert compose_system_prompt(ws, "test-agent") == text


def test_known_placeholder_substituted(ws, monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "Sam")
    monkeypatch.setenv("SYSTEM_NAME", "sys1")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md",
        "{{AGENT_NAME}}/{{SYSTEM_NAME}}/{{OWNER_NAME}}/{{SHARD_ID}}/{{nope}}")
    assert compose_system_prompt(ws, "test-agent") == "test-agent/sys1/Sam/test-agent/{{nope}}"


def test_channels_and_other_agents_from_registry(ws):
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", "C:\n{{CHANNELS}}\nO:\n{{OTHER_AGENTS}}")
    out = compose_system_prompt(ws, "test-agent")
    assert "- #general" in out and "- #signals" in out
    assert "- **Relay** (haiku)" in out
    assert "test-agent" not in out.split("O:")[1]


def test_splice_marker_once_and_never_in_output(ws):
    put(ws, "agents/CORE.md", "CORE BODY")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", f"top\n{SPLICE}\nbottom\n")
    out = compose_system_prompt(ws, "test-agent")
    assert out == "top\n<!-- begin:core -->\nCORE BODY\n<!-- end:core -->\nbottom\n"
    assert SPLICE not in out and out.count("CORE BODY") == 1


def test_prepend_without_marker_keeps_content(ws):
    put(ws, "agents/CORE.md", "CORE BODY")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", "legacy prompt\n")
    out = compose_system_prompt(ws, "test-agent")
    assert out == "<!-- begin:core -->\nCORE BODY\n<!-- end:core -->\n\nlegacy prompt\n"


def test_house_style_last_after_shard(ws):
    put(ws, "agents/CORE.md", "CORE")
    put(ws, "agents/HOUSE_STYLE.md", "STYLE")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", "SECTION")
    put(ws, "agents/test-agent/shards/test-agent.md", "SHARD {{SHARD_ID}}")
    out = compose_system_prompt(ws, "test-agent")
    assert out.index("CORE") < out.index("SECTION") < out.index("SHARD test-agent") \
        < out.index("STYLE")
    assert out.endswith("<!-- end:house-style -->")


def test_recompose_generated_file_is_golden(ws):
    put(ws, "agents/CORE.md", "CORE {{CHANNELS}}")
    put(ws, "agents/HOUSE_STYLE.md", "STYLE")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", f"intro\n{SPLICE}\nrole\n")
    first = compose_system_prompt(ws, "test-agent")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", first)
    second = compose_system_prompt(ws, "test-agent")
    assert second == first
    assert second.count("<!-- begin:core -->") == 1
    assert second.count("<!-- begin:house-style -->") == 1


def test_recompose_prepended_core_is_idempotent(ws):
    put(ws, "agents/CORE.md", "CORE")
    put(ws, "agents/HOUSE_STYLE.md", "STYLE")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", "plain\n")
    first = compose_system_prompt(ws, "test-agent")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", first)
    assert compose_system_prompt(ws, "test-agent") == first


def test_wrapper_only_section_gets_core_back_in_place(ws):
    put(ws, "agents/CORE.md", "NEW CORE")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md",
        "head\n<!-- begin:core -->\nOLD CORE\n<!-- end:core -->\ntail\n")
    out = compose_system_prompt(ws, "test-agent")
    assert out == "head\n<!-- begin:core -->\nNEW CORE\n<!-- end:core -->\ntail\n"


def test_flags_disable_core_and_house_style(ws):
    put(ws, "agents/CORE.md", "CORE")
    put(ws, "agents/HOUSE_STYLE.md", "STYLE")
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", f"a\n{SPLICE}\nb\n")
    cfg = {"prompt": {"section": "agents/test-agent/SYSTEM_PROMPT.md",
                      "core": False, "house_style": False}}
    out = compose_system_prompt(ws, "test-agent", config=cfg)
    assert "CORE" not in out and "STYLE" not in out and SPLICE not in out


def test_registry_prompt_key(ws):
    import yaml
    path = ws / "config" / "agents.yaml"
    data = yaml.safe_load(path.read_text())
    del data["agents"]["test-agent"]["system_prompt"]
    data["agents"]["test-agent"]["prompt"] = {"section": "agents/x/sec.md", "core": False}
    path.write_text(yaml.safe_dump(data))
    put(ws, "agents/CORE.md", "CORE")
    put(ws, "agents/HOUSE_STYLE.md", "STYLE")
    put(ws, "agents/x/sec.md", "SECTION")
    out = compose_system_prompt(ws, "test-agent")
    assert out.startswith("SECTION") and "CORE" not in out and "STYLE" in out


def test_registry_rejects_bad_prompt_key(ws):
    import registry
    with pytest.raises(registry.RegistryError):
        registry.parse_registry({"version": 2, "agents": {
            "a": {"name": "a", "role": "primary", "prompt": {"core": "yes"}},
            "m": {"name": "m", "role": "monitor"}}})


def test_unreadable_core_warns_and_composes(ws, caplog):
    (ws / "agents" / "CORE.md").mkdir(parents=True)     # reading a directory fails
    put(ws, "agents/test-agent/SYSTEM_PROMPT.md", "only section")
    with caplog.at_level(logging.WARNING):
        assert compose_system_prompt(ws, "test-agent") == "only section"
    assert "cannot read" in caplog.text


def test_missing_section_does_not_raise(ws):
    put(ws, "agents/CORE.md", "CORE")
    assert "CORE" in compose_system_prompt(ws, "test-agent")


def test_generated_paths(ws):
    assert pc.generated_path(ws, "a").name == "SYSTEM_PROMPT.generated.md"
    assert pc.generated_path(ws, "a", "a").name == "SYSTEM_PROMPT.generated.md"
    assert pc.generated_path(ws, "a", "s1") == ws / "agents/a/shards/s1.generated.md"


def test_shipped_templates_compose_cleanly(tmp_path):
    ws = tmp_path
    (ws / "config").mkdir()
    agents_dir = ws / "agents"
    agents_dir.mkdir()
    for name in ("CORE.md", "HOUSE_STYLE.md"):
        (agents_dir / name).write_text((PACKAGE_ROOT / "agents" / name).read_text())
    for tpl in ("primary", "relay"):
        text = (PACKAGE_ROOT / "agents" / "templates" / f"{tpl}.md").read_text()
        assert text.count(SPLICE) == 1
        put(ws, f"agents/{tpl}/SYSTEM_PROMPT.md", text)
    out = compose_system_prompt(ws, "primary", config={})
    assert SPLICE not in out and "## Core Operating Rules" in out
    assert out.rstrip().endswith("<!-- end:house-style -->")


# -- harness argv checks ------------------------------------------------------

def _argv_prompt(h, shard):
    argv = h.argv(shard)
    return argv[argv.index("--system-prompt") + 1]


def _spawn(h, *shards):
    async def scenario():
        async with h:
            for s in shards:
                await h.send(s, "x")
            for s in shards:
                await h.wait_idle(s)
    asyncio.run(scenario())


def test_unreadable_core_spawn_succeeds(tmp_workspace):
    h = Harness(tmp_workspace, agents=["a"])
    (tmp_workspace / "agents" / "CORE.md").mkdir()
    _spawn(h, "a")
    assert _argv_prompt(h, "a") == "You are harness agent a."


def test_core_edit_changes_next_spawn_for_two_agents(tmp_workspace):
    (tmp_workspace / "agents").mkdir(exist_ok=True)
    core = tmp_workspace / "agents" / "CORE.md"
    core.write_text("CORE ONE")
    h = Harness(tmp_workspace, agents=["a", "b"])
    _spawn(h, "a", "b")
    for s in ("a", "b"):
        assert "CORE ONE" in _argv_prompt(h, s)
        assert f"You are harness agent {s}." in _argv_prompt(h, s)
    assert (tmp_workspace / "agents/a/SYSTEM_PROMPT.generated.md").read_text() \
        == _argv_prompt(h, "a")

    core.write_text("CORE TWO")
    h2 = Harness(tmp_workspace, agents=["a", "b"])
    _spawn(h2, "a", "b")
    for s in ("a", "b"):
        assert "CORE TWO" in _argv_prompt(h2, s) and "CORE ONE" not in _argv_prompt(h2, s)
