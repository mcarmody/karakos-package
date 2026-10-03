"""Context budget, handoff note and reset policy end to end (step 2.6): the real
agent-server, a fake `claude`.

Fixture: agent `a` (shards `a` and `a-2`) with `context_budget_tokens: 50000`
and `handoff_on_reset: true`; agent `b` with neither.
"""

import asyncio
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from harness import handoff_rule

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import session_policy as sp  # noqa: E402

AGENTS = {"a": {"context_budget_tokens": 50000, "handoff_on_reset": True}, "b": {}}
SHARDS = {"a": ["a", "a-2"]}
BIG = {"input_tokens": 10, "cache_creation_input_tokens": 0,
       "cache_read_input_tokens": 60000, "output_tokens": 5}
SMALL = {"input_tokens": 10, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 2000, "output_tokens": 5}
NOTE = "NOTE-FROM-OLD-SESSION"


def run(coro):
    return asyncio.run(coro)


def rule(match, step, shard=None):
    r = {"match": match, "step": step}
    if shard:
        r["shard"] = shard
    return r


def big_rules(extra=()):
    return [rule("TRIGGER", {"text": "big", "usage": BIG}, shard="^a$"),
            rule("PING", {"text": "pong", "usage": BIG}, shard="^a$"),
            *extra]


def make(harness, agents=AGENTS, shards=SHARDS):
    return harness(agents=agents, shards=shards)


async def reset_done(h, shard, old_sid, timeout=20):
    await h.wait_for(lambda: h.session_id(shard) != old_sid
                     and h.module.agent_states.get(shard) == "IDLE"
                     and not h.module.STATE.session_policy.resetting.get(shard)
                     and h.argv(shard) is not None, timeout)
    await h.wait_idle(shard, timeout=timeout)


def handoff_rows(h, shard):
    return [r for r in h.queue_rows(shard) if r["channel"] == "handoff"]


def appended(h, shard):
    argv = h.argv(shard)
    return argv[argv.index("--append-system-prompt") + 1] if "--append-system-prompt" in argv else ""


def pids(h):
    return {s: h.module.agent_processes[s].pid for s in ("a", "a-2", "b")}


def test_policy_hooks_register_after_hive(harness):
    h = make(harness)

    async def scenario():
        async with h:
            hooks = h.module.STATE.hooks
            assert hooks.on_turn_end[0] is h.module.hive_on_turn_end
            assert hooks.on_turn_end[-1] is h.module.session_policy_on_turn_end

    run(scenario())


# -- budget reset with handoff ----------------------------------------------------------

def test_budget_reset_runs_a_handoff_then_a_fresh_session_carries_the_note(harness):
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"},
                     rules=[handoff_rule(NOTE, shard="^a$"), *big_rules()])
            before = pids(h)
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await reset_done(h, "a", old)
            return old, before, pids(h)

    old, before, after = run(scenario())
    (row,) = handoff_rows(h, "a")
    assert row["priority"] == sp.HANDOFF_PRIORITY and row["channel_id"] == "0"
    assert row["author"] == "session-handoff" and row["is_bot"] == 1
    assert row["processed"] == 2 and row["owner_agent"] == "a"
    # the old session ran the human turn, then the handoff prompt
    old_in = (h.log_dir / f"{old}.in.jsonl").read_text()
    assert "TRIGGER" in old_in and "[handoff]" in old_in
    # nothing was posted for the handoff turn
    assert [m["content"] for m in h.discord if m["agent"] == "a"] == ["big"]
    # written, then rotated; the fresh spawn's argv carries it
    files = h.handoff_files("a")
    assert "a.md" not in files and len(files) == 1
    assert (h.workspace / "data" / "handoff" / files[0]).read_text() == NOTE
    assert h.session_id("a") != old
    assert NOTE in appended(h, "a")
    assert "handoff note from your previous session" in appended(h, "a")
    # the siblings are untouched
    assert after["a-2"] == before["a-2"] and after["b"] == before["b"]
    assert after["a"] != before["a"]


def test_no_budget_means_no_policy_activity(harness):
    h = make(harness, agents={"a": {}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok", "usage": BIG})
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await h.wait_idle("a")
            return old

    old = run(scenario())
    assert h.session_id("a") == old and not handoff_rows(h, "a")


# -- the handoff never blocks the reset -----------------------------------------------------

def test_a_hung_handoff_is_interrupted_and_the_human_row_runs_on_the_new_session(
        harness, monkeypatch):
    monkeypatch.setattr(sp, "HANDOFF_TURN_TIMEOUT_S", 2)
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"},
                     rules=[handoff_rule(hang=True, shard="^a$"), *big_rules()])
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await h.wait_for(lambda: handoff_rows(h, "a"), timeout=15)
            await h.send("a", "HUMAN-WHILE-HANDOFF")
            await reset_done(h, "a", old, timeout=30)
            await h.wait_idle("a", timeout=30)
            return old

    old = run(scenario())
    assert h.session_id("a") != old
    assert "--append-system-prompt" not in h.argv("a") or \
        "handoff note" not in appended(h, "a")
    assert any("HUMAN-WHILE-HANDOFF" in t for t in h.sent_to("a"))
    assert "ok" in [m["content"] for m in h.discord if m["agent"] == "a"]
    human = [r for r in h.queue_rows("a") if r["content"] == "HUMAN-WHILE-HANDOFF"]
    assert human[0]["processed"] == 2
    assert not h.handoff_files("a")


# -- overflow and the gates ----------------------------------------------------------------------

def test_overflow_resets_at_once_with_no_handoff(harness):
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("OVER", {"text": "prompt is too long", "is_error": True}, shard="^a$"),
                handoff_rule(NOTE, shard="^a$")])
            assert h.module.classify_wall("prompt is too long", True, None) is None
            old = h.session_id("a")
            await h.send("a", "OVER")
            await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert not handoff_rows(h, "a") and not h.handoff_files("a")
    assert h.session_id("a") != old
    assert not [r for r in h.queue_rows("a") if r["not_before"]]      # no hold


def test_overflow_resets_an_agent_with_no_budget(harness):
    h = make(harness, agents={"a": {"handoff_on_reset": True}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("OVER", {"text": "prompt is too long", "is_error": True}, shard="^a$"),
                handoff_rule(NOTE, shard="^a$")])
            old = h.session_id("a")
            await h.send("a", "OVER")
            await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert h.session_id("a") != old
    assert not handoff_rows(h, "a")


def test_an_ordinary_error_without_overflow_text_does_not_reset(harness):
    h = make(harness, agents={"a": {"context_budget_tokens": 50000}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("BOOM", {"text": "some other failure", "is_error": True}, shard="^a$")])
            old = h.session_id("a")
            await h.send("a", "BOOM")
            await h.wait_idle("a")
            return old

    old = run(scenario())
    assert h.session_id("a") == old


def test_a_held_shard_resets_without_a_handoff(harness):
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[handoff_rule(NOTE, shard="^a$"),
                                                    *big_rules()])
            real = h.module.agent_hold_until
            calls = []

            async def held_after_first(shard, now=None):
                calls.append(shard)
                if shard == "a" and len(calls) > 1:
                    return int(time.time()) + 3600
                return None

            h.module.agent_hold_until = held_after_first
            try:
                old = h.session_id("a")
                await h.send("a", "TRIGGER")
                await reset_done(h, "a", old)
            finally:
                h.module.agent_hold_until = real

    run(scenario())
    assert not handoff_rows(h, "a") and not h.handoff_files("a")


def test_an_open_account_breaker_resets_without_a_handoff(harness, monkeypatch):
    h = make(harness)

    async def scenario():
        async with h:
            monkeypatch.setattr(h.module.usage_gate, "account_paused", lambda: True)
            h.script(default={"text": "ok"}, rules=[handoff_rule(NOTE, shard="^a$"),
                                                    *big_rules()])
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert h.session_id("a") != old
    assert not handoff_rows(h, "a") and not h.handoff_files("a")


def test_handoff_off_resets_cold(harness):
    h = make(harness, agents={"a": {"context_budget_tokens": 50000,
                                    "handoff_on_reset": False}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[handoff_rule(NOTE), *big_rules()])
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await reset_done(h, "a", old)

    run(scenario())
    assert not handoff_rows(h, "a")


# -- no loop; hive call turn ---------------------------------------------------------------------

def test_two_triggers_inside_the_interval_give_one_reset_and_one_warning(harness, caplog):
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"},
                     rules=[handoff_rule(NOTE, shard="^a$"), *big_rules()])
            old = h.session_id("a")
            with caplog.at_level(logging.WARNING, logger="agent-server"):
                await h.send("a", "TRIGGER")
                await reset_done(h, "a", old)
                fresh = h.session_id("a")
                await h.send("a", "PING")
                await h.wait_idle("a")
            return fresh

    fresh = run(scenario())
    assert h.session_id("a") == fresh
    assert len(handoff_rows(h, "a")) == 1
    skipped = [r for r in caplog.records
               if r.levelno == logging.WARNING and "reset (context_budget) skipped" in r.getMessage()]
    assert len(skipped) == 1


def test_a_hive_call_turn_never_ends_in_a_reset(harness):
    h = make(harness)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("GO", {"mcp": [{"tool": "hive_call", "args": {"to": "a", "question": "q?"}}],
                            "text": "{{mcp:0.answer}}"}, shard="^b$"),
                rule("hive call from b", {"text": "42", "usage": BIG}, shard="^a$"),
                handoff_rule(NOTE, shard="^a$"), *big_rules()])
            old = h.session_id("a")
            await h.send("b", "GO")
            await h.wait_idle("b", timeout=20)
            await h.wait_idle("a", timeout=20)
            same_after_call = h.session_id("a") == old
            await h.send("a", "PING")
            await reset_done(h, "a", old)
            return same_after_call

    assert run(scenario()) is True
    # the ordinary turn after the call is where the reset happened
    (row,) = handoff_rows(h, "a")
    assert row["processed"] == 2


# -- reload keeps the note, reset consumes it ----------------------------------------------------

def test_reload_leaves_the_note_and_the_next_reset_consumes_it_once(harness):
    h = make(harness, agents={"a": {}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"})
            conn = sqlite3.connect(str(h.module.DB_PATH))
            conn.execute("UPDATE sessions SET last_compacted = datetime('now', '-1 hour')"
                         " WHERE agent = 'a'")
            conn.commit()
            conn.close()
            note = h.workspace / "data" / "handoff" / "a.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("RELOAD-NOTE")
            assert h.argv("a") is not None  # first spawn's argv written before we clear it
            argv_file = h.log_dir / f"{h.session_id('a')}.argv.json"
            argv_file.unlink()          # same session id after a reload: wait for the new spawn's
            r = await h.client.post("/agents/a/reload", headers=h._headers())
            assert r.status == 200
            await h.wait_for(lambda: h.argv("a") is not None)
            after_reload = (note.exists(), "RELOAD-NOTE" in " ".join(h.argv("a")))
            r = await h.client.post("/agents/a/reset", headers=h._headers())
            assert (await r.json())["status"] == "reset"
            await h.wait_for(lambda: h.argv("a") is not None)
            first = " ".join(h.argv("a")).count("RELOAD-NOTE")
            gone = not note.exists()
            await h.client.post("/agents/a/reset", headers=h._headers())
            await h.wait_for(lambda: h.argv("a") is not None)
            second = " ".join(h.argv("a")).count("RELOAD-NOTE")
            return after_reload, first, gone, second

    after_reload, first, gone, second = run(scenario())
    assert after_reload == (True, False)
    assert (first, gone, second) == (1, True, 0)


def test_operator_reset_with_handoff_runs_the_handoff_first(harness):
    h = make(harness, agents={"a": {"handoff_on_reset": True}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[handoff_rule(NOTE, shard="^a$")])
            old = h.session_id("a")
            r = await h.client.post("/agents/a/reset?handoff=1", headers=h._headers())
            body = await r.json()
            await reset_done(h, "a", old)
            return body

    body = run(scenario())
    assert body["status"] == "scheduled"
    assert len(handoff_rows(h, "a")) == 1 and NOTE in appended(h, "a")


# -- agent-requested reset ---------------------------------------------------------------------------

def test_session_finalize_schedules_a_reset_at_turn_end_and_runs_no_summarizer(harness):
    h = make(harness, agents={"a": {"handoff_on_reset": True}, "b": {}}, shards=None)
    marker = h.workspace / "summarizer-ran"
    script = h.workspace / "bin" / "summarize-session.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\nraise SystemExit(1)\n")

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=[
                rule("FIN", {"mcp": [{"tool": "session", "args": {"action": "finalize"}}],
                             "text": "status={{mcp:0.status}}"}, shard="^a$"),
                handoff_rule(NOTE, shard="^a$")])
            old = h.session_id("a")
            await h.send("a", "FIN")
            await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert "status=scheduled" in [m["content"] for m in h.discord if m["agent"] == "a"]
    assert h.session_id("a") != old and NOTE in appended(h, "a")
    assert not marker.exists()
    assert len(handoff_rows(h, "a")) == 1


def test_finalize_for_an_unknown_shard_is_a_404(harness):
    h = make(harness, agents={"a": {}, "b": {}}, shards=None)

    async def scenario():
        async with h:
            r = await h.client.post("/agents/nope/session/finalize", headers=h._headers())
            r2 = await h.client.post("/agents/a/session/finalize")
            return r.status, r2.status

    assert run(scenario()) == (404, 401)


# -- the summary is retired --------------------------------------------------------------------------------

def test_the_server_has_no_summary_machinery():
    src = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    for name in ("load_last_session", "LAST_SUMMARY_TEMPLATE", "summarize-session"):
        assert name not in src, name


# -- compact mode (patched on) ---------------------------------------------------------------------------------

def compact_rules(boundary=True):
    step = {"text": "compacted", "usage": SMALL}
    if boundary:
        step.update(compact=True, pre_tokens=60000, post_tokens=2000)
    return [rule("/compact", step, shard="^a$"),
            handoff_rule(NOTE, shard="^a$"), *big_rules()]


COMPACT_AGENTS = {"a": {"context_budget_tokens": 50000, "handoff_on_reset": True,
                        "reset_mode": "compact"}, "b": {}}


def context_tokens(h, shard):
    return h._query("SELECT context_tokens FROM sessions WHERE agent = ?", (shard,))[0][
        "context_tokens"]


def test_compact_keeps_the_session_and_shrinks_the_context(harness, monkeypatch):
    monkeypatch.setattr(sp, "COMPACT_VERIFIED", True)
    h = make(harness, agents=COMPACT_AGENTS, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=compact_rules())
            old = h.session_id("a")
            pid = h.module.agent_processes["a"].pid
            await h.send("a", "TRIGGER")
            await h.wait_for(lambda: [r for r in handoff_rows(h, "a") if r["processed"] == 2],
                             timeout=15)
            await h.wait_idle("a")
            return old, pid, h.module.agent_processes["a"].pid

    old, pid, pid_after = run(scenario())
    assert h.session_id("a") == old and pid_after == pid
    assert context_tokens(h, "a") < 50000
    (row,) = handoff_rows(h, "a")
    assert row["content"].startswith("/compact")
    assert [m["content"] for m in h.discord if m["agent"] == "a"] == ["big"]


def test_compact_without_a_boundary_falls_back_to_a_reset(harness, monkeypatch):
    monkeypatch.setattr(sp, "COMPACT_VERIFIED", True)
    h = make(harness, agents=COMPACT_AGENTS, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=compact_rules(boundary=False))
            old = h.session_id("a")
            await h.send("a", "TRIGGER")
            await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert h.session_id("a") != old
    assert NOTE in appended(h, "a")


def test_compact_unverified_warns_once_and_behaves_as_reset(harness, caplog):
    assert sp.COMPACT_VERIFIED is False
    h = make(harness, agents=COMPACT_AGENTS, shards=None)

    async def scenario():
        async with h:
            h.script(default={"text": "ok"}, rules=compact_rules())
            old = h.session_id("a")
            with caplog.at_level(logging.WARNING, logger="agent-server"):
                await h.send("a", "TRIGGER")
                await reset_done(h, "a", old)
            return old

    old = run(scenario())
    assert h.session_id("a") != old
    assert not [r for r in handoff_rows(h, "a") if r["content"].startswith("/compact")]
    assert len([r for r in caplog.records if "COMPACT_VERIFIED" in r.getMessage()]) == 1


# -- steering guard (2.5) ------------------------------------------------------------------------------------------

@pytest.mark.skipif(not (PACKAGE_ROOT / "lib" / "steering.py").exists(),
                    reason="2.5 (lib/steering.py) has not merged; the predicate and its "
                           "test land with whichever step merges second")
def test_a_human_line_during_the_handoff_turn_is_not_steered():
    import steering  # noqa: F401
    pytest.skip("written when 2.5 merges")
