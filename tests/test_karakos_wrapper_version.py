"""bin/karakos picks the image tag: environment, else config/.env, else v2.0
(a tag release.yml publishes). The docker shim only records what compose would see."""
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(tmp_path, env_file=None, env_version=None):
    inst = tmp_path / "inst"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "docker-compose.yml").write_text("services: {}\n")
    if env_file is not None:
        (inst / "config" / ".env").write_text(env_file)
    log = tmp_path / "docker.log"
    d = tmp_path / "shim"
    d.mkdir()
    f = d / "docker"
    f.write_text(f'#!/usr/bin/env bash\necho "V=$KARAKOS_VERSION" >> {log}\nexit 0\n')
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": f"{d}:{os.environ['PATH']}",
           "KARAKOS_COMPOSE_FILE": str(inst / "config" / "docker-compose.yml"),
           "KARAKOS_BACKUP_DIR": str(tmp_path / "bk")}
    if env_version is not None:
        env["KARAKOS_VERSION"] = env_version
    r = subprocess.run(["bash", str(ROOT / "bin" / "karakos"), "migrate", "--dry-run"],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    return set(log.read_text().split()) - {""}


def test_default_is_a_published_tag(tmp_path):
    assert run(tmp_path) == {"V=v2.0"}


def test_default_matches_release_workflow_tag_shapes(tmp_path):
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "pattern=v{{major}}.{{minor}}" in wf and "pattern=v{{major}}," in wf
    (tag,) = run(tmp_path / "x")
    assert re.fullmatch(r"V=v\d+(\.\d+)?|V=latest", tag)   # never a bare 2.0.0


def test_reads_config_env_when_unset(tmp_path):
    assert run(tmp_path, env_file="FOO=1\nKARAKOS_VERSION=v2.1  # pin\nBAR=2\n") == {"V=v2.1"}


def test_quoted_value_and_last_line_wins(tmp_path):
    assert run(tmp_path, env_file='KARAKOS_VERSION=v1\nexport KARAKOS_VERSION="v2"\n') == {"V=v2"}


def test_environment_beats_config_env(tmp_path):
    assert run(tmp_path, env_file="KARAKOS_VERSION=v2.1\n", env_version="latest") == {"V=latest"}


def test_env_file_without_the_key_falls_back(tmp_path):
    assert run(tmp_path, env_file="OTHER=1\n# KARAKOS_VERSION=v9\n") == {"V=v2.0"}
