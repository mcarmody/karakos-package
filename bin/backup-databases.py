#!/usr/bin/env python3
"""Nightly local backup of an install's own SQLite databases.

Copies every database under data/ (agent queue, graph memory, tool audit, outbox,
build queue, ...) with SQLite's online backup API, which is consistent even with
WAL and a live writer, into <dest>/<YYYY-MM-DD>/ keeping the path relative to
data/, then keeps only the newest N dated directories.

  python3 bin/backup-databases.py                    # back up now
  python3 bin/backup-databases.py --list             # list the dated backups
  python3 bin/backup-databases.py --restore DATE     # put a day's databases back

Destination: --dest, else $KARAKOS_BACKUP_DIR, else <install>/.karakos/backups/databases
(the .karakos directory is bind-mounted from the host in Docker installs, so the
copies survive the loss of the data volume). Keep: --keep, else $KARAKOS_BACKUP_KEEP,
else 7. Run by the scheduler as the `db-backup` job (lib/job_registry.py).

--restore overwrites the live database files: stop the stack first (docs/UPGRADING.md).

Exit codes: 0 done, 1 a database could not be backed up (the others still were) or
the restore failed, 2 bad arguments.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402

DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_KEEP = 7
MANIFEST = "manifest.json"


def find_databases(data_dir: Path, dest: Path) -> list:
    """Database files under data_dir, never anything inside the backup directory."""
    dest_r = dest.resolve()
    out = []
    for p in sorted(data_dir.rglob("*")):
        if p.is_file() and p.suffix in DB_SUFFIXES and dest_r not in p.resolve().parents:
            out.append(p)
    return out


def backup_one(src: Path, dst: Path) -> None:
    """Online backup of src to dst, written under a .partial name, checked, then renamed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(tmp))
    try:
        s.backup(d)
        result = d.execute("PRAGMA integrity_check").fetchone()
    finally:
        d.close()
        s.close()
    if not result or result[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"integrity_check failed on the copy of {src.name}: {result}")
    os.replace(tmp, dst)


def prune(dest: Path, keep: int) -> list:
    """Remove the oldest dated directories beyond `keep`. Only YYYY-MM-DD directories are touched."""
    days = sorted(p for p in dest.iterdir() if p.is_dir() and DATE_RE.match(p.name))
    gone = days[:-keep] if keep > 0 else []
    for p in gone:
        shutil.rmtree(p)
    return [p.name for p in gone]


def run_backup(data_dir: Path, dest: Path, keep: int, today: str = "") -> int:
    day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = dest / day
    dbs = find_databases(data_dir, dest)
    if not dbs:
        print(f"backup-databases: no databases under {data_dir}; nothing to do")
        return 0
    failed, files = [], []
    for src in dbs:
        rel = src.relative_to(data_dir)
        try:
            backup_one(src, out / rel)
            files.append({"path": rel.as_posix(), "size": (out / rel).stat().st_size})
        except Exception as e:  # keep going: one bad database must not skip the rest
            failed.append(f"{rel}: {e}")
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST).write_text(json.dumps(
        {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "source": str(data_dir), "files": files, "failed": failed}, indent=1))
    removed = prune(dest, keep)
    print(f"backup-databases: {len(files)} database(s) -> {out}; removed {len(removed)} old backup(s)")
    for f in failed:
        print(f"backup-databases: FAILED {f}", file=sys.stderr)
    return 1 if failed else 0


def run_restore(data_dir: Path, dest: Path, day: str) -> int:
    src_dir = dest / day
    if not DATE_RE.match(day) or not (src_dir / MANIFEST).is_file():
        print(f"backup-databases: no backup for {day} under {dest} (try --list)", file=sys.stderr)
        return 1
    manifest = json.loads((src_dir / MANIFEST).read_text())
    for entry in manifest["files"]:
        src = src_dir / entry["path"]
        target = data_dir / entry["path"]
        if not src.is_file():
            print(f"backup-databases: missing {src}", file=sys.stderr)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restore")
        shutil.copyfile(src, tmp)
        for sfx in ("-wal", "-shm", "-journal"):  # stale journals must not be replayed onto the restored file
            Path(str(target) + sfx).unlink(missing_ok=True)
        os.replace(tmp, target)
        print(f"backup-databases: restored {entry['path']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nightly local backup of the install's SQLite databases.")
    ap.add_argument("--data", help="data directory (default: <install>/data)")
    ap.add_argument("--dest", help="backup directory (default: $KARAKOS_BACKUP_DIR or <install>/.karakos/backups/databases)")
    ap.add_argument("--keep", type=int, help="dated backups to keep (default: $KARAKOS_BACKUP_KEEP or 7)")
    ap.add_argument("--list", action="store_true", help="list the dated backups and exit")
    ap.add_argument("--restore", metavar="DATE", help="restore the databases saved on DATE (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    root = Path(workspace_root())
    data_dir = Path(args.data) if args.data else root / "data"
    dest = Path(args.dest or os.environ.get("KARAKOS_BACKUP_DIR") or root / ".karakos" / "backups" / "databases")
    try:
        keep = args.keep if args.keep is not None else int(os.environ.get("KARAKOS_BACKUP_KEEP", DEFAULT_KEEP))
    except ValueError:
        print("backup-databases: KARAKOS_BACKUP_KEEP must be a whole number", file=sys.stderr)
        return 2
    if keep < 1:
        print("backup-databases: keep must be at least 1", file=sys.stderr)
        return 2

    if args.list:
        days = sorted(p.name for p in dest.iterdir() if p.is_dir() and DATE_RE.match(p.name)) if dest.is_dir() else []
        print("\n".join(days) if days else f"no backups under {dest}")
        return 0
    if args.restore:
        return run_restore(data_dir, dest, args.restore)
    if not data_dir.is_dir():
        print(f"backup-databases: {data_dir} is not a directory", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    return run_backup(data_dir, dest, keep)


if __name__ == "__main__":
    sys.exit(main())
