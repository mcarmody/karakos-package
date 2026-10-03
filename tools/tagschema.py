"""Run a tag's own schema code: extract its CREATE TABLE statements and
ensure_column() calls from the source tree and execute them in sqlite. Used by
inventory-tag.sh (to list tables/columns) and seed_fixture.py (to build the
fixture databases), so both derive from the tagged code, not from typing."""
import re
import sqlite3
from pathlib import Path


def _balanced(text, i):
    d = 0
    for j in range(i, len(text)):
        d += (text[j] == "(") - (text[j] == ")")
        if d == 0:
            return text[i:j + 1]
    return ""


def databases(root):
    """{relative source path: in-memory connection holding that file's tables}"""
    root = Path(root)
    out = {}
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        if rel.startswith(("tests/", "node_modules/")):
            continue
        src = f.read_text(errors="replace")
        ddl = [(m.group(1), _balanced(src, m.end() - 1))
               for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\(", src)]
        if not ddl:
            continue
        con = sqlite3.connect(":memory:")
        for name, body in ddl:
            try:
                con.execute(f"CREATE TABLE IF NOT EXISTS {name} {body}")
            except sqlite3.Error:
                pass
        for t, c, d in re.findall(
                r"ensure_column\(\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']\s*,\s*[\"']([^\"']+)[\"']", src):
            try:
                con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
            except sqlite3.Error:
                pass
        out[rel] = con
    return out


def tables_of(root):
    res = {}
    for rel, con in databases(root).items():
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            res[f"{rel}:{t}"] = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        con.close()
    return res
