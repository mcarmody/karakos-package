"""memory.db to graph (spec 4.4). One way, no downgrade; the backup is the way back.

This module is the only code that opens memory.db. Layout:
  detect  -> legacy memory.db present and unmigrated / nothing present / interrupted
  plan    -> dry-run lines (writes nothing)
  apply   -> build data/memory/graph.db.tmp from memory.db (read-only) + candidates
  verify  -> counts, fidelity, integrity, recall parity on the staging file, and
             only then cutover: os.replace to graph.db, memory.db to memory.db.migrated
Any failure removes the staging file and leaves memory.db untouched.
"""
import hashlib
import importlib
import json
import math
import os
import random
import re
import sqlite3
from pathlib import Path

from lib.graph import embed
from lib.graph.schema import ensure_schema
from lib.graph.store import GraphStore, norm, now_iso
from lib.migrate import backup as backup_mod
from lib.migrate.detect import ro_uri
from lib.migrate.runner import Step

legacy = importlib.import_module("lib.migrate.steps._legacy_memory")

LEGACY_MODEL = "BAAI/bge-small-en-v1.5"   # what every 1.x install embedded with
FOREIGN_NOTE = "memory migration is one way; restore from the backup to go back"
PARITY_K = 5
PARITY_FILE = "memory-parity.jsonl"
OPERATOR_QUERIES = "memory-parity-queries.txt"
TABLES = ("episodes", "facts", "patterns")
KNOWN_COLUMNS = {
    "episodes": {"id", "summary", "importance", "base_importance", "channel", "tags",
                 "agents", "created_at", "inserted_at", "consolidated_at", "embedding"},
    "facts": {"id", "subject", "content", "confidence", "domain", "created_at",
              "updated_at"},
    "patterns": {"id", "agent", "pattern_type", "content", "confidence",
                 "reinforcement_count", "created_at", "updated_at"},
}
_IGNORED_TABLES = {"sqlite_sequence"}
_CANDIDATE = re.compile(r"^\s*-\s+\*\*(.+?):\*\*\s*(.+?)\s*$")


class MemoryMigrationError(Exception):
    pass


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------
def legacy_path(ctx) -> Path:
    data = Path(ctx.data_dir)
    for p in (data / "memory" / "memory.db", data / "memory.db"):
        if p.is_file():
            return p
    return data / "memory" / "memory.db"


def graph_file(ctx) -> Path:
    return Path(ctx.data_dir) / "memory" / "graph.db"


def staging_file(ctx) -> Path:
    return Path(ctx.data_dir) / "memory" / "graph.db.tmp"


def _migrated_name(p: Path) -> Path:
    return p.with_name(p.name + ".migrated")


def _sidecars(p: Path):
    return [Path(str(p) + s) for s in ("-wal", "-shm")]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro(path: Path):
    con = sqlite3.connect(ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------
# reading the legacy database
# --------------------------------------------------------------------------
def _schema(con):
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
    return out


def _legacy_tables(path: Path):
    """{table: columns}; None when the file cannot be read as a database."""
    try:
        con = _ro(path)
        try:
            return _schema(con)
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _migration_meta(gpath: Path):
    """meta.migration as a dict, or None (no graph, no row, unreadable)."""
    if not gpath.is_file():
        return None
    try:
        con = _ro(gpath)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='migration'").fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _rows(con, table, cols):
    have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    if not have:
        return []
    sel = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
    return con.execute(f"SELECT {sel} FROM {table} ORDER BY id").fetchall()


EP_COLS = ("id", "summary", "importance", "base_importance", "channel", "tags", "agents",
           "created_at", "inserted_at", "consolidated_at", "embedding")
FACT_COLS = ("id", "subject", "content", "confidence", "domain", "created_at", "updated_at")
PAT_COLS = ("id", "agent", "pattern_type", "content", "confidence", "reinforcement_count",
            "created_at", "updated_at")


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def parse_agents(v):
    if _blank(v):
        return []
    s = str(v).strip()
    names = None
    if s.startswith("["):
        try:
            j = json.loads(s)
            if isinstance(j, list):
                names = [str(x) for x in j]
        except ValueError:
            pass
    if names is None:
        names = s.split(",")
    out = []
    for n in names:
        n = n.strip()
        if n and norm(n) not in {norm(x) for x in out}:
            out.append(n)
    return out


def read_candidates(data_dir):
    """[(file name, line no, subject, text)] from data/memory-candidates/*.md."""
    d = Path(data_dir) / "memory-candidates"
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = _CANDIDATE.match(line)
            if m and norm(m.group(1)) and norm(m.group(2)):
                out.append((f.name, i, m.group(1).strip(), m.group(2).strip()))
    return out


def read_source(lpath: Path, data_dir) -> dict:
    """Everything the migration needs from memory.db, with skips decided once."""
    con = _ro(lpath)
    try:
        schema = _schema(con)
        src = {"schema": schema, "episodes": [], "facts": [], "patterns": [],
               "skipped": {"episodes": [], "facts": [], "patterns": []},
               "totals": {}}
        for table, cols, text in (("episodes", EP_COLS, "summary"),
                                  ("facts", FACT_COLS, "content"),
                                  ("patterns", PAT_COLS, "content")):
            rows = _rows(con, table, cols) if table in schema else []
            src["totals"][table] = len(rows)
            for r in rows:
                (src["skipped"][table] if _blank(r[text]) else src[table]).append(r)
    finally:
        con.close()
    # candidates, deduplicated against migrated facts (normalised subject + text)
    seen = {(norm(r["subject"]), norm(r["content"])) for r in src["facts"]}
    cands = []
    for fname, line, subject, text in read_candidates(data_dir):
        key = (norm(subject), norm(text))
        if key in seen:
            continue
        seen.add(key)
        cands.append((fname, line, subject, text))
    src["candidates"] = cands
    return src


def _is_foreign(blob) -> bool:
    return blob is not None and len(bytes(blob)) != embed.DIM * 4


def _model_changed() -> bool:
    return embed.MODEL_NAME != LEGACY_MODEL


def _stats(src) -> dict:
    eps = src["episodes"]
    with_emb = [r for r in eps if r["embedding"] is not None]
    foreign = [r for r in with_emb if _is_foreign(r["embedding"])]
    subjects = {norm(r["subject"]) for r in src["facts"] if norm(r["subject"])}
    subjects |= {norm(s) for _, _, s, _ in src["candidates"]}
    agents = {}
    for r in eps:
        for a in parse_agents(r["agents"]):
            agents.setdefault(norm(a), a)
    for r in src["patterns"]:
        if not _blank(r["agent"]):
            agents.setdefault(norm(r["agent"]), str(r["agent"]).strip())
    return {"episodes": src["totals"]["episodes"], "facts": src["totals"]["facts"],
            "patterns": src["totals"]["patterns"], "candidates": len(src["candidates"]),
            "with_embedding": len(with_emb) - len(foreign), "foreign": len(foreign),
            "without_embedding": len(eps) - len(with_emb),
            "subjects": len(subjects), "agents": len(agents),
            "skipped": {t: [r["id"] for r in src["skipped"][t]] for t in TABLES}}


# --------------------------------------------------------------------------
# detect / preflight / plan
# --------------------------------------------------------------------------
def _case(ctx):
    """'migrate' | 'empty' | 'finish' | None."""
    lp, gp = legacy_path(ctx), graph_file(ctx)
    mig = _migration_meta(gp)
    if lp.is_file():
        tables = _legacy_tables(lp)
        if mig is not None:
            return "finish" if mig.get("source_sha256") == _sha256(lp) else None
        if tables is None or any(t in tables for t in TABLES):
            return "migrate"   # unreadable: fail loudly in apply, never skip silently
        return None if gp.is_file() else "empty"
    if gp.is_file():
        return None
    return "empty"


def detect(ctx) -> bool:
    return _case(ctx) is not None


def preflight(ctx):
    """Fork policy: tables or columns this migration does not map."""
    lp = legacy_path(ctx)
    if _case(ctx) != "migrate":
        return []
    schema = _legacy_tables(lp) or {}
    out = []
    for t in sorted(schema):
        if t in _IGNORED_TABLES or t.startswith("sqlite_"):
            continue
        if t not in KNOWN_COLUMNS:
            out.append(f"memory.db: extra table {t} ({len(schema[t])} columns)")
            continue
        for c in sorted(schema[t] - KNOWN_COLUMNS[t]):
            out.append(f"memory.db: extra column {t}.{c}")
    return out


def _plan_lines(ctx, src, case):
    s = _stats(src)
    skipped = sum(len(v) for v in s["skipped"].values())
    est = sum(len(str(r["summary"])) for r in src["episodes"]) + \
        sum(len(str(r["content"])) for r in src["facts"] + src["patterns"]) + \
        s["with_embedding"] * embed.DIM * 4
    n = s["episodes"] + s["facts"] + s["patterns"] + s["candidates"] - skipped
    return [
        f"memory: {s['episodes']} episodes, {s['facts']} facts, {s['patterns']} patterns, "
        f"{s['candidates']} candidate lines (after dedup)",
        f"memory: embeddings: {s['with_embedding']} carried, {s['without_embedding']} none, "
        f"{s['foreign']} foreign length (stored NULL for 4.3 to re-embed)",
        f"memory: skipped as malformed (empty text): {skipped}"
        + (" " + json.dumps(s["skipped"]) if skipped else ""),
        f"memory: entities to create: {s['subjects']} fact subjects, {s['agents']} agents",
        f"memory: estimated graph: {n} observations, about {est // 1024 + 1} KiB of content",
        "memory: embedding model available now: "
        + ("yes (parity runs in hybrid mode)" if embed.embedder_available()
           else "no (parity runs in keyword mode)"),
        FOREIGN_NOTE,
    ]


def plan(ctx):
    case = _case(ctx)
    if case == "empty":
        return ["memory: no memory.db; will create an empty graph", FOREIGN_NOTE]
    if case == "finish":
        return ["memory: interrupted migration found; will re-verify and rename "
                "memory.db to memory.db.migrated"]
    try:
        src = read_source(legacy_path(ctx), ctx.data_dir)
    except sqlite3.Error as e:
        return [f"memory: memory.db is unreadable ({e}); migration would fail"]
    return _plan_lines(ctx, src, case)


# --------------------------------------------------------------------------
# precondition: the runner's backup covers memory.db
# --------------------------------------------------------------------------
def _check_backup(ctx, lp: Path):
    if ctx.backup_dir is None:
        raise MemoryMigrationError("no backup exists; refusing to touch memory.db")
    try:
        m = json.loads((Path(ctx.backup_dir) / backup_mod.MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise MemoryMigrationError(f"backup manifest unreadable ({e}); refusing") from e
    rel = "data/" + lp.relative_to(Path(ctx.data_dir)).as_posix()
    ent = [e for e in m.get("files", []) if e.get("path") == rel]
    if not ent or not ent[0].get("sha256"):
        raise MemoryMigrationError(
            f"backup manifest does not list {rel} with a sha256; refusing to migrate")


# --------------------------------------------------------------------------
# apply: build the staging graph
# --------------------------------------------------------------------------
def _discard_staging(ctx):
    sp = staging_file(ctx)
    for p in (sp, *_sidecars(sp)):
        p.unlink(missing_ok=True)


def _entity(con, cache, name, kind, ts):
    n = norm(name)
    key = (n, kind)
    if key not in cache:
        cur = con.execute(
            "INSERT INTO entities(name, name_norm, kind, created_at, updated_at, "
            "last_seen_at) VALUES (?,?,?,?,?,?)", (str(name).strip(), n, kind, ts, ts, ts))
        cache[key] = cur.lastrowid
    return cache[key]


_OBS = ("INSERT INTO observations(kind, subkind, content, entity_id, importance, "
        "base_importance, confidence, domain, agent, channel, tags, reinforcement_count, "
        "source, legacy_ref, created_at, inserted_at, updated_at, consolidated_at, "
        "embedding, embed_model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_staging(ctx, src, lpath: Path, stats: dict) -> dict:
    sp = staging_file(ctx)
    sp.parent.mkdir(parents=True, exist_ok=True)
    _discard_staging(ctx)
    ts = now_iso()
    changed = _model_changed()
    con = sqlite3.connect(str(sp), isolation_level=None)
    con.row_factory = sqlite3.Row
    cache = {}
    carried = 0
    try:
        ensure_schema(con)
        for k, v in (("embed_model", embed.MODEL_NAME), ("embed_dim", str(embed.DIM))):
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (k, v))

        con.execute("BEGIN")
        for r in src["episodes"]:
            agents = parse_agents(r["agents"])
            blob, model = None, None
            if r["embedding"] is not None and not _is_foreign(r["embedding"]) \
                    and not changed:
                blob, model = bytes(r["embedding"]), embed.MODEL_NAME
                carried += 1
            imp = r["importance"]
            base = r["base_importance"] if r["base_importance"] is not None else imp
            cur = con.execute(_OBS, (
                "episode", None, r["summary"], None, imp, base, None, None,
                agents[0] if agents else None, r["channel"], r["tags"], 1, "migrated",
                f"episodes:{r['id']}", r["created_at"], r["inserted_at"] or ts,
                r["created_at"], r["consolidated_at"], blob, model))
            for a in agents:
                eid = _entity(con, cache, a, "agent", ts)
                con.execute("INSERT OR IGNORE INTO observation_mentions"
                            "(observation_id, entity_id) VALUES (?,?)", (cur.lastrowid, eid))
        con.execute("COMMIT")

        con.execute("BEGIN")
        for r in src["facts"]:
            eid = None if _blank(r["subject"]) else _entity(con, cache, r["subject"], "topic", ts)
            conf = _num(r["confidence"])
            imp = (conf if conf is not None else 0.8) * 10
            con.execute(_OBS, ("fact", None, r["content"], eid, imp, imp, conf, r["domain"],
                               None, None, None, 1, "migrated", f"facts:{r['id']}",
                               r["created_at"], r["created_at"] or ts,
                               r["updated_at"] or r["created_at"], None, None, None))
        con.execute("COMMIT")

        con.execute("BEGIN")
        for r in src["patterns"]:
            eid = None if _blank(r["agent"]) else _entity(con, cache, r["agent"], "agent", ts)
            conf = _num(r["confidence"], 0.7)
            rc = int(_num(r["reinforcement_count"], 1) or 1)
            imp = min(10.0, conf * 10 + math.log2(max(rc, 1)))
            con.execute(_OBS, ("pattern", r["pattern_type"], r["content"], eid, imp, imp,
                               conf, None, None if _blank(r["agent"]) else str(r["agent"]).strip(),
                               None, None, rc, "migrated", f"patterns:{r['id']}",
                               r["created_at"], r["created_at"] or ts,
                               r["updated_at"] or r["created_at"], None, None, None))
        con.execute("COMMIT")

        con.execute("BEGIN")
        for fname, line, subject, text in src["candidates"]:
            eid = _entity(con, cache, subject, "topic", ts)
            con.execute(_OBS, ("fact", None, text, eid, 5.0, 5.0, 0.5, None, None, None,
                               None, 1, "candidate", f"candidates:{fname}:{line}",
                               ts, ts, ts, None, None, None))
        con.execute("COMMIT")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return {"embeddings_carried": carried, "foreign_embeddings": stats["foreign"],
            "migrated_at": ts}


def apply(ctx) -> None:
    ctx._memory = None
    case = _case(ctx)
    if case == "finish":
        ctx._memory = {"case": "finish"}
        return
    if case == "empty":
        ctx._memory = {"case": "empty"}
        return
    lp = legacy_path(ctx)
    try:
        _check_backup(ctx, lp)
        if _migration_meta(graph_file(ctx)) is None and graph_file(ctx).is_file():
            gc = sqlite3.connect(ro_uri(graph_file(ctx)), uri=True)
            try:
                try:
                    n = gc.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                except sqlite3.Error:
                    n = 0
            finally:
                gc.close()
            if n:
                raise MemoryMigrationError(
                    f"graph.db already holds {n} observations and no migration record; "
                    "refusing to replace it")
        if _legacy_tables(lp) is None:
            raise MemoryMigrationError(f"{lp.name} is not a readable database")
        src = read_source(lp, ctx.data_dir)
        stats = _stats(src)
        info = build_staging(ctx, src, lp, stats)
        ctx._memory = {"case": "migrate", "stats": stats, "info": info, "src": src,
                       "source_sha256": None}
    except BaseException:
        _discard_staging(ctx)
        raise


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------
def _fail(msg):
    raise MemoryMigrationError(msg)


def verify_counts(gcon, src, stats):
    skipped = {t: len(src["skipped"][t]) for t in TABLES}
    want = {"episode": stats["episodes"] - skipped["episodes"],
            "fact": stats["facts"] - skipped["facts"] + stats["candidates"],
            "pattern": stats["patterns"] - skipped["patterns"]}
    got = {r[0]: r[1] for r in gcon.execute(
        "SELECT kind, COUNT(*) FROM observations GROUP BY kind")}
    for k, w in want.items():
        if got.get(k, 0) != w:
            _fail(f"count mismatch for {k}: graph has {got.get(k, 0)}, expected {w} "
                  f"(source rows minus skipped plus candidates)")
    total = sum(want.values())
    fts = gcon.execute("SELECT COUNT(*) FROM observations_fts_docsize").fetchone()[0]
    if fts != total:
        _fail(f"observations_fts has {fts} rows, observations has {total}")
    try:
        gcon.execute("INSERT INTO observations_fts(observations_fts) VALUES('integrity-check')")
    except sqlite3.Error as e:
        _fail(f"observations_fts integrity-check failed: {e}")
    ents = gcon.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if ents < stats["subjects"] + stats["agents"]:
        _fail(f"entities: graph has {ents}, expected at least "
              f"{stats['subjects']} subjects + {stats['agents']} agents")


def verify_fidelity(gcon, src, changed):
    exp = []
    for r in src["episodes"]:
        base = r["base_importance"] if r["base_importance"] is not None else r["importance"]
        exp.append((f"episodes:{r['id']}", r["summary"], r["importance"], base,
                    r["created_at"]))
    got = [tuple(r) for r in gcon.execute(
        "SELECT legacy_ref, content, importance, base_importance, created_at "
        "FROM observations WHERE kind='episode' AND legacy_ref LIKE 'episodes:%' "
        "ORDER BY CAST(substr(legacy_ref, 10) AS INTEGER)")]
    h = lambda rows: hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()  # noqa: E731
    if h(exp) != h(got):
        diff = next((i for i, (a, b) in enumerate(zip(exp, got)) if a != b), None)
        _fail(f"episode fidelity hash mismatch (source {len(exp)} rows, graph {len(got)}); "
              f"first difference at index {diff}: "
              f"{exp[diff] if diff is not None else '-'} vs {got[diff] if diff is not None else '-'}")
    si = sum(float(r[2]) for r in exp if r[2] is not None)
    gi = sum(float(r[2]) for r in got if r[2] is not None)
    if abs(si - gi) > 1e-9:
        _fail(f"episode importance sum differs: source {si!r}, graph {gi!r}")

    want = [] if changed else [bytes(r["embedding"]) for r in src["episodes"]
                               if r["embedding"] is not None and not _is_foreign(r["embedding"])]
    got_b = [bytes(r[0]) for r in gcon.execute(
        "SELECT embedding FROM observations WHERE kind='episode' AND embedding IS NOT NULL "
        "AND legacy_ref LIKE 'episodes:%' "
        "ORDER BY CAST(substr(legacy_ref, 10) AS INTEGER)")]
    hb = lambda bs: hashlib.sha256(b"".join(bs)).hexdigest()  # noqa: E731
    if len(want) != len(got_b) or hb(want) != hb(got_b):
        _fail(f"embedding blobs differ: source carries {len(want)}, graph has {len(got_b)} "
              "(sha256 over blobs ordered by legacy id)")
    empty = gcon.execute("SELECT COUNT(*) FROM observations WHERE content IS NULL "
                         "OR TRIM(content) = ''").fetchone()[0]
    if empty:
        _fail(f"{empty} observations have empty content")


def verify_integrity(gcon):
    res = [r[0] for r in gcon.execute("PRAGMA integrity_check")]
    if res != ["ok"]:
        _fail(f"PRAGMA integrity_check: {res[:3]}")
    fk = gcon.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        _fail(f"PRAGMA foreign_key_check: {len(fk)} violations, first {tuple(fk[0])}")


def _queries(ctx, src, n):
    """Deterministic: Random(0) over episodes then facts sorted by id, plus the
    operator file."""
    rows = [str(r["summary"]) for r in sorted(src["episodes"], key=lambda r: r["id"])]
    rows += [str(r["content"]) for r in sorted(src["facts"], key=lambda r: r["id"])]
    rows = [t for t in rows if t.split()]
    rng = random.Random(0)
    qs = [legacy.query_from(rng.choice(rows), rng) for _ in range(n)] if rows else []
    f = Path(ctx.config_dir) / OPERATOR_QUERIES
    if n > 0 and f.is_file():
        qs += [l.strip() for l in f.read_text(errors="replace").splitlines() if l.strip()]
    return qs


def run_parity(ctx, src, lpath: Path, gpath: Path, n=None) -> dict:
    """Gate A (hard) and Gate B (logged) over seeded queries. Raises on Gate A."""
    n = ctx.parity_queries if n is None else n
    if n <= 0:
        ctx.report.setdefault("Memory", []).append(
            "WARNING: recall-parity check disabled (--parity-queries 0); "
            "nothing proved that recall still works")
        ctx.log.warning("memory recall-parity check disabled")
        return {"queries": 0, "mode": "disabled", "gate_a_pass": 0,
                "overlap_mean": None, "top1_mean": None}
    changed = _model_changed()
    mode = "hybrid" if (not changed and embed.embedder_available(probe=True)) else "keyword"
    queries = _queries(ctx, src, n)
    store = GraphStore(gpath)
    lcon = _ro(lpath)
    saved = os.environ.get("KARAKOS_SEMANTIC_RECALL")
    if mode == "keyword" and embed._enabled():
        os.environ["KARAKOS_SEMANTIC_RECALL"] = "0"   # both sides keyword
    results = []
    try:
        with store.read() as g:
            refs = {r[0]: r[1] for r in g.execute(
                "SELECT id, legacy_ref FROM observations WHERE legacy_ref IS NOT NULL")}
            frefs = {r[0]: r[1] for r in g.execute(
                "SELECT o.id, o.legacy_ref FROM observations o WHERE o.kind='fact' "
                "AND o.legacy_ref LIKE 'facts:%'")}
        from lib.graph.recall import legacy_blend
        for q in queries:
            old = legacy.recall_episodes(lcon, q, PARITY_K, semantic=(mode == "hybrid"))
            old_refs = [f"episodes:{e['id']}" for e in old["episodes"]]
            nl = legacy_blend(store, q, PARITY_K)
            nl_refs = [refs.get(e["id"]) for e in nl["episodes"]]
            nd = store.recall(q, limit=PARITY_K, kinds=["episode"])
            nd_refs = [refs.get(r["id"]) for r in nd["results"]]
            old_facts = {f"facts:{i}" for i in legacy.facts_like(lcon, q)}
            like = "%" + q.strip() + "%"
            with store.read() as g:
                new_facts = {r[0] for r in g.execute(
                    "SELECT o.legacy_ref FROM observations o LEFT JOIN entities e "
                    "ON e.id=o.entity_id WHERE o.kind='fact' AND o.legacy_ref LIKE 'facts:%' "
                    "AND (o.content LIKE ? OR e.name LIKE ?)", (like, like))}
            a_ok = old_refs == nl_refs and old_facts == new_facts
            overlap = (len(set(old_refs) & set(nd_refs)) / len(old_refs)) if old_refs else \
                (1.0 if not nd_refs else 0.0)
            results.append({
                "query": q, "mode": mode, "old_top_k": old_refs,
                "new_legacy_top_k": nl_refs, "new_default_top_k": nd_refs,
                "gate_a_ok": a_ok, "overlap": round(overlap, 4),
                "top1_same": (old_refs[:1] == nd_refs[:1]),
                "dropped": [r for r in old_refs if r not in nd_refs],
                "added": [r for r in nd_refs if r not in old_refs],
                "facts_ok": old_facts == new_facts})
    finally:
        lcon.close()
        if saved is None:
            os.environ.pop("KARAKOS_SEMANTIC_RECALL", None)
        else:
            os.environ["KARAKOS_SEMANTIC_RECALL"] = saved

    rdir = Path(ctx.data_dir) / "migration-reports"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / PARITY_FILE).write_text("".join(json.dumps(r) + "\n" for r in results))
    passed = sum(1 for r in results if r["gate_a_ok"])
    summary = {"queries": len(results), "mode": mode, "gate_a_pass": passed,
               "overlap_mean": round(sum(r["overlap"] for r in results) / len(results), 4),
               "top1_mean": round(sum(1 for r in results if r["top1_same"]) / len(results), 4)}
    diffs = [r for r in results if r["dropped"] or r["added"] or not r["top1_same"]]
    lines = ["### Recall parity", "",
             "| queries run | mode | Gate A pass | Gate B mean overlap | Gate B top-1 agreement "
             "| queries with differences |", "|---|---|---|---|---|---|",
             f"| {summary['queries']} | {mode} | {passed}/{len(results)} | "
             f"{summary['overlap_mean']} | {summary['top1_mean']} | {len(diffs)} |", "",
             f"Every query is in data/migration-reports/{PARITY_FILE}."]
    if diffs:
        lines += ["", "First differences (Gate B, logged, never failing):"]
        for r in diffs[:20]:
            lines.append(f"- `{r['query']}` dropped {r['dropped']} added {r['added']}")
    ctx.report.setdefault("Memory", []).extend([""] + lines)
    if passed != len(results):
        bad = [r for r in results if not r["gate_a_ok"]][:3]
        _fail(f"recall parity Gate A failed: {passed}/{len(results)} queries identical; "
              f"first failures: " + "; ".join(
                  f"{r['query']!r} old={r['old_top_k']} new={r['new_legacy_top_k']} "
                  f"facts_ok={r['facts_ok']}" for r in bad))
    return summary


def _record(gcon, ctx, lp, src, stats, info, parity):
    changed = _model_changed()
    has_emb = stats["with_embedding"] + stats["foreign"] > 0
    rec = {
        "source_sha256": info["source_sha256"],
        "source_path": lp.relative_to(Path(ctx.data_dir)).as_posix(),
        "migrated_at": info["migrated_at"],
        "package_from": ctx.detected.version,
        "episodes": stats["episodes"], "facts": stats["facts"],
        "patterns": stats["patterns"], "candidates": stats["candidates"],
        "skipped": stats["skipped"],
        "embeddings_carried": 0 if changed else info["embeddings_carried"],
        "foreign_embeddings": stats["foreign"],
        "embedding_model_before": LEGACY_MODEL if has_emb else "unknown",
        "embedding_model_after": embed.MODEL_NAME,
        "embedding_model_changed": changed,
        "reembedded": 0,
        "parity": {k: parity[k] for k in ("queries", "mode", "gate_a_pass",
                                          "overlap_mean", "top1_mean")},
        "note": "the migrator never calls the embedding model to write; 4.3 re-embeds "
                "rows with NULL embeddings",
    }
    gcon.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('migration', ?)",
                 (json.dumps(rec),))
    return rec


def _checkpoint_legacy(lp: Path):
    if any(p.exists() and p.stat().st_size for p in _sidecars(lp)[:1]):
        con = sqlite3.connect(str(lp))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()


def _swap_in(ctx):
    sp, gp = staging_file(ctx), graph_file(ctx)
    for p in _sidecars(gp):
        p.unlink(missing_ok=True)
    os.replace(sp, gp)


def _rename_legacy(lp: Path):
    dst = _migrated_name(lp)
    for side in _sidecars(lp):
        if side.exists():
            os.replace(side, Path(str(dst) + side.name[len(lp.name):]))
    os.replace(lp, dst)


def _report_head(ctx, stats, rec):
    skipped = {t: ids for t, ids in stats["skipped"].items() if ids}
    lines = [f"- episodes {stats['episodes']}, facts {stats['facts']}, patterns "
             f"{stats['patterns']}, candidates added {stats['candidates']}",
             f"- embeddings carried {rec['embeddings_carried']}, foreign (stored NULL) "
             f"{rec['foreign_embeddings']}, re-embedded 0 (4.3 does it)",
             f"- embedding model: before {rec['embedding_model_before']}, after "
             f"{rec['embedding_model_after']}, changed {rec['embedding_model_changed']}",
             "- skipped as malformed (ids): " + (json.dumps(skipped) if skipped else "none"),
             f"- memory.db kept as {rec['source_path']}.migrated; never deleted by the "
             f"migrator. {FOREIGN_NOTE}."]
    ctx.report["Memory"] = lines + ctx.report.get("Memory", [])


def verify(ctx) -> None:
    st = getattr(ctx, "_memory", None)
    if st is None:
        raise MemoryMigrationError("40_memory.verify called before apply")
    if st["case"] == "empty":
        from lib.graph.store import open_graph
        open_graph(ctx.data_dir, create=True)
        ctx.report["Memory"] = ["- no memory.db found; created an empty graph", FOREIGN_NOTE]
        return
    if st["case"] == "finish":
        return _finish(ctx)
    lp, sp = legacy_path(ctx), staging_file(ctx)
    try:
        src, stats, info = st["src"], st["stats"], st["info"]
        g = sqlite3.connect(str(sp), isolation_level=None)
        g.row_factory = sqlite3.Row
        g.execute("PRAGMA foreign_keys=ON")
        try:
            verify_counts(g, src, stats)
            verify_fidelity(g, src, _model_changed())
            verify_integrity(g)
        finally:
            g.close()
        parity = run_parity(ctx, src, lp, sp)
        _checkpoint_legacy(lp)
        info["source_sha256"] = _sha256(lp)
        g = sqlite3.connect(str(sp), isolation_level=None)
        try:
            g.execute("BEGIN")
            rec = _record(g, ctx, lp, src, stats, info, parity)
            g.execute("COMMIT")
            g.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            g.close()
        for side in _sidecars(sp):
            if side.exists() and side.stat().st_size == 0:
                side.unlink()
    except BaseException:
        _discard_staging(ctx)
        raise
    _swap_in(ctx)
    _rename_legacy(lp)
    _report_head(ctx, stats, rec)


def _finish(ctx):
    """Interrupted between swap and rename: re-verify against the legacy file
    and the recorded hash, then finish the rename."""
    lp, gp = legacy_path(ctx), graph_file(ctx)
    mig = _migration_meta(gp)
    if not mig or mig.get("source_sha256") != _sha256(lp):
        _fail("graph.db migration record does not match memory.db; refusing to rename")
    src = read_source(lp, ctx.data_dir)
    stats = _stats(src)
    g = sqlite3.connect(str(gp), isolation_level=None)   # fts integrity-check is a command
    g.row_factory = sqlite3.Row
    try:
        verify_counts(g, src, stats)
        verify_fidelity(g, src, mig.get("embedding_model_changed", False))
        verify_integrity(g)
    finally:
        g.close()
    _rename_legacy(lp)
    ctx.report["Memory"] = [f"- resumed an interrupted migration; re-verified counts and "
                            f"the recorded hash, then renamed memory.db. {FOREIGN_NOTE}."]


STEP = Step("40_memory", 1, 2, detect=detect, apply=apply, verify=verify,
            plan=plan, preflight=preflight)
