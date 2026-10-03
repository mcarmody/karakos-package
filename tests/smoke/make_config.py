#!/usr/bin/env python3
"""Write what the setup wizard writes, without the wizard (7.3a).

setup.sh is interactive and has no non-interactive mode, so the fresh-install
smoke builds its config here: config/.env, config/agents.yaml (through the
harness's write_agents_config, so the registry is valid by construction, with
the fake claude's ${FAKE_CLAUDE_*} env references), config/channels.json and a
config/docker-compose.smoke.yml override that mounts tests/harness read-only at
/opt/fake and a one-line `claude` wrapper over the image's resolved claude path.

Stated gap: the wizard itself is covered by 7.3b's manual checklist; this proves
the artifacts it must produce start a working install.

usage: make_config.py <install-dir> --dashboard-port N --server-port N
           --claude-path /usr/bin/claude [--version-tag smoke-abc] [--reply TEXT]
Prints the generated values as KEY=VALUE lines (the password included; it is a
random throwaway).
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

REPLY = "smoke-ok"
FAKE_LOG_DIR = "/tmp/fake-claude-logs"
FAKE_SCRIPT = "/workspace/config/fake-claude-script.json"


def entrypoint_required_vars(entrypoint: Path = ROOT / "bin" / "entrypoint.sh"):
    m = re.search(r"required_vars=\(([^)]*)\)", entrypoint.read_text())
    assert m, "bin/entrypoint.sh no longer declares required_vars"
    return re.findall(r'"([A-Z_]+)"', m.group(1))


def write_install(dest: Path, dashboard_port: int, server_port: int, claude_path: str,
                  reply: str = REPLY) -> dict:
    from harness import write_agents_config
    dest = Path(dest)
    config = dest / "config"
    config.mkdir(parents=True, exist_ok=True)
    values = {
        "DASHBOARD_PORT": str(dashboard_port),
        "AGENT_SERVER_PORT": str(server_port),
        "AGENT_SERVER_TOKEN": secrets.token_hex(24),
        "SESSION_SECRET": secrets.token_hex(32),
        "DASHBOARD_USER": "admin",
        "DASHBOARD_PASSWORD": secrets.token_hex(12),
        "TZ": "UTC",
        "FAKE_CLAUDE_LOG_DIR": FAKE_LOG_DIR,
        "FAKE_CLAUDE_SCRIPT": FAKE_SCRIPT,
    }
    (config / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    write_agents_config(dest, ["main"])      # primary `main` + the harness's monitor
    (config / "channels.json").write_text(json.dumps(
        {"server_id": "0", "channels": {"general": {"id": "0"}, "signals": {"id": "0"}}}, indent=2))
    (config / "fake-claude-script.json").write_text(json.dumps(
        {"default": {"text": reply}, "rules": []}))
    wrapper = config / "fake-claude-wrapper.sh"
    wrapper.write_text('#!/bin/sh\nexec python3 /opt/fake/fake_claude.py "$@"\n')
    wrapper.chmod(0o755)
    (config / "docker-compose.smoke.yml").write_text(
        "services:\n  karakos:\n    volumes:\n"
        "      - ../tests/harness:/opt/fake:ro\n"
        f"      - ./fake-claude-wrapper.sh:{claude_path}:ro\n")
    (dest / ".karakos").mkdir(exist_ok=True)
    (dest / "agents").mkdir(exist_ok=True)
    validate(dest, values)
    return values


def validate(dest: Path, values: dict) -> None:
    """Parity guard: the registry validates and every variable the entrypoint
    requires is present in the .env we wrote."""
    proc = subprocess.run([sys.executable, str(ROOT / "lib" / "registry.py"), "--workspace", str(dest), "validate"],
                          cwd=str(dest), capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WORKSPACE_ROOT": str(dest),
                               "PYTHONPATH": str(ROOT)})
    if proc.returncode != 0:
        raise SystemExit(f"registry validate failed:\n{proc.stdout}{proc.stderr}")
    missing = [v for v in entrypoint_required_vars() if not values.get(v)]
    if missing:
        raise SystemExit(f"config/.env lacks variables bin/entrypoint.sh requires: {missing}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dest")
    ap.add_argument("--dashboard-port", type=int, required=True)
    ap.add_argument("--server-port", type=int, required=True)
    ap.add_argument("--claude-path", default="/usr/local/bin/claude")
    ap.add_argument("--reply", default=REPLY)
    a = ap.parse_args(argv)
    for k, v in write_install(Path(a.dest), a.dashboard_port, a.server_port,
                              a.claude_path, a.reply).items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
