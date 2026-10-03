"""system/check-coupling.sh against a throwaway git repo (no HOME, no network)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "system" / "check-coupling.sh"
DENY = REPO / "system" / "coupling-denylist.txt"


def _git(cwd, *args):
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_repo(tmp_path, files):
    root = tmp_path / "repo"
    (root / "system").mkdir(parents=True)
    shutil.copy(DENY, root / "system" / "coupling-denylist.txt")
    (root / "system" / "coupling-allow.txt").write_text("")
    _git(root, "init", "-q")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    return root


def _run(root):
    env = {"PATH": os.environ["PATH"], "COUPLING_ROOT": str(root),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def _placeholder(kind):
    for line in DENY.read_text().splitlines():
        if line.startswith(kind + "\t"):
            inner = line.split("\t")[1]
            return inner[inner.index("(") + 1:].split("|")[0].rstrip(")\\b)")
    raise AssertionError(kind)


def test_clean_repo(tmp_path):
    root = _make_repo(tmp_path, {
        "a.txt": "host 127.0.0.1 0.0.0.0 192.0.2.5 198.51.100.7 203.0.113.9\n",
        "docs/x.md": "path /home/user/work and channel id 123\n",
    })
    r = _run(root)
    assert r.returncode == 0
    assert r.stdout.strip() == "coupling: clean"


def test_each_pattern_hits(tmp_path):
    ip = ".".join(["192", "168", "1", "20"])
    snow = "1" * 18
    home = "/home/" + "bob" + "/x"
    host = _placeholder("household-hostname")
    name = _placeholder("personal-name")
    root = _make_repo(tmp_path, {
        "ip.txt": f"server {ip}\n",
        "config/ch.json": f'{{"channel": "{snow}"}}\n',
        "home.txt": f"cd {home}\n",
        "host.txt": f"ssh {host}\n",
        "name.txt": f"hello {name}\n",
        "ok.txt": "nothing here\n",
        "docs/noctx.md": f"id {snow}\n",  # no channel/guild/user/bot word: not a hit
    })
    r = _run(root)
    assert r.returncode == 1
    lines = r.stdout.strip().splitlines()
    assert sorted(lines) == sorted([
        "config/ch.json:1: discord-snowflake",
        "home.txt:1: home-path",
        "host.txt:1: household-hostname",
        "ip.txt:1: private-ipv4",
        "name.txt:1: personal-name",
    ])
    assert "coupling: clean" not in r.stdout


def test_allow_entry_suppresses(tmp_path):
    ip = ".".join(["10", "0", "0", "5"])
    root = _make_repo(tmp_path, {"ip.txt": f"server {ip}\n"})
    assert _run(root).returncode == 1
    (root / "system" / "coupling-allow.txt").write_text("ip.txt\tserver\n")
    r = _run(root)
    assert r.returncode == 0 and r.stdout.strip() == "coupling: clean"


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_grep_fallback_matches_rg(tmp_path):
    ip = ".".join(["172", "16", "0", "1"])
    root = _make_repo(tmp_path, {"ip.txt": f"x {ip}\n"})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("git", "grep", "bash", "cut", "sort", "tr", "mktemp", "rm", "cat"):
        p = shutil.which(tool)
        if p:
            (bindir / tool).symlink_to(p)
    env = {"PATH": str(bindir), "COUPLING_ROOT": str(root),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run([shutil.which("bash"), str(SCRIPT)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert r.stdout.strip() == "ip.txt:1: private-ipv4"
