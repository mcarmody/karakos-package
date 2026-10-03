"""Tests for the Bash PreToolUse rails and the shared command segmenter."""
import json
import os
import subprocess
import sys

import pytest

from conftest import PACKAGE_ROOT, import_script

HOOKS = PACKAGE_ROOT / "system" / "hooks"
sys.path.insert(0, str(HOOKS / "lib"))
import bashcmd  # noqa: E402

rails = import_script("bash-safety-rails", HOOKS / "bash-safety-rails.py")
ssh = import_script("block-bare-ssh", HOOKS / "block-bare-ssh.py")
heavy = import_script("block-heavy-build", HOOKS / "block-heavy-build.py")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "home" / "ws"
    (root / "build").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


@pytest.fixture
def env(ws, tmp_path):
    return {"WORKSPACE_ROOT": str(ws), "HOME": str(tmp_path / "home")}


def ev(cmd, env, cwd):
    return rails.evaluate(cmd, env, str(cwd))


# ---- segmenter ---------------------------------------------------------

def test_segmenter_strips_wrappers_and_recurses():
    flat = [[str(w) for w in a] for a in bashcmd.commands(
        "sudo -u bob env A=1 timeout 5 nice -n 3 pkill -f foo")]
    assert flat == [["pkill", "-f", "foo"]]
    names = [str(a[0]) for a in bashcmd.commands("bash -c 'ls; rm -rf x' && echo $(pwd)")]
    assert "rm" in names and "pwd" in names


def test_segmenter_ignores_heredoc_and_quotes():
    cmd = "cat <<'EOF'\npkill -f x\nEOF\necho 'pkill -f y' \"pkill -f z\""
    names = [str(a[0]) for a in bashcmd.commands(cmd)]
    assert names == ["cat", "echo"]


# ---- rails: deny -------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "pkill -f node", "pkill -9f node", "pkill --full node",
    "sudo pkill -f node", "bash -c 'pkill -f node'", "echo $(pkill -f node)",
    "npx prisma db push --accept-data-loss", "prisma migrate reset --force-reset",
    "git push --force origin main", "git push -f origin master",
    "git push origin +main", "git push --force-with-lease origin HEAD:main",
    "git -C /tmp push --force origin refs/heads/main",
])
def test_denies(cmd, env, ws):
    assert ev(cmd, env, ws) is not None


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "rm -rf ~", "rm -rf ~/", "rm -rf ./", "rm -rf .", "rm -rf ../ws",
    "rm -rf $WORKSPACE_ROOT/", "rm -rf ${WORKSPACE_ROOT}", "rm -rf .git", "rm -rf $WORKSPACE_ROOT/.git",
    'rm -rf "$X"', "rm -rf ./*", "rm -rf $(pwd)", "sudo rm -rf /", "bash -c 'rm -rf /'",
    "rm -r -f -- /",
])
def test_rm_denied(cmd, env, ws):
    assert ev(cmd, env, ws) is not None, cmd


def test_rm_through_symlink_to_root_denied(env, ws, tmp_path):
    link = tmp_path / "lnk"
    link.symlink_to(ws)
    assert ev(f"rm -rf {link}", env, ws) is not None
    assert ev(f"rm -rf {link}/.git", env, ws) is not None


def test_rm_ancestor_of_workspace_denied(env, ws):
    assert ev(f"rm -rf {ws.parent}", env, ws) is not None


def test_rm_relative_uses_hook_cwd(env, ws):
    other = ws.parent / "other"
    other.mkdir()
    assert ev("rm -rf ../ws", env, other) is not None
    assert ev("rm -rf ../ws", env, ws / "build") is None  # resolves to ws/ws, not the root
    assert ev("rm -rf ..", env, ws / "build") is not None


def test_rm_allowed(env, ws):
    assert ev("rm -rf ./build", env, ws) is None
    assert ev("rm -rf build/", env, ws) is None
    assert ev("rm file.txt", env, ws) is None
    assert ev("rm -f ./a", env, ws) is None


def test_unresolvable_message_asks_for_explicit_path(env, ws):
    label, reason = ev('rm -rf "$X"', env, ws)
    assert "explicit path" in reason
    assert str(ws) not in reason


# ---- rails: negative controls -----------------------------------------

@pytest.mark.parametrize("cmd", [
    'echo "never run pkill -f"', "echo 'rm -rf /'", "rg 'pkill -f' src",
    "grep -r '--accept-data-loss' .", "cat <<'EOF'\nrm -rf /\npkill -f x\nEOF",
    "git push origin feature", "git push --force origin feature",
    "git push origin main", "pkill node", "printf '%s' 'git push --force origin main'",
    "pgrep -f node",
])
def test_negative_controls(cmd, env, ws):
    assert ev(cmd, env, ws) is None, cmd


# ---- hook process: output + log ---------------------------------------

def run_hook(script, command, env, cwd, extra=None):
    e = {"PATH": os.environ.get("PATH", ""), **env, **(extra or {})}
    p = subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}),
        capture_output=True, text=True, env=e)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def test_deny_output_and_jsonl_log(env, ws):
    out = run_hook("bash-safety-rails.py", "pkill -f node", env, ws)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    lines = (ws / "logs" / "blocked-bash.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    assert set(rec) == {"ts", "label", "command"} and rec["command"] == "pkill -f node"


def test_allowed_emits_nothing(env, ws):
    assert run_hook("bash-safety-rails.py", "ls -la", env, ws) is None
    assert not (ws / "logs").exists()


def test_non_bash_tool_ignored(env, ws):
    p = subprocess.run([sys.executable, str(HOOKS / "bash-safety-rails.py")], input=json.dumps(
        {"tool_name": "Read", "tool_input": {"command": "pkill -f x"}}),
        capture_output=True, text=True, env={**env})
    assert p.stdout == ""


# ---- bare ssh ----------------------------------------------------------

CFG = {"bare_ssh_hosts": ["box1", "build-host-2"], "ssh_wrapper": "bin/ssh-safe"}


@pytest.mark.parametrize("cmd", [
    "ssh box1 uptime", "ssh -p 22 -i key me@box1 ls", "scp f me@box1:/tmp/",
    "rsync -e ssh -a d/ box1:/d/", "rsync -a d/ me@box1:/d/", "bash -c 'ssh box1 id'",
    "sudo ssh BOX1 id", "ssh -o StrictHostKeyChecking=no box1 id",
])
def test_ssh_denied(cmd):
    hit = ssh.evaluate(cmd, CFG)
    assert hit and "bin/ssh-safe" in hit[1]


@pytest.mark.parametrize("cmd", [
    "ssh otherhost uptime", 'echo "ssh box1"', "rg 'ssh box1' docs", "bin/ssh-safe box1 uptime",
    "rsync -e bin/ssh-safe -a d/ box1:/d/", "cat <<EOF\nssh box1\nEOF",
])
def test_ssh_allowed(cmd):
    assert ssh.evaluate(cmd, CFG) is None


def test_ssh_default_config_is_noop():
    assert ssh.evaluate("ssh box1 uptime", {}) is None
    assert ssh.evaluate("ssh box1 uptime", {"bare_ssh_hosts": []}) is None


def test_ssh_hook_reads_config_file(env, ws):
    (ws / "config").mkdir()
    (ws / "config" / "hooks.json").write_text(json.dumps(CFG))
    out = run_hook("block-bare-ssh.py", "ssh box1 id", env, ws)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_hook("block-bare-ssh.py", "ssh other id", env, ws) is None


# ---- heavy build -------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "tsc --noEmit", "npx tsc", "pnpm exec tsc -p .", "next build", "npx next build",
    "webpack --mode production", "vite build", "npm ci", "npm install", "npm i",
    "pnpm install", "yarn", "yarn install", "bun install", "npm install --save-dev",
    "npm run build", "npm run typecheck", "pnpm run build", "yarn build",
    "sudo npm ci", "bash -c 'npm run build'", "cd app && npm run build", "echo $(tsc)",
])
def test_heavy_denied(cmd):
    assert heavy.evaluate(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "npm install left-pad", "npm i -D typescript", "yarn add left-pad", "pnpm add zod",
    "npm run test", "npm run lint", "rg 'next build' f", 'echo "run npm ci later"',
    "grep tsc package.json", "cat <<EOF\nnpm run build\nEOF", "next dev", "npm test",
])
def test_heavy_allowed(cmd):
    assert heavy.evaluate(cmd) is None, cmd
