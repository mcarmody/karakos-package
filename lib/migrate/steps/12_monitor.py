"""Give the default monitor its 2.0 template (config only, idempotent).

10_registry writes a default monitor whose prompt section was the 1.x relay template; a
dev install may still have that. This step repoints it at agents/templates/monitor.md,
copies that file into the user's agents/templates/ (a host bind mount the image's copy
is not on the path of), and adds the default tool denials, max_turns and timeout when
the entry has none of them. A monitor with its own prompt is left exactly as it is.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

from lib.migrate.runner import Step

_PKG = Path(__file__).resolve().parents[3]
OLD_SECTION = "agents/templates/relay.md"


def _registry():
    lib = str(_PKG / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import registry
    return registry


def _workspace(ctx):
    return Path(ctx.config_dir).parent


def _monitor(ws):
    """(agent_id, entry) of the monitor in config/agents.yaml, or (None, None).

    The runner plans every step before running any, so while 10_registry is still
    pending (agents.json only) this answers from what that step will write."""
    path = ws / "config" / "agents.yaml"
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        else:
            old = json.loads((ws / "config" / "agents.json").read_text())
            data = _registry().legacy_to_registry_dict(old)
    except (OSError, ValueError, yaml.YAMLError, _registry().RegistryError):
        return None, None
    for aid, entry in (data.get("agents") or {}).items():
        if isinstance(entry, dict) and entry.get("role") == "monitor":
            return aid, entry
    return None, None


def _section(entry):
    p = entry.get("prompt") if isinstance(entry, dict) else None
    return p.get("section") if isinstance(p, dict) else None


def _detect(ctx):
    ws = _workspace(ctx)
    aid, entry = _monitor(ws)
    if aid is None:
        return False
    section = _section(entry)
    if section == OLD_SECTION:
        return True
    return section == _registry().MONITOR_TEMPLATE and not (ws / section).is_file()


def _with_defaults(text, aid, entry, reg):
    if any(k in entry for k in ("disallowed_tools", "max_turns", "timeout")):
        return text
    m = re.search(rf"^([ \t]*){re.escape(aid)}:[ \t]*(#.*)?\n", text, re.M)
    if not m:
        return text
    nxt = re.search(r"^([ \t]+)\S", text[m.end():], re.M)
    indent = nxt.group(1) if nxt else m.group(1) + "  "
    add = (f"{indent}max_turns: 10\n{indent}timeout: 300\n"
           f"{indent}disallowed_tools: [{', '.join(reg.MONITOR_DISALLOWED_TOOLS)}]\n")
    return text[:m.end()] + add + text[m.end():]


def _apply(ctx):
    reg = _registry()
    ws = _workspace(ctx)
    aid, entry = _monitor(ws)
    if aid is None:
        return
    dest = ws / reg.MONITOR_TEMPLATE
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_PKG / reg.MONITOR_TEMPLATE, dest)
    if _section(entry) == OLD_SECTION:
        path = ws / "config" / "agents.yaml"
        text = path.read_text()
        if text.count(OLD_SECTION) != 1:
            raise RuntimeError(f"expected one occurrence of {OLD_SECTION} in agents.yaml")
        text = text.replace(OLD_SECTION, reg.MONITOR_TEMPLATE)
        text = _with_defaults(text, aid, entry, reg)
        reg.parse_registry(yaml.safe_load(text), None)     # refuse an invalid result
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    reg.load_registry(ws)


def _verify(ctx):
    reg = _registry()
    ws = _workspace(ctx)
    r = reg.load_registry(ws)
    if len(r.by_role("monitor")) != 1:
        raise RuntimeError("the registry no longer has exactly one monitor")
    aid, entry = _monitor(ws)
    section = _section(entry)
    if section == OLD_SECTION or not (ws / section).is_file():
        raise RuntimeError(f"monitor prompt section {section!r} does not resolve")
    if _detect(ctx):
        raise RuntimeError("12_monitor would still apply")


STEP = Step("12_monitor", 2, 2, detect=_detect, apply=_apply, verify=_verify)
