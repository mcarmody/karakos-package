"""Compose and .env migration for `karakos migrate` (05_layout).

Writes config/docker-compose.yml for 2.0 from lib/migrate/templates, keeping the
user's host-side ports and project name; rewrites renamed env vars; lists the
variables 2.0 does not know. The old files are kept as `.pre-2.0`.
"""
import re
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).parent / "templates" / "docker-compose.yml.tmpl"
SUFFIX = ".pre-2.0"
MARK = "# Karakos 2.0 compose"

# Old name -> new name. Across the nine 1.x tags (docs/migration-inventory.md)
# no variable is renamed, so this is empty; the dashboard auth variables of
# spec 5.2 are added here by that step. Values are carried over unchanged.
ENV_RENAMES: dict = {}

# Variables the 2.0 package itself reads or documents (config/.env.template),
# plus compose-level ones. Anything else is kept and listed, never dropped.
EXTRA_KNOWN = {"KARAKOS_VERSION", "COMPOSE_PROJECT_NAME", "HOME"}

_DEFAULT_PORTS = {"dashboard": ("DASHBOARD_PORT", "3000"),
                  "agent": ("AGENT_SERVER_PORT", "18791")}


def known_env_names() -> set:
    tmpl = Path(__file__).resolve().parents[2] / "config" / ".env.template"
    names = set(EXTRA_KNOWN)
    if tmpl.is_file():
        names |= set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", tmpl.read_text(), re.M))
    return names


def is_migrated(config_dir) -> bool:
    f = Path(config_dir) / "docker-compose.yml"
    return f.is_file() and MARK in f.read_text()


def _host_side(entry: str) -> str:
    """'127.0.0.1:${X:-18791}:18791' -> '127.0.0.1:${X:-18791}'; '3000:3000' -> '3000'."""
    entry = entry.strip().strip('"').strip("'")
    parts = entry.rsplit(":", 1)
    return parts[0] if len(parts) == 2 else entry


def read_old(config_dir) -> dict:
    """{'name': str|None, 'ports': {'dashboard': host, 'agent': host}} from the old
    compose file and .env. Falls back to the 2.0 defaults for what it cannot find."""
    cfg = Path(config_dir)
    text = (cfg / "docker-compose.yml").read_text() if (cfg / "docker-compose.yml").is_file() else ""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        doc = {}
    name = doc.get("name") if isinstance(doc, dict) else None
    if not name:
        env = _env_file(cfg)
        m = env and re.search(r"^\s*COMPOSE_PROJECT_NAME\s*=\s*(\S+)", env.read_text(), re.M)
        name = m.group(1).strip("\"'") if m else None
    ports = {}
    svc = (doc.get("services") or {}) if isinstance(doc, dict) else {}
    for s in svc.values():
        for p in (s or {}).get("ports") or []:
            host = _host_side(str(p))
            if "AGENT_SERVER_PORT" in str(p) or "18791" in str(p):
                ports["agent"] = host
            elif "DASHBOARD_PORT" in str(p) or "3000" in str(p):
                ports["dashboard"] = host
    for key, (var, default) in _DEFAULT_PORTS.items():
        ports.setdefault(key, "127.0.0.1:${%s:-%s}" % (var, default)
                         if key == "agent" else "${%s:-%s}" % (var, default))
    return {"name": name, "ports": ports}


def render(old: dict) -> str:
    tmpl = TEMPLATE.read_text()
    lines = []
    for key in ("dashboard", "agent"):
        var, default = _DEFAULT_PORTS[key]
        lines.append('      - "%s:${%s:-%s}"' % (old["ports"][key], var, default))
    name = f"name: {old['name']}\n" if old.get("name") else ""
    return tmpl.replace("@@NAME@@", name).replace("@@PORTS@@", "\n".join(lines))


def _env_file(config_dir: Path):
    for f in (config_dir / ".env", config_dir.parent / ".env"):
        if f.is_file():
            return f
    return None


def migrate_env(config_dir) -> dict:
    """Rewrite renamed variables in place (original kept as .env.pre-2.0).
    Returns {'renamed': [(old, new)], 'unknown': [names]}."""
    f = _env_file(Path(config_dir))
    if f is None:
        return {"renamed": [], "unknown": []}
    original = f.read_text()
    known = known_env_names()
    renamed, unknown, out = [], [], []
    for line in original.splitlines(keepends=True):
        m = re.match(r"(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=.*)", line, re.S)
        if m and m.group(2) in ENV_RENAMES:
            new = ENV_RENAMES[m.group(2)]
            renamed.append((m.group(2), new))
            line = f"{m.group(1)}{new}{m.group(3)}"
        elif m and m.group(2) not in known and m.group(2) not in unknown:
            unknown.append(m.group(2))
        out.append(line)
    pre = f.with_name(f.name + SUFFIX)
    if not pre.exists():
        pre.write_text(original)
    if renamed:
        f.write_text("".join(out))
    return {"renamed": renamed, "unknown": unknown}


def migrate_compose(config_dir) -> dict:
    """Write the 2.0 compose file; keep the old one as .pre-2.0. Idempotent."""
    cfg = Path(config_dir)
    target = cfg / "docker-compose.yml"
    if is_migrated(cfg):
        return {"written": False}
    old = read_old(cfg)
    if target.is_file():
        pre = target.with_name(target.name + SUFFIX)
        if not pre.exists():
            pre.write_text(target.read_text())
    target.write_text(render(old))
    return {"written": True, **old}


def keep_bind_override(config_dir, host_dir: str) -> Path:
    """docker-compose.override.yml mounting the old host checkout's data, logs
    and inbox at the 2.0 locations (instead of copying them into volumes)."""
    host = str(host_dir).rstrip("/")
    doc = {"services": {"karakos": {"volumes": [
        f"{host}/data:/workspace/data", f"{host}/logs:/workspace/logs",
        f"{host}/inbox:/workspace/inbox"]}}}
    p = Path(config_dir) / "docker-compose.override.yml"
    p.write_text("# written by `karakos migrate --keep-bind`: the old host paths stay in use\n"
                 + yaml.safe_dump(doc, sort_keys=False))
    return p
