"""Migration step 10_registry: agents.json -> agents.yaml (+ hooks-sync)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
import registry  # noqa: E402
from lib.migrate import guard, runner  # noqa: E402

STEP = next(s for s in runner.load_steps() if s.name == "10_registry")

LEGACY = {"agents": {
    "boss": {"model": "opus", "system_prompt": "agents/boss/SYSTEM_PROMPT.md",
             "discord_bot_token_env": "T_BOSS", "discord_bot_id_env": "I_BOSS",
             "env": {"A": "1"}, "label": "The Boss"},
    "relay": {"model": "haiku", "max_turns": 10, "dashboard_chat": False},
    "builder": {"system_prompt": "agents/builder/SYSTEM_PROMPT.md"},
    "reviewer": {"system_prompt": "agents/reviewer/SYSTEM_PROMPT.md"},
    "fifth": {"model": "sonnet", "allowed_tools": ["Read"]},
}}
CHANNELS = {"channels": {"general": {"id": "1", "default_agent": "boss"},
                         "signals": {"id": "2", "default_agent": None},
                         "ops": {"id": "3", "default_agent": "fifth"}}}


def make(tmp_path, legacy=LEGACY, channels=CHANNELS, settings=True):
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "data" / "old.db").write_text("x")
    (ws / "config" / "agents.json").write_text(json.dumps(legacy))
    if channels is not None:
        (ws / "config" / "channels.json").write_text(json.dumps(channels))
    if settings:
        (ws / "config" / "claude-settings.json").write_text(
            json.dumps({"permissions": {"allow": ["X"], "deny": []}}))
    return ws


def _no_prompt(agents):
    """legacy_view minus the prompt flags the migration adds and the effective
    handoff_on_reset (2.6 always writes it)."""
    return {a: {k: v for k, v in e.items() if k not in ("prompt", "handoff_on_reset")}
            for a, e in agents.items()}


def migrate(ws, **kw):
    lines = []
    rc = runner.run(ws / "data", ws / "config", ws / "backups",
                    steps=[STEP], out=lines.append, **kw)
    return rc, lines


def test_migrates_conftest_fixture(legacy_workspace):
    ws = legacy_workspace
    rc, lines = migrate(ws)
    assert rc == 0, lines
    reg = registry.load_registry(ws)
    assert reg.primary().id == "test-agent"
    assert _no_prompt(reg.legacy_view()["agents"])["test-agent"] == \
        json.loads((ws / "config" / "agents.json").read_text())["agents"]["test-agent"]
    assert guard.read_stamp(ws / "data")


def test_second_run_is_a_noop(tmp_path):
    ws = make(tmp_path)
    assert migrate(ws)[0] == 0
    before = (ws / "config" / "agents.yaml").read_bytes()
    ctx = runner.Context(ws / "data", ws / "config", None, None, None)
    assert STEP.detect(ctx) is False
    assert registry.migrate_legacy(ws) is False
    assert (ws / "config" / "agents.yaml").read_bytes() == before
    rc, lines = migrate(ws)
    assert rc == 0 and any("nothing to do" in l for l in lines)


def test_backup_and_pre_2_0_exist(tmp_path):
    ws = make(tmp_path)
    original = (ws / "config" / "agents.json").read_text()
    assert migrate(ws)[0] == 0
    assert (ws / "config" / "agents.json.pre-2.0").read_text() == original
    backup = next((ws / "backups").iterdir())
    assert any(p.name == "agents.json" for p in backup.rglob("agents.json"))


def test_role_mapping_and_channels(tmp_path):
    ws = make(tmp_path)
    assert migrate(ws)[0] == 0
    reg = registry.load_registry(ws)
    roles = {a.id: a.role for a in reg.agents()}
    assert roles == {"boss": "primary", "relay": "monitor", "builder": "builder",
                     "reviewer": "reviewer", "fifth": "custom"}
    assert reg.shard_for_channel("general").agent == "boss"
    assert reg.shard_for_channel("ops").agent == "fifth"
    assert reg.shard_for_channel("signals") is None
    assert reg.agent("relay").get("model") == "haiku"
    view = reg.legacy_view()["agents"]
    assert _no_prompt({a: view[a] for a in LEGACY["agents"]}) == LEGACY["agents"]


def test_no_relay_gets_default_monitor(tmp_path):
    legacy = {"agents": {"solo": {"model": "opus"}}}
    ws = make(tmp_path, legacy=legacy, channels=None)
    assert migrate(ws)[0] == 0
    reg = registry.load_registry(ws)
    mon = reg.monitor()
    assert mon.id == "relay" and mon.get("model") == "haiku"
    assert reg.primary().id == "solo"


def test_migrated_agents_keep_prompt_verbatim(tmp_path):
    from prompt_compose import compose_system_prompt
    ws = make(tmp_path)
    (ws / "agents" / "boss").mkdir(parents=True)
    (ws / "agents" / "boss" / "SYSTEM_PROMPT.md").write_text("OLD BOSS PROMPT\n")
    (ws / "agents" / "CORE.md").write_text("CORE TEXT")
    (ws / "agents" / "HOUSE_STYLE.md").write_text("HOUSE TEXT")
    assert migrate(ws)[0] == 0
    reg = registry.load_registry(ws)
    for aid in LEGACY["agents"]:
        if aid == "relay":
            continue
        p = reg.agent(aid).get("prompt")
        assert p["core"] is False and p["house_style"] is False, aid
    assert reg.agent("boss").get("prompt")["section"] == "agents/boss/SYSTEM_PROMPT.md"
    assert compose_system_prompt(ws, "boss") == "OLD BOSS PROMPT\n"


def test_fresh_install_agent_has_flags_on(tmp_path):
    from prompt_compose import compose_system_prompt
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "agents" / "a").mkdir(parents=True)
    (ws / "agents" / "CORE.md").write_text("CORE TEXT")
    (ws / "agents" / "HOUSE_STYLE.md").write_text("HOUSE TEXT")
    (ws / "agents" / "a" / "SYSTEM_PROMPT.md").write_text("A PROMPT")
    (ws / "config" / "agents.yaml").write_text(yaml.safe_dump(
        {"version": registry.REGISTRY_VERSION,
         "agents": {"a": {"name": "a", "role": "primary"}}}))
    out = compose_system_prompt(ws, "a")
    assert "CORE TEXT" in out and "HOUSE TEXT" in out and "A PROMPT" in out


def test_default_monitor_prompt_section_exists(tmp_path):
    ws = make(tmp_path, legacy={"agents": {"solo": {"model": "opus"}}}, channels=None)
    assert migrate(ws)[0] == 0
    p = registry.load_registry(ws).monitor().get("prompt")
    assert p["core"] is True and p["house_style"] is True
    assert (ROOT / p["section"]).is_file()


def test_verify_fails_when_a_field_is_dropped(tmp_path):
    ws = make(tmp_path)
    assert migrate(ws)[0] == 0
    path = ws / "config" / "agents.yaml"
    data = yaml.safe_load(path.read_text())
    del data["agents"]["boss"]["model"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    ctx = runner.Context(ws / "data", ws / "config", None, None, None)
    with pytest.raises(RuntimeError, match="model"):
        STEP.verify(ctx)


def test_unknown_legacy_key_is_carried_not_dropped(tmp_path):
    legacy = {"agents": {"solo": {"model": "opus", "future_key": 7}}}
    ws = make(tmp_path, legacy=legacy, channels=None)
    assert migrate(ws)[0] == 3                      # fork policy refuses unknown keys
    assert migrate(ws, force=True)[0] == 0
    assert yaml.safe_load((ws / "config" / "agents.yaml").read_text())[
        "agents"]["solo"]["future_key"] == 7


def test_unusable_agents_json_fails_without_stamp(tmp_path):
    ws = make(tmp_path, legacy={"agents": {}})
    rc, _ = migrate(ws)
    assert rc != 0 and guard.read_stamp(ws / "data") is None
    assert not (ws / "config" / "agents.yaml").exists()


def test_migration_runs_hooks_sync(tmp_path):
    ws = make(tmp_path)
    assert not (ws / "config" / "hooks.json").exists()
    assert migrate(ws)[0] == 0
    assert (ws / "config" / "hooks.json").exists()
    settings = json.loads((ws / "config" / "claude-settings.json").read_text())
    assert "hooks" in settings and settings["permissions"]["allow"] == ["X"]


def test_boot_leaves_unstamped_1x_settings_byte_identical(tmp_path):
    ws = make(tmp_path)
    settings = ws / "config" / "claude-settings.json"
    before = settings.read_bytes()
    env = {**__import__("os").environ, "WORKSPACE_ROOT": str(ws),
           "DASHBOARD_PORT": "3000", "AGENT_SERVER_TOKEN": "t"}
    env.pop("KARAKOS_SKIP_STAMP_CHECK", None)
    for n in ("logs", "inbox"):
        (ws / n).mkdir()
    r = subprocess.run(["bash", str(ROOT / "bin" / "entrypoint.sh")], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 78, r.stderr
    assert settings.read_bytes() == before
    assert not (ws / "config" / "hooks.json").exists()
    assert not (ws / "config" / "agents.yaml").exists()
