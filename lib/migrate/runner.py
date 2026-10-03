"""Migrator core: detect, backup, run applicable steps, verify, stamp last."""
import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from lib.migrate import (EXIT_OK, EXIT_REFUSED, EXIT_STEP_FAILED, SCHEMA_VERSION)
from lib.migrate import backup as backup_mod
from lib.migrate import guard
from lib.migrate.detect import Detected, detect_version


@dataclass
class Context:
    data_dir: Path
    config_dir: Path
    backup_dir: Optional[Path]
    detected: Detected
    log: logging.Logger


@dataclass
class Step:
    name: str
    from_schema: int
    to_schema: int
    detect: Callable[[Context], bool]
    apply: Callable[[Context], None]
    verify: Callable[[Context], None]


_NAME = re.compile(r"^(\d\d)_")


def load_steps() -> List[Step]:
    """Import lib/migrate/steps/NN_name.py in numeric order; each exposes STEP."""
    from lib.migrate import steps as pkg
    mods = sorted((m.name for m in pkgutil.iter_modules(pkg.__path__)
                   if _NAME.match(m.name)))
    return [importlib.import_module(f"{pkg.__name__}.{n}").STEP for n in mods]


def unknown_report(detected: Detected) -> List[str]:
    """Lines describing what the migrator does not recognise (Fork policy)."""
    if detected.version != "unknown":
        return []
    return ["unknown schema; evidence:"] + [f"  - {e}" for e in detected.evidence]


def run(data_dir, config_dir, backup_root=None, steps=None, dry_run=False,
        force=False, out=print) -> int:
    data_dir, config_dir = Path(data_dir), Path(config_dir)
    steps = load_steps() if steps is None else steps
    log = logging.getLogger("karakos.migrate")
    detected = detect_version(data_dir, config_dir)
    out(f"detected: {detected.version} ({detected.layout})")
    for e in detected.evidence:
        out(f"  - {e}")

    stamp = guard.read_stamp(data_dir)
    if stamp and stamp["schema"] >= SCHEMA_VERSION:
        out(f"already at schema {stamp['schema']}; nothing to do")
        return EXIT_OK

    if detected.version == "unknown" and any(data_dir.glob("*")):
        for line in unknown_report(detected):
            out(line)
        if not force and not dry_run:
            out("refusing: unknown schema (use --force to proceed)")
            return EXIT_REFUSED
        if dry_run:
            out("dry-run: would refuse without --force")
            return EXIT_OK

    ctx = Context(data_dir, config_dir, None, detected, log)
    plan = [s for s in steps if s.detect(ctx)]
    out("plan: " + (", ".join(s.name for s in plan) or "no data steps") + ", stamp")
    if dry_run:
        out("dry-run: nothing written")
        return EXIT_OK

    root = Path(backup_root) if backup_root else data_dir.parent / "backups"
    try:
        ctx.backup_dir = backup_mod.backup(data_dir, config_dir, root)
    except backup_mod.BackupError as e:
        out(f"{e}; nothing was changed")
        return EXIT_STEP_FAILED
    out(f"backup: {ctx.backup_dir}")

    for s in plan:
        try:
            out(f"step {s.name}: apply")
            s.apply(ctx)
            s.verify(ctx)
        except Exception as e:
            out(f"step {s.name} FAILED: {e}")
            out(f"data is NOT stamped. backup: {ctx.backup_dir}")
            out(f"restore with: python3 -m lib.migrate --to-backup {ctx.backup_dir}")
            return EXIT_STEP_FAILED

    guard.write_stamp(data_dir, migrated_from=_from_version(detected))
    out(f"migrated to schema {SCHEMA_VERSION}")
    return EXIT_OK


def _from_version(d: Detected):
    return d.version if d.version not in ("unknown", "2.0") else None
