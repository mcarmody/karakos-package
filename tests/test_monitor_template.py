"""Monitor template and default monitor (step 3.2b)."""
import re
import sys

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import registry  # noqa: E402
from prompt_compose import compose_system_prompt  # noqa: E402

TEMPLATE = PACKAGE_ROOT / "agents" / "templates" / "monitor.md"
SENTINEL = "This prompt is the monitor template, version 2.0."
SIX = {"AGENT_NAME", "AGENT_ID", "SYSTEM_NAME", "OWNER_NAME", "OWNER_ID", "CHANNELS"}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "channels.json").write_text('{"channels": {"general": {"id": "1"}}}')
    return tmp_path


def test_template_exists_and_relay_is_gone():
    assert TEMPLATE.is_file()
    assert not (PACKAGE_ROOT / "agents" / "templates" / "relay.md").exists()
    assert SENTINEL in TEMPLATE.read_text()


def test_only_known_placeholders():
    names = set(re.findall(r"\{\{\s*([A-Za-z_]+)\s*\}\}", TEMPLATE.read_text()))
    assert names and names <= {"AGENT_NAME", "SYSTEM_NAME", "OWNER_NAME"} | SIX


def test_template_has_no_shell_instructions():
    text = TEMPLATE.read_text()
    assert "Bash" not in text and "poke.sh" not in text
    assert "Read tool" in text and "Why:" in text


def test_default_monitor_shape():
    m = registry._DEFAULT_MONITOR
    assert m["role"] == "monitor" and m["model"] == "haiku"
    assert m["max_turns"] == 10 and m["timeout"] == 300 and m["dashboard_chat"] is False
    assert m["prompt"]["section"] == "agents/templates/monitor.md"
    assert set(m["disallowed_tools"]) == {"Bash", "Write", "Edit", "NotebookEdit",
                                          "WebFetch", "WebSearch"}


@pytest.mark.parametrize("bad", ["relay", "scheduler", "mcp-tools", "server"])
def test_init_refuses_reserved_monitor_ids(ws, bad):
    with pytest.raises(Exception):
        registry.init_registry(ws, "boss", "Boss", monitor_id=bad)


def test_init_round_trips_display_name(ws):
    registry.init_registry(ws, "boss", "Boss", monitor_id="watch-dog",
                           monitor_name="Watch, Dog (v2)!")
    reg = registry.load_registry(ws)
    mon = reg.monitor()
    assert mon.id == "watch-dog" and mon.get("name") == "Watch, Dog (v2)!"
    assert mon.get("disallowed_tools") == registry.MONITOR_DISALLOWED_TOOLS


def test_composed_prompt_has_sentinel(ws):
    (ws / "agents" / "templates").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "templates" / "monitor.md").write_text(TEMPLATE.read_text())
    registry.init_registry(ws, "boss", "Boss", monitor_id="watch")
    out = compose_system_prompt(ws, "watch")
    assert SENTINEL in out


def test_monitor_argv_carries_denials():
    server = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    assert '"--disallowedTools"' in server and 'config.get("disallowed_tools"' in server
