"""/message with the payloads the relay sends (step 2.2), real server + fake claude."""

import asyncio
import logging

SHARDS = {"a": ["a", "a-2"]}


def run(coro):
    return asyncio.run(coro)


def post(h, body):
    async def _p():
        r = await h.client.post("/message", headers=h._headers(), json={
            "content": "hi", "channel_id": "1", "server": "local",
            "author": "harness", **body})
        return r.status
    return _p()


def test_shard_payloads_land_on_the_right_shard(harness, caplog):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            h.script(default={"text": "{{env:KARAKOS_SHARD}}"})
            assert await post(h, {"agent": "a", "shard": "a-2", "message_id": "1"}) == 202
            assert await post(h, {"agent": "a", "shard": "a", "message_id": "2"}) == 202
            with caplog.at_level(logging.WARNING):
                assert await post(h, {"agent": "a", "shard": "a-9", "message_id": "3"}) == 202
            assert await post(h, {"agent": "zz", "shard": "a-9", "message_id": "4"}) == 400
            assert await post(h, {"shard": "a-9", "message_id": "5"}) == 400
            for s in ("a", "a-2"):
                await h.wait_idle(s)
            await h.wait_for(lambda: all(r["response"] for s in ("a", "a-2")
                                         for r in h.queue_rows(s)))

    run(scenario())
    assert [r["message_id"] for r in h.queue_rows("a-2")] == ["1"]
    assert [r["message_id"] for r in h.queue_rows("a")] == ["2", "3"]
    assert [r["response"] for r in h.queue_rows("a-2")] == ["a-2"]
    assert [r["response"] for r in h.queue_rows("a")] == ["a", "a"]
    assert "message for unknown shard a-9, using a" in caplog.text


def test_no_schema_change(harness):
    h = harness(agents=["a", "b"], shards=SHARDS)

    async def scenario():
        async with h:
            pass

    run(scenario())
    cols = [r["name"] for r in h._query("PRAGMA table_info(message_queue)")]
    assert "shard" not in cols
    assert "agent" in cols
