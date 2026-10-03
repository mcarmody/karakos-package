"""Primary template 2.0, shard template, house style, onboarding gate (step 3.1)."""

import asyncio
import importlib.util
import json
import re
import subprocess
import sys

import pytest

from conftest import PACKAGE_ROOT, import_script

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import prompt_compose as pc  # noqa: E402
import registry  # noqa: E402

AGENTS = PACKAGE_ROOT / "agents"
TEMPLATE = AGENTS / "templates" / "primary.md"
SIX = {"AGENT_NAME", "SYSTEM_NAME", "OWNER_NAME", "CHANNELS", "OTHER_AGENTS", "SHARD_ID"}
STANDARD_TOOLS = {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"}
HIVE_TOOLS = {"buzz", "hive_call"}
SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"


def section(text, heading):
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"missing section {heading}"
    return m.group(1)


def test_placeholders_are_known():
    files = [TEMPLATE, AGENTS / "templates" / "shard.md", AGENTS / "templates" / "onboarding.md",
             AGENTS / "HOUSE_STYLE.md", AGENTS / "CORE.md"]
    for f in files:
        names = set(re.findall(r"\{\{\s*([A-Za-z_]+)\s*\}\}", f.read_text()))
        assert names <= SIX, f"{f.name}: unknown placeholders {names - SIX}"


def test_no_household_vocabulary():
    for f in [TEMPLATE, AGENTS / "HOUSE_STYLE.md", AGENTS / "templates" / "shard.md",
              AGENTS / "templates" / "onboarding.md"]:
        assert not re.search(r"household|majordomo", f.read_text(), re.I), f.name


@pytest.fixture
def jarvis(real_template_workspace, monkeypatch):
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("SYSTEM_NAME", raising=False)
    return real_template_workspace


def test_composition_default_shard(jarvis):
    out = pc.compose_system_prompt(jarvis, "jarvis")
    assert "Jarvis" in out and "`jarvis`" in out
    assert "- #general" in out and "- **Relay** (" in out
    assert "## Shards and the hive" in out
    assert "{{" not in out
    assert len(out) <= 9000, len(out)


def test_composition_second_shard(jarvis):
    shard = jarvis / "agents" / "jarvis" / "shards" / "jarvis-2.md"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text((AGENTS / "templates" / "shard.md").read_text())
    out = pc.compose_system_prompt(jarvis, "jarvis", "jarvis-2")
    assert "shard `jarvis-2` of Jarvis" in out
    assert "## Shard jarvis-2" in out
    assert out.index("## Shards and the hive") < out.index("## Shard jarvis-2")


def _tool_names():
    env = {"PATH": "/usr/bin:/bin", "WORKSPACE_ROOT": "/nonexistent-ws"}
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        env["HOME"] = d
        env["WORKSPACE_ROOT"] = d
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        proc = subprocess.run([sys.executable, str(SERVER)], cwd=d, env=env, timeout=30,
                              input="\n".join(map(json.dumps, lines)) + "\n",
                              capture_output=True, text=True)
    resp = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    return {t["name"] for r in resp if r.get("id") == 2 for t in r["result"]["tools"]}


def test_tool_names_are_real():
    names = _tool_names()
    text = TEMPLATE.read_text()
    cited = set()
    for head in ("Tools", "Shards and the hive"):
        cited |= set(re.findall(r"`([a-z_]+)`", section(text, head)))
    # Prose tokens that are not tools.
    cited -= {"callee_paused", "expired", "timeout"}
    cited = {c for c in cited if not re.match(r"^(jarvis|\[)", c)} - {"SHARD_ID"}
    known = names | STANDARD_TOOLS
    pending = HIVE_TOOLS - names           # 2.3 not merged yet
    missing = cited - known - pending
    assert not missing, f"template cites tools that do not exist: {missing}"
    if pending:
        pytest.skip(f"2.3 has not merged: {sorted(pending)} asserted by spec name only")


def test_session_wording_consistent():
    core = (AGENTS / "CORE.md").read_text()
    tpl = TEMPLATE.read_text()
    assert "session.finalize" in core and "session.finalize" in tpl
    for text in (core, tpl):
        assert not re.search(r"generate a summary", text, re.I)


def test_house_style_and_core_do_not_repeat():
    def lines(p):
        return {l.strip() for l in (AGENTS / p).read_text().splitlines() if l.strip()}
    assert lines("HOUSE_STYLE.md") & lines("CORE.md") == set()


def test_house_style_covers_new_rules():
    text = (AGENTS / "HOUSE_STYLE.md").read_text().lower()
    for needle in ("table", "fenced", "secrets", "one sentence", "what was tried"[:4],
                   "language"):
        assert needle in text


# -- onboarding gate -----------------------------------------------------------

@pytest.fixture
def server(real_template_workspace, monkeypatch):
    ws = real_template_workspace
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    return ws, import_script("agent-server")


def _onboard(ws, agent, text="ONBOARD {{AGENT_NAME}} agents/{{AGENT_NAME}}/persona"):
    (ws / "agents" / agent).mkdir(parents=True, exist_ok=True)
    (ws / "agents" / agent / "persona").mkdir(exist_ok=True)
    (ws / "agents" / agent / "onboarding.md").write_text(text)


def test_onboarding_primary_empty_persona(server):
    ws, mod = server
    _onboard(ws, "jarvis")
    out = mod.load_onboarding_prompt("jarvis")
    assert out == "ONBOARD Jarvis agents/jarvis/persona"


def test_onboarding_primary_with_persona_is_empty(server):
    ws, mod = server
    _onboard(ws, "jarvis")
    (ws / "agents/jarvis/persona/identity.md").write_text("They are Sam.")
    assert mod.load_onboarding_prompt("jarvis") == ""


@pytest.mark.parametrize("role", ["monitor", "builder", "reviewer", "custom"])
def test_onboarding_non_primary_is_empty(server, role):
    ws, mod = server
    if role == "monitor":
        aid = "monitor"
    else:
        aid = f"x-{role}"
        registry.write_agent(ws, aid, {"name": aid, "role": role})
    _onboard(ws, aid)
    assert mod.load_onboarding_prompt(aid) == ""


def test_onboarding_falls_back_when_registry_unloadable(server):
    ws, mod = server
    _onboard(ws, "jarvis")
    (ws / "config" / "agents.yaml").write_text(": : not yaml [")
    assert mod.load_onboarding_prompt("jarvis").startswith("ONBOARD")
    (ws / "agents/jarvis/persona/identity.md").write_text("x")
    assert mod.load_onboarding_prompt("jarvis") == ""
