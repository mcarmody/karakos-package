#!/usr/bin/env bash
# Layout inventory of one release tag, as markdown. Every fact is extracted
# from the tagged tree (git archive into a temp dir); nothing is hand typed.
#   tools/inventory-tag.sh v1.5.0 [--json]
# Tables/columns come from running the tag's own CREATE TABLE statements and
# ensure_column() calls in a throwaway in-memory sqlite, then PRAGMA table_info.
set -euo pipefail
TAG="${1:?usage: inventory-tag.sh <tag> [--json]}"
FORMAT="${2:-md}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -C "$REPO" archive "$TAG" | tar -x -C "$TMP"
python3 - "$TMP" "$TAG" "$FORMAT" <<'PY'
import hashlib, json, re, sqlite3, sys
from pathlib import Path
root, tag, fmt = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def read(p):
    f = root / p
    return f.read_text(errors="replace") if f.is_file() else ""

# --- compose volumes / mounts / ports / image
compose = read("config/docker-compose.yml")
vols, ports, image, in_vol = [], [], "build: ..", False
for ln in compose.splitlines():
    s = ln.strip()
    m = re.match(r"image:\s*(\S+)", s)
    if m: image = m.group(1)
    if s.startswith("- ") and ":" in s and not s.startswith("- TZ") and "CMD" not in s:
        (ports if re.match(r'- "[^"]*\$\{.*PORT|- "\d|- "127', s) else vols).append(s[2:].strip('"'))
named = re.findall(r"^  (karakos-[\w-]+):\s*$", compose, re.M)

# --- .env names
env = sorted(set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", read("config/.env.template"), re.M)))
kv = "KARAKOS_VERSION" in compose or "KARAKOS_VERSION" in "".join(env)

# --- config files, hooks, settings
cfg = sorted(p.name for p in (root / "config").glob("*") if p.is_file())
hooks = sorted(p.relative_to(root).as_posix() for p in (root / "system" / "hooks").glob("*")) \
    if (root / "system" / "hooks").is_dir() else []
settings = [n for n in ("config/claude-settings.json", "config/hooks.json",
                        "system/install-hooks.sh", "system/check-protected-paths.py")
            if (root / n).is_file()]
karakos_dir = sorted(p.name for p in (root / ".karakos").glob("*")) if (root / ".karakos").is_dir() else []

# --- entrypoint
ep = read("bin/entrypoint.sh")
ep_facts = {"sha256": hashlib.sha256(ep.encode()).hexdigest()[:12],
            "exec": next((l.strip() for l in ep.splitlines() if l.startswith("exec ")), ""),
            "mkdirs": sorted(set(re.findall(r'"\$WORKSPACE_ROOT/([\w/-]+)"', ep)))}

# --- tables (execute the tag's own DDL; per source file => per database)
def balanced(text, i):
    d = 0
    for j in range(i, len(text)):
        d += (text[j] == "(") - (text[j] == ")")
        if d == 0:
            return text[i:j + 1]
    return ""

tables = {}
for f in sorted(root.rglob("*.py")):
    rel = f.relative_to(root).as_posix()
    if rel.startswith(("tests/", "node_modules/")): continue
    src = f.read_text(errors="replace")
    ddl = [(m.group(1), balanced(src, m.end() - 1))
           for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", src)]
    ddl += [(m.group(1), balanced(src, m.end() - 1))
            for m in re.finditer(r"CREATE TABLE\s+(?!IF)(\w+)\s*\(", src)]
    if not ddl: continue
    con = sqlite3.connect(":memory:")
    for name, body in ddl:
        try: con.execute(f"CREATE TABLE IF NOT EXISTS {name} {body}")
        except sqlite3.Error: pass
    for t, c, d in re.findall(r"ensure_column\(\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']\s*,\s*[\"']([^\"']+)[\"']", src):
        try: con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
        except sqlite3.Error: pass
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        tables[f"{rel}:{t}"] = cols
    con.close()

# --- agents.json / channels.json shape as written by setup.sh
setup = read("setup.sh")
agent_keys = sorted(set(re.findall(r'^\s+"(\w+)":', setup[setup.find("Create agents.json"):setup.find("Create channels.json")], re.M))) \
    if "Create agents.json" in setup else []
chan_keys = sorted(set(re.findall(r'\\"(\w+)\\":', setup[setup.find("Create channels.json"):][:900])))

facts = {"tag": tag, "image": image, "mounts": vols, "ports": ports, "named_volumes": named,
         "env": env, "karakos_version_var": kv, "config_files": cfg, ".karakos": karakos_dir,
         "hooks": hooks, "settings": settings, "entrypoint": ep_facts, "tables": tables,
         "agents_json_keys": agent_keys, "channels_json_keys": chan_keys}
if fmt == "--json":
    print(json.dumps(facts, indent=1, sort_keys=True)); sys.exit(0)
print(f"### {tag}\n")
rows = [("image", image), ("mounts", "<br>".join(f"`{v}`" for v in vols)),
        ("ports", "<br>".join(f"`{v}`" for v in ports)),
        ("named volumes", ", ".join(named)),
        ("KARAKOS_VERSION", "yes" if kv else "no"),
        (".env names", ", ".join(env)),
        ("config/ files", ", ".join(cfg)), (".karakos/", ", ".join(karakos_dir) or "absent"),
        ("hooks", ", ".join(hooks) or "none"), ("settings", ", ".join(settings)),
        ("entrypoint", f"sha {ep_facts['sha256']}; `{ep_facts['exec']}`; mkdir {', '.join(ep_facts['mkdirs'])}"),
        ("agents.json keys", ", ".join(agent_keys)), ("channels.json keys", ", ".join(chan_keys))]
for t, c in sorted(tables.items()):
    rows.append((f"table {t}", ", ".join(c)))
print("| fact | value |\n|---|---|")
for k, v in rows: print(f"| {k} | {v} |")
PY
