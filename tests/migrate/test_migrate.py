import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.migrate import SCHEMA_VERSION, backup as bk, guard, runner  # noqa: E402
from lib.migrate.detect import detect_version  # noqa: E402

ENTRYPOINT = ROOT / "bin" / "entrypoint.sh"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KARAKOS_SKIP_STAMP_CHECK", raising=False)
    monkeypatch.delenv("KARAKOS_ENV", raising=False)


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def mk_db(path: Path, sql: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(sql)
    con.commit()
    con.close()


# -- guard -------------------------------------------------------------------

def test_stamped_ok(tmp_path):
    guard.write_stamp(tmp_path / "data", migrated_from="1.5.0")
    st = guard.read_stamp(tmp_path / "data")
    assert st["schema"] == SCHEMA_VERSION and st["migrated_from"] == "1.5.0"
    guard.require_stamp(tmp_path / "data")


def test_older_stamp_exit_78(tmp_path, capsys):
    d = tmp_path / "data"
    d.mkdir()
    (d / ".schema-version").write_text(json.dumps({"schema": 1}))
    with pytest.raises(SystemExit) as e:
        guard.require_stamp(d)
    assert e.value.code == 78
    assert "schema 1 is older than this release needs; run: karakos migrate" in capsys.readouterr().err


def test_unstamped_nonempty_exit_78(tmp_path, capsys):
    d = tmp_path / "data"
    d.mkdir()
    (d / "x.db").write_text("x")
    with pytest.raises(SystemExit) as e:
        guard.require_stamp(d)
    assert e.value.code == 78
    assert "this data directory is from Karakos 1.x; run: karakos migrate" in capsys.readouterr().err


def test_empty_or_absent_is_fresh_and_stamps(tmp_path):
    guard.require_stamp(tmp_path / "absent")
    assert guard.stamp_fresh(tmp_path / "absent")
    assert guard.read_stamp(tmp_path / "absent")["migrated_from"] is None
    (tmp_path / "e").mkdir()
    assert guard.stamp_fresh(tmp_path / "e")


def test_stamp_fresh_refuses_nonempty(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("1")
    assert guard.stamp_fresh(tmp_path / "d") is False
    assert guard.read_stamp(tmp_path / "d") is None


def test_env_bypass_and_production_refusal(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    (d / "f").write_text("1")
    monkeypatch.setenv("KARAKOS_SKIP_STAMP_CHECK", "1")
    guard.require_stamp(d)
    monkeypatch.setenv("KARAKOS_ENV", "production")
    with pytest.raises(SystemExit) as e:
        guard.require_stamp(d)
    assert e.value.code == 78


# -- detector ----------------------------------------------------------------

def test_detect_buckets(tmp_path):
    def tree(name, agents_json=True, dot=False, yaml=False, rate=False, queue=False):
        r = tmp_path / name
        (r / "config").mkdir(parents=True)
        if agents_json:
            (r / "config" / "agents.json").write_text(json.dumps({"agents": {"a": {}}}))
        if yaml:
            (r / "config" / "agents.yaml").write_text("agents: {}\n")
        if dot:
            (r / ".karakos").mkdir()
        sql = "CREATE TABLE x(a);"
        if queue:
            sql += "CREATE TABLE message_queue(id);"
        if rate:
            sql += "CREATE TABLE rate_limit_state(agent);"
        mk_db(r / "data" / "memory" / "agent-server.db", sql)
        return detect_version(r / "data", r / "config")

    assert tree("v10").version == "1.0"
    assert tree("v13", dot=True, queue=True).version == "1.3"
    assert tree("v15", dot=True, rate=True).version == "1.5"
    assert tree("v15y", agents_json=False, yaml=True, dot=True).version == "1.5"
    d = detect_version(tmp_path / "nothing" / "data", tmp_path / "nothing" / "config")
    assert d.version == "unknown" and d.evidence


def test_detect_corrupt_db_reported_not_raised(tmp_path):
    (tmp_path / "data" / "memory").mkdir(parents=True)
    (tmp_path / "data" / "memory" / "agent-server.db").write_bytes(b"not a database" * 50)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.json").write_text("{broken")
    before = tree_hash(tmp_path)
    d = detect_version(tmp_path / "data", tmp_path / "config")
    assert any("corrupt" in e for e in d.evidence)
    assert tree_hash(tmp_path) == before


# -- backup ------------------------------------------------------------------

def make_install(root: Path):
    (root / "config").mkdir(parents=True)
    (root / "config" / "agents.json").write_text('{"agents": {"a": {}}}')
    (root / ".env").write_text("TOKEN=abc\n")
    (root / "agents" / "a").mkdir(parents=True)
    (root / "agents" / "a" / "SYSTEM_PROMPT.md").write_text("hi")
    db = root / "data" / "memory" / "agent-server.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(v)")
    con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(100)])
    con.commit()
    return con  # open writer, WAL not checkpointed


def test_backup_restore_round_trip_with_open_wal_writer(tmp_path):
    root = tmp_path / "ws"
    writer = make_install(root)
    writer.execute("INSERT INTO t VALUES(999)")
    writer.commit()
    out = bk.backup(root / "data", root / "config", root / "backups")
    assert out.name.startswith("pre-2.0-")
    m = json.loads((out / "MANIFEST.json").read_text())
    paths = {e["path"] for e in m["files"]}
    assert {".env", "config/agents.json", "agents/a/SYSTEM_PROMPT.md",
            "data/memory/agent-server.db"} <= paths
    assert all(len(e["sha256"]) == 64 and e["size"] >= 0 for e in m["files"])
    bk.verify(out)
    writer.execute("DELETE FROM t")
    writer.commit()
    writer.close()
    (root / ".env").write_text("changed")
    bk.restore(out)
    con = sqlite3.connect(root / "data" / "memory" / "agent-server.db")
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 101
    con.close()
    assert (root / ".env").read_text() == "TOKEN=abc\n"


def test_restore_detects_tampered_backup(tmp_path):
    root = tmp_path / "ws"
    make_install(root).close()
    out = bk.backup(root / "data", root / "config", root / "backups")
    (out / "files" / ".env").write_text("tampered")
    with pytest.raises(bk.BackupError):
        bk.restore(out)


def test_backup_failure_leaves_data_untouched(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    make_install(root).close()
    before = tree_hash(root)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(bk, "_sqlite_copy", boom)
    with pytest.raises(bk.BackupError):
        bk.backup(root / "data", root / "config", tmp_path / "bk")
    assert tree_hash(root) == before
    assert not list((tmp_path / "bk").glob("pre-2.0-*"))


# -- runner ------------------------------------------------------------------

def mk_step(name, log, fail=False):
    def apply(ctx):
        log.append(name)
        if fail:
            raise RuntimeError("boom")
    return runner.Step(name, 1, 2, lambda c: True, apply, lambda c: None)


def runner_install(tmp_path):
    root = tmp_path / "ws"
    make_install(root).close()
    return root


def test_steps_run_in_order_and_stamp_is_last(tmp_path):
    root = runner_install(tmp_path)
    seen = []

    def step(name):
        def apply(ctx):
            assert guard.read_stamp(ctx.data_dir) is None
            seen.append(name)
        return runner.Step(name, 1, 2, lambda c: True, apply, lambda c: None)
    rc = runner.run(root / "data", root / "config", root / "backups",
                    steps=[step("a"), step("b")], out=lambda *_: None)
    assert rc == 0 and seen == ["a", "b"]
    assert guard.read_stamp(root / "data")["migrated_from"] == "1.0"
    assert list((root / "backups").glob("pre-2.0-*"))


def test_failing_step_stops_chain_no_stamp(tmp_path):
    root = runner_install(tmp_path)
    log, lines = [], []
    rc = runner.run(root / "data", root / "config", root / "backups",
                    steps=[mk_step("a", log), mk_step("b", log, fail=True),
                           mk_step("c", log)], out=lines.append)
    assert rc == 1 and log == ["a", "b"]
    assert guard.read_stamp(root / "data") is None
    text = "\n".join(lines)
    assert "pre-2.0-" in text and "--to-backup" in text


def test_dry_run_writes_nothing(tmp_path):
    root = runner_install(tmp_path)
    before = tree_hash(root)
    log, lines = [], []
    rc = runner.run(root / "data", root / "config", root / "backups",
                    steps=[mk_step("a", log)], dry_run=True, out=lines.append)
    assert rc == 0 and log == [] and tree_hash(root) == before
    assert any("plan: a" in l for l in lines)


def test_unknown_schema_refused_without_force(tmp_path):
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    (root / "data" / "weird.bin").write_text("?")
    (root / "config").mkdir()
    lines = []
    rc = runner.run(root / "data", root / "config", root / "backups", steps=[],
                    out=lines.append)
    assert rc == 3 and guard.read_stamp(root / "data") is None
    rc = runner.run(root / "data", root / "config", root / "backups", steps=[],
                    force=True, out=lines.append)
    assert rc == 0 and guard.read_stamp(root / "data") is not None


def test_default_step_chain_loads():
    assert [s.name for s in runner.load_steps()] == ["00_noop", "10_registry", "12_monitor", "20_queue", "30_sessions", "35_rate_limit", "40_memory"]


def test_cli_exit_codes(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "KARAKOS_SKIP_STAMP_CHECK"}
    r = subprocess.run([sys.executable, "-m", "lib.migrate", "--bogus"], cwd=ROOT,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2
    root = runner_install(tmp_path)
    r = subprocess.run([sys.executable, "-m", "lib.migrate", "--auto", "--root", str(root)],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert guard.read_stamp(root / "data")


# -- entrypoint and server refuse to boot ---------------------------------------

def run_entrypoint(ws: Path):
    env = {**os.environ, "WORKSPACE_ROOT": str(ws), "DASHBOARD_PORT": "3000",
           "AGENT_SERVER_TOKEN": "t"}
    env.pop("KARAKOS_SKIP_STAMP_CHECK", None)
    return subprocess.run(["bash", str(ENTRYPOINT)], env=env, capture_output=True,
                          text=True, timeout=30)


def test_entrypoint_refuses_unstamped_fixture(tmp_path):
    for n in ("data", "logs", "inbox"):
        (tmp_path / n).mkdir()
    (tmp_path / "data" / "old.db").write_text("x")
    r = run_entrypoint(tmp_path)
    assert r.returncode == 78 and "run: karakos migrate" in r.stderr
    assert not (tmp_path / "data" / "messages").exists()  # nothing mutated


def test_server_refuses_unstamped_fixture_no_spawn(tmp_path, monkeypatch):
    from conftest import import_script
    from harness import FAKE_BIN_DIR
    ws = tmp_path
    (ws / "data").mkdir()
    (ws / "data" / "old.db").write_text("x")
    log_dir = ws / "fake-claude-logs"
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("AGENT_SERVER_TOKEN", "t")
    monkeypatch.setenv("FAKE_CLAUDE_LOG_DIR", str(log_dir))
    monkeypatch.setenv("PATH", f"{FAKE_BIN_DIR}{os.pathsep}{os.environ['PATH']}")
    mod = import_script("agent-server")
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code == 78
    assert not log_dir.exists() or not list(log_dir.iterdir())  # no spawn
    assert not (ws / "data" / "memory").exists()  # no DB opened


def test_dry_run_lists_env_vars_no_agent_references(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".env").write_text("DISCORD_TOKEN_A=x\nGITHUB_TOKEN=y\nOTHER=z\n")
    (cfg / "agents.json").write_text(json.dumps({"agents": {"a": {
        "discord_bot_token_env": "DISCORD_TOKEN_A", "env": {"OTHER": "${OTHER}"}}}}))
    lines = runner.unreferenced_env_report(cfg)
    assert "  - GITHUB_TOKEN" in lines
    assert "  - OTHER" not in lines and "  - DISCORD_TOKEN_A" not in lines
