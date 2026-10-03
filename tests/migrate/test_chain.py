"""The migration chain end to end (spec 7.1): step order, per-tag fixtures,
fork policy, restore, interruption, compose/.env, and the host wrapper."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile

import pytest
import yaml

from _memory_helpers import (ROOT, fx, graph, install, runner, tree_hash, _env,  # noqa: F401
                             fake_embedder)
from lib.migrate import backup as bk, guard
from lib.migrate.detect import detect_version

FIXTURES = ROOT / "tests" / "migrate" / "fixtures"
# fixture tag -> expected layout bucket (docs/migration-inventory.md)
TAGS = {"v1.0.0": "1.0", "v1.1.1": "1.0", "v1.3": "1.3", "v1.4.1": "1.3", "v1.5.0": "1.5"}
AGENTS = ["alpha", "relay", "builder", "reviewer", "scout"]

# The fixed, reserved order (00 is the merged no-op step). Steps land one build at a
# time: an entry absent from the tree is fine, a step file NOT listed here is not.
CHAIN_TABLE = ["00_noop", "05_layout", "10_registry", "12_monitor", "20_queue",
               "30_sessions", "35_rate_limit", "40_memory", "50_build_queue",
               "60_outbox", "90_stamp"]
PRESENT_NOW = ["05_layout", "10_registry", "20_queue", "30_sessions", "35_rate_limit",
               "40_memory", "90_stamp"]


def step_files():
    return sorted(p.stem for p in (ROOT / "lib" / "migrate" / "steps").glob("[0-9][0-9]_*.py"))


def test_chain_table_is_ordered():
    assert CHAIN_TABLE == sorted(CHAIN_TABLE)
    assert len(set(CHAIN_TABLE)) == len(CHAIN_TABLE)


def test_every_step_file_is_in_the_table_in_order():
    files = step_files()
    unlisted = [f for f in files if f not in CHAIN_TABLE]
    assert not unlisted, f"step file(s) not in the reserved table: {unlisted}"
    assert files == [n for n in CHAIN_TABLE if n in files]
    assert [s.name for s in runner.load_steps()] == files


def test_required_steps_are_present_now():
    missing = [n for n in PRESENT_NOW if n not in step_files()]
    assert not missing, missing


def test_full_chain_post_migration_tree_is_usable(tmp_path, fake_embedder):
    install(tmp_path)
    lines = []
    rc = runner.run(tmp_path / "data", tmp_path / "config", tmp_path / "backups",
                    out=lines.append, parity_queries=20)
    assert rc == 0, lines
    data = tmp_path / "data"
    assert not (data / "memory" / "memory.db").exists()
    assert (data / "memory" / "memory.db.migrated").exists()
    # no code reads memory.db any more: the graph opens and the tools answer
    from lib.graph import tools as graph_tools
    from lib.graph.store import open_graph
    store = open_graph(data, create=False)
    res = graph_tools.memory_tool({"action": "recall", "query": "garden", "limit": 3},
                                  store, "alpha")
    assert res["results"], res
    assert graph_tools.memory_tool({"action": "status"}, store, "alpha")


# -- per-tag fixtures ----------------------------------------------------------

def extract(tag, dest):
    with tarfile.open(FIXTURES / f"{tag}.tar.gz") as t:
        t.extractall(dest, filter="data")
    return dest / "install"


def snapshot(root):
    """{relative path: bytes or None for a directory} of everything under root."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def go(root, backups, **kw):
    lines = []
    rc = runner.run(root / "data", root / "config", backups, out=lines.append,
                    parity_queries=kw.pop("parity_queries", 10), **kw)
    return rc, lines


def queue_rows(root):
    con = sqlite3.connect(root / "data" / "memory" / "agent-server.db")
    try:
        return con.execute("SELECT agent, message_id, processed FROM message_queue "
                           "ORDER BY id").fetchall()
    finally:
        con.close()


def session_rows(root):
    con = sqlite3.connect(root / "data" / "memory" / "agent-server.db")
    try:
        return con.execute("SELECT * FROM sessions ORDER BY 1").fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("tag", list(TAGS))
def test_fixture_detects_its_bucket(tag, tmp_path):
    root = extract(tag, tmp_path)
    assert detect_version(root / "data", root / "config").version == TAGS[tag]


@pytest.mark.parametrize("tag", list(TAGS))
def test_dry_run_writes_nothing(tag, tmp_path, fake_embedder):
    root = extract(tag, tmp_path)
    before = snapshot(root)
    rc, lines = go(root, tmp_path / "backups", dry_run=True)
    assert rc == 0, lines
    assert snapshot(root) == before
    assert not (tmp_path / "backups").exists()
    assert any("no downgrade" in l for l in lines)


@pytest.mark.parametrize("tag", list(TAGS))
def test_full_chain_reaches_the_stamp(tag, tmp_path, fake_embedder):
    root = extract(tag, tmp_path)
    q_before, s_before = queue_rows(root), session_rows(root)
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines
    assert guard.read_stamp(root / "data")["schema"] == 2
    assert any("40_memory" in l for l in lines) and any("05_layout" in l for l in lines)
    # queue rows and sessions survive, keyed by the agent id (default shard id)
    q_after = queue_rows(root)
    assert q_after == q_before and {a for a, *_ in q_after} <= set(AGENTS)
    assert {r[0] for r in session_rows(root)} == set(AGENTS) == {r[0] for r in s_before}
    # registry: all five agents, including the custom one
    reg = yaml.safe_load((root / "config" / "agents.yaml").read_text())
    assert set(reg["agents"]) >= set(AGENTS)
    assert (root / "config" / "docker-compose.yml.pre-2.0").is_file()
    assert (root / "config" / ".env.pre-2.0").is_file()
    assert "MY_CUSTOM_VAR" in (root / "config" / ".env").read_text()
    assert (root / "config" / "custom-hook.sh").is_file()   # user hook untouched
    # second run on a stamped directory does nothing
    again = snapshot(root)
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0 and any("already at schema" in l for l in lines)
    assert snapshot(root) == again


@pytest.mark.parametrize("tag", ["v1.0.0", "v1.5.0"])
def test_recall_parity_and_boot_against_the_result(tag, tmp_path, fake_embedder):
    root = extract(tag, tmp_path)
    rc, lines = go(root, tmp_path / "backups", parity_queries=10)
    assert rc == 0, lines
    con = sqlite3.connect(root / "data" / "memory" / "graph.db")
    meta = json.loads(con.execute("SELECT value FROM meta WHERE key='migration'").fetchone()[0])
    con.close()
    assert meta["parity"]["queries"] == 10 and meta["parity"]["gate_a_pass"]
    assert (meta["episodes"], meta["facts"]) == (40, 10)
    from lib.graph import tools as graph_tools
    from lib.graph.store import open_graph
    store = open_graph(root / "data", create=False)
    res = graph_tools.memory_tool({"action": "recall", "query": "pantry", "limit": 3},
                                  store, "alpha")
    assert res["results"], res


@pytest.mark.parametrize("tag", ["v1.5.0", "v1.3"])
def test_server_boots_under_the_harness_on_the_migrated_tree(tag, tmp_path, fake_embedder):
    from harness import Harness
    import asyncio
    root = extract(tag, tmp_path)
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines
    h = Harness(root, agents=["alpha"], write_config=False)

    async def scenario():
        async with h:
            resp = await h.client.get("/health", headers=h._headers())
            assert resp.status == 200
            ag = await (await h.client.get("/agents", headers=h._headers())).json()
            return {a["name"] for a in ag["agents"]}

    names = asyncio.run(scenario())
    assert set(AGENTS) <= names


@pytest.mark.parametrize("tag", ["v1.0.0", "v1.5.0"])
def test_backup_restores_to_the_manifest_exactly(tag, tmp_path, fake_embedder):
    root = extract(tag, tmp_path)
    rc, _ = go(root, tmp_path / "backups")
    assert rc == 0
    bdir = next((tmp_path / "backups").glob("pre-2.0-*"))
    bk.restore(bdir, data_dir=root / "data", config_dir=root / "config")
    m = bk.verify(bdir)
    expected = {}
    for e in m["files"]:
        label, _, rest = e["path"].partition("/")
        expected[Path(m["roots"][".env"]) if e["path"] == ".env" else (root / label / rest)] = e["sha256"]
    present = {}
    for label in ("data", "config", "agents"):
        for p in (root / label).rglob("*"):
            if p.is_file():
                present[p] = bk._sha256(p)
    assert present == expected                      # same file set, same hashes
    assert guard.read_stamp(root / "data") is None
    assert not (root / "config" / "agents.yaml").exists()


# -- fork policy -----------------------------------------------------------------

def fork_shape(root):
    con = sqlite3.connect(root / "data" / "memory" / "agent-server.db")
    con.execute("CREATE TABLE my_fork_table (id INTEGER)")
    con.execute("ALTER TABLE sessions ADD COLUMN fork_col TEXT")
    con.commit()
    con.close()
    cfg = json.loads((root / "config" / "agents.json").read_text())
    cfg["agents"]["alpha"]["fork_key"] = 1
    (root / "config" / "agents.json").write_text(json.dumps(cfg))


@pytest.mark.parametrize("tag", ["v1.3", "v1.5.0"])
def test_vanilla_fixtures_pass_the_fork_check(tag, tmp_path):
    from lib.migrate import fork
    root = extract(tag, tmp_path)
    assert fork.check(root / "data", root / "config") == []


def test_fork_shape_is_refused_then_forced_with_a_report(tmp_path, fake_embedder):
    root = extract("v1.5.0", tmp_path)
    fork_shape(root)
    before = snapshot(root)
    rc, lines = go(root, tmp_path / "backups")
    text = "\n".join(lines)
    assert rc == 3
    assert "my_fork_table" in text and "sessions.fork_col" in text and "fork_key" in text
    assert snapshot(root) == before and guard.read_stamp(root / "data") is None
    rc, lines = go(root, tmp_path / "backups", dry_run=True)
    assert rc == 0 and snapshot(root) == before
    rc, lines = go(root, tmp_path / "backups", force=True)
    assert rc == 0, lines
    report = (root / "data" / "migration-reports" / "migration-report.md").read_text()
    assert "my_fork_table" in report and "fork_key" in report


def test_dry_run_report_goes_outside_the_install(tmp_path, fake_embedder):
    root = extract("v1.5.0", tmp_path)
    fork_shape(root)
    before = snapshot(root)
    rep = tmp_path / "out" / "report.md"
    rc, _ = go(root, tmp_path / "backups", dry_run=True, report_to=rep)
    assert rc == 0 and snapshot(root) == before
    assert "my_fork_table" in rep.read_text()


# -- interruption and resume ---------------------------------------------------

class Kill(BaseException):
    """Stands in for SIGKILL: no handler in the runner gets to run."""


def test_killed_run_then_rerun_restarts_from_the_backup(tmp_path, fake_embedder):
    root = extract("v1.5.0", tmp_path)
    before = snapshot(root)
    steps = runner.load_steps()
    killer = runner.Step("30_sessions", 1, 2, lambda c: True,
                         lambda c: (_ for _ in ()).throw(Kill()), lambda c: None)
    steps = [killer if s.name == "30_sessions" else s for s in steps]
    lines = []
    with pytest.raises(Kill):
        runner.run(root / "data", root / "config", tmp_path / "backups", steps=steps,
                   out=lines.append, parity_queries=5)
    assert guard.read_stamp(root / "data") is None
    assert (tmp_path / "backups" / runner.MARKER).is_file()
    assert (root / "config" / "agents.yaml").exists()        # half-state is really there
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines
    assert any("previous run did not finish" in l for l in lines)
    assert guard.read_stamp(root / "data") is not None
    assert not (tmp_path / "backups" / runner.MARKER).exists()
    assert len(queue_rows(root)) == 12


def test_failed_step_then_rerun_succeeds(tmp_path, fake_embedder):
    root = extract("v1.3", tmp_path)
    steps = runner.load_steps()
    bad = runner.Step("30_sessions", 1, 2, lambda c: True,
                      lambda c: (_ for _ in ()).throw(RuntimeError("boom")), lambda c: None)
    steps = [bad if s.name == "30_sessions" else s for s in steps]
    lines = []
    rc = runner.run(root / "data", root / "config", tmp_path / "backups", steps=steps,
                    out=lines.append, parity_queries=5)
    assert rc == 1 and guard.read_stamp(root / "data") is None
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines



def _failed_run(root, backups):
    steps = runner.load_steps()
    bad = runner.Step("30_sessions", 1, 2, lambda c: True,
                      lambda c: (_ for _ in ()).throw(RuntimeError("boom")), lambda c: None)
    steps = [bad if s.name == "30_sessions" else s for s in steps]
    rc = runner.run(root / "data", root / "config", backups, steps=steps,
                    out=lambda l: None, parity_queries=5)
    assert rc == 1 and (backups / runner.MARKER).is_file()


def test_explicit_restore_clears_the_marker(tmp_path, fake_embedder):
    """After `--restore`, the restored install is the operator's: a later run
    must not restore the old backup over what it has written since."""
    from lib.migrate.__main__ import main
    root = extract("v1.3", tmp_path)
    backups = tmp_path / "backups"
    _failed_run(root, backups)
    prev = (backups / runner.MARKER).read_text().strip()
    rc = main(["--root", str(root), "--backup-to", str(backups), "--to-backup", prev])
    assert rc == 0
    assert not (backups / runner.MARKER).exists()
    (root / "data" / "written-after-restore.txt").write_text("keep me")
    rc, lines = go(root, backups)
    assert rc == 0, lines
    assert not any("previous run did not finish" in l for l in lines)
    assert (root / "data" / "written-after-restore.txt").read_text() == "keep me"


def test_a_stale_marker_is_not_restored_automatically(tmp_path, fake_embedder):
    root = extract("v1.3", tmp_path)
    backups = tmp_path / "backups"
    _failed_run(root, backups)
    marker = backups / runner.MARKER
    old = marker.stat().st_mtime - runner.MARKER_MAX_AGE_S - 60
    os.utime(marker, (old, old))
    (root / "data" / "written-later.txt").write_text("keep me")
    rc, lines = go(root, backups)
    assert rc != 0
    assert any("--restore" in l for l in lines)
    assert marker.is_file()
    assert (root / "data" / "written-later.txt").read_text() == "keep me"
    assert guard.read_stamp(root / "data") is None


# -- 05_layout: compose, .env, import, keep-bind -------------------------------

def test_compose_keeps_ports_and_project_name(tmp_path, fake_embedder):
    root = extract("v1.4.1", tmp_path)
    old = (root / "config" / "docker-compose.yml").read_text()
    (root / "config" / "docker-compose.yml").write_text(
        "name: myhome\n" + old.replace('"${DASHBOARD_PORT:-3000}:${DASHBOARD_PORT:-3000}"',
                                       '"8080:${DASHBOARD_PORT:-3000}"'))
    pre_text = (root / "config" / "docker-compose.yml").read_text()
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines
    new = yaml.safe_load((root / "config" / "docker-compose.yml").read_text())
    assert new["name"] == "myhome"
    ports = new["services"]["karakos"]["ports"]
    assert ports[0].startswith("8080:") and ports[1].startswith("127.0.0.1:")
    assert (root / "config" / "docker-compose.yml.pre-2.0").read_text() == pre_text
    assert set(new["volumes"]) == {"karakos-data", "karakos-logs", "karakos-inbox"}


def test_unknown_env_is_kept_and_listed(tmp_path, fake_embedder):
    root = extract("v1.5.0", tmp_path)
    (root / "config" / ".env").write_text(
        (root / "config" / ".env").read_text() + "WEIRD_THING=1\n")
    rc, _ = go(root, tmp_path / "backups")
    assert rc == 0
    assert "WEIRD_THING=1" in (root / "config" / ".env").read_text()
    report = (root / "data" / "migration-reports" / "migration-report.md").read_text()
    assert "WEIRD_THING" in report and "MY_CUSTOM_VAR" in report


def test_env_rename_table_is_applied(tmp_path, monkeypatch):
    from lib.migrate import compose
    monkeypatch.setitem(compose.ENV_RENAMES, "OLD_NAME", "NEW_NAME")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".env").write_text("OLD_NAME=v\nKEEP=1\n")
    res = compose.migrate_env(cfg)
    assert res["renamed"] == [("OLD_NAME", "NEW_NAME")]
    assert (cfg / ".env").read_text() == "NEW_NAME=v\nKEEP=1\n"
    assert (cfg / ".env.pre-2.0").read_text() == "OLD_NAME=v\nKEEP=1\n"


def test_import_from_copies_logs_and_inbox_without_overwriting(tmp_path, fake_embedder):
    root = extract("v1.0.0", tmp_path / "new")
    old = tmp_path / "old"
    (old / "logs" / "agent-streams").mkdir(parents=True)
    (old / "logs" / "agent-streams" / "legacy.log").write_text("old log")
    (old / "inbox" / "alpha").mkdir(parents=True)
    (old / "inbox" / "alpha" / "note.md").write_text("DIFFERENT")
    before_old = snapshot(old)
    rc, lines = go(root, tmp_path / "backups", import_from=old)
    assert rc == 0, lines
    assert (root / "logs" / "agent-streams" / "legacy.log").read_text() == "old log"
    assert (root / "inbox" / "alpha" / "note.md").read_text() == "fixture inbox\n"  # not overwritten
    assert snapshot(old) == before_old                  # the old checkout is untouched


def test_keep_bind_writes_an_override(tmp_path, fake_embedder):
    root = extract("v1.0.0", tmp_path)
    rc, lines = go(root, tmp_path / "backups", keep_bind="/srv/karakos-old")
    assert rc == 0, lines
    ov = yaml.safe_load((root / "config" / "docker-compose.override.yml").read_text())
    assert "/srv/karakos-old/data:/workspace/data" in ov["services"]["karakos"]["volumes"]


def test_import_and_keep_bind_are_exclusive(tmp_path):
    root = extract("v1.0.0", tmp_path)
    (tmp_path / "o").mkdir()
    rc, lines = go(root, tmp_path / "backups", import_from=tmp_path / "o", keep_bind="/x")
    assert rc == 1 and guard.read_stamp(root / "data") is None


# -- CLI and the host wrapper ----------------------------------------------------

def test_cli_flags(tmp_path, fake_embedder):
    root = extract("v1.3", tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "KARAKOS_SKIP_STAMP_CHECK"}
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, "-m", "lib.migrate", "--root", str(root), "--dry-run",
                        "--backup-to", str(tmp_path / "bk")], cwd=ROOT, capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / "bk").exists()


def shim(tmp_path, body):
    d = tmp_path / "shimbin"
    d.mkdir(exist_ok=True)
    f = d / "docker"
    f.write_text("#!/usr/bin/env bash\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return d


def wrapper(tmp_path, args, rc=0, tty=False):
    inst = tmp_path / "inst"
    (inst / "config").mkdir(parents=True, exist_ok=True)
    (inst / "config" / "docker-compose.yml").write_text("services: {}\n")
    log = tmp_path / "docker.log"
    d = shim(tmp_path, f'echo "$@" >> {log}\n'
                       f'case "$*" in *" run "*) exit {rc};; esac\nexit 0\n')
    env = {"PATH": f"{d}:{os.environ['PATH']}", "KARAKOS_COMPOSE_FILE": str(inst / "config" / "docker-compose.yml"),
           "KARAKOS_BACKUP_DIR": str(tmp_path / "bk")}
    r = subprocess.run(["bash", str(ROOT / "bin" / "karakos"), "migrate", *args],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return r, log.read_text().splitlines() if log.exists() else []


def test_wrapper_stops_stack_runs_migrator_and_prints_restore(tmp_path):
    (tmp_path / "bk" / "pre-2.0-20260101T000000Z").mkdir(parents=True)
    r, calls = wrapper(tmp_path, ["--auto"])
    assert r.returncode == 0, r.stderr
    assert any(" down" in c for c in calls)
    run = next(c for c in calls if " run " in c)
    assert "--entrypoint /workspace/bin/karakos-migrate" in run
    assert "--backup-to /backups" in run and "--import-from /old" in run and "--auto" in run
    assert calls.index(next(c for c in calls if " down" in c)) < calls.index(run)
    assert "karakos migrate --restore" in r.stdout


def test_wrapper_dry_run_leaves_the_stack_running(tmp_path):
    r, calls = wrapper(tmp_path, ["--dry-run"])
    assert r.returncode == 0
    assert not any(" down" in c for c in calls) and any("--dry-run" in c for c in calls)


def test_wrapper_keep_bind_and_failure_exit_code(tmp_path):
    r, calls = wrapper(tmp_path, ["--auto", "--keep-bind"], rc=3)
    assert r.returncode == 3
    run = next(c for c in calls if " run " in c)
    assert "--keep-bind" in run and "--import-from" not in run
    assert "next run restores" in r.stderr


def test_wrapper_restore_maps_to_to_backup(tmp_path):
    b = tmp_path / "somebackup"
    b.mkdir()
    r, calls = wrapper(tmp_path, ["--restore", str(b)])
    assert r.returncode == 0, r.stderr
    run = next(c for c in calls if " run " in c)
    assert "--to-backup /restore" in run
    assert "--backup-to /backups" in run      # so the restore clears the marker there


def test_dry_run_says_logs_and_inbox_are_not_backed_up(tmp_path, fake_embedder):
    root = extract("v1.3", tmp_path)
    rc, lines = go(root, tmp_path / "backups", dry_run=True)
    assert rc == 0, lines
    assert any("logs/ and inbox/ are not in the backup" in l for l in lines)


def test_no_step_writes_logs_or_inbox_and_backup_matches_the_claim(tmp_path, fake_embedder):
    """The dry-run note is only true while a migration leaves logs/ and inbox/ alone."""
    root = extract("v1.3", tmp_path)
    (root / "logs").mkdir(exist_ok=True)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "logs" / "a.log").write_text("keep")
    (root / "inbox" / "b.md").write_text("keep")
    before = {p: (root / p).read_bytes() for p in ("logs/a.log", "inbox/b.md")}
    rc, lines = go(root, tmp_path / "backups")
    assert rc == 0, lines
    assert {p: (root / p).read_bytes() for p in before} == before
    bk = next((tmp_path / "backups").glob("pre-2.0-*"))
    assert not (bk / "files" / "logs").exists() and not (bk / "files" / "inbox").exists()
