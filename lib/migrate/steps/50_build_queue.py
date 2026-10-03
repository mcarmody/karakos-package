"""build_queue: create the build queue database and its config (spec 3.3).

Additive: no 1.x data exists to move. Creates data/build-queue.db (SQLite, WAL)
through `buildq.init_schema` -- the same function setup uses -- and writes
config/build-queue.yaml with `enabled: false` and a commented example host when
the file is absent (an existing file is never overwritten). Boot code and the
dispatcher only check the tables exist.
"""
import os
import sqlite3
import sys
from pathlib import Path

from lib.migrate.runner import Step

_PKG = Path(__file__).resolve().parents[3]

CONFIG_TEXT = """\
# Build queue (spec 3.3). Off by default: briefs dropped in inbox/builder/ and
# inbox/reviewer/ are run directly by the relay, exactly as before. Set
# `enabled: true` to queue them (priority, per-host concurrency, admission,
# cancel, retry); then use `bin/buildq`.
enabled: false
default_host: local        # an explicit host on a row wins
cost_ceiling_usd: 75       # per run, remote runs only (--max-budget-usd)
roles:
  build: {timeout_s: 21600}
  review: {timeout_s: 3600}
retry: {max_attempts: 1}   # a retry can open a second PR; keep at 1
governor: true             # machine-originated rows yield to weekly usage (2.7)
unreachable_grace_s: 900
hosts:
  local:
    kind: local
    concurrency: 1
    min_free_ram_mb: 2048
    # max_load1: 8
  # remote:                # example: a second machine reached over ssh
  #   kind: ssh
  #   target: build-box    # an ssh destination or alias from your ssh config
  #   concurrency: 2
  #   workdir: ~/karakos-builds
  #   probe: [sh, -c, 'echo "{\\"free_ram_mb\\": $(free -m | awk "/Mem:/{print \\$7}"), \\"load1\\": $(cut -d" " -f1 /proc/loadavg)}"']
  #   min_free_ram_mb: 4096
  #   max_load1: 6
"""


def _buildq():
    lib = str(_PKG / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import buildq
    return buildq


def _db(ctx) -> Path:
    return Path(ctx.data_dir) / "build-queue.db"


def _present(db: Path) -> bool:
    if not db.is_file():
        return False
    con = sqlite3.connect(db)
    try:
        return _buildq().tables_present(con)
    finally:
        con.close()


def detect(ctx) -> bool:
    return not _present(_db(ctx))


def plan(ctx):
    return [f"build queue: create {_db(ctx).name}"
            + ("" if (Path(ctx.config_dir) / "build-queue.yaml").exists()
               else " and config/build-queue.yaml (enabled: false)")]


def apply(ctx) -> None:
    db = _db(ctx)
    db.parent.mkdir(parents=True, exist_ok=True)
    bq = _buildq()
    con = sqlite3.connect(db)
    try:
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        bq.init_schema(con)
    finally:
        con.close()
    cfg = Path(ctx.config_dir) / "build-queue.yaml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.with_name(cfg.name + ".tmp")
        tmp.write_text(CONFIG_TEXT)
        os.replace(tmp, cfg)


def verify(ctx) -> None:
    if not _present(_db(ctx)):
        raise RuntimeError("build-queue.db lacks build_queue / build_queue_events")
    if detect(ctx):
        raise RuntimeError("50_build_queue would still apply")


STEP = Step("50_build_queue", 2, 2, detect=detect, apply=apply, verify=verify, plan=plan)
