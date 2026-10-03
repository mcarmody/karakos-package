"""lib/build_hosts.py: config, admission and the probe (spec 3.3)."""
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildq_helpers import HARNESS_BIN  # noqa: E402

import build_hosts as bh  # noqa: E402

H = bh.HostCfg(name="h", min_free_ram_mb=2048, max_load1=4)
SSH = bh.HostCfg(name="far", kind="ssh", target="box", probe=["probe"])


@pytest.fixture(autouse=True)
def _reset_warnings():
    bh._warned.clear()


# --- admit -----------------------------------------------------------------------

def test_admit_ram_floor_boundaries():
    assert bh.admit(H, {"free_ram_mb": 2048}).ok
    assert bh.admit(H, {"free_ram_mb": 5000}).ok
    r = bh.admit(H, {"free_ram_mb": 2047})
    assert not r.ok and "free_ram_mb" in r.reason


def test_admit_load_ceiling_boundaries():
    assert bh.admit(H, {"free_ram_mb": 9999, "load1": 4.0}).ok
    assert not bh.admit(H, {"free_ram_mb": 9999, "load1": 4.01}).ok


def test_admit_unconstrained_load_ignored():
    h = bh.HostCfg(name="h", max_load1=None)
    assert bh.admit(h, {"free_ram_mb": 9999, "load1": 500}).ok


def test_missing_or_partial_probe_admits():
    assert bh.admit(H, None).ok
    assert bh.admit(H, {}).ok
    assert bh.admit(H, {"load1": 1}).ok                    # no RAM field, floor unconstrained by data
    assert bh.admit(H, {"free_ram_mb": "junk"}).ok


# --- run_probe -----------------------------------------------------------------------

def runner(rc=0, out="", exc=None, seen=None):
    def run(argv, timeout):
        if seen is not None:
            seen.append((argv, timeout))
        if exc:
            raise exc
        return rc, out
    return run


def test_run_probe_good(monkeypatch):
    seen = []
    data = bh.run_probe(SSH, runner(0, json.dumps({"free_ram_mb": 100, "load1": 1}) + "\n", seen=seen))
    assert data == {"free_ram_mb": 100, "load1": 1}
    argv, timeout = seen[0]
    assert timeout == 10 and "box" in argv


@pytest.mark.parametrize("r", [runner(1, "{}"), runner(0, "garbage"), runner(0, "[1]"),
                               runner(0, ""), runner(exc=subprocess.TimeoutExpired("x", 10)),
                               runner(exc=OSError("nope"))])
def test_run_probe_failures_admit(r, caplog):
    with caplog.at_level(logging.WARNING, logger="build_hosts"):
        assert bh.run_probe(SSH, r) is None
    assert bh.admit(SSH, None).ok


def test_probe_warns_once_per_hour(caplog):
    with caplog.at_level(logging.WARNING, logger="build_hosts"):
        for t in (0, 10, 3599):
            bh.run_probe(SSH, runner(1, ""), now=t)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        bh.run_probe(SSH, runner(1, ""), now=3601)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_ssh_255_and_timeout_are_unreachable_not_busy():
    assert bh.probe_status(SSH, runner(255, "")) == (None, True)
    assert bh.probe_status(SSH, runner(exc=subprocess.TimeoutExpired("x", 10))) == (None, True)
    assert bh.probe_status(SSH, runner(1, "")) == (None, False)      # probe error: fail open
    data, un = bh.probe_status(SSH, runner(0, '{"free_ram_mb": 1}'))
    assert (data, un) == ({"free_ram_mb": 1}, False)
    assert bh.admit(SSH, data).ok is False                          # busy, a different thing


def test_ssh_host_without_probe_admits():
    h = bh.HostCfg(name="far", kind="ssh", target="box")
    assert bh.probe_status(h, runner(255)) == (None, False)


def test_local_probe_reads_proc_and_loadavg():
    data = bh.run_probe(bh.HostCfg(name="local"))
    assert data is None or set(data) <= {"free_ram_mb", "load1"}


def test_probe_cache_30s():
    calls = []
    clock = [1000.0]
    cache = bh.ProbeCache(runner(0, '{"free_ram_mb": 9000}', seen=calls), lambda: clock[0])
    for dt in (0, 10, 29):
        clock[0] = 1000 + dt
        assert cache.get(SSH)[0] == {"free_ram_mb": 9000}
    assert len(calls) == 1
    clock[0] = 1031
    cache.get(SSH)
    assert len(calls) == 2


def test_probe_command_runs_through_the_ssh_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("KARAKOS_SSH_BIN", str(HARNESS_BIN / "fake-ssh"))
    monkeypatch.setenv("KARAKOS_FAKE_REMOTE_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{HARNESS_BIN}:/usr/bin:/bin")
    monkeypatch.setenv("KARAKOS_FAKE_PROBE_RAM", "1234")
    assert bh.run_probe(SSH)["free_ram_mb"] == 1234
    monkeypatch.setenv("KARAKOS_FAKE_SSH_FAIL", "255")
    assert bh.probe_status(SSH)[1] is True


# --- config ----------------------------------------------------------------------------

def test_defaults_when_file_absent(tmp_path):
    c = bh.load_config(tmp_path / "nope.yaml")
    assert c.enabled is False and c.default_host == "local" and "local" in c.hosts
    assert (c.cost_ceiling_usd, c.max_attempts, c.governor, c.unreachable_grace_s) == (75, 1, True, 900)
    assert c.timeout_s("build") == 21600 and c.timeout_s("review") == 3600
    assert c.hosts["local"].min_free_ram_mb == 2048 and c.hosts["local"].workdir == "~/karakos-builds"


def write(tmp_path, text):
    p = tmp_path / "build-queue.yaml"
    p.write_text(text)
    return p


def test_full_config_parses(tmp_path):
    c = bh.load_config(write(tmp_path, """
enabled: true
default_host: far
hosts:
  far: {kind: ssh, target: box, concurrency: 2, workdir: /srv/b, probe: [probe, --json],
        min_free_ram_mb: 100, max_load1: 3.5}
roles: {build: {timeout_s: 100}}
cost_ceiling_usd: 5
retry: {max_attempts: 2}
governor: false
unreachable_grace_s: 5
"""))
    f = c.hosts["far"]
    assert c.enabled and c.invalid is None and c.default_host == "far"
    assert (f.kind, f.target, f.concurrency, f.workdir, f.probe, f.max_load1) == \
        ("ssh", "box", 2, "/srv/b", ["probe", "--json"], 3.5)
    assert (c.timeout_s("build"), c.timeout_s("review"), c.cost_ceiling_usd, c.max_attempts,
            c.governor, c.unreachable_grace_s) == (100, 3600, 5, 2, False, 5)
    assert "local" in c.hosts                                  # implicit


def test_unknown_keys_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="build_hosts"):
        c = bh.load_config(write(tmp_path, "enabled: false\nbogus: 1\nhosts:\n  local: {kind: local, nope: 2}\n"))
    assert c.invalid is None and len(c.warnings) == 2
    assert any("bogus" in r.message for r in caplog.records)


@pytest.mark.parametrize("text", [
    "enabled: maybe\n", "- a\n", "hosts: {x: {kind: ssh}}\n", "hosts: {x: {kind: ftp}}\n",
    "default_host: ghost\n", "hosts: {x: {kind: ssh, target: b, probe: notalist}}\n",
    "hosts: {x: {concurrency: 0}}\n", "enabled: [\n", "cost_ceiling_usd: lots\n"])
def test_invalid_file_disables_and_reports(tmp_path, text):
    c = bh.load_config(write(tmp_path, text))
    assert c.enabled is False and c.invalid


def test_invalid_file_remembers_it_asked_to_be_enabled(tmp_path):
    c = bh.load_config(write(tmp_path, "enabled: true\ndefault_host: ghost\n"))
    assert c.invalid and c.requested and not c.enabled
    c = bh.load_config(write(tmp_path, "enabled: false\ndefault_host: ghost\n"))
    assert c.invalid and not c.requested


def test_host_names_and_targets_come_only_from_the_file(tmp_path):
    c = bh.load_config(write(tmp_path, "hosts: {far: {kind: ssh, target: box}}\n"))
    assert set(c.hosts) == {"far", "local"}
    assert c.host("evil") is None
    with pytest.raises(bh.ConfigError):
        bh.parse_config({"hosts": {"x": {"kind": "ssh", "target": "-oProxyCommand=bad"}}})
