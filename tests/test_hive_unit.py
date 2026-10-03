"""lib/hive.py: pure rules for buzz and hive call (step 2.3)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import hive  # noqa: E402
from shards import ShardSpec  # noqa: E402

SPECS = [ShardSpec("a", "a"), ShardSpec("a-2", "a", (), False),
         ShardSpec("b", "b"), ShardSpec("c", "c")]


def pick(to, caller, states=None, depths=None, waits=None, specs=SPECS, **kw):
    return hive.pick_callee(specs, to, caller, states or {}, depths or {},
                            waits or {}, **kw)


# -- deadlock ----------------------------------------------------------------

def test_would_deadlock_self_and_empty_graph():
    assert hive.would_deadlock({}, "a", "a")
    assert not hive.would_deadlock({}, "a", "b")


def test_would_deadlock_direct_pair_and_three_cycle():
    assert hive.would_deadlock({"a": {"b"}}, "b", "a")
    waits = {"a": {"b"}, "b": {"c"}}  # a waits on b, b waits on c
    assert hive.would_deadlock(waits, "c", "a")
    assert not hive.would_deadlock(waits, "c", "d")


def test_would_deadlock_chain_that_does_not_return():
    assert not hive.would_deadlock({"a": {"b"}, "b": {"c"}}, "d", "a")
    assert not hive.would_deadlock({"b": {"c"}}, "a", "b")


# -- pick_callee ---------------------------------------------------------------

def test_pick_shard_id():
    assert pick("b", "a") == ("b", None)
    assert pick("a-2", "a") == ("a-2", None)


def test_pick_agent_prefers_idle_then_shortest_queue_then_plan_order():
    specs = [ShardSpec("x", "x"), ShardSpec("x-2", "x", (), False),
             ShardSpec("x-3", "x", (), False), ShardSpec("me", "me")]
    st = {"x": "PROCESSING", "x-2": "IDLE", "x-3": "IDLE"}
    assert pick("x", "me", st, specs=specs) == ("x-2", None)
    assert pick("x", "me", st, {"x-2": 3, "x-3": 1}, specs=specs) == ("x-3", None)
    st = {"x": "PROCESSING", "x-2": "PROCESSING", "x-3": "PROCESSING"}
    assert pick("x", "me", st, {"x": 2, "x-2": 1, "x-3": 1}, specs=specs) == ("x-2", None)


def test_pick_own_agent_gets_sibling():
    assert pick("a", "a") == ("a-2", None)
    assert pick("a", "a-2") == ("a", None)


def test_pick_own_agent_single_shard_is_self_call_or_own_for_buzz():
    assert pick("b", "b") == (None, "self_call")
    assert pick("b", "b", kind="buzz") == ("b", None)


def test_pick_named_own_shard():
    assert pick("a-2", "a-2") == (None, "self_call")
    assert pick("a-2", "a-2", kind="buzz") == ("a-2", None)


def test_pick_unknown():
    assert pick("nobody", "a") == (None, "unknown_target")


def test_pick_all_cyclic_is_deadlock_and_first_non_cyclic_wins():
    waits = {"b": {"a"}}  # b is blocked on a
    assert pick("b", "a", waits=waits) == (None, "deadlock")
    specs = [ShardSpec("x", "x"), ShardSpec("x-2", "x", (), False), ShardSpec("me", "me")]
    waits = {"x": {"me"}}
    assert pick("x", "me", waits=waits, specs=specs) == ("x-2", None)
    waits = {"x": {"me"}, "x-2": {"me"}}
    assert pick("x", "me", waits=waits, specs=specs) == (None, "deadlock")


def test_pick_buzz_is_not_deadlock_checked():
    assert pick("b", "a", waits={"b": {"a"}}, kind="buzz") == ("b", None)


def test_pick_skips_error_recovery_and_paused():
    assert pick("a", "b", {"a": "ERROR_RECOVERY"}) == ("a-2", None)
    assert pick("b", "a", {"b": "ERROR_RECOVERY"}) == (None, "no_available_shard")
    assert pick("a", "b", paused=("a", "a-2")) == (None, "callee_paused")
    assert pick("a", "b", paused=("a",)) == ("a-2", None)


# -- depth, timeout, rows -----------------------------------------------------

def test_max_depth_constant_and_next_depth():
    assert hive.HIVE_MAX_DEPTH == 2
    assert hive.next_depth([]) == 1
    assert hive.next_depth([{"depth": None}, {"depth": 0}]) == 1
    assert hive.next_depth([{"depth": 1}, {"depth": 2}, {}]) == 3


def test_clamp_timeout_edges():
    assert hive.clamp_timeout(0) == hive.HIVE_MIN_TIMEOUT_S
    assert hive.clamp_timeout(-3) == hive.HIVE_MIN_TIMEOUT_S
    assert hive.clamp_timeout(10_000) == hive.HIVE_MAX_TIMEOUT_S
    assert hive.clamp_timeout(30) == 30
    assert hive.clamp_timeout(None) == hive.HIVE_DEFAULT_TIMEOUT_S
    assert hive.clamp_timeout("x") == hive.HIVE_DEFAULT_TIMEOUT_S


def test_new_call_id_shape():
    cid = hive.new_call_id()
    assert cid.startswith("c-") and len(cid) == 14
    assert cid != hive.new_call_id()


def test_call_row_columns_and_expiry():
    row = hive.call_row("c-1", "a", "a (a-2)", "b", "b", "why?", 2, 30, now=1_000_000)
    assert row["agent"] == "b" and row["reply_to_agent"] == "a"
    assert row["channel"] == "hive" and row["channel_id"] == "0"
    assert row["message_id"] == "call-c-1" and row["call_id"] == "c-1"
    assert row["author"] == "a (a-2)" and row["owner_agent"] == "b"
    assert row["depth"] == 2 and row["priority"] == 0 and row["is_bot"] == 1
    assert row["expires_at"] == "1970-01-12T13:47:10Z"
    assert row["content"].startswith("[hive call from a (a-2), depth 2 of 2] why?")
    assert "not posted to any channel" in row["content"]
    assert hive.is_call_row(row) and not hive.is_reply_row(row)


def test_buzz_row_columns():
    row = hive.buzz_row("a", "b", "b", "fyi", 1)
    assert row["message_id"].startswith("buzz-")
    assert row["call_id"] is None and row["reply_to_agent"] is None
    assert "expires_at" not in row and row["depth"] == 1
    assert row["content"] == "[buzz from a] fyi"
    assert not hive.is_call_row(row) and not hive.is_reply_row(row)


def test_reply_row_and_bodies():
    call = hive.call_row("c-1", "a", "a", "b", "b", "q", 1, 30)
    row = hive.reply_row(call, hive.answer_body("c-1", "42"))
    assert row["agent"] == "a" and row["author"] == "b" and row["channel"] == "call"
    assert row["message_id"] == "reply-c-1" and row["reply_to_agent"] is None
    assert row["depth"] == 1 and json.loads(row["content"]) == {"call_id": "c-1", "answer": "42"}
    assert hive.is_reply_row(row) and not hive.is_call_row(row)


def test_answer_truncation_flag():
    body = hive.answer_body("c", "x" * (hive.HIVE_MAX_ANSWER_CHARS + 5))
    assert len(body["answer"]) == hive.HIVE_MAX_ANSWER_CHARS and body["truncated"] is True
    assert "truncated" not in hive.answer_body("c", "short")


def test_error_codes_include_reserved_callee_paused():
    assert set(hive.HIVE_ERRORS) == {"expired", "callee_error", "callee_failed",
                                     "cancelled", "callee_paused"}
    assert hive.error_body("c", "callee_error", "boom") == {
        "call_id": "c", "error": "callee_error", "detail": "boom"}


# -- call_status ----------------------------------------------------------------

def _call(processed=0, response=None, expires="2099-01-01T00:00:00Z"):
    return {"call_id": "c", "processed": processed, "response": response,
            "expires_at": expires}


def _reply(body, processed=2, response=None):
    return {"content": json.dumps(body), "processed": processed, "response": response}


def test_call_status_all_six():
    now = 1_000_000.0
    assert hive.call_status(_call(1), None, True, now) == "pending"
    assert hive.call_status(_call(), _reply({"answer": "x"}), False, now) == "answered"
    assert hive.call_status(_call(4, "expired"), _reply({"error": "expired"}), False, now) == "expired"
    assert hive.call_status(_call(3), _reply({"error": "callee_error"}), False, now) == "error"
    assert hive.call_status(_call(3), None, False, now) == "error"
    assert hive.call_status(_call(4, "cancelled"), None, False, now) == "timeout"
    assert hive.call_status(_call(1), _reply({"answer": "x"}, 4, "late"), False, now) == "timeout"
    past = "1970-01-01T00:00:00Z"
    assert hive.call_status(_call(1, expires=past), None, True, now) == "timeout"
    assert hive.call_status(_call(4, "abandoned"), None, False, now) == "abandoned"
    assert hive.call_status(_call(0), None, False, now) == "abandoned"
    assert hive.call_status(_call(), _reply({"error": "callee_paused"}), False, now) == "error"
