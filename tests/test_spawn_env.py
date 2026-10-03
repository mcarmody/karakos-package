"""Subprocess env allowlist (2.0 step 1.6)."""
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import spawn_env  # noqa: E402
from harness import Harness  # noqa: E402

BASE = {
    "PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "XDG_DATA_HOME": "/x",
    "https_proxy": "p", "ANTHROPIC_API_KEY": "ak", "CLAUDE_CODE_OAUTH_TOKEN": "ot",
    "DISCORD_BOT_TOKEN": "d1", "DISCORD_TOKEN_AMOS": "d2", "AGENT_SERVER_TOKEN": "st",
    "GITHUB_TOKEN": "gh", "RANDOM_THING": "r",
}


def test_tokens_absent_allowlisted_present():
    env = spawn_env.build_subprocess_env(BASE, {}, {})
    for k in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN_AMOS", "AGENT_SERVER_TOKEN",
              "GITHUB_TOKEN", "RANDOM_THING"):
        assert k not in env
    for k in ("PATH", "HOME", "LANG", "LC_ALL", "XDG_DATA_HOME", "https_proxy",
              "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert env[k] == BASE[k]


def test_layering_order():
    env = spawn_env.build_subprocess_env(
        {"HOME": "base", "PATH": "base"}, {"HOME": "agent", "PATH": "agent"},
        {"PATH": "extra"})
    assert env == {"HOME": "agent", "PATH": "extra"}


def test_ref_resolution_and_unresolved(caplog):
    with caplog.at_level(logging.WARNING, logger="spawn_env"):
        env = spawn_env.build_subprocess_env(
            BASE, {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "GONE": "${NOPE}", "PLAIN": "v"}, {})
    assert env["GITHUB_TOKEN"] == "gh" and env["PLAIN"] == "v"
    assert "GONE" not in env
    assert "NOPE" in caplog.text


def test_agent_env_may_name_a_denied_token():
    env = spawn_env.build_subprocess_env(BASE, {"DISCORD_BOT_TOKEN": "${DISCORD_BOT_TOKEN}"}, {})
    assert env["DISCORD_BOT_TOKEN"] == "d1"


def test_passthrough_flag_and_production_refusal(caplog):
    assert spawn_env.passthrough_requested({}) is False
    with caplog.at_level(logging.WARNING, logger="spawn_env"):
        assert spawn_env.passthrough_requested({"KARAKOS_ENV_PASSTHROUGH": "1"}) is True
    assert "Debugging only" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="spawn_env"):
        assert spawn_env.passthrough_requested(
            {"KARAKOS_ENV_PASSTHROUGH": "1", "KARAKOS_ENV": "production"}) is False
    assert "refused" in caplog.text


# -- harness: the fake claude records its real environment -------------------

def test_secret_absent_from_subprocess_unless_named(harness, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN_X", "secret")
    monkeypatch.setenv("NAMED_SECRET", "granted")
    h = harness(agents={"a": {"env": {"NAMED_SECRET": "${NAMED_SECRET}"}}, "b": {}})

    async def scenario():
        async with h:
            h.script(default={"text": "env:{{env:DISCORD_BOT_TOKEN_X}}|{{env:NAMED_SECRET}}|{{env:KARAKOS_AGENT}}"})
            await h.send("a", "hi")
            await h.wait_idle("a")
            await h.send("b", "hi")
            await h.wait_idle("b")

    asyncio.run(scenario())
    assert h.queue_rows("a")[0]["response"] == "env:|granted|a"
    assert h.queue_rows("b")[0]["response"] == "env:||b"
