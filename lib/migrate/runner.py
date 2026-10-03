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
from lib.migrate import fork, guard
from lib.migrate.detect import Detected, detect_version


@dataclass
class Context:
    data_dir: Path
    config_dir: Path
    backup_dir: Optional[Path]
    detected: Detected
    log: logging.Logger
    force: bool = False
    parity_queries: int = 50
    # report section -> lines; written to migration-reports/migration-report.md
    report: dict = field(default_factory=dict)
    # 05_layout inputs: copy logs/inbox/data from an old checkout mounted here, or
    # keep the old host path bind-mounted (HOST_DIR as the host sees it)
    import_from: Optional[Path] = None
    keep_bind: Optional[str] = None


@dataclass
class Step:
    name: str
    from_schema: int
    to_schema: int
    detect: Callable[[Context], bool]
    apply: Callable[[Context], None]
    verify: Callable[[Context], None]
    # optional: dry-run lines for this step (must write nothing)
    plan: Optional[Callable[[Context], List[str]]] = None
    # optional: lines naming data the step cannot carry (fork policy)
    preflight: Optional[Callable[[Context], List[str]]] = None


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


def unreferenced_env_report(config_dir) -> List[str]:
    """Variables in config/.env that no agent's `env:` references. From 2.0 the
    server no longer hands its environment to agent subprocesses, so an agent
    that relied on an inherited variable must now name it (`NAME: ${NAME}`)."""
    import json
    import re
    config_dir = Path(config_dir)
    env_file = config_dir / ".env"
    if not env_file.is_file():
        return []
    names = []
    for line in env_file.read_text(errors="replace").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            names.append(m.group(1))
    referenced = set()
    try:
        if (config_dir / "agents.yaml").is_file():
            import yaml
            doc = yaml.safe_load((config_dir / "agents.yaml").read_text()) or {}
            agents = list((doc.get("agents") or {}).values())
        elif (config_dir / "agents.json").is_file():
            doc = json.loads((config_dir / "agents.json").read_text())
            agents = list((doc.get("agents") or doc).values())
        else:
            agents = []
    except Exception:
        agents = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        for k, v in (a.get("env") or {}).items():
            referenced.add(str(k))
            referenced.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(v)))
        d = a.get("discord") or {}
        for key in ("token_env", "bot_id_env"):
            if d.get(key):
                referenced.add(d[key])
        for key in ("discord_bot_token_env", "discord_bot_id_env"):
            if a.get(key):
                referenced.add(a[key])
    missing = [n for n in dict.fromkeys(names) if n not in referenced]
    if not missing:
        return []
    return (["env: agent subprocesses no longer inherit the server environment.",
             "  Variables in config/.env that no agent's `env:` references",
             "  (add `NAME: ${NAME}` to an agent's env: if it needs one):"]
            + [f"  - {n}" for n in missing])


MARKER = ".migration-in-progress"


def _recover_interrupted(root: Path, data_dir: Path, config_dir: Path, out) -> bool:
    """A run that was killed (or failed) after its backup left a marker and no
    stamp: put the backup back so this run starts from the original, never from
    half-state. False when the restore itself fails."""
    marker = root / MARKER
    if not marker.is_file():
        return True
    prev = Path(marker.read_text().strip())
    out(f"previous run did not finish; restoring {prev} first")
    try:
        backup_mod.restore(prev, data_dir=data_dir, config_dir=config_dir)
    except backup_mod.BackupError as e:
        out(f"cannot restore {prev}: {e}")
        return False
    marker.unlink(missing_ok=True)
    return True


def run(data_dir, config_dir, backup_root=None, steps=None, dry_run=False,
        force=False, parity_queries=50, out=print, import_from=None,
        keep_bind=None, report_to=None) -> int:
    data_dir, config_dir = Path(data_dir), Path(config_dir)
    steps = load_steps() if steps is None else steps
    log = logging.getLogger("karakos.migrate")
    root = Path(backup_root) if backup_root else data_dir.parent / "backups"

    stamp = guard.read_stamp(data_dir)
    if stamp and stamp["schema"] >= SCHEMA_VERSION:
        out(f"already at schema {stamp['schema']}; nothing to do")
        return EXIT_OK
    if not dry_run and not _recover_interrupted(root, data_dir, config_dir, out):
        return EXIT_STEP_FAILED

    detected = detect_version(data_dir, config_dir)
    out(f"detected: {detected.version} ({detected.layout})")
    for e in detected.evidence:
        out(f"  - {e}")

    if detected.version == "unknown" and any(data_dir.glob("*")):
        for line in unknown_report(detected):
            out(line)
        if not force and not dry_run:
            out("refusing: unknown schema (use --force to proceed)")
            return EXIT_REFUSED
        if dry_run:
            out("dry-run: would refuse without --force")
            return EXIT_OK

    ctx = Context(data_dir, config_dir, None, detected, log, force=force,
                  parity_queries=parity_queries,
                  import_from=Path(import_from) if import_from else None,
                  keep_bind=keep_bind)
    plan = [s for s in steps if s.detect(ctx)]
    out("plan: " + (", ".join(s.name for s in plan) or "no data steps") + ", stamp")
    left_behind = fork.check(data_dir, config_dir)
    for s in plan:
        if s.preflight:
            left_behind += s.preflight(ctx)
    if left_behind:
        out("data this migration does not recognise:")
        for line in left_behind:
            out(f"  - {line}")
        ctx.report["Left behind"] = list(left_behind)
    if dry_run:
        for s in plan:
            if s.plan:
                for line in s.plan(ctx):
                    out(line)
        for line in unreferenced_env_report(config_dir):
            out(line)
        out("memory has no downgrade: the backup is the only way back "
            "(karakos migrate --restore <backup-dir>)")
        if left_behind and not force:
            out("dry-run: would refuse without --force (exit 3)")
        if report_to:
            _write_report(ctx, Path(report_to))
        out("dry-run: nothing written")
        return EXIT_OK
    if left_behind and not force:
        out("refusing: unrecognised data (use --force to proceed; it stays only "
            "in the retained original files, and migration-report.md lists it)")
        return EXIT_REFUSED

    try:
        ctx.backup_dir = backup_mod.backup(data_dir, config_dir, root)
    except backup_mod.BackupError as e:
        out(f"{e}; nothing was changed")
        return EXIT_STEP_FAILED
    out(f"backup: {ctx.backup_dir}")
    # written right after the backup: a killed process runs no handler, so the
    # next start finds this and restores before doing anything else
    (root / MARKER).write_text(str(ctx.backup_dir))

    for s in plan:
        try:
            out(f"step {s.name}: apply")
            s.apply(ctx)
            s.verify(ctx)
        except Exception as e:
            out(f"step {s.name} FAILED: {e}")
            out(f"data is NOT stamped. backup: {ctx.backup_dir}")
            out(f"restore with: python3 -m lib.migrate --to-backup {ctx.backup_dir}")
            out("the next run restores this backup first, then starts again")
            return EXIT_STEP_FAILED

    _write_report(ctx)
    guard.write_stamp(data_dir, migrated_from=_from_version(detected))
    (root / MARKER).unlink(missing_ok=True)
    out(f"migrated to schema {SCHEMA_VERSION}")
    return EXIT_OK


def _write_report(ctx: Context, path: Optional[Path] = None) -> None:
    if not ctx.report:
        return
    out = ["# Migration report", ""]
    for section, lines in ctx.report.items():
        out += [f"## {section}", ""] + list(lines) + [""]
    if path is None:
        path = Path(ctx.data_dir) / "migration-reports" / "migration-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))


def _from_version(d: Detected):
    return d.version if d.version not in ("unknown", "2.0") else None
