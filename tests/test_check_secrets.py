"""Tests for system/check-secrets.py. Uses a temp git repo only; never touches
the working repo or HOME. Sample tokens are assembled at runtime so this file
itself holds no token-shaped literal."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT, import_script

SCRIPT = PACKAGE_ROOT / "system" / "check-secrets.py"
mod = import_script("check-secrets")

A36 = "a" * 36
SAMPLES = {
    "github": "gh" + "p_" + A36,
    "anthropic": "sk-" + "ant-" + "A1" * 12,
    "openai": "sk-" + "proj-" + "B2" * 20,
    "slack": "xox" + "b-" + "1234567890-abc",
    "discord": "M" + "T" * 23 + "." + "abcdef" + "." + "x" * 27,
    "bearer": "Authorization: " + "Bearer " + "abcd1234efgh5678ijkl",
    "pem": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=r, check=True)
    return r


def run(repo, **extra):
    env = {"PATH": os.environ["PATH"], "HOME": str(repo.parent), "WORKSPACE_ROOT": str(repo),
           "KARAKOS_SECRET_SCAN": "regex", **extra}
    for k in [k for k, v in env.items() if v is None]:
        env.pop(k)
    # the working repo must be the temp one, never the real checkout
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    assert Path(top).resolve().is_relative_to(repo.parent.resolve())
    return subprocess.run([sys.executable, str(SCRIPT), "--staged"], cwd=repo, env=env,
                          capture_output=True, text=True)


def stage(repo, name, content):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_each_pattern_blocked(repo, kind):
    stage(repo, "notes.txt", f"value = {SAMPLES[kind]}\n")
    r = run(repo)
    assert r.returncode == 1, r.stderr
    assert "notes.txt" in r.stderr


@pytest.mark.parametrize("name", [".env", ".env.local", "k/server.pem", "a.key", "id_rsa",
                                  "x/secrets/cfg.json"])
def test_forbidden_paths(repo, name):
    stage(repo, name, "harmless\n")
    r = run(repo)
    assert r.returncode == 1 and name in r.stderr


def test_env_template_allowed(repo):
    stage(repo, ".env.template", "TOKEN=\n")
    assert run(repo).returncode == 0


def test_clean_commit_passes(repo):
    stage(repo, "a.py", "print('hi')\nslug = 'sk-short-slug-abc'\n")
    assert run(repo).returncode == 0


def test_skip_lines_and_allowlist_and_samples_dir(repo):
    stage(repo, "a.txt", f"example: {SAMPLES['github']}\nplaceholder {SAMPLES['slack']}\n")
    assert run(repo).returncode == 0
    stage(repo, "tests/fixtures/secrets-samples/s.txt", SAMPLES["github"] + "\n")
    assert run(repo).returncode == 0
    line = f"tok={SAMPLES['openai']}"
    stage(repo, "b.txt", line + "\n")
    assert run(repo).returncode == 1
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "secrets-allow.txt").write_text(line + "\n")
    assert run(repo).returncode == 0


def test_override_skips_content_not_paths(repo):
    stage(repo, "n.txt", SAMPLES["github"] + "\n")
    assert run(repo, KARAKOS_SECRET_SCAN="off").returncode == 0
    stage(repo, ".env", "x\n")
    assert run(repo, KARAKOS_SECRET_SCAN="off").returncode == 1


def test_only_staged_content_scanned(repo):
    stage(repo, "n.txt", "clean\n")
    (repo / "n.txt").write_text(SAMPLES["github"] + "\n")        # unstaged edit
    (repo / "untracked.txt").write_text(SAMPLES["github"] + "\n")  # untracked
    assert run(repo).returncode == 0
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    (repo / "n.txt").write_text("clean2\n")
    subprocess.run(["git", "add", "n.txt"], cwd=repo, check=True)
    assert run(repo).returncode == 0


def test_preexisting_secret_in_unchanged_line_not_flagged(repo):
    stage(repo, "n.txt", "x\n")
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    stage(repo, "n.txt", "x\ny\n")
    assert run(repo).returncode == 0


def test_install_hooks_script_runs_secrets_check():
    text = (PACKAGE_ROOT / "system" / "install-hooks.sh").read_text()
    assert text.index("check-protected-paths.py") < text.index("check-secrets.py")


def test_scan_line_unit():
    assert mod.scan_line("x " + SAMPLES["github"], set()) == "github-token"
    assert mod.scan_line("x " + SAMPLES["github"], {"x " + SAMPLES["github"]}) is None
