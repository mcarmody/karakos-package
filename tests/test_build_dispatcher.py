"""lib/build_dispatcher.py end to end: the fake claude, fake ssh and a local bare
repository (spec 3.3). Only processes this test started are ever signalled."""
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildq_helpers as bq  # noqa: E402
from buildq_helpers import FakeRegistry, brief, make_remote, make_workspace, write_script  # noqa: E402

import build_dispatcher as bd  # noqa: E402
import build_hosts  # noqa: E402
import build_run  # noqa: E402
import buildq  # noqa: E402
import procinfo  # noqa: E402

PR = "https://example.invalid/owner/name/pull/1"


class Env:
    pass


@pytest.fixture
def e(tmp_path, monkeypatch):
    x = Env()
    x.ws = make_workspace(tmp_path)
    x.tmp = tmp_path
    x.iso = bq.isolate(monkeypatch, tmp_path, x.ws)
    x.base = make_remote(tmp_path)
    x.db = x.ws / "data" / "build-queue.db"
    c = buildq.connect(x.db)
    buildq.init_schema(c)
    c.close()
    x.notes = []
    x.t = [1_800_000_000.0]
    x.monkeypatch = monkeypatch
    monkeypatch.setenv("KARAKOS_FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    x.ssh_log = tmp_path / "ssh.log"
    return x


def cfg(e, **over):
    d = {"enabled": True,
         "hosts": {"local": {"kind": "local", "concurrency": 1, "min_free_ram_mb": 0},
                   "remote": {"kind": "ssh", "target": "box", "concurrency": 2,
                              "probe": ["probe"], "min_free_ram_mb": 2048,
                              "repo_url": f"file://{e.base}/{{repo}}.git"}},
         "unreachable_grace_s": 5, "governor": False}
    d.update(over)
    return build_hosts.parse_config(d)


def make(e, config=None, governor=None, clock=None):
    config = config or cfg(e)
    ws = e.ws
    runners = {"local": build_run.LocalRunner(ws),
               "ssh": build_run.SshRunner(ws, lambda kind, row: "SYSTEM PROMPT TEXT\n")}
    return bd.QueueDispatcher(
        e.db, config, runners, clock or (lambda: e.t[0]), governor, workspace=ws,
        registry_loader=lambda: FakeRegistry(),
        notifier=lambda req, ch, msg: e.notes.append((req, ch, msg)),
        cost_poster=lambda *a: e.__dict__.setdefault("costs", []).append(a))


def conn(e):
    return buildq.connect(e.db)


def enq(e, host=None, n=0, **kw):
    c = conn(e)
    kw.setdefault("brief", brief(body=f"task {n}"))
    qid = buildq.enqueue(c, "build", host=host, registry=FakeRegistry(), now=e.t[0], **kw)
    c.close()
    return qid


def row(e, qid):
    c = conn(e)
    try:
        return dict(buildq.get(c, qid))
    finally:
        c.close()


def evs(e, qid):
    c = conn(e)
    try:
        return [(x["event"], x["detail"]) for x in buildq.events(c, qid)]
    finally:
        c.close()


async def pump(d, until, timeout=120, every=0.05):
    end = time.time() + timeout
    while time.time() < end:
        await d.tick()
        if until():
            return
        await asyncio.sleep(every)
    raise AssertionError("condition not reached")


def done(e, *ids):
    return lambda: all(row(e, i)["status"] in ("done", "failed", "cancelled") for i in ids)


def run(coro):
    return asyncio.run(coro)


def alive(pid):
    return procinfo.proc_state(pid) not in (None, "Z")


# --- (1) per-host concurrency ----------------------------------------------------------

def test_concurrency_is_per_host_and_a_full_host_does_not_block_the_other(e):
    write_script(e.iso.script, {"text": f"opened {PR}", "delay_ms": 700})
    ids = [enq(e, "local", 0), enq(e, "local", 1), enq(e, "remote", 2), enq(e, "remote", 3),
           enq(e, "remote", 4)]
    d = make(e)
    peaks = {"local": 0, "remote": 0}
    both = []

    def watch():
        c = conn(e)
        counts = {r[0]: r[1] for r in c.execute(
            "SELECT exec_host, COUNT(*) FROM build_queue WHERE status='running' GROUP BY exec_host")}
        c.close()
        for h in peaks:
            peaks[h] = max(peaks[h], counts.get(h, 0))
        if counts.get("local") and counts.get("remote"):
            both.append(1)
        return done(e, *ids)()

    async def go():
        await pump(d, watch, timeout=90)
    run(go())
    assert peaks["local"] == 1 and peaks["remote"] == 2
    assert both, "a full local host blocked remote"
    assert all(row(e, i)["status"] == "done" for i in ids), [row(e, i)["reason"] for i in ids]
    assert row(e, ids[2])["result"] and json.loads(row(e, ids[2])["result"])["pr_url"] == PR


def test_a_row_with_an_unknown_host_fails(e):
    c = conn(e)
    qid = buildq.enqueue(c, "build", brief(), host="ghost", registry=FakeRegistry(), now=e.t[0])
    c.close()
    d = make(e)
    run(pump(d, done(e, qid), timeout=10))
    r = row(e, qid)                                   # host names come only from the config file
    assert (r["status"], r["reason"]) == ("failed", "unknown-host")
    assert "unknown-host" in e.notes[0][2] or not e.notes


# --- (2) ingest ----------------------------------------------------------------------------

def test_ingest_moves_the_brief_and_is_idempotent(e):
    d = make(e)
    inbox = e.ws / "inbox" / "builder"
    (inbox / "a.md").write_text(brief(body="same"))
    ids = d.ingest_inboxes()
    assert len(ids) == 1
    assert not (inbox / "a.md").exists() and (inbox / "queued" / "a.md").exists()
    (inbox / "b.md").write_text(brief(body="same"))
    ids2 = d.ingest_inboxes()
    assert ids2 == ids                                   # same content, same row
    c = conn(e)
    assert c.execute("SELECT COUNT(*) FROM build_queue").fetchone()[0] == 1
    c.close()
    assert (inbox / "queued" / "b.md").exists()
    r = row(e, ids[0])
    assert (r["origin"], r["source"], r["status"]) == ("human", "inbox:builder", "queued")


def test_ingest_origin_and_reviewer_inbox(e):
    d = make(e)
    (e.ws / "inbox" / "builder" / "m.md").write_text(brief(extra="origin: machine\npriority: 4\n"))
    (e.ws / "inbox" / "reviewer" / "r.md").write_text("---\nrepo: owner/name\n---\nreview this\n")
    a, b = d.ingest_inboxes()
    assert (row(e, a)["origin"], row(e, a)["priority"], row(e, a)["kind"]) == ("machine", 4, "build")
    assert row(e, b)["kind"] == "review"


def test_invalid_brief_is_rejected_with_a_reason_and_poked_once(e):
    d = make(e)
    inbox = e.ws / "inbox" / "builder"
    (inbox / "bad.md").write_text("---\nrepo: owner/name\nrequester: prim\n---\nno branch\n")
    assert d.ingest_inboxes() == []
    assert (inbox / "rejected" / "bad.md").exists()
    assert "target_branch" in (inbox / "rejected" / "bad.md.reason").read_text()
    assert len(e.notes) == 1 and e.notes[0][0] == "prim" and "target_branch" in e.notes[0][2]
    d.ingest_inboxes()
    assert len(e.notes) == 1


# --- (3) by content ----------------------------------------------------------------------------

def test_remote_gets_brief_and_system_prompt_by_content_and_no_secret(e):
    e.monkeypatch.setenv("AGENT_SERVER_TOKEN", "sekret-token")
    write_script(e.iso.script, {"text": f"opened {PR}"})
    body = brief(body="UNIQUE-BRIEF-TEXT-91823")
    qid = enq(e, "remote", brief=body)
    d = make(e)
    run(pump(d, done(e, qid)))
    assert row(e, qid)["status"] == "done"
    rdir = e.iso.rhome / "karakos-builds" / qid
    assert (rdir / "brief.md").read_text() == body
    assert (rdir / "system.md").read_text() == "SYSTEM PROMPT TEXT\n"
    log = e.ssh_log.read_text()
    assert str(e.ws) not in log and "UNIQUE-BRIEF-TEXT" not in log and "sekret-token" not in log
    assert str(e.tmp / "ws") not in log
    rec = json.loads((e.iso.logs / "prompts.jsonl").read_text().splitlines()[0])
    for bad in ("AGENT_SERVER_TOKEN", "WORKSPACE_ROOT", "KARAKOS_QUEUE_RUN"):
        assert bad not in rec["env_keys"]
    assert "UNIQUE-BRIEF-TEXT-91823" in rec["prompt"]    # the remote claude did get the brief


def test_remote_cost_is_reported_under_the_role_agent(e):
    write_script(e.iso.script, {"text": f"opened {PR}", "cost": 1.25})
    qid = enq(e, "remote")
    d = make(e)
    run(pump(d, done(e, qid)))
    assert e.costs and e.costs[0][0] == "bld" and e.costs[0][1] == 1.25


# --- (4) outcome verification -----------------------------------------------------------------------

@pytest.mark.parametrize("host", ["remote", "local"])
def test_no_pr_is_failed_and_the_notice_says_nothing_was_produced(e, host):
    write_script(e.iso.script, {"no_pr": True, "text": "All done, I changed nothing."})
    qid = enq(e, host)
    run(pump(make(e), done(e, qid)))
    r = row(e, qid)
    assert (r["status"], r["reason"]) == ("failed", "no-pr")
    assert len(e.notes) == 1 and "produced nothing" in e.notes[0][2] and qid in e.notes[0][2]


@pytest.mark.parametrize("host", ["remote", "local"])
def test_a_pr_url_makes_the_row_done(e, host):
    write_script(e.iso.script, {"text": f"PR: {PR}"})
    qid = enq(e, host)
    run(pump(make(e), done(e, qid)))
    r = row(e, qid)
    assert r["status"] == "done" and json.loads(r["result"])["pr_url"] == PR
    assert PR in e.notes[0][2] and e.notes[0][0] == "prim"


# --- (5) failures notify ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["remote", "local"])
def test_a_runner_exit_1_is_failed_and_notified(e, host):
    write_script(e.iso.script, {"exit": 1, "text": "boom"})
    qid = enq(e, host)
    run(pump(make(e), done(e, qid)))
    r = row(e, qid)
    assert (r["status"], r["reason"]) == ("failed", "exit-1")
    assert len(e.notes) == 1 and "exit-1" in e.notes[0][2]


def test_local_queue_run_skips_poke_and_archive_in_the_script(e):
    """The script's own notice/archive are off for queue runs; the dispatcher's poke
    (to the workspace's poke.sh) is the only one."""
    write_script(e.iso.script, {"text": f"PR: {PR}"})
    qid = enq(e, "local")
    d = make(e)
    d.notify = bd.poke_notifier(e.ws)
    run(pump(d, done(e, qid)))
    assert len(bq.pokes(e.ws)) == 1 and "--source build-queue" in bq.pokes(e.ws)[0]
    assert not (e.ws / "inbox" / "bld" / "archive").exists()
    assert (e.ws / "data" / "build-queue" / "briefs" / f"{qid}.md").exists()


# --- (6) timeout ------------------------------------------------------------------------------------

def test_timeout_kills_the_group_and_the_grandchild(e):
    write_script(e.iso.script, {"hang": True, "spawn_child": {"seconds": 60}})
    qid = enq(e, "local")
    d = make(e, cfg(e, roles={"build": {"timeout_s": 2}}))
    children = e.iso.logs / "children.pid"
    run(pump(d, done(e, qid), timeout=60))
    r = row(e, qid)
    assert (r["status"], r["reason"]) == ("failed", "timeout")
    pids = [int(x) for x in children.read_text().split()]
    assert pids and not any(alive(p) for p in pids)
    ref = json.loads(r["run_ref"])
    assert not alive(ref["pid"])
    assert [n for n in e.notes if "timed out" in n[2]]


# --- (7) cancel ---------------------------------------------------------------------------------------

def start_hanging_remote(e, **step):
    write_script(e.iso.script, {"write_file": {"path": "dirty.txt", "content": "unsaved\n"},
                                "hang": True, **step})
    qid = enq(e, "remote")
    d = make(e)
    rdir = e.iso.rhome / "karakos-builds" / qid
    return qid, d, rdir


def ready(rdir):
    return lambda: (rdir / "run.pid").exists() and (rdir / "work" / "dirty.txt").exists()


def test_cancel_running_ssh_row_terms_the_remote_group_and_salvages(e):
    qid, d, rdir = start_hanging_remote(e)

    async def go():
        await pump(d, ready(rdir))
        pg = int((rdir / "run.pid").read_text().split()[1])
        assert pg > 1
        prev = await d.cancel(qid)
        assert prev == "running"
        await pump(d, lambda: not d.tasks)
        return pg
    pg = run(go())
    r = row(e, qid)
    assert r["status"] == "cancelled"
    assert ("remote-kill", "remote-kill: termed") in evs(e, qid)
    assert not alive(pg)
    files = os.popen(f"git --git-dir {bq.bare_path(e.base)} diff --name-only main bld/salvage-{qid}").read()
    assert files.split() == ["dirty.txt"]


def test_cancel_kills_a_stub_that_ignores_term(e):
    e.monkeypatch.setattr(build_run, "KILL_GRACE_S", 1.0)
    qid, d, rdir = start_hanging_remote(e, ignore_term=True)

    async def go():
        await pump(d, ready(rdir))
        pg = int((rdir / "run.pid").read_text().split()[1])
        await d.cancel(qid)
        await pump(d, lambda: not d.tasks)
        return pg
    pg = run(go())
    assert ("remote-kill", "remote-kill: killed") in evs(e, qid)
    assert row(e, qid)["status"] == "cancelled" and not alive(pg)


def test_cancel_with_ssh_failing_leaves_cancel_failed_then_succeeds(e):
    qid, d, rdir = start_hanging_remote(e)

    async def go():
        await pump(d, ready(rdir))
        pg = int((rdir / "run.pid").read_text().split()[1])
        e.monkeypatch.setenv("KARAKOS_FAKE_SSH_FAIL", "255")
        prev = await d.cancel(qid)
        r = row(e, qid)
        assert (r["status"], r["reason"]) == ("running", "cancel-failed")
        assert ("remote-kill", "remote-kill: failed") in evs(e, qid) and alive(pg)
        e.monkeypatch.delenv("KARAKOS_FAKE_SSH_FAIL")
        await d.cancel(qid)
        await pump(d, lambda: not d.tasks)
        return pg
    pg = run(go())
    assert row(e, qid)["status"] == "cancelled" and not alive(pg)


def test_cli_style_cancel_is_noticed_by_the_poll(e):
    qid, d, rdir = start_hanging_remote(e)

    async def go():
        await pump(d, ready(rdir))
        pg = int((rdir / "run.pid").read_text().split()[1])
        c = conn(e)
        assert buildq.cancel(c, qid, e.t[0]) == "running"
        c.close()
        await pump(d, lambda: not d.tasks and not d.killing)
        return pg
    pg = run(go())
    assert row(e, qid)["status"] == "cancelled" and not alive(pg)
    assert ("remote-kill", "remote-kill: termed") in evs(e, qid)
    # one kill per cancel, not one per tick (a re-kill loop kept the run task from finishing)
    assert len([x for x in evs(e, qid) if x[0] == "remote-kill"]) == 1


def test_cancel_queued_row_is_immediate(e):
    qid = enq(e, "remote")
    d = make(e)
    assert run(d.cancel(qid)) == "queued"
    assert row(e, qid)["status"] == "cancelled"
    assert run(d.cancel("bq-nope")) is None


# --- (8) restart ----------------------------------------------------------------------------------------

def test_restart_fails_the_running_row_and_reaps_the_leftover_group(e):
    write_script(e.iso.script, {"hang": True, "spawn_child": {"seconds": 60}})
    qid = enq(e, "local")

    async def go():
        a = make(e)
        await pump(a, lambda: (e.iso.logs / "children.pid").exists() and row(e, qid)["run_ref"])
        ref = json.loads(row(e, qid)["run_ref"])
        for t in list(a.tasks.values()):            # the dispatcher "dies": its tasks stop,
            t.cancel()                              # the processes it started keep running
        await asyncio.gather(*a.tasks.values(), return_exceptions=True)
        assert alive(ref["pid"])
        b = make(e)
        await b.tick()
        return ref
    ref = run(go())
    r = row(e, qid)
    assert (r["status"], r["reason"]) == ("failed", "dispatcher-restart")
    assert not alive(ref["pid"])
    children = [int(x) for x in (e.iso.logs / "children.pid").read_text().split()]
    assert not any(alive(p) for p in children)
    assert len((e.iso.logs / "prompts.jsonl").read_text().splitlines()) == 1     # no second run


def test_restart_never_retries_with_the_default_attempts(e):
    qid = enq(e, "local")
    c = conn(e)
    buildq.claim_next(c, {"local": 1}, e.t[0])
    c.close()
    d = make(e)
    run(d.tick())
    assert row(e, qid)["status"] == "failed"


# --- (9) admission -------------------------------------------------------------------------------------------

def test_busy_host_leaves_the_row_queued_until_the_probe_changes(e):
    write_script(e.iso.script, {"text": f"opened {PR}"})
    e.monkeypatch.setenv("KARAKOS_FAKE_PROBE_RAM", "100")
    qid = enq(e, "remote")
    d = make(e)

    async def go():
        for _ in range(3):
            await d.tick()
            await asyncio.sleep(0.05)
        assert row(e, qid)["status"] == "queued"
        assert [x for x in evs(e, qid) if x[0] == "host-busy"]
        assert len([x for x in evs(e, qid) if x[0] == "host-busy"]) == 1       # once an hour
        e.monkeypatch.setenv("KARAKOS_FAKE_PROBE_RAM", "9000")
        e.t[0] += 31                                                           # the 30 s cache
        await pump(d, done(e, qid))
    run(go())
    assert row(e, qid)["status"] == "done"


def test_probe_failing_fails_open(e):
    write_script(e.iso.script, {"text": f"opened {PR}"})
    config = cfg(e)
    config.hosts["remote"].probe = ["false"]
    qid = enq(e, "remote")
    run(pump(make(e, config), done(e, qid)))
    assert row(e, qid)["status"] == "done"


def test_unreachable_host_holds_rows_then_fails_them_after_the_grace(e):
    e.monkeypatch.setenv("KARAKOS_FAKE_SSH_FAIL", "255")
    qid = enq(e, "remote")
    other = enq(e, "local", n=1)
    write_script(e.iso.script, {"text": f"opened {PR}"})
    d = make(e, cfg(e, unreachable_grace_s=100))

    async def go():
        await pump(d, done(e, other))
        assert row(e, qid)["status"] == "queued"
        assert [x for x in evs(e, qid) if x[0] == "host-unreachable"]
        assert not [x for x in evs(e, qid) if x[0] == "host-busy"]
        e.t[0] += 31
        await d.tick()
        assert row(e, qid)["status"] == "queued"          # inside the grace
        e.t[0] += 80
        await d.tick()
    run(go())
    r = row(e, qid)
    assert (r["status"], r["reason"]) == ("failed", "host-unreachable")
    assert row(e, other)["status"] == "done"
    assert [n for n in e.notes if "host-unreachable" in n[2]]


def test_first_connect_failure_without_a_probe_requeues_the_row(e):
    config = cfg(e, unreachable_grace_s=1000)
    config.hosts["remote"].probe = None
    e.monkeypatch.setenv("KARAKOS_FAKE_SSH_FAIL", "255")
    qid = enq(e, "remote")
    d = make(e, config)

    async def go():
        await pump(d, lambda: [x for x in evs(e, qid) if x[0] == "host-unreachable"
                               and "255" in x[1]])
        await pump(d, lambda: not d.tasks)
        assert row(e, qid)["status"] == "queued" and row(e, qid)["attempts"] == 0
        e.monkeypatch.delenv("KARAKOS_FAKE_SSH_FAIL")
        write_script(e.iso.script, {"text": f"opened {PR}"})
        e.t[0] += 31                                       # past the retry throttle
        await pump(d, done(e, qid))
    run(go())
    assert row(e, qid)["status"] == "done"


# --- (10) governor ----------------------------------------------------------------------------------------------

pytestmark_gov = pytest.mark.skipif(bd.usage_governor is None, reason="2.7 usage_governor absent")


def rate_db(e, pct, resets=None):
    p = e.ws / "data" / "memory" / "agent-server.db"
    c = sqlite3.connect(p)
    c.execute("DROP TABLE IF EXISTS rate_limit_state")
    c.execute("CREATE TABLE rate_limit_state (rate_limit_type TEXT PRIMARY KEY, status TEXT,"
              " resets_at INTEGER, utilization REAL, updated_at TEXT)")
    c.execute("INSERT INTO rate_limit_state VALUES ('seven_day','allowed',?,?, 'x')",
              (int(resets or time.time() + 86400), pct))
    c.commit()
    c.close()


def gov_dispatcher(e, **over):
    config = cfg(e, **{"governor": True, **over})
    return make(e, config, governor=bd.make_governor(e.ws, config))


@pytestmark_gov
def test_machine_row_defers_at_85_and_runs_at_70(e):
    write_script(e.iso.script, {"text": f"opened {PR}"})
    rate_db(e, 85)
    qid = enq(e, "local", origin="machine")
    d = gov_dispatcher(e)

    async def go():
        for _ in range(3):
            await d.tick()
            await asyncio.sleep(0.05)
        assert row(e, qid)["status"] == "queued"
        assert [x for x in evs(e, qid) if x[0] == "governor"]
        rate_db(e, 70)
        await pump(d, done(e, qid))
    run(go())
    assert row(e, qid)["status"] == "done"


@pytestmark_gov
def test_human_row_runs_at_99_and_machine_does_not(e):
    write_script(e.iso.script, {"text": f"opened {PR}"})
    rate_db(e, 99)
    h = enq(e, "local", origin="human", n=1)
    d = gov_dispatcher(e)
    run(pump(d, done(e, h)))
    assert row(e, h)["status"] == "done"
    assert not [x for x in evs(e, h) if x[0] == "governor"]


@pytestmark_gov
def test_governor_false_bypasses_and_an_unreadable_db_fails_open(e):
    write_script(e.iso.script, {"text": f"opened {PR}"})
    rate_db(e, 99)
    a = enq(e, "local", origin="machine")
    config = cfg(e, governor=False)
    assert bd.make_governor(e.ws, config) is None
    run(pump(make(e, config), done(e, a)))
    assert row(e, a)["status"] == "done"
    (e.ws / "data" / "memory" / "agent-server.db").write_text("not a database")
    b = enq(e, "local", origin="machine", n=2)
    run(pump(gov_dispatcher(e), done(e, b)))
    assert row(e, b)["status"] == "done"


# --- schema check, heartbeat, registry ---------------------------------------------------------------------------

def test_enabled_without_tables_exits_with_migrate_message(e):
    e.db.unlink()
    d = make(e)
    with pytest.raises(SystemExit) as ei:
        d.open()
    assert "run karakos migrate" in str(ei.value)
    c = sqlite3.connect(e.db)
    c.execute("CREATE TABLE other (x)")
    c.commit()
    c.close()
    with pytest.raises(SystemExit):
        make(e).open()


@pytest.mark.skipif(bd.heartbeats is None, reason="3.2 heartbeats absent")
def test_tick_touches_the_heartbeat(e):
    d = make(e)
    run(d.tick())
    assert bd.heartbeats.read(e.ws, "build-dispatcher")["ok"] is True


@pytest.mark.skipif(bd.job_registry is None, reason="3.2 job_registry absent")
def test_component_registered_only_while_enabled_and_running(e):
    async def go(config):
        d = make(e, config)
        await d.start()
        await asyncio.sleep(0.2)
        seen = "build-dispatcher" in bd.job_registry._TABLE
        await d.stop()
        return seen, "build-dispatcher" in bd.job_registry._TABLE
    try:
        assert run(go(cfg(e))) == (True, False)
        off = cfg(e)
        off.enabled = False
        assert run(go(off)) == (False, False)
    finally:
        bd.job_registry.unregister("build-dispatcher")


def test_dispatcher_imports_without_governor_or_heartbeats(monkeypatch):
    import importlib
    for name in ("usage_governor", "heartbeats", "job_registry"):
        monkeypatch.setitem(sys.modules, name, None)
    mod = importlib.reload(bd)
    try:
        assert mod.usage_governor is None and mod.heartbeats is None and mod.job_registry is None
        assert mod.make_governor("/x", build_hosts.BuildConfig()) is None
    finally:
        monkeypatch.undo()
        importlib.reload(bd)
