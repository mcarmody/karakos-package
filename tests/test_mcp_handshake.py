"""Tests for the MCP initialize handshake in mcp/tools-server.py (stdio subprocess)."""
import json
import subprocess
import sys

from conftest import PACKAGE_ROOT

SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"


def _rpc(lines, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "WORKSPACE_ROOT": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(json.dumps(l) for l in lines) + "\n",
        capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
    )
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


def _names(resp):
    return sorted(t["name"] for t in resp["result"]["tools"])


def test_initialize(tmp_path):
    [r] = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-03-26"}}], tmp_path)
    assert r["id"] == 1
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["serverInfo"]["name"] == "karakos-tools"
    assert r["result"]["serverInfo"]["version"]
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}


def test_initialize_unsupported_version_gets_latest(tmp_path):
    [r] = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "1999-01-01"}}], tmp_path)
    assert r["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_silent_and_ping_and_unknown(tmp_path):
    out = _rpc([
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        {"jsonrpc": "2.0", "method": "bogus/notification"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "nope/nope"},
    ], tmp_path)
    assert [r["id"] for r in out] == [2, 3]
    assert out[0]["result"] == {}
    assert out[1]["error"]["code"] == -32601


def test_tools_list_with_and_without_handshake(tmp_path):
    bare = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], tmp_path)
    shaken = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ], tmp_path)
    assert _names(bare[0])
    assert _names(shaken[1]) == _names(bare[0])
