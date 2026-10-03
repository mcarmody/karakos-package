"""lib/buildq.py: pure queue functions over sqlite (spec 3.3)."""
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildq_helpers import FakeRegistry, brief  # noqa: E402

import buildq  # noqa: E402

REG = FakeRegistry()
NOW = 1_800_000_000.0


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    buildq.init_schema(c)
    return c


def enq(conn, n=0, **kw):
    kw.setdefault("kind", "build")
    kw.setdefault("brief", brief(body=f"task {n}"))
    kw.setdefault("now", NOW + n)
    kw.setdefault("registry", REG)
    return buildq.enqueue(conn, **kw)


def test_schema_idempotent_and_tables_present(conn):
    buildq.init_schema(conn)
    assert buildq.tables_present(conn)


# --- enqueue / validation ----------------------------------------------------

def test_build_needs_explicit_target_branch(conn):
    with pytest.raises(buildq.BriefError, match="target_branch"):
        buildq.enqueue(conn, "build", "---\nrepo: owner/name\n---\nx", registry=REG)


def test_build_needs_repo(conn):
    with pytest.raises(buildq.BriefError, match="repo"):
        buildq.enqueue(conn, "build", "---\ntarget_branch: main\n---\nx", registry=REG)


@pytest.mark.parametrize("repo", ["noslash", "a/b c", "-a/b", "a/..", "a/b/c", "../x/y", "a/-b"])
def test_bad_repo_patterns(conn, repo):
    with pytest.raises(buildq.BriefError):
        buildq.enqueue(conn, "build", f"---\nrepo: {repo}\ntarget_branch: main\n---\nx", registry=REG)


@pytest.mark.parametrize("branch", ["-x", "a b", "a..b", "a/../b"])
def test_bad_branch_patterns(conn, branch):
    with pytest.raises(buildq.BriefError):
        buildq.enqueue(conn, "build", f"---\nrepo: o/n\ntarget_branch: {branch}\n---\nx", registry=REG)


def test_no_role_agent(conn):
    with pytest.raises(buildq.BriefError, match="no builder agent in the registry"):
        enq(conn, registry=FakeRegistry(reviewer=["rev"]))
    with pytest.raises(buildq.BriefError, match="no reviewer agent"):
        buildq.enqueue(conn, "review", brief(), registry=FakeRegistry(builder=["b"]))


def test_review_needs_repo_or_body(conn):
    assert buildq.enqueue(conn, "review", "just a spec in the body", registry=REG)
    with pytest.raises(buildq.BriefError):
        buildq.enqueue(conn, "review", "   ", registry=REG)


def test_duplicate_source_ref_returns_first_id(conn):
    a = enq(conn, source_ref="abc")
    b = enq(conn, 1, source_ref="abc")
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM build_queue").fetchone()[0] == 1
    assert a.startswith("bq-") and len(a) == 15


def test_enqueue_logs_event_and_defaults(conn):
    qid = enq(conn)
    row = buildq.get(conn, qid)
    assert (row["status"], row["origin"], row["priority"]) == ("queued", "human", 0)
    assert row["repo"] == "owner/name" and row["target_branch"] == "main"
    assert [e["event"] for e in buildq.events(conn, qid)] == ["queued"]


def test_frontmatter_matches_the_relays_reader():
    import importlib.util
    import os
    import tempfile
    src = (Path(__file__).resolve().parent.parent / "bin" / "relay.py").read_text()
    # extract the relay's reader without importing discord
    start = src.index("    def parse_frontmatter(self, content: str) -> Dict:")
    end = src.index("# =====", start)
    ns = {"Dict": dict}
    body = "\n".join(l[4:] if l.startswith("    ") else l for l in src[start:end].splitlines())
    exec(body, ns)
    fixtures = ["", "no front", "---\na: b\n---\nbody", "---\nrepo: o/n\nx: y: z\n  k :  v  \n---\n---\nz: 1\n",
                "---\n# c\nnocolon\nq:\n---\n", " ---\na: b\n---", "---\na: b\n"]
    for f in fixtures:
        assert buildq.parse_frontmatter(f) == ns["parse_frontmatter"](None, f), f


# --- claiming ------------------------------------------------------------------

def test_priority_then_age_order(conn):
    a = enq(conn, 0)
    b = enq(conn, 1)
    c = enq(conn, 2, priority=5)
    caps = {"local": 9}
    order = [buildq.claim_next(conn, caps, NOW + 10)["id"] for _ in range(3)]
    assert order == [c, a, b]
    assert buildq.claim_next(conn, caps, NOW + 10) is None


def test_claim_sets_fields(conn):
    qid = enq(conn)
    row = buildq.claim_next(conn, {"local": 1}, NOW)
    assert row["id"] == qid and row["status"] == "running"
    assert (row["exec_host"], row["attempts"]) == ("local", 1) and row["started_at"]


def test_full_host_is_skipped_and_free_host_claims_behind_it(conn):
    a = enq(conn, 0, host="local")
    b = enq(conn, 1, host="remote")
    row = buildq.claim_next(conn, {"local": 0, "remote": 1}, NOW + 5, default_host="local")
    assert row["id"] == b
    assert buildq.get(conn, a)["status"] == "queued"


def test_default_host_applies_when_unset(conn):
    a = enq(conn)
    assert buildq.claim_next(conn, {"local": 0, "far": 1}, NOW, default_host="far")["id"] == a


def test_not_before_respected(conn):
    a = enq(conn)
    conn.execute("UPDATE build_queue SET not_before=? WHERE id=?", (int(NOW) + 100, a))
    conn.commit()
    assert buildq.claim_next(conn, {"local": 1}, NOW) is None
    assert buildq.claim_next(conn, {"local": 1}, NOW + 101)["id"] == a


def test_rejecting_gate_leaves_row_queued_and_claims_next(conn):
    a = enq(conn, 0)
    b = enq(conn, 1)
    seen = []

    def gate(row):
        seen.append(row["id"])
        return (row["id"] != a), "host-busy: low ram"

    row = buildq.claim_next(conn, {"local": 2}, NOW + 5, gate)
    assert row["id"] == b and seen == [a, b]
    assert buildq.get(conn, a)["status"] == "queued"
    evs = [e["event"] for e in buildq.events(conn, a)]
    assert "host-busy" in evs
    # the deferral event is logged at most once an hour
    buildq.claim_next(conn, {"local": 2}, NOW + 6, gate)
    assert [e["event"] for e in buildq.events(conn, a)].count("host-busy") == 1
    buildq.claim_next(conn, {"local": 2}, NOW + 7200, gate)
    assert [e["event"] for e in buildq.events(conn, a)].count("host-busy") == 2


def test_two_claimers_never_get_the_same_row(tmp_path):
    db = tmp_path / "q.db"
    c0 = buildq.connect(db)
    buildq.init_schema(c0)
    for round_ in range(200):
        qid = buildq.enqueue(c0, "build", brief(body=f"r{round_}"), registry=REG, now=NOW)
        got, barrier = [], threading.Barrier(2)

        def claimer():
            c = buildq.connect(db)
            try:
                barrier.wait()
                r = buildq.claim_next(c, {"local": 5}, NOW + 1)
                got.append(r["id"] if r is not None else None)
            finally:
                c.close()

        ts = [threading.Thread(target=claimer) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(g for g in got if g) == [qid], (round_, got)
        buildq.finish(c0, qid, "done", "", None, NOW)
    c0.close()


# --- cancel / requeue / recover / finish ----------------------------------------

def test_cancel_queued_and_running(conn):
    a, b = enq(conn, 0), enq(conn, 1)
    assert buildq.cancel(conn, a, NOW) == "queued"
    assert buildq.get(conn, a)["status"] == "cancelled"
    buildq.claim_next(conn, {"local": 1}, NOW)
    assert buildq.cancel(conn, b, NOW) == "running"
    assert buildq.get(conn, b)["status"] == "cancelled"
    assert buildq.cancel(conn, "bq-nope") is None
    assert buildq.cancel(conn, a, NOW) == "cancelled"      # already terminal: no change


def test_requeue_makes_a_new_row(conn):
    a = enq(conn, priority=3, host="remote")
    buildq.claim_next(conn, {"remote": 1}, NOW)
    buildq.finish(conn, a, "failed", "exit-1", {"exit": 1}, NOW)
    n = buildq.requeue(conn, a, NOW)
    assert n != a
    new, old = buildq.get(conn, n), buildq.get(conn, a)
    assert (new["status"], new["attempts"], new["brief"], new["priority"], new["host"]) == \
        ("queued", 0, old["brief"], 3, "remote")
    assert old["status"] == "failed"
    assert buildq.requeue(conn, "bq-nope") is None


def test_recover_fails_running_rows_without_retry(conn):
    a, b = enq(conn, 0), enq(conn, 1)
    buildq.claim_next(conn, {"local": 1}, NOW)
    cleaned = []
    ids = buildq.recover(conn, NOW, cleanup=lambda r: cleaned.append(r["id"]), max_attempts=1)
    assert ids == [a] and cleaned == [a]
    row = buildq.get(conn, a)
    assert (row["status"], row["reason"]) == ("failed", "dispatcher-restart")
    assert buildq.get(conn, b)["status"] == "queued"


def test_recover_retries_only_when_allowed(conn):
    a = enq(conn)
    buildq.claim_next(conn, {"local": 1}, NOW)
    buildq.recover(conn, NOW, max_attempts=2)
    assert buildq.get(conn, a)["status"] == "queued"


def test_recover_survives_a_cleanup_error(conn):
    a = enq(conn)
    buildq.claim_next(conn, {"local": 1}, NOW)

    def boom(row):
        raise RuntimeError("x")

    buildq.recover(conn, NOW, cleanup=boom)
    assert buildq.get(conn, a)["status"] == "failed"


def test_list_rows(conn):
    a, b = enq(conn, 0), enq(conn, 1)
    buildq.cancel(conn, a)
    assert [r["id"] for r in buildq.list_rows(conn, "queued")] == [b]
    assert len(buildq.list_rows(conn)) == 2


# --- verify_outcome ----------------------------------------------------------------

def test_verify_outcome_every_branch():
    v = buildq.verify_outcome
    pr = "https://example.invalid/o/n/pull/7"
    assert v("build", 0, f"done {pr}", None) == ("done", "")
    assert v("build", 0, "", {"pr_url": pr}) == ("done", "")
    assert v("build", 0, "all good, no link", None) == ("failed", "no-pr")      # the guard
    assert v("build", 0, "", {"pr_url": ""}) == ("failed", "no-pr")
    assert v("build", 1, pr, None) == ("failed", "exit-1")
    assert v("build", 137, "", None) == ("failed", "exit-137")
    assert v("build", "timeout", pr, None) == ("failed", "timeout")
    assert v("review", 0, "Verdict: APPROVE", None) == ("done", "")
    assert v("review", 0, "  \n", None) == ("failed", "empty-review")
    assert v("review", 2, "x", None) == ("failed", "exit-2")
    assert v("review", "timeout", "x", None) == ("failed", "timeout")
