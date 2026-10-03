#!/usr/bin/env python3
"""Seed a throwaway 1.x install from a git-archive of a release tag and export
it as a tarball. The databases are created by running the tag's own schema code
(tools/tagschema.py); config/, compose, hooks and entrypoint are copied from the
tagged tree. Never reads HOME or any live install.

    seed_fixture.py <tag-tree-dir> <tag> <out.tar.gz>
"""
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tagschema  # noqa: E402

AGENTS = ["alpha", "relay", "builder", "reviewer", "scout"]   # scout: the custom fifth
SESSION_COLS = {"agent", "session_id", "input_tokens", "compaction_count", "last_compacted"}


def agent_entry(name):
    e = {"model": "sonnet", "max_turns": 50, "timeout": 600,
         "system_prompt": f"agents/{name}/SYSTEM_PROMPT.md",
         "tool_streaming": name == "alpha", "stream_to_channel": name == "alpha"}
    if name == "alpha":
        e.update(discord_bot_token_env="DISCORD_BOT_TOKEN_PRIMARY",
                 discord_bot_id_env="DISCORD_BOT_ID_PRIMARY")
    if name == "scout":
        e["model"] = "haiku"        # custom agent, not one of setup.sh's defaults
    return e


def insert(con, table, rows):
    cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    for row in rows:
        row = {k: v for k, v in row.items() if k in cols}
        con.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" * len(row))})',
                    list(row.values()))


def seed_agent_db(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = []
    for i in range(12):               # every status 0..3 for several agents
        a = AGENTS[i % len(AGENTS)]
        rows.append({"agent": a, "channel": "general", "channel_id": "100", "server": "fx",
                     "author": "user", "author_id": "1", "is_bot": 0,
                     "content": f"fixture message {i}", "message_id": f"fx-{i}",
                     "mentions_agent": 1, "processed": i % 4,
                     "response": "done" if i % 4 >= 2 else None,
                     "created_at": f"2026-01-{i + 1:02d} 10:00:00"})
    insert(con, "message_queue", rows)
    insert(con, "sessions", [{"agent": a, "session_id": f"sess-{a}", "input_tokens": 1000 * (i + 1),
                              "compaction_count": i, "last_compacted": None}
                             for i, a in enumerate(AGENTS)])
    insert(con, "cost_events", [{"agent": a, "cost_delta": 0.01 * (i + 1), "session_total": 0.5,
                                 "input_tokens": 10, "output_tokens": 5, "duration_ms": 100.0,
                                 "session_id": f"sess-{a}"} for i, a in enumerate(AGENTS)])
    if "rate_limit_state" in tables:
        insert(con, "rate_limit_state", [
            {"agent": "alpha", "status": "allowed", "rate_limit_type": "five_hour",
             "resets_at": 1900000000, "overage_status": None, "is_using_overage": 0},
            {"agent": "builder", "status": "allowed", "rate_limit_type": "five_hour",
             "resets_at": 1900000100, "overage_status": None, "is_using_overage": 0}])
    if "turn_events" in tables:
        insert(con, "turn_events", [{"message_id": "fx-0", "seq": 0, "kind": "text",
                                     "content": "hello"}])


def seed_memory_db(con):
    insert(con, "episodes", [{"summary": f"episode {i}: the pantry and the garden, note {i}",
                              "importance": 3.0 + (i % 6), "channel": "general",
                              "tags": "home", "agents": "alpha",
                              "created_at": f"2026-01-{(i % 28) + 1:02d}T10:00:00Z"}
                             for i in range(40)])
    insert(con, "facts", [{"subject": f"subject {i}", "content": f"fact content {i}",
                           "confidence": 0.8, "domain": "home"} for i in range(10)])
    insert(con, "patterns", [{"agent": "alpha", "pattern_type": "habit",
                              "content": "checks the calendar first", "confidence": 0.7}])


def main(tree, tag, out):
    tree, out = Path(tree), Path(out)
    work = Path(tempfile.mkdtemp(prefix="karakos-fx-seed-"))
    try:
        root = work / "install"
        (root / "config").mkdir(parents=True)
        for f in (tree / "config").glob("*"):
            if f.is_file() and f.name != ".env.template":
                shutil.copy2(f, root / "config" / f.name)
        if (tree / ".karakos").is_dir():
            shutil.copytree(tree / ".karakos", root / ".karakos")
        # .env from the tag's template with throwaway values, plus one custom variable
        env = []
        for line in (tree / "config" / ".env.template").read_text().splitlines():
            m = re.match(r"#?\s*([A-Z][A-Z0-9_]+)=", line)
            if m and not line.lstrip().startswith("#"):
                env.append(f"{m.group(1)}=fixture-{m.group(1).lower()}")
        env.append("MY_CUSTOM_VAR=kept")
        (root / "config" / ".env").write_text("\n".join(env) + "\n")
        (root / "config" / "agents.json").write_text(
            json.dumps({"agents": {a: agent_entry(a) for a in AGENTS}}, indent=2) + "\n")
        (root / "config" / "channels.json").write_text(json.dumps(
            {"server_id": "fx", "channels": {"general": {"id": "100", "default_agent": "alpha"},
                                             "signals": {"id": "101", "default_agent": None}}},
            indent=2) + "\n")
        (root / "config" / "custom-hook.sh").write_text("#!/bin/sh\n# user hook (fixture)\nexit 0\n")
        for a in AGENTS:
            d = root / "agents" / a
            d.mkdir(parents=True)
            (d / "SYSTEM_PROMPT.md").write_text(f"You are {a} (fixture).\n")
        for sub in ("logs/agent-streams", "inbox/alpha", "data/health", "data/messages"):
            (root / sub).mkdir(parents=True)
        (root / "logs" / "agent-streams" / "alpha.log").write_text("fixture stream\n")
        (root / "inbox" / "alpha" / "note.md").write_text("fixture inbox\n")
        dbs = tagschema.databases(tree)
        for rel, target, seeder in (("bin/agent-server.py", "agent-server.db", seed_agent_db),
                                    ("bin/memory-maintenance.py", "memory.db", seed_memory_db)):
            con = dbs[rel]
            seeder(con)
            con.commit()
            dest = root / "data" / "memory" / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            d = sqlite3.connect(dest)
            con.backup(d)
            d.close()
            con.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            for p in sorted(root.rglob("*")):
                ti = tar.gettarinfo(str(p), arcname=p.relative_to(work).as_posix())
                ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
                if p.is_file():
                    with open(p, "rb") as fh:
                        tar.addfile(ti, fh)
                else:
                    tar.addfile(ti)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
