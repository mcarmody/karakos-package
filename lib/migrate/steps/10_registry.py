"""1.x config -> schema 2: agents.json becomes agents.yaml (lib/registry.py),
and the hooks section of claude-settings.json is synced (bin/hooks-sync.py),
because boot no longer touches 1.x data.

Runs after the runner's backup. Idempotent: detect is false once agents.yaml exists.
"""
import importlib.util
import json
import sys
from pathlib import Path

from lib.migrate.runner import Step

_PKG = Path(__file__).resolve().parents[3]


def _registry():
    lib = str(_PKG / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import registry
    return registry


def _workspace(ctx):
    return Path(ctx.config_dir).parent


def _detect(ctx):
    cfg = Path(ctx.config_dir)
    return (cfg / "agents.json").is_file() and not (cfg / "agents.yaml").exists()


def _hooks_sync(workspace):
    path = _PKG / "bin" / "hooks-sync.py"
    spec = importlib.util.spec_from_file_location("hooks_sync_migrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.sync(workspace)
    except SystemExit as e:           # hooks-sync signals refusal via SystemExit
        raise RuntimeError(str(e))


def _apply(ctx):
    ws = _workspace(ctx)
    _registry().migrate_legacy(ws)
    _hooks_sync(ws)


def _verify(ctx):
    reg_mod = _registry()
    ws = _workspace(ctx)
    cfg = Path(ctx.config_dir)
    pre = cfg / ("agents.json" + reg_mod.LEGACY_BACKUP_SUFFIX)
    if not pre.is_file():
        raise RuntimeError("agents.json.pre-2.0 was not written")
    old = json.loads(pre.read_text()).get("agents", {})
    reg = reg_mod.load_registry(ws)
    new = reg.legacy_view()["agents"]
    import yaml
    raw = yaml.safe_load((cfg / "agents.yaml").read_text())["agents"]
    expressible = set(reg_mod._LEGACY_PASSTHROUGH) | {
        "discord_bot_token_env", "discord_bot_id_env"}
    for aid, entry in old.items():
        if aid not in new:
            raise RuntimeError(f"agent '{aid}' missing after migration")
        for key, val in (entry or {}).items():
            if key in expressible:
                if new[aid].get(key) != val:
                    raise RuntimeError(
                        f"agent '{aid}': '{key}' changed ({val!r} -> {new[aid].get(key)!r})")
            elif key not in raw[aid]:      # unknown key must at least be carried over
                raise RuntimeError(f"agent '{aid}': key '{key}' was dropped")
        extra = set(new[aid]) - set(entry or {})
        if extra:
            raise RuntimeError(f"agent '{aid}': unexpected keys {sorted(extra)}")
    settings = cfg / "claude-settings.json"
    if settings.is_file() and "hooks" not in json.loads(settings.read_text()):
        raise RuntimeError("hooks section missing after hooks-sync")


STEP = Step("10_registry", 1, 2, detect=_detect, apply=_apply, verify=_verify)
