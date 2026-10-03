"""Build queue config, hosts and the admission gate (spec 3.3).

`config/build-queue.yaml` is the only source of host names, ssh targets and
probe commands: nothing from a brief or an agent message reaches them. Admission
is pure (`admit`); `run_probe` fails open (a broken probe never wedges the queue)
and an unreachable host is reported separately from a busy one.
"""
import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("build_hosts")

DEFAULT_WORKDIR = "~/karakos-builds"
PROBE_TIMEOUT_S = 10
PROBE_CACHE_S = 30
WARN_EVERY_S = 3600
ROOT_KEYS = {"enabled", "default_host", "hosts", "roles", "cost_ceiling_usd", "retry",
             "governor", "unreachable_grace_s"}
HOST_KEYS = {"kind", "concurrency", "target", "workdir", "probe", "min_free_ram_mb",
             "max_load1", "repo_url"}


class ConfigError(Exception):
    pass


@dataclass
class HostCfg:
    name: str
    kind: str = "local"
    concurrency: int = 1
    target: Optional[str] = None
    workdir: str = DEFAULT_WORKDIR
    probe: Optional[list] = None
    min_free_ram_mb: float = 2048
    max_load1: Optional[float] = None
    repo_url: str = "https://github.com/{repo}.git"


@dataclass
class BuildConfig:
    enabled: bool = False
    default_host: str = "local"
    hosts: dict = field(default_factory=dict)
    roles: dict = field(default_factory=lambda: {"build": {"timeout_s": 21600},
                                                  "review": {"timeout_s": 3600}})
    cost_ceiling_usd: float = 75
    max_attempts: int = 1
    governor: bool = True
    unreachable_grace_s: int = 900
    invalid: Optional[str] = None     # why the file is unusable (queue disabled)
    requested: bool = False           # the file said `enabled: true` (even if invalid)
    warnings: list = field(default_factory=list)

    def host(self, name) -> Optional[HostCfg]:
        return self.hosts.get(name)

    def timeout_s(self, kind) -> int:
        return int((self.roles.get(kind) or {}).get("timeout_s") or
                   (21600 if kind == "build" else 3600))


def _num(v, name, lo=None, optional=False):
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if lo is not None and v < lo:
        raise ConfigError(f"{name} must be >= {lo}")
    return v


def parse_config(data) -> BuildConfig:
    cfg = BuildConfig()
    if data is None:
        return cfg
    if not isinstance(data, dict):
        raise ConfigError("build-queue.yaml must be a mapping")
    for k in data:
        if k not in ROOT_KEYS:
            cfg.warnings.append(f"unknown key {k!r}")
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ConfigError("enabled must be true or false")
        cfg.enabled = data["enabled"]
    if data.get("default_host") is not None:
        cfg.default_host = str(data["default_host"])
    hosts = data.get("hosts") or {}
    if not isinstance(hosts, dict):
        raise ConfigError("hosts must be a mapping")
    for name, h in hosts.items():
        h = h or {}
        if not isinstance(h, dict):
            raise ConfigError(f"host {name!r} must be a mapping")
        for k in h:
            if k not in HOST_KEYS:
                cfg.warnings.append(f"unknown key {k!r} in host {name!r}")
        kind = h.get("kind", "local")
        if kind not in ("local", "ssh"):
            raise ConfigError(f"host {name!r}: kind must be local or ssh")
        hc = HostCfg(name=str(name), kind=kind)
        hc.concurrency = int(_num(h.get("concurrency", 1), f"{name}.concurrency", 1))
        if kind == "ssh":
            if not h.get("target") or not isinstance(h["target"], str) or h["target"].startswith("-"):
                raise ConfigError(f"host {name!r}: ssh needs a target")
            hc.target = h["target"]
        hc.workdir = str(h.get("workdir") or DEFAULT_WORKDIR)
        probe = h.get("probe")
        if probe is not None:
            if not isinstance(probe, list) or not probe or not all(isinstance(p, str) for p in probe):
                raise ConfigError(f"host {name!r}: probe must be a command list")
            hc.probe = probe
        hc.min_free_ram_mb = _num(h.get("min_free_ram_mb", 2048), f"{name}.min_free_ram_mb", 0)
        hc.max_load1 = _num(h.get("max_load1"), f"{name}.max_load1", 0, optional=True)
        if h.get("repo_url"):
            hc.repo_url = str(h["repo_url"])
        cfg.hosts[hc.name] = hc
    if "local" not in cfg.hosts:
        cfg.hosts["local"] = HostCfg(name="local")
    if cfg.default_host not in cfg.hosts:
        raise ConfigError(f"default_host {cfg.default_host!r} is not a configured host")
    roles = data.get("roles")
    if roles is not None:
        if not isinstance(roles, dict):
            raise ConfigError("roles must be a mapping")
        for k, v in roles.items():
            if k not in ("build", "review"):
                cfg.warnings.append(f"unknown role {k!r}")
                continue
            if isinstance(v, dict) and "timeout_s" in v:
                cfg.roles[k] = {"timeout_s": int(_num(v["timeout_s"], f"roles.{k}.timeout_s", 1))}
    if "cost_ceiling_usd" in data:
        cfg.cost_ceiling_usd = _num(data["cost_ceiling_usd"], "cost_ceiling_usd", 0)
    if "unreachable_grace_s" in data:
        cfg.unreachable_grace_s = int(_num(data["unreachable_grace_s"], "unreachable_grace_s", 0))
    retry = data.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            raise ConfigError("retry must be a mapping")
        cfg.max_attempts = int(_num(retry.get("max_attempts", 1), "retry.max_attempts", 1))
    if "governor" in data:
        if not isinstance(data["governor"], bool):
            raise ConfigError("governor must be true or false")
        cfg.governor = data["governor"]
    return cfg


def load_config(path) -> BuildConfig:
    """Never raises. A missing file is the shipped default (disabled); an invalid
    one is disabled with `invalid` set to the reason."""
    path = Path(path)
    if not path.exists():
        return BuildConfig(hosts={"local": HostCfg(name="local")})
    try:
        import yaml
        cfg = parse_config(yaml.safe_load(path.read_text()))
    except Exception as e:  # noqa: BLE001
        cfg = BuildConfig(hosts={"local": HostCfg(name="local")})
        cfg.enabled = False
        cfg.invalid = f"{path.name}: {e}"
        try:
            raw = yaml.safe_load(path.read_text())
            cfg.requested = isinstance(raw, dict) and raw.get("enabled") is True
        except Exception:  # noqa: BLE001
            cfg.requested = False
        log.error("invalid build queue config: %s", cfg.invalid)
        return cfg
    cfg.requested = cfg.enabled
    for w in cfg.warnings:
        log.warning("build-queue.yaml: %s", w)
    return cfg


# --- admission -------------------------------------------------------------

@dataclass(frozen=True)
class Admit:
    ok: bool
    reason: str = ""


def admit(host_cfg, probe) -> Admit:
    """Pure. Not ok when free RAM is under the floor or load over the ceiling; a
    missing probe, or a field the config does not constrain, admits."""
    if not probe:
        return Admit(True, "no probe")
    ram = probe.get("free_ram_mb")
    floor = host_cfg.min_free_ram_mb
    if floor and isinstance(ram, (int, float)) and not isinstance(ram, bool) and ram < floor:
        return Admit(False, f"free_ram_mb {ram:g} < {floor:g}")
    load = probe.get("load1")
    ceil = host_cfg.max_load1
    if ceil is not None and isinstance(load, (int, float)) and not isinstance(load, bool) \
            and load > ceil:
        return Admit(False, f"load1 {load:g} > {ceil:g}")
    return Admit(True, "ok")


_warned: dict = {}


def _warn_once(host, msg, now=None):
    now = time.time() if now is None else now
    if now - _warned.get(host, -WARN_EVERY_S) >= WARN_EVERY_S:
        _warned[host] = now
        log.warning("probe %s: %s", host, msg)


def _default_exec(argv, timeout):
    """-> (returncode, stdout). Raises subprocess.TimeoutExpired. The child is its own
    session so nothing a probe does can reach the caller's group."""
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, start_new_session=True)
    return p.returncode, p.stdout


def ssh_bin() -> str:
    return os.environ.get("KARAKOS_SSH_BIN") or "ssh"


def ssh_argv(target, command: str) -> list:
    return [ssh_bin(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command]


def _local_probe() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["free_ram_mb"] = int(line.split()[1]) / 1024.0
                break
    except (OSError, ValueError):
        pass
    try:
        out["load1"] = os.getloadavg()[0]
    except OSError:
        pass
    return out or None


def probe_status(host_cfg, runner: Optional[Callable] = None, now=None):
    """-> (data or None, unreachable: bool). Any failure is (None, ...) so the caller
    admits; ssh exit 255 or a timeout is reported unreachable."""
    runner = runner or _default_exec
    try:
        if host_cfg.kind == "local":
            return _local_probe(), False
        if not host_cfg.probe:
            return None, False
        argv = ssh_argv(host_cfg.target, shlex.join(host_cfg.probe))
        try:
            rc, out = runner(argv, PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _warn_once(host_cfg.name, "timed out", now)
            return None, True
        if rc == 255:
            _warn_once(host_cfg.name, "host unreachable (ssh exit 255)", now)
            return None, True
        if rc != 0:
            _warn_once(host_cfg.name, f"probe exited {rc}; admitting", now)
            return None, False
        data = json.loads(out.strip().splitlines()[-1])
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data, False
    except Exception as e:  # noqa: BLE001
        _warn_once(host_cfg.name, f"probe failed ({e}); admitting", now)
        return None, False


def run_probe(host_cfg, runner: Optional[Callable] = None, now=None) -> Optional[dict]:
    """The probe reading, or None on any failure (which admits)."""
    return probe_status(host_cfg, runner, now)[0]


class ProbeCache:
    """Probe results cached 30 s per host."""

    def __init__(self, runner=None, clock=time.time, ttl=PROBE_CACHE_S):
        self.runner, self.clock, self.ttl = runner, clock, ttl
        self._c: dict = {}

    def get(self, host_cfg):
        now = self.clock()
        hit = self._c.get(host_cfg.name)
        if hit and now - hit[0] < self.ttl:
            return hit[1], hit[2]
        data, unreachable = probe_status(host_cfg, self.runner, now)
        self._c[host_cfg.name] = (now, data, unreachable)
        return data, unreachable

    def last(self, name):
        return self._c.get(name)

    def forget(self, name):
        self._c.pop(name, None)
