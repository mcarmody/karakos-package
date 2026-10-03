"""bin/fetch-dashboard.sh: pinned fetch, hash verification, token hygiene.

`gh` is stubbed on PATH; nothing here reads HOME, binds a port or touches the
network. The fixture tarball is a real `git archive`, so it carries the commit
id in its pax header exactly as GitHub's tarballs do.
"""
import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
FETCH = ROOT / "bin" / "fetch-dashboard.sh"
TOKEN = "ghp_FAKETOKENVALUE0123456789abcdef"


def _git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "package.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    sha = _git(repo, "rev-parse", "HEAD")
    tarball = tmp_path / "fixture.tar.gz"
    tarball.write_bytes(subprocess.run(
        ["git", "archive", "--format=tar.gz", "--prefix=dash/", "HEAD"],
        cwd=repo, check=True, capture_output=True).stdout)
    pins = tmp_path / "pins"
    pins.mkdir()
    (pins / "dashboard.ref").write_text(sha + "\n")
    (pins / "dashboard.ref.sha256").write_text(hashlib.sha256(tarball.read_bytes()).hexdigest() + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "gh $*" >> "{tmp_path}/gh-trace"\n'
        f'[ -n "${{GH_TOKEN:-}}" ] || exit 4\n'
        f'cat "{tarball}"\n')
    gh.chmod(0o755)
    return {"tmp": tmp_path, "sha": sha, "pins": pins, "bin": bindir, "tarball": tarball}


def _run(fx, *args, trace=False):
    env = {
        "PATH": f"{fx['bin']}:{os.environ['PATH']}",
        "HOME": str(fx["tmp"] / "home"),
        "GH_TOKEN": TOKEN,
        "DASHBOARD_PIN_DIR": str(fx["pins"]),
    }
    cmd = ["bash"] + (["-x"] if trace else []) + [str(FETCH), *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def test_writes_verified_tarball(fx):
    out = fx["tmp"] / "out" / "d.tar.gz"
    r = _run(fx, "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == fx["tarball"].read_bytes()
    trace = (fx["tmp"] / "gh-trace").read_text()
    assert f"repos/mcarmody/karakos-dashboard/tarball/{fx['sha']}" in trace


def test_wrong_hash_fails_and_leaves_no_file(fx):
    (fx["pins"] / "dashboard.ref.sha256").write_text("0" * 64 + "\n")
    out = fx["tmp"] / "out" / "d.tar.gz"
    r = _run(fx, "--out", str(out))
    assert r.returncode != 0
    assert "sha256 mismatch" in r.stderr
    assert not out.exists()
    assert not list(out.parent.glob("*")), "temp file left behind"


def test_embedded_commit_must_match_requested_ref(fx):
    other = "a" * 40
    r = _run(fx, "--ref", other, "--out", str(fx["tmp"] / "o.tgz"))
    assert r.returncode != 0
    assert "does not match requested" in r.stderr


def test_bad_ref_rejected(fx):
    r = _run(fx, "--ref", "main", "--out", str(fx["tmp"] / "o.tgz"))
    assert r.returncode == 2


def test_requires_token_from_environment(fx):
    env = {"PATH": f"{fx['bin']}:{os.environ['PATH']}", "HOME": str(fx["tmp"]),
           "DASHBOARD_PIN_DIR": str(fx["pins"])}
    r = subprocess.run(["bash", str(FETCH), "--out", str(fx["tmp"] / "o.tgz")],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert not (fx["tmp"] / "o.tgz").exists()


def test_token_never_appears_in_output_or_trace(fx):
    for case in ("ok", "bad"):
        if case == "bad":
            (fx["pins"] / "dashboard.ref.sha256").write_text("1" * 64 + "\n")
        r = _run(fx, "--out", str(fx["tmp"] / f"{case}.tgz"), trace=True)
        assert TOKEN not in r.stdout and TOKEN not in r.stderr
    assert TOKEN not in (fx["tmp"] / "gh-trace").read_text()


def test_script_has_no_token_or_household_literals():
    text = FETCH.read_text()
    assert not re.search(r"ghp_|github_pat_", text)
