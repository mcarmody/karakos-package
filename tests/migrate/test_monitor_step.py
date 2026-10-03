"""Migration step 12_monitor: default monitor gets the 2.0 template (config only)."""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
import registry  # noqa: E402
from lib.migrate import guard, runner  # noqa: E402

STEPS = {s.name: s for s in runner.load_steps()}
CHAIN = [STEPS["10_registry"], STEPS["12_monitor"]]
SENTINEL = "This prompt is the monitor template, version 2.0."


def make(tmp_path, legacy):
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "data" / "old.db").write_text("x")
    (ws / "config" / "agents.json").write_text(json.dumps(legacy))
    (ws / "config" / "claude-settings.json").write_text(
        json.dumps({"permissions": {"allow": ["X"], "deny": []}}))
    return ws


def migrate(ws, steps=CHAIN):
    guard.stamp_path(ws / "data").unlink(missing_ok=True)   # re-run the plan
    lines = []
    rc = runner.run(ws / "data", ws / "config", ws / "backups", steps=steps, out=lines.append)
    return rc, lines


def test_one_agent_install_gets_default_monitor(tmp_path):
    ws = make(tmp_path, {"agents": {"solo": {"model": "opus"}}})
    assert migrate(ws)[0] == 0
    mon = registry.load_registry(ws).monitor()
    assert mon.get("prompt")["section"] == "agents/templates/monitor.md"
    assert SENTINEL in (ws / "agents/templates/monitor.md").read_text()
    assert "Bash" in mon.get("disallowed_tools")


def test_second_run_is_a_noop(tmp_path):
    ws = make(tmp_path, {"agents": {"solo": {"model": "opus"}}})
    assert migrate(ws)[0] == 0
    before = (ws / "config" / "agents.yaml").read_text()
    assert migrate(ws)[0] == 0
    assert (ws / "config" / "agents.yaml").read_text() == before


def test_old_relay_section_repointed_comments_kept(tmp_path):
    ws = make(tmp_path, {"agents": {"solo": {"model": "opus"}}})
    assert migrate(ws, [STEPS["10_registry"]])[0] == 0
    path = ws / "config" / "agents.yaml"
    text = path.read_text()
    doc = yaml.safe_load(text)
    mid = next(a for a, e in doc["agents"].items() if e["role"] == "monitor")
    doc["agents"][mid].pop("disallowed_tools", None)
    doc["agents"][mid].pop("max_turns", None)
    doc["agents"][mid].pop("timeout", None)
    doc["agents"][mid]["prompt"]["section"] = "agents/templates/relay.md"
    path.write_text("# keep me\n" + yaml.safe_dump(doc))
    assert migrate(ws, [STEPS["12_monitor"]])[0] == 0
    out = path.read_text()
    assert out.startswith("# keep me") and "relay.md" not in out
    mon = registry.load_registry(ws).monitor()
    assert mon.get("max_turns") == 10 and mon.get("timeout") == 300
    assert "Bash" in mon.get("disallowed_tools")


def test_missing_template_file_is_copied_never_overwritten(tmp_path):
    ws = make(tmp_path, {"agents": {"solo": {"model": "opus"}}})
    assert migrate(ws)[0] == 0
    dest = ws / "agents/templates/monitor.md"
    dest.write_text("MY OWN\n")
    assert migrate(ws)[0] == 0
    assert dest.read_text() == "MY OWN\n"
    dest.unlink()
    assert migrate(ws)[0] == 0
    assert SENTINEL in dest.read_text()


def test_own_relay_monitor_untouched(tmp_path):
    legacy = {"agents": {
        "boss": {"model": "opus"},
        "relay": {"model": "haiku", "system_prompt": "agents/relay/SYSTEM_PROMPT.md",
                  "allowed_tools": ["Read"]}}}
    ws = make(tmp_path, legacy)
    (ws / "agents/relay").mkdir(parents=True)
    (ws / "agents/relay/SYSTEM_PROMPT.md").write_text("MINE")
    assert migrate(ws)[0] == 0
    mon = registry.load_registry(ws).monitor()
    assert mon.get("prompt")["section"] == "agents/relay/SYSTEM_PROMPT.md"
    assert "Bash" not in (mon.get("disallowed_tools") or [])
    assert mon.get("allowed_tools") == ["Read"]
    assert not (ws / "agents/templates/monitor.md").exists()
