"""/effort (6.4): the override file, the spawn argv and the HTTP route.

Agent `a` has shards `a` and `a-2`; `b` has its default shard. The `slow` +
`realcli` case runs the real CLI with every level (by hand; it proves the CLI
accepts the value the server passes, not that the model reasons differently).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import runtime_overrides as ro  # noqa: E402

SHARDS = {"a": ["a", "a-2"]}
NO_STEER = {"enabled": False}


def run(coro):
    return asyncio.run(coro)


# -- unit -----------------------------------------------------------------------

def test_effective_level_order(tmp_path):
    assert ro.effective_effort(tmp_path, "a", {}) == (None, "default")
    assert ro.effective_effort(tmp_path, "a", {"effort": "low"}) == ("low", "registry")
    ro.set_effort(tmp_path, "a", "max")
    assert ro.effective_effort(tmp_path, "a", {"effort": "low"}) == ("max", "override")
    assert ro.effective_effort(tmp_path, "b", {"effort": "low"}) == ("low", "registry")


def test_default_removes_the_entry(tmp_path):
    ro.set_effort(tmp_path, "a", "high")
    assert ro.set_effort(tmp_path, "a", "default") is None
    assert json.loads(ro.path_for(tmp_path).read_text()) == {"agents": {}}
    assert ro.effective_effort(tmp_path, "a", {}) == (None, "default")


def test_invalid_level_and_tolerant_read(tmp_path):
    with pytest.raises(ValueError):
        ro.set_effort(tmp_path, "a", "extreme")
    assert ro.load(tmp_path) == {}
    ro.path_for(tmp_path).parent.mkdir(parents=True)
    ro.path_for(tmp_path).write_text("{oops")
    assert ro.load(tmp_path) == {}
    ro.path_for(tmp_path).write_text('{"agents": 3}')
    assert ro.load(tmp_path) == {}
    assert ro.effective_effort(tmp_path, "a", {"effort": "low"}) == ("low", "registry")


def test_override_write_is_atomic(tmp_path):
    ro.set_effort(tmp_path, "a", "low")
    assert not list(ro.path_for(tmp_path).parent.glob("*.tmp"))


def test_cli_without_the_flag_is_reported(monkeypatch):
    monkeypatch.setattr(ro, "_cli_has_effort", None)
    out = subprocess.CompletedProcess([], 0, stdout="Usage: claude\n  --model <m>\n")
    monkeypatch.setattr(ro.subprocess, "run", lambda *a, **k: out)
    assert ro.cli_supports_effort() is False
    monkeypatch.setattr(ro, "_cli_has_effort", None)
    monkeypatch.setattr(ro.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout="  --model <m>\n  --effort <level>\n"))
    assert ro.cli_supports_effort() is True
    monkeypatch.setattr(ro, "_cli_has_effort", None)


# -- harness --------------------------------------------------------------------

async def post(h, path, body=None):
    resp = await h.client.post(path, headers=h._headers(), json=body)
    return resp.status, await resp.json()


async def agents(h):
    return (await (await h.client.get("/agents", headers=h._headers())).json())["agents"]


def effort_arg(argv):
    return argv[argv.index("--effort") + 1] if argv and "--effort" in argv else None


async def respawned(h, shard, level):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        argv = h.argv(shard)
        if effort_arg(argv) == level:
            return argv
        await asyncio.sleep(0.05)
    raise TimeoutError(f"{shard} not respawned with --effort {level}: {h.argv(shard)}")


def test_registry_effort_reaches_the_argv_and_agents_report_it(harness):
    h = harness(agents={"a": {"effort": "low"}, "b": {}}, steering=NO_STEER)

    async def scenario():
        async with h:
            assert effort_arg(h.argv("a")) == "low"
            assert effort_arg(h.argv("b")) is None and "--effort" not in h.argv("b")
            by = {a["name"]: a["shards"][0] for a in await agents(h)}
            assert (by["a"]["effort"], by["a"]["effort_source"]) == ("low", "registry")
            assert (by["b"]["effort"], by["b"]["effort_source"]) == (None, "default")

    run(scenario())


def test_idle_shard_respawns_with_the_override_and_same_session(harness):
    h = harness(agents=["a", "b"], shards=SHARDS, steering=NO_STEER)
    registry = h.workspace / "config" / "agents.yaml"
    before_bytes = registry.read_bytes()

    async def scenario():
        async with h:
            sid = {s: h.session_id(s) for s in ("a", "a-2", "b")}
            code, body = await post(h, "/agents/a/effort", {"level": "max"})
            assert code == 200
            assert body == {"effort": "max", "applied": ["a", "a-2"], "deferred": []}
            for s in ("a", "a-2"):
                argv = await respawned(h, s, "max")
                assert h.session_id(s) == sid[s] and sid[s] in argv
            assert "--effort" not in h.argv("b")
            by = {x["id"]: x for a in await agents(h) for x in a["shards"]}
            assert (by["a-2"]["effort"], by["a-2"]["effort_source"]) == ("max", "override")
            # a shard id names its agent: the override is agent-level
            code, body = await post(h, "/agents/a-2/effort", {"level": "default"})
            assert body["effort"] is None
            await h.wait_for(lambda: "--effort" not in (h.argv("a") or ["--effort"]))
            assert json.loads(ro.path_for(h.workspace).read_text()) == {"agents": {}}

    run(scenario())
    assert registry.read_bytes() == before_bytes


def test_processing_shard_finishes_its_turn_then_respawns(harness):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            h.script(rules=[{"match": "slow", "step": {"text": "r-slow", "delay_ms": 600}}])
            await h.send("a", "slow")
            await h.wait_for(lambda: h.module.agent_states.get("a") == "PROCESSING")
            pid = h.module.agent_processes["a"].pid
            code, body = await post(h, "/agents/a/effort", {"level": "high"})
            assert body == {"effort": "high", "applied": [], "deferred": ["a"]}
            assert h.module.agent_processes["a"].pid == pid     # not interrupted
            await h.wait_idle("a")
            assert any(d["content"] == "r-slow" for d in h.discord)   # the reply posted
            await respawned(h, "a", "high")
            assert h.module.agent_processes["a"].pid != pid
            await h.send("a", "after")
            await h.wait_idle("a")
            assert "after" in " ".join(h.sent_to("a"))

    run(scenario())


def test_bad_requests(harness):
    h = harness(agents=["a", "b"], steering=NO_STEER)

    async def scenario():
        async with h:
            code, body = await post(h, "/agents/a/effort", {"level": "extreme"})
            assert code == 400 and "low, medium, high, xhigh, max, default" in body["error"]
            assert (await post(h, "/agents/a/effort", {}))[0] == 400
            assert (await post(h, "/agents/nobody/effort", {"level": "low"}))[0] == 404
            assert not ro.path_for(h.workspace).exists()
            unauth = await h.client.post("/agents/a/effort", json={"level": "low"})
            assert unauth.status == 401

    run(scenario())


def test_unsupported_cli_is_refused_and_flag_omitted(harness, monkeypatch):
    monkeypatch.setattr(ro, "_cli_has_effort", False)
    h = harness(agents={"a": {"effort": "low"}, "b": {}}, steering=NO_STEER)

    async def scenario():
        async with h:
            assert "--effort" not in h.argv("a")
            code, body = await post(h, "/agents/b/effort", {"level": "low"})
            assert code == 400 and "not supported" in body["error"]

    run(scenario())


# -- real CLI -------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.realcli
@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_real_cli_accepts_every_level(level):
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("no claude CLI")
    p = subprocess.run(
        [claude, "-p", "Reply with the single word: ok", "--model", "haiku",
         "--effort", level, "--output-format", "json", "--max-turns", "1"],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr[-500:] + p.stdout[-500:]
    assert "usage" not in p.stderr.lower() and "invalid" not in p.stderr.lower()
