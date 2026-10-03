"""GET /hive/calls: the hive call log (step 2.3). Rows are seeded by hand so
every status and filter is exercised without running a turn."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import hive  # noqa: E402

LONG = "q" * 300


def run(coro):
    return asyncio.run(coro)


async def seed(h, n, caller, callee, *, processed=0, response=None, reply=None,
               reply_processed=2, reply_response=None, created, expires="2099-01-01T00:00:00Z",
               question="why?", depth=1):
    cid = f"c-{n:03d}"
    db = h.module.db
    await db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, author, content, message_id,"
        " call_id, reply_to_agent, depth, expires_at, owner_agent, processed, response,"
        " created_at) VALUES (?, 'hive', '0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (callee, caller, hive.call_prompt(caller, question, depth), f"call-{cid}", cid,
         caller, depth, expires, callee, processed, response, created))
    if reply is not None:
        await db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
            " message_id, call_id, depth, processed, response, created_at)"
            " VALUES (?, 'call', '0', ?, ?, ?, ?, ?, ?, ?, ?)",
            (caller, callee, json.dumps(reply), f"reply-{cid}", cid, depth,
             reply_processed, reply_response, created.replace(":0", ":1", 1)))
    await db.commit()
    return cid


def test_log_statuses_filters_and_clipping(harness):
    h = harness(agents=["a", "b"])
    out = {}

    async def scenario():
        async with h:
            h.script(default={"text": "ok"})
            await seed(h, 1, "a", "b", processed=2, created="2026-01-01 10:00:00",
                       reply={"call_id": "c-001", "answer": "A" * 300, "duration_ms": 1234},
                       question=LONG)
            await seed(h, 2, "a", "b", processed=4, response="expired",
                       created="2026-01-01 10:01:00",
                       reply={"call_id": "c-002", "error": "expired"}, reply_processed=2)
            await seed(h, 3, "b", "a", processed=3, created="2026-01-01 10:02:00",
                       reply={"call_id": "c-003", "error": "callee_error", "detail": "x"})
            await seed(h, 4, "a", "b", processed=4, response="cancelled",
                       created="2026-01-01 10:03:00")
            await seed(h, 5, "a", "b", processed=4, response="abandoned",
                       created="2026-01-01 10:04:00", depth=2)
            await seed(h, 6, "a", "b", processed=1, created="2026-01-01 10:05:00")
            h.module.STATE.hive.open["c-006"] = hive.OpenCall("a", "b", 1, time.time() + 60)
            out["all"] = await h.hive_calls()
            out["limit"] = await h.hive_calls(limit=2)
            out["since"] = await h.hive_calls(since="2026-01-01T10:03:00Z")
            out["shard_b"] = await h.hive_calls(shard="b")
            for st in ("answered", "expired", "error", "timeout", "abandoned", "pending"):
                out[st] = await h.hive_calls(status=st)
            out["max"] = await h.hive_calls(limit=100000)

    run(scenario())
    ids = [c["call_id"] for c in out["all"]]
    assert ids == [f"c-00{i}" for i in (6, 5, 4, 3, 2, 1)]           # newest first
    assert [c["call_id"] for c in out["limit"]] == ["c-006", "c-005"]
    assert [c["call_id"] for c in out["since"]] == ["c-006", "c-005", "c-004"]
    assert {c["call_id"] for c in out["shard_b"]} == set(ids)          # a<->b both ways
    by = {c["call_id"]: c for c in out["all"]}
    assert {k: v["status"] for k, v in by.items()} == {
        "c-001": "answered", "c-002": "expired", "c-003": "error",
        "c-004": "timeout", "c-005": "abandoned", "c-006": "pending"}
    for st, cid in (("answered", "c-001"), ("expired", "c-002"), ("error", "c-003"),
                    ("timeout", "c-004"), ("abandoned", "c-005"), ("pending", "c-006")):
        assert [c["call_id"] for c in out[st]] == [cid]
    first = by["c-001"]
    assert len(first["question"]) == 200 and len(first["answer"]) == 200
    assert first["duration_ms"] == 1234 and first["from"] == "a" and first["to"] == "b"
    assert first["from_agent"] == "a" and first["to_agent"] == "b" and first["depth"] == 1
    assert by["c-005"]["depth"] == 2 and by["c-005"]["error"] == "abandoned"
    assert by["c-003"]["error"] == "callee_error"
    assert by["c-002"]["error"] == "expired" and by["c-004"]["error"] == "cancelled"
    assert set(first) == {"call_id", "from", "to", "from_agent", "to_agent", "depth",
                          "status", "created_at", "started_at", "answered_at",
                          "duration_ms", "question", "answer", "error"}
    assert len(out["max"]) == 6


def test_no_schema_change_and_buzzes_are_not_logged(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            h.script(default={"text": "ok"})
            await h.module.db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                " message_id) VALUES ('b', 'hive', '0', 'a', 'x', 'buzz-1')")
            await h.module.db.commit()
            return await h.hive_calls()

    assert run(scenario()) == []
    cols = [r["name"] for r in h._query("PRAGMA table_info(message_queue)")]
    assert cols == [
        "id", "agent", "channel", "channel_id", "server", "author", "author_id",
        "is_bot", "content", "message_id", "mentions_agent", "attachments",
        "processed", "response", "discord_response_id", "created_at",
        "processing_started_at", "processed_at", "not_before", "call_id",
        "reply_to_agent", "priority", "expires_at", "depth", "partial_response",
        "restart_count", "claimed_by", "owner_agent"]
    from conftest import PACKAGE_ROOT
    steps = sorted(p.name for p in (PACKAGE_ROOT / "lib" / "migrate" / "steps").glob("*.py"))
    assert steps == ["00_noop.py", "10_registry.py", "20_queue.py", "30_sessions.py",
                     "__init__.py"]
