"""The agent server listens on loopback by default; the container opts in to all interfaces."""
import asyncio
import configparser
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
PACKAGE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))


@pytest.fixture
def ags(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("ags_bind", PACKAGE_ROOT / "bin" / "agent-server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ags_bind"] = module
    spec.loader.exec_module(module)
    module.AGENT_SERVER_TOKEN = "t0ken"
    return module


def test_default_is_loopback(ags, monkeypatch):
    monkeypatch.delenv("AGENT_SERVER_BIND", raising=False)
    assert ags.bind_host() == "127.0.0.1"
    monkeypatch.setenv("AGENT_SERVER_BIND", "  ")
    assert ags.bind_host() == "127.0.0.1"


def test_env_overrides(ags, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_BIND", "0.0.0.0")
    assert ags.bind_host() == "0.0.0.0"


def test_main_passes_the_bind_host_to_run_app(ags, monkeypatch):
    seen = {}
    monkeypatch.setattr(ags, "require_stamp", lambda _p: None)
    monkeypatch.setattr(ags.web, "run_app", lambda app, **kw: seen.update(kw))
    monkeypatch.delenv("AGENT_SERVER_BIND", raising=False)
    ags.main()
    assert seen["host"] == "127.0.0.1"
    monkeypatch.setenv("AGENT_SERVER_BIND", "0.0.0.0")
    ags.main()
    assert seen["host"] == "0.0.0.0"


def test_container_opts_in_to_all_interfaces():
    cp = configparser.RawConfigParser(strict=False)
    cp.read(PACKAGE_ROOT / "config" / "supervisord.conf")
    assert 'AGENT_SERVER_BIND="0.0.0.0"' in cp["program:agent-server"]["environment"]


def test_native_units_do_not_open_the_api():
    sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
    import native_units
    units = native_units.render("/opt/karakos", "/venv/bin/python", path="/usr/bin:/bin")
    assert "0.0.0.0" not in units["karakos-agent-server.service"]
    assert "AGENT_SERVER_BIND" not in units["karakos-agent-server.service"]


def test_token_matches(ags):
    assert ags._token_matches("Bearer t0ken") is True
    assert ags._token_matches("Bearer t0kex") is False
    assert ags._token_matches("Bearer ") is False
    assert ags._token_matches("t0ken") is False
    assert ags._token_matches("Bearer töken") is False   # non-ASCII: 401, not an exception


def test_no_plain_token_comparisons_remain():
    src = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    assert "[7:] != AGENT_SERVER_TOKEN" not in src
    assert "[7:] == AGENT_SERVER_TOKEN" not in src


def test_wrong_token_is_401_over_http(ags):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        client = TestClient(TestServer(ags.create_app(with_lifecycle=False), host="127.0.0.1"))
        await client.start_server()
        try:
            bad = await client.post("/message", json={}, headers={"Authorization": "Bearer nope"})
            return bad.status
        finally:
            await client.close()

    assert asyncio.run(go()) == 401
