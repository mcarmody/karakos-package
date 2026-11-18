"""A single stream-json line larger than asyncio's default 64 KiB reader limit
(a parallel Gmail or Drive search result is enough) must not desync the turn:
the reply has to be posted for the turn that produced it."""

import asyncio

import pytest

from harness import Harness  # noqa: F401  (fixture plumbing lives in conftest)

BIG = 200 * 1024


def run(coro):
    return asyncio.run(coro)


@pytest.mark.xfail(
    reason="agent server spawns claude without a stdout `limit=`; remove this "
           "marker with the fix", strict=False)
def test_oversized_line_does_not_desync_reply(harness):
    h = harness(agents=["a"])

    async def scenario():
        async with h:
            h.script(rules=[
                {"match": "big", "step": {"text": "X" * BIG + " end-big"}},
            ], default={"text": "small: {{text}}"})
            await h.send("a", "big please", channel_id="42")
            await h.wait_idle("a", timeout=10)
            await h.send("a", "after", channel_id="42")
            await h.wait_idle("a", timeout=10)

    run(scenario())
    rows = h.queue_rows("a")
    assert len(rows) == 2
    assert rows[0]["response"].endswith("end-big")
    assert rows[1]["response"].startswith("small:")
    replies = [d["content"] for d in h.discord if d["channel_id"] == "42"]
    assert replies and "small:" in replies[-1], replies
