"""Shared MCP handshake helpers for the stdio JSON-RPC servers."""
import re
from pathlib import Path

SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[-1]


def package_version() -> str:
    """Latest released version from CHANGELOG.md (no other version source exists)."""
    try:
        text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
        m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def initialize_result(params, server_name: str) -> dict:
    """Result for `initialize`: echo the client's protocol version if supported."""
    asked = (params or {}).get("protocolVersion")
    return {
        "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": server_name, "version": package_version()},
    }
