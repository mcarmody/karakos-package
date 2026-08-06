"""
Tests for issue #94 — hooks wiring via --settings on the claude spawn line.

The full agent-server is heavy (event loop + sqlite + subprocesses), so the
spawn-line assertions follow the source-parsing style already used in
test_agent_server_routes.py rather than booting the server. The hook
script itself is exercised for real: it's the acceptance test's "append a
line to a file" behavior, just triggered directly instead of via a live
Discord round trip through a real claude subprocess (neither is available
on this box).
"""

import json
import os
import stat
import subprocess

from conftest import PACKAGE_ROOT

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
SETTINGS_PATH = PACKAGE_ROOT / "config" / "claude-settings.json"
HOOK_SCRIPT = PACKAGE_ROOT / "system" / "hooks" / "log-user-prompt.sh"


def test_settings_file_exists_and_is_package_owned():
    """The settings file must ship in config/, not require the installer or
    the user to scaffold a .claude/ dir."""
    assert SETTINGS_PATH.exists()
    assert not (PACKAGE_ROOT / ".claude").exists()


def test_settings_file_wires_user_prompt_submit_hook():
    config = json.loads(SETTINGS_PATH.read_text())
    hooks = config.get("hooks", {})
    assert "UserPromptSubmit" in hooks
    entries = hooks["UserPromptSubmit"]
    assert entries, "UserPromptSubmit hook list is empty"
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any("log-user-prompt.sh" in cmd for cmd in commands)


def test_agent_server_passes_settings_flag_on_spawn():
    """The claude spawn line in start_agent_subprocess must pass --settings
    pointing at the package-owned config, per issue #94's fix shape."""
    src = AGENT_SERVER.read_text()
    assert 'CLAUDE_SETTINGS_PATH = WORKSPACE_ROOT / "config" / "claude-settings.json"' in src

    start_idx = src.index("async def start_agent_subprocess")
    next_def = src.index("\nasync def ", start_idx + 1)
    body = src[start_idx:next_def]
    assert '"--settings"' in body
    assert "CLAUDE_SETTINGS_PATH" in body


def test_hook_script_is_executable():
    assert HOOK_SCRIPT.exists()
    mode = HOOK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "hook script must be executable for claude to invoke it"


def test_hook_script_appends_one_line_per_invocation(tmp_workspace):
    """This is the issue's stated acceptance test: firing the
    UserPromptSubmit hook once must add exactly one line to a file."""
    log_path = tmp_workspace / "logs" / "hook-events.log"
    assert not log_path.exists()

    env = dict(os.environ, WORKSPACE_ROOT=str(tmp_workspace))
    subprocess.run(["bash", str(HOOK_SCRIPT)], env=env, check=True)

    lines_after_one = log_path.read_text().splitlines()
    assert len(lines_after_one) == 1

    subprocess.run(["bash", str(HOOK_SCRIPT)], env=env, check=True)
    lines_after_two = log_path.read_text().splitlines()
    assert len(lines_after_two) == 2
