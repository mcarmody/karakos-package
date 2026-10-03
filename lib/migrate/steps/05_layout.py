"""Layout step: get a 1.x install onto the 2.0 container layout.

 - 1.0 bucket (compose bind-mounts the whole checkout): `data/` is already the
   `karakos-data` volume; `logs/` and `inbox/` lived in the checkout. With
   --import-from (old checkout mounted read-only) they are copied into the
   2.0 volumes, never overwriting; with --keep-bind a compose override keeps
   the host paths mounted instead. Nothing is moved or deleted: the runner has
   already taken the backup, and the old checkout is left as it was.
 - every bucket: config/docker-compose.yml is rewritten for 2.0 (ports and
   project name kept) and config/.env is migrated; the originals are kept as
   `.pre-2.0`. Unknown .env variables are kept and listed in the report.
"""
import shutil
from pathlib import Path

from lib.migrate import compose
from lib.migrate.runner import Step

_COPY = ("logs", "inbox")      # siblings of data/ in the container (/workspace/*)


def _workspace(ctx) -> Path:
    return Path(ctx.data_dir).parent


def _detect(ctx) -> bool:
    cfg = Path(ctx.config_dir)
    return ctx.detected.version in ("1.0", "1.3", "1.5") and not compose.is_migrated(cfg) \
        and ((cfg / "docker-compose.yml").is_file() or bool(ctx.import_from or ctx.keep_bind))


def _copy_missing(src: Path, dst: Path) -> int:
    n = 0
    for p in sorted(src.rglob("*")):
        if p.is_file() and not p.is_symlink():
            t = dst / p.relative_to(src)
            if not t.exists():
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, t)
                n += 1
    return n


def _plan(ctx):
    lines = ["layout: config/docker-compose.yml is rewritten for 2.0 (old file kept as "
             "docker-compose.yml.pre-2.0); ports and project name kept"]
    old = compose.read_old(ctx.config_dir)
    lines.append(f"layout: ports kept: dashboard {old['ports']['dashboard']}, "
                 f"agent-server {old['ports']['agent']}; project name: {old['name'] or '(default)'}")
    if ctx.import_from:
        lines.append(f"layout: logs/inbox/data copied (missing files only) from {ctx.import_from}")
    if ctx.keep_bind:
        lines.append(f"layout: compose override keeps {ctx.keep_bind}/{{data,logs,inbox}} mounted")
    return lines


def _apply(ctx):
    rep = ctx.report.setdefault("Layout", [])
    cfg = Path(ctx.config_dir)
    if ctx.import_from and ctx.keep_bind:
        raise RuntimeError("--import-from and --keep-bind are alternatives; pass one")
    if ctx.import_from:
        src = Path(ctx.import_from)
        if not src.is_dir():
            raise RuntimeError(f"--import-from {src} is not a directory")
        ws = _workspace(ctx)
        for name in _COPY:
            if (src / name).is_dir():
                n = _copy_missing(src / name, ws / name)
                rep.append(f"- {name}/: {n} file(s) copied from {src}")
        if (src / "data").is_dir():
            n = _copy_missing(src / "data", Path(ctx.data_dir))
            rep.append(f"- data/: {n} file(s) copied that the volume did not have")
    if ctx.keep_bind:
        p = compose.keep_bind_override(cfg, ctx.keep_bind)
        rep.append(f"- {p.name}: keeps {ctx.keep_bind} mounted for data, logs, inbox")
    if (cfg / "docker-compose.yml").is_file():
        res = compose.migrate_compose(cfg)
        if res.get("written"):
            rep.append("- docker-compose.yml rewritten for 2.0; old file: docker-compose.yml.pre-2.0")
    env = compose.migrate_env(cfg)
    for old, new in env["renamed"]:
        rep.append(f"- .env: {old} renamed to {new}")
    if env["unknown"]:
        ctx.report["Unknown .env variables (kept)"] = [f"- {n}" for n in env["unknown"]]
    if not rep:
        ctx.report.pop("Layout", None)


def _verify(ctx):
    cfg = Path(ctx.config_dir)
    f = cfg / "docker-compose.yml"
    if f.is_file():
        import yaml
        doc = yaml.safe_load(f.read_text())
        vols = doc["services"]["karakos"]["volumes"]
        if any(str(v).startswith("..:/workspace") for v in vols):
            raise RuntimeError("compose still bind-mounts the whole checkout")
        if not (cfg / "docker-compose.yml.pre-2.0").is_file():
            raise RuntimeError("docker-compose.yml.pre-2.0 was not written")


STEP = Step("05_layout", 1, 2, detect=_detect, apply=_apply, verify=_verify, plan=_plan)
