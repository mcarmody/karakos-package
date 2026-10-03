"""registry.py init: the fresh-install registry writer (step 3.1)."""

import subprocess
import sys

import pytest
import yaml

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import registry  # noqa: E402

REGISTRY_PY = PACKAGE_ROOT / "lib" / "registry.py"


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "channels.json").write_text('{"channels": {"general": {"id": "1"}}}')
    return tmp_path


@pytest.mark.parametrize("name", [
    "Jarvis: the second", 'say "hi"', "a # b", "R&D / ops", "Zoë Müller", "- dash", "yes", "123",
])
def test_awkward_names_round_trip(ws, name):
    registry.init_registry(ws, registry.slugify_id(name), name)
    reg = registry.load_registry(ws)
    assert reg.agent(registry.slugify_id(name)).name == name.strip()


def test_slug_rules():
    assert registry.slugify_id("Jarvis 2!") == "jarvis-2"
    assert registry.slugify_id("") == "karakos"
    assert registry.slugify_id("123") == "karakos"
    assert len(registry.slugify_id("x" * 80)) <= 32


def test_newline_refused(ws):
    with pytest.raises(registry.RegistryError):
        registry.init_registry(ws, "jarvis", "Jar\nvis")
    assert not (ws / "config" / "agents.yaml").exists()


def test_overlong_name_refused(ws):
    with pytest.raises(registry.RegistryError):
        registry.init_registry(ws, "jarvis", "x" * 65)


def test_primary_equal_monitor_refused(ws):
    with pytest.raises(registry.RegistryError, match="equals the monitor id"):
        registry.init_registry(ws, "monitor", "Monitor")


def test_existing_registry_refused(ws):
    (ws / "config" / "agents.yaml").write_text("keep: me\n")
    with pytest.raises(registry.RegistryError, match="already exists"):
        registry.init_registry(ws, "jarvis", "Jarvis")
    assert (ws / "config" / "agents.yaml").read_text() == "keep: me\n"


def test_document_shape(ws):
    registry.init_registry(ws, "jarvis", "Jarvis", channels=["general"])
    doc = yaml.safe_load((ws / "config" / "agents.yaml").read_text())
    p = doc["agents"]["jarvis"]
    assert "system_prompt" not in p and "system_prompt" not in doc["agents"]["monitor"]
    assert p["prompt"] == {"section": "agents/jarvis/SYSTEM_PROMPT.md", "core": True,
                           "house_style": True}
    assert p["context_budget_tokens"] == 150000
    assert "token_budget_4h" not in p and "handoff_on_reset" not in p
    assert p["shards"] == [{"id": "jarvis", "channels": ["general"]}]
    m = doc["agents"]["monitor"]
    assert m["role"] == "monitor" and m["model"] == "haiku"
    assert m["prompt"]["section"] == "agents/templates/monitor.md"
    assert registry.load_registry(ws).primary().id == "jarvis"


def test_cli_init(ws):
    r = subprocess.run([sys.executable, str(REGISTRY_PY), "init", "--workspace", str(ws),
                        "--primary-id", "jarvis", "--primary-name", "Jarvis: \"J\" #1",
                        "--channel", "general"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert registry.load_registry(ws).agent("jarvis").name == 'Jarvis: "J" #1'
    r = subprocess.run([sys.executable, str(REGISTRY_PY), "init", "--workspace", str(ws),
                        "--primary-id", "jarvis", "--primary-name", "x"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "already exists" in r.stderr


def test_cli_derives_id_from_name(ws):
    r = subprocess.run([sys.executable, str(REGISTRY_PY), "init", "--workspace", str(ws),
                        "--primary-name", "Jarvis 2!"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert registry.load_registry(ws).primary().id == "jarvis-2"
