"""Durable Discord delivery in bin/agent-server.py: the real `post_to_discord`
and `outbox_send_row` against a stubbed `http_session` (replaces test_dead_letter.py).

Nothing here reads HOME, binds a port or touches Discord. The clock is injected
through `ags.OUTBOX_CLOCK`; retry waits are never slept.
"""
import ast
import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import outbox as ob  # noqa: E402

T0 = 1_000_000.0


# --- structural helpers (AST, not substring: comments are source text too) ----

def _tree(path=AGENT_SERVER):
    return ast.parse(Path(path).read_text())


def _function(name, tree=None):
    for node in ast.walk(tree or _tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls_to(func_node, callee):
    out = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == callee:
                out.append(node)
    return out


def _kwarg(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def ags(tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("ags_outbox_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_outbox_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    module.AGENT_TOKENS["amos"] = "fake-token"
    module.POST_RETRY_BASE_SEC = 0.0
    clock = {"t": T0}
    module.CLOCK = clock
    module.OUTBOX_CLOCK = lambda: clock["t"]
    module.OUTBOX_PATH = tmp_path / "data" / "outbox" / "outbox.db"
    return module


class FakeResponse:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status, self._payload, self._text = status, payload if payload is not None else {}, text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeDiscord:
    """Scripted statuses (int, or an Exception to raise); records every body."""

    def __init__(self, statuses=(), retry_after=0):
        self.statuses, self.bodies, self.retry_after = list(statuses), [], retry_after
        self.gate = None          # asyncio.Event: block inside the POST until set
        self.entered = None       # asyncio.Event: set once a POST is in flight

    @property
    def calls(self):
        return len(self.bodies)

    def post(self, url, **kwargs):
        if not str(url).endswith("/messages"):      # typing indicator and friends
            return FakeResponse(204)
        self.bodies.append(kwargs.get("json"))
        return self._respond()

    def _respond(self):
        n = len(self.bodies)
        status = self.statuses.pop(0) if self.statuses else 200
        fake = self

        class Ctx:
            async def __aenter__(self_):
                if fake.entered is not None:
                    fake.entered.set()
                if fake.gate is not None:
                    await fake.gate.wait()
                if isinstance(status, Exception):
                    raise status
                if status in (200, 201):
                    return FakeResponse(200, {"id": f"msg-{n}"})
                if status == 429:
                    return FakeResponse(429, {"retry_after": fake.retry_after})
                return FakeResponse(status, text=json.dumps({"message": f"error {status}", "code": 1}))

            async def __aexit__(self_, *exc):
                return False
        return Ctx()

    async def close(self):
        pass


def rows(ags, status=None):
    conn = ags._outbox_store(create=False)
    if conn is None:
        return []
    q = "SELECT * FROM outbox" + (" WHERE status=?" if status else "") + " ORDER BY created_at, rowid"
    return [dict(r) for r in conn.execute(q, (status,) if status else ())]


def post(ags, statuses, content="here is your answer", dead_letter=True, channel="555", fake=None):
    ags.http_session = fake or FakeDiscord(statuses)
    result = asyncio.run(ags.post_to_discord("amos", channel, content, dead_letter=dead_letter))
    return result, ags.http_session


def run_pass(ags, advance=0.0):
    ags.CLOCK["t"] += advance
    return asyncio.run(ags.outbox_pass())


@pytest.fixture
def skip_log(ags):
    lines = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append((record.levelname, record.getMessage()))
    h = H(level=logging.DEBUG)
    ags.log.addHandler(h)
    old = ags.log.level
    ags.log.setLevel(logging.DEBUG)
    yield lines
    ags.log.removeHandler(h)
    ags.log.setLevel(old)


# ---------------------------------------------------------------------------
# Ported from test_dead_letter.py
# ---------------------------------------------------------------------------

def test_permission_revoked_writes_the_reply_to_the_dead_letter_queue(ags):
    result, fake = post(ags, [403], content="the answer you waited for")
    assert result is None and fake.calls == 1
    (row,) = rows(ags)
    assert row["status"] == "dead" and row["content"] == "the answer you waited for"
    assert row["agent"] == "amos" and row["channel_id"] == "555"
    assert row["last_status"] == 403 and "403" in row["dead_reason"]


def test_permission_revoked_shows_up_in_the_health_count(ags):
    assert ags.dead_letter_count() == 0
    post(ags, [403])
    post(ags, [403])
    assert ags.dead_letter_count() == 2
    assert ags.outbox_health()["dead"] == 2


def test_health_route_reports_the_count(ags):
    health = _function("handle_health")
    assert _calls_to(health, "dead_letter_count") and _calls_to(health, "outbox_health")
    keys = [n.value for node in ast.walk(health) if isinstance(node, ast.Dict)
            for n in node.keys if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert {"dead_letters", "dead_letter_path", "outbox"} <= set(keys)
    assert set(ags.outbox_health()) == {"pending", "sending", "dead", "oldest_pending_age_s"}


def test_a_403_is_not_retried(ags):
    _, fake = post(ags, [403])
    assert fake.calls == 1
    run_pass(ags, advance=10_000)
    assert fake.calls == 1


def test_a_500_is_retried_to_the_limit_then_dead_lettered(ags, monkeypatch):
    monkeypatch.setenv("DISCORD_OUTBOX_MAX_ATTEMPTS", "4")
    ags.http_session = FakeDiscord([500] * 10)
    asyncio.run(ags.post_to_discord("amos", "555", "hello", dead_letter=True))
    waits = []
    for _ in range(5):
        (row,) = rows(ags)
        if row["status"] == "dead":
            break
        waits.append(round(row["next_attempt_at"] - ags.CLOCK["t"]))
        run_pass(ags, advance=row["next_attempt_at"] - ags.CLOCK["t"])
    (row,) = rows(ags)
    assert row["status"] == "dead" and row["attempts"] == 4 and "max attempts" in row["dead_reason"]
    for got, attempt in zip(waits, (1, 2, 3)):                 # grows per backoff_s (±10% jitter)
        base = ob.backoff_s(attempt, jitter=False)
        assert base * 0.9 - 1 <= got <= base * 1.1 + 1
    assert ags.http_session.calls == 4
    assert ags.http_session.statuses            # nothing sent after dead
    run_pass(ags, advance=100_000)
    assert ags.http_session.calls == 4


def test_a_transient_failure_that_recovers_is_not_dead_lettered(ags):
    result, fake = post(ags, [500, 200])
    assert result is None and rows(ags)[0]["status"] == "pending"
    run_pass(ags, advance=100)
    (row,) = rows(ags)
    assert row["status"] == "delivered" and json.loads(row["message_ids"]) == ["msg-2"]
    assert ags.dead_letter_count() == 0


def test_rate_limit_is_retried_and_can_succeed(ags):
    fake = FakeDiscord([429, 200], retry_after=42)
    post(ags, None, fake=fake)
    (row,) = rows(ags)
    assert row["status"] == "pending" and row["next_attempt_at"] == T0 + 42 and row["attempts"] == 1
    assert run_pass(ags, advance=41) == 0
    assert run_pass(ags, advance=2) == 1
    assert rows(ags)[0]["status"] == "delivered"


def test_a_network_exception_is_retried_then_dead_lettered(ags, monkeypatch):
    monkeypatch.setenv("DISCORD_OUTBOX_MAX_ATTEMPTS", "2")
    fake = FakeDiscord([ConnectionResetError("connection reset")] * 5)
    result, _ = post(ags, None, fake=fake)
    assert result is None and rows(ags)[0]["status"] == "pending"
    run_pass(ags, advance=100)
    (row,) = rows(ags)
    assert row["status"] == "dead" and "ConnectionResetError" in row["last_error"]
    assert fake.calls == 2


def test_incidental_posts_are_not_dead_lettered(ags):
    post(ags, [403], content="🔧 Bash", dead_letter=False)
    assert rows(ags) == [] and ags.dead_letter_count() == 0
    assert not ags.OUTBOX_PATH.exists()


def test_incidental_posts_use_the_direct_retry_path(ags):
    result, fake = post(ags, [500, 200], content="🔧 Bash", dead_letter=False)
    assert result == "msg-2" and fake.calls == 2


def test_the_reply_path_opts_in(ags):
    opted_in = []
    turn_loop_tree = _tree(PACKAGE_ROOT / "lib" / "turn_loop.py")
    for node in list(ast.walk(_tree())) + list(ast.walk(turn_loop_tree)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in _calls_to(node, "post_to_discord"):
                value = _kwarg(call, "dead_letter")
                if isinstance(value, ast.Constant) and value.value is True:
                    opted_in.append(node.name)
    assert any("response" in n or "process" in n or "finish" in n for n in opted_in), opted_in


def test_crash_recovery_does_not_dead_letter(ags):
    """crash_recovery's only durable post is the sweep's, which enters the
    outbox (find_for_reply decides first); its crash notice stays direct."""
    recovery = _function("crash_recovery")
    durable = [c for c in _calls_to(recovery, "post_to_discord")
               if isinstance(_kwarg(c, "dead_letter"), ast.Constant) and _kwarg(c, "dead_letter").value is True]
    assert len(durable) == 1 and _calls_to(recovery, "find_for_reply")
    notice = [c for c in _calls_to(recovery, "post_to_discord") if c not in durable]
    assert notice, "the crash notice is an incidental, non-durable post"


def test_a_broken_dead_letter_file_does_not_take_down_the_reply_loop(ags, skip_log):
    ags.OUTBOX_PATH.parent.mkdir(parents=True)
    ags.OUTBOX_PATH.write_bytes(b"this is not a sqlite database" * 50)
    r1, fake = post(ags, [200], content="still delivered")
    r2, _ = post(ags, [200], content="and again", fake=fake)
    assert r1 == "msg-1" and r2 == "msg-2"
    errors = [m for lvl, m in skip_log if lvl == "ERROR" and "outbox unavailable" in m]
    assert len(errors) == 1, "logged once, not per post"


def test_count_survives_a_corrupt_line_and_records_append(ags):
    post(ags, [403], content="first")
    post(ags, [403], content="second")
    assert [r["content"] for r in rows(ags, "dead")] == ["first", "second"]


# ---------------------------------------------------------------------------
# Partial chunks, nonces
# ---------------------------------------------------------------------------

def long_text():
    return ("a" * 1500 + "\n") + ("b" * 1500 + "\n") + ("c" * 1500)


def test_partial_failure_resends_only_the_missing_chunks(ags):
    fake = FakeDiscord([200, 500, 200, 200])
    result, _ = post(ags, None, content=long_text(), fake=fake)
    assert result is None
    (row,) = rows(ags)
    assert row["chunks_total"] == 3 and row["chunks_done"] == 1 and row["status"] == "pending"
    run_pass(ags, advance=100)
    (row,) = rows(ags)
    assert row["status"] == "delivered" and len(json.loads(row["message_ids"])) == 3
    texts = [b["content"][0] for b in fake.bodies]
    assert texts == ["a", "b", "b", "c"], "chunk 1 sent once ever, chunk 2 retried"


def test_chunks_carry_distinct_stable_nonces(ags):
    fake = FakeDiscord([200, 500, 200, 200])
    post(ags, None, content=long_text(), fake=fake)
    run_pass(ags, advance=100)
    nonces = [b["nonce"] for b in fake.bodies]
    assert len(set(nonces[:2] + nonces[3:])) == 3, "three chunks, three distinct nonces"
    assert nonces[1] == nonces[2], "the same chunk keeps its nonce across a retry"
    assert all(len(n) <= 25 and b["enforce_nonce"] is True for n, b in zip(nonces, fake.bodies))


def test_reply_reference_only_on_first_chunk_and_flags(ags):
    fake = FakeDiscord()
    ags.http_session = fake
    asyncio.run(ags.post_to_discord("amos", "555", long_text(), reply_to="42", dead_letter=True))
    assert fake.bodies[0]["message_reference"] == {"message_id": "42"}
    assert all("message_reference" not in b for b in fake.bodies[1:])
    assert all("flags" not in b for b in fake.bodies)


# ---------------------------------------------------------------------------
# Inline versus loop, ordering, restart
# ---------------------------------------------------------------------------

def test_a_store_error_during_the_inline_send_never_reaches_the_turn(ags, monkeypatch, skip_log):
    def boom(*a, **k):
        raise ob.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ob, "record_chunk", boom)
    monkeypatch.setattr(ob, "record_failure", boom)
    result, fake = post(ags, [200], content="reply")
    assert result is None and fake.calls == 1                 # no exception into finish_turn
    assert rows(ags)[0]["status"] == "sending"               # recovered at the next boot
    assert any("outbox unavailable" in m for _, m in skip_log)
    monkeypatch.undo()
    ags._outbox_conn = None
    assert asyncio.run(self_recover(ags)) == 1


def test_loop_pass_during_an_inline_send_does_not_double_send(ags):
    async def go():
        fake = FakeDiscord()
        fake.gate, fake.entered = asyncio.Event(), asyncio.Event()
        ags.http_session = fake
        task = asyncio.create_task(ags.post_to_discord("amos", "555", "once", dead_letter=True))
        await fake.entered.wait()
        ags.CLOCK["t"] += 1000
        assert await ags.outbox_pass() == 0          # the row is 'sending': the loop cannot take it
        fake.gate.set()
        return await task, fake
    result, fake = asyncio.run(go())
    assert result == "msg-1" and fake.calls == 1 and rows(ags)[0]["status"] == "delivered"


def test_a_new_reply_never_overtakes_one_in_backoff(ags):
    fake = FakeDiscord([500])
    first, _ = post(ags, None, content="first", fake=fake)
    assert first is None and rows(ags)[0]["status"] == "pending"
    second, _ = post(ags, None, content="second", fake=fake)
    assert second is None and fake.calls == 1, "returned at once, no HTTP"
    assert [r["status"] for r in rows(ags)] == ["pending", "pending"]
    run_pass(ags, advance=100)
    run_pass(ags, advance=1)
    assert [b["content"] for b in fake.bodies] == ["first", "first", "second"]
    assert [r["status"] for r in rows(ags)] == ["delivered", "delivered"]


def test_another_channel_is_not_blocked(ags):
    fake = FakeDiscord([500])
    post(ags, None, content="a", channel="1", fake=fake)
    other, _ = post(ags, None, content="b", channel="2", fake=fake)
    assert other == "msg-2"


def test_restart_delivers_a_pending_reply_exactly_once(ags):
    # A POST whose response is lost (crash/connection) is retried with the same nonce.
    fake = FakeDiscord([ConnectionResetError("lost"), 200])
    post(ags, None, content="survive", fake=fake)
    ags._outbox_conn = None                                    # simulated restart
    asyncio.run(self_recover(ags))
    run_pass(ags, advance=100)
    ok = [b for b in fake.bodies]
    assert len(ok) == 2 and ok[0]["nonce"] == ok[1]["nonce"]
    assert rows(ags)[0]["status"] == "delivered"
    assert run_pass(ags, advance=100) == 0


async def self_recover(ags):
    conn = ags._outbox_store(create=False)
    return ob.recover_sending(conn, ags.CLOCK["t"])


def test_a_row_left_sending_is_recovered_and_sent(ags):
    conn = ags._outbox_store()
    rid, claimed = ob.enqueue(conn, "amos", "555", "hello", claimed=True, now=T0, chunks_total=1)
    assert claimed
    ags._outbox_conn = None
    assert asyncio.run(self_recover(ags)) == 1
    ags.http_session = FakeDiscord()
    assert run_pass(ags, advance=1) == 1
    assert rows(ags)[0]["status"] == "delivered"


def test_the_loop_task_delivers_and_recovers_at_startup(ags):
    async def go():
        conn = ags._outbox_store()
        ob.enqueue(conn, "amos", "555", "stranded", claimed=True, now=T0, chunks_total=1)
        ags._outbox_conn = None
        ags.http_session = FakeDiscord()
        ags.OUTBOX_CLOCK = lambda: T0 + 5
        task = asyncio.create_task(ags.outbox_loop())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if rows(ags) and rows(ags)[0]["status"] == "delivered":
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert rows(ags)[0]["status"] == "delivered"
    assert "recovered" in [r[0] for r in ags._outbox_conn.execute("SELECT event FROM outbox_events")]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_every_state_change_writes_one_event_and_none_hold_content(ags):
    secret = "ZEBRA-distinct-9981"
    fake = FakeDiscord([500, 200])
    post(ags, None, content=secret, fake=fake)
    run_pass(ags, advance=100)
    (row,) = rows(ags)
    ev = [r["event"] for r in ags._outbox_conn.execute("SELECT event FROM outbox_events ORDER BY id")]
    assert ev == ["enqueued", "attempt_failed", "chunk_delivered", "delivered"]
    details = [r[0] or "" for r in ags._outbox_conn.execute("SELECT detail FROM outbox_events")]
    assert all(secret not in d and len(d) <= 200 for d in details)
    assert secret not in (row["last_error"] or "")


# ---------------------------------------------------------------------------
# B2: empty, whitespace, PASS never post
# ---------------------------------------------------------------------------

SKIPPED = ["", "   \n\t", "​‍⁠", "PASS", " pass. ", "**PASS**"]


@pytest.mark.parametrize("durable", [True, False])
@pytest.mark.parametrize("text", SKIPPED)
def test_b2_guard_skips_empty_and_pass(ags, skip_log, text, durable):
    result, fake = post(ags, [200], content=text, dead_letter=durable)
    assert result is None and fake.calls == 0
    assert rows(ags) == [] and not ags.OUTBOX_PATH.exists()
    assert ags.dead_letter_count() == 0
    skips = [m for _, m in skip_log if m.startswith("skip post (")]
    assert len(skips) == 1 and "agent=amos channel=555" in skips[0]
    assert ("(pass)" in skips[0]) == ("pass" in text.lower())


@pytest.mark.parametrize("text", ["PASS/WARN/FAIL: looks fine", "pass the salt"])
def test_b2_real_text_containing_pass_is_posted(ags, text):
    result, fake = post(ags, [200], content=text)
    assert result == "msg-1" and fake.bodies[0]["content"] == text


def test_b2_empty_payload_makes_no_call(ags):
    ags.http_session = FakeDiscord()
    assert asyncio.run(ags.post_discord_payload("amos", "555", {"content": "  \n"})) is None
    assert asyncio.run(ags.post_discord_payload("amos", "555", {})) is None
    assert ags.http_session.calls == 0
    ags.http_session = FakeDiscord()
    assert asyncio.run(ags.post_discord_payload("amos", "555", {"embeds": [{"title": "q"}]})) == "msg-1"


def test_b2_a_400_is_not_retried_on_the_direct_path(ags):
    _, fake = post(ags, [400, 200], content="hello", dead_letter=False)
    assert fake.calls == 1


def test_b2_split_returns_no_chunks_for_blank(ags):
    assert ags.split_discord_message("") == [] and ags.split_discord_message("  \n") == []


def test_b2_a_pass_turn_completes_without_a_post(harness):
    h = harness(agents=["a"])
    real_import = h._import_script
    grabbed = {}

    def import_and_keep_real_poster(name):
        mod = real_import(name)
        grabbed["post"] = mod.post_to_discord       # the harness replaces it with a recorder
        return mod
    h._import_script = import_and_keep_real_poster
    fake = FakeDiscord()

    async def scenario():
        async with h:
            h.module.post_to_discord = grabbed["post"]
            h.module.AGENT_TOKENS["a"] = "tok"
            old, h.module.http_session = h.module.http_session, fake
            await old.close()
            h.script(default={"text": "PASS"})
            await h.send("a", "hi", channel_id="77")
            await h.wait_idle("a")

    asyncio.run(scenario())
    (row,) = h.queue_rows("a")
    assert row["processed"] == 2 and row["response"] == "PASS" and row["discord_response_id"] is None
    assert fake.bodies == []


# ---------------------------------------------------------------------------
# Crash-recovery sweep
# ---------------------------------------------------------------------------

def _sweep(ags, rows_spec, fake=None):
    """Insert COMPLETE rows (channel_id '55', agent amos) and run crash_recovery."""
    ags.http_session = fake or FakeDiscord()

    async def go():
        await ags.init_db()
        for i, (response, age_h) in enumerate(rows_spec):
            await ags.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
                " processed, response, processed_at) VALUES ('amos','c','55','u','x',?,2,?,"
                " datetime('now', ?))", (f"m{i}", response, f"-{age_h} hours"))
        await ags.db.commit()
        await ags.crash_recovery()
        async with ags.db.execute("SELECT message_id, discord_response_id FROM message_queue ORDER BY id") as c:
            out = [tuple(r) for r in await c.fetchall()]
        await ags.db.close()
        return out
    return asyncio.run(go())


@pytest.fixture
def real_clock(ags):
    import time
    ags.OUTBOX_CLOCK = time.time
    return ags


def test_sweep_skips_pass_blank_and_old_rows_and_delivers_recent_through_the_outbox(real_clock):
    ags = real_clock
    fake = FakeDiscord()
    out = _sweep(ags, [("PASS", 1), ("  \n", 1), ("old reply", 30), ("real reply", 1)], fake)
    assert [b["content"] for b in fake.bodies] == ["real reply"]
    assert [i for _, i in out] == [None, None, None, "msg-1"]
    (row,) = rows(ags)
    assert row["status"] == "delivered" and "nonce" in fake.bodies[0]


def test_sweep_does_not_repost_what_the_outbox_owns(real_clock):
    ags = real_clock
    import time
    conn = ags._outbox_store()
    sha = ob.content_sha("waiting reply")
    ob.enqueue(conn, "amos", "55", "waiting reply", now=time.time(), content_sha=sha)
    fake = FakeDiscord()
    out = _sweep(ags, [("waiting reply", 0)], fake)
    assert fake.calls == 0 and out[0][1] is None


def test_sweep_backfills_the_id_of_a_reply_the_outbox_delivered(real_clock):
    ags = real_clock
    import time
    conn = ags._outbox_store()
    rid, _ = ob.enqueue(conn, "amos", "55", "done reply", claimed=True, now=time.time())
    ob.record_chunk(conn, rid, "disc-9", time.time())
    ob.mark_delivered(conn, rid, time.time())
    fake = FakeDiscord()
    out = _sweep(ags, [("done reply", 0)], fake)
    assert fake.calls == 0 and out[0][1] == "disc-9"


def test_sweep_leaves_dead_rows_to_the_operator(real_clock):
    ags = real_clock
    import time
    conn = ags._outbox_store()
    rid, _ = ob.enqueue(conn, "amos", "55", "dead reply", now=time.time())
    ob.claim_due(conn, time.time() + 1)
    ob.record_failure(conn, rid, "permanent", 403, "HTTP 403", None, time.time())
    fake = FakeDiscord()
    out = _sweep(ags, [("dead reply", 0)], fake)
    assert fake.calls == 0 and out[0][1] is None


def _sweep_scenario(ags, fake, steps):
    """One event loop, one open db: insert a recent COMPLETE row, then run
    `steps` (async callables taking ags) after each of which state is observable."""
    ags.http_session = fake

    async def go():
        await ags.init_db()
        await ags.db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
            " processed, response, processed_at) VALUES ('amos','c','55','u','x','m0',2,"
            " 'the reply', datetime('now', '-1 hours'))")
        await ags.db.commit()
        out = []
        for step in steps:
            await step(ags)
            async with ags.db.execute("SELECT discord_response_id FROM message_queue") as c:
                out.append([r[0] for r in await c.fetchall()])
        await ags.db.close()
        return out
    return asyncio.run(go())


async def _sweep_step(a):
    await a.crash_recovery()


def _advance(seconds):
    async def step(a):
        a.CLOCK["t"] += seconds
    return step


def test_sweep_does_not_re_enqueue_after_a_long_outage(ags):
    """A reply the sweep enqueued while Discord was down is found by key on a
    later boot, however much later: one row, one successful POST."""
    fake = FakeDiscord([503])
    _sweep_scenario(ags, fake, [_sweep_step, _advance(600), _sweep_step])
    (row,) = rows(ags)
    assert row["queue_message_id"] == "m0" and row["status"] == "pending"
    assert fake.calls == 1
    run_pass(ags, advance=3600)
    assert fake.calls == 2 and rows(ags)[0]["status"] == "delivered"
    assert [r["queue_message_id"] for r in rows(ags)] == ["m0"]


def test_loop_delivery_writes_the_id_back_and_a_later_sweep_posts_nothing(ags):
    fake = FakeDiscord([503])

    async def loop_pass(a):
        a.CLOCK["t"] += 3600
        await a.outbox_pass()

    out = _sweep_scenario(ags, fake, [_sweep_step, loop_pass, _advance(600), _sweep_step])
    assert out[0] == [None] and out[1] == [["msg-2"]][0]
    assert out[3] == ["msg-2"]
    assert fake.calls == 2 and len(rows(ags)) == 1


def test_turn_path_enqueue_carries_the_queue_key(ags):
    src = (PACKAGE_ROOT / "lib" / "turn_loop.py").read_text()
    call = [c for c in ast.walk(ast.parse(src)) if isinstance(c, ast.Call)
            and _kwarg(c, "dead_letter") is not None]
    assert call and all(_kwarg(c, "queue_message_id") is not None for c in call)


def test_an_outbox_db_from_an_earlier_build_gains_the_key_column(tmp_path):
    import sqlite3
    path = tmp_path / "o.db"
    old = sqlite3.connect(path)
    old.executescript(ob.SCHEMA.replace(",\n  queue_message_id TEXT", ""))
    old.close()
    conn = ob.open_store(path)
    assert "queue_message_id" in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    rid, _ = ob.enqueue(conn, "a", "1", "x", queue_message_id="q1")
    assert ob.find_by_queue_id(conn, "q1")["id"] == rid and ob.find_by_queue_id(conn, "q2") is None


# ---------------------------------------------------------------------------
# One door: every channel-messages POST is guarded
# ---------------------------------------------------------------------------

def _posts_to_messages(func):
    return [n for n in ast.walk(func) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "post"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "http_session"]


def test_only_guarded_functions_post_channel_messages():
    allowed = {"_post_direct", "outbox_send_row", "post_discord_payload"}
    seen = set()
    for path in [AGENT_SERVER, *sorted((PACKAGE_ROOT / "lib").glob("*.py"))]:
        tree = _tree(path)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _posts_to_messages(func):
                src = ast.get_source_segment(path.read_text(), func)
                if "/messages" not in src:
                    continue   # not a channel-messages URL (e.g. an interaction callback)
                if func.name == "ux_create_thread":
                    # 6.2: creates a thread on a message; carries no content.
                    assert '/threads"' in src and '"content"' not in src
                    continue
                assert func.name in allowed, f"{path.name}:{func.name} posts to channel messages unguarded"
                seen.add(func.name)
                if func.name == "post_discord_payload":
                    assert _calls_to(func, "has_visible"), "payload path lost its empty check"
                else:
                    assert any(isinstance(c.func, ast.Attribute) and
                               getattr(c.func.value, "id", None) == "post_guard"
                               for c in ast.walk(func) if isinstance(c, ast.Call)), \
                        f"{func.name} does not call post_guard"
    assert seen == allowed
