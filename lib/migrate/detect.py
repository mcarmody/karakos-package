"""Read-only fingerprinting of a 1.x install. Never writes, never raises."""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Detected:
    version: str            # "1.0" | "1.3" | "1.5" | "2.0" | "unknown"
    layout: str
    evidence: list = field(default_factory=list)


def ro_uri(db: Path) -> str:
    """Read-only URI that cannot create files. A plain mode=ro open of a WAL
    database creates -wal/-shm beside it; with no -wal present there is nothing
    to replay, so immutable=1 is exact and writes nothing."""
    wal = Path(str(db) + "-wal")
    return f"file:{db}?mode=ro" if wal.exists() else f"file:{db}?mode=ro&immutable=1"


def _tables(db: Path, evidence: list):
    """{table: {columns}} or None if unreadable (reported in evidence)."""
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(ro_uri(db), uri=True)
        try:
            out = {}
            for (name,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                out[name] = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
            return out
        finally:
            con.close()
    except sqlite3.Error as e:
        evidence.append(f"corrupt or unreadable database {db.name}: {e}")
        return None


def _load_json(path: Path, evidence: list):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        evidence.append(f"corrupt {path.name}: {e}")
        return None


def detect_version(data_dir, config_dir) -> Detected:
    ev: list = []
    try:
        return _detect(Path(data_dir), Path(config_dir), ev)
    except Exception as e:  # last resort: report, never raise
        ev.append(f"detector error: {type(e).__name__}: {e}")
        return Detected("unknown", "unknown", ev)


def _detect(data: Path, config: Path, ev: list) -> Detected:
    from lib.migrate.guard import read_stamp
    stamp = read_stamp(data)
    if stamp:
        ev.append(f".schema-version present (schema {stamp['schema']})")
        return Detected("2.0", "stamped", ev)

    dot_karakos = (data.parent / ".karakos").is_dir() or (data / ".karakos").is_dir()
    has_yaml = (config / "agents.yaml").is_file()
    agents_json = _load_json(config / "agents.json", ev)
    agent_db = _tables(data / "memory" / "agent-server.db", ev) or \
        _tables(data / "agent.db", ev)
    memory_db = _tables(data / "memory" / "memory.db", ev) or \
        _tables(data / "memory.db", ev)

    if dot_karakos:
        ev.append(".karakos/ present")
    if has_yaml:
        ev.append("config/agents.yaml present")
    if agents_json is not None:
        shape = "agents map" if isinstance(agents_json.get("agents"), dict) else "unexpected shape"
        ev.append(f"config/agents.json: {shape}")
    for label, t in (("agent db", agent_db), ("memory db", memory_db)):
        if t is not None:
            ev.append(f"{label} tables: {sorted(t)}")

    if agent_db is None and memory_db is None and agents_json is None and not has_yaml:
        if not ev:
            ev.append("no 1.x fingerprints found")
        return Detected("unknown", "unknown", ev)

    # Buckets from docs/migration-inventory.md (tags with identical mounts,
    # volumes and tables collapse): 1.0 = v1.0.0..v1.2 (compose bind-mounts the
    # whole checkout), 1.3 = v1.3..v1.4.1 (config/agents bind + logs/inbox
    # volumes), 1.5 = v1.5.0 (adds rate_limit_state / turn_events). `.karakos/`
    # is tracked in every tag, so it fingerprints nothing.
    tables = agent_db or {}
    if "rate_limit_state" in tables or "turn_events" in tables:
        ev.append("rate_limit_state/turn_events table present")
        return Detected("1.5", "agents.json+rate_limit_state", ev)
    if has_yaml:
        return Detected("1.5", "agents.yaml", ev)
    compose = _compose_text(config)
    if compose is not None:
        if re.search(r"^\s*-\s*\.\.:/workspace\s*$", compose, re.M):
            ev.append("compose bind-mounts the whole checkout (..:/workspace)")
            return Detected("1.0", "checkout-bind", ev)
        if "/workspace/config" in compose:
            ev.append("compose bind-mounts config and agents separately")
            return Detected("1.3", "split-volumes", ev)
    if agents_json is not None or agent_db is not None:
        # no compose file to read: fall back on the data shape
        return Detected("1.3" if dot_karakos else "1.0",
                        "split-volumes" if dot_karakos else "checkout-bind", ev)
    return Detected("unknown", "unknown", ev)


def _compose_text(config: Path):
    for name in ("docker-compose.yml", "docker-compose.yml.pre-2.0"):
        f = config / name
        if f.is_file():
            try:
                return f.read_text()
            except OSError:
                return None
    return None
