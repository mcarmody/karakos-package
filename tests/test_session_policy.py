"""lib/session_policy.py: pure functions of the 2.6 context handoff (no server)."""

import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import msgqueue  # noqa: E402
import session_policy as sp  # noqa: E402

NOW = 1_800_000_000.0   # an arbitrary "now"


def stamp(epoch):
    """sqlite CURRENT_TIMESTAMP form: naive UTC, whole seconds."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_note(ws, shard, text="note", mtime=None):
    path = sp.handoff_path(ws, shard)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# -- should_reset --------------------------------------------------------------

@pytest.mark.parametrize("tokens,budget,overflow,want", [
    (0, 50000, False, None),                       # unknown never triggers
    (49999, 50000, False, None),                   # below
    (50000, 50000, False, "context_budget"),       # at
    (90000, 50000, False, "context_budget"),       # above
    (90000, None, False, None),                    # no budget: nothing on size
    (90000, 0, False, None),
    (0, None, True, "context_overflow"),           # overflow needs no budget
    (90000, 50000, True, "context_overflow"),      # overflow wins
])
def test_should_reset_truth_table(tokens, budget, overflow, want):
    assert sp.should_reset(tokens, budget, overflow) == want


# -- consume_handoff -------------------------------------------------------------

def test_no_file_gives_empty(tmp_path):
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == ""


def test_no_clear_time_leaves_the_file(tmp_path):
    path = write_note(tmp_path, "a", mtime=NOW - 10)
    assert sp.consume_handoff(tmp_path, "a", None, NOW) == ""
    assert path.exists()


def test_file_newer_than_the_clear_is_left_alone(tmp_path):
    path = write_note(tmp_path, "a", "for later", mtime=NOW - 10)
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW - 100), NOW) == ""
    assert path.exists() and path.read_text() == "for later"


def test_fresh_file_is_returned_rotated_and_consumed_once(tmp_path):
    path = write_note(tmp_path, "a", "  carry on  \n", mtime=NOW - 50)
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == "carry on"
    assert not path.exists()
    rotated = list(path.parent.glob("a.*.md"))
    assert len(rotated) == 1 and rotated[0].read_text().strip() == "carry on"
    assert re.fullmatch(r"a\.\d{8}T\d{6}Z\.md", rotated[0].name)
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == ""


def test_note_written_in_the_same_second_as_the_clear_counts(tmp_path):
    """last_compacted has whole-second resolution; the note is written moments
    before the restart that clears the session."""
    clear = int(NOW)
    write_note(tmp_path, "a", "same second", mtime=clear + 0.6)
    assert sp.consume_handoff(tmp_path, "a", stamp(clear), NOW) == "same second"


def test_clear_time_is_read_as_utc_not_local(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        write_note(tmp_path, "a", "tz", mtime=NOW - 5)
        assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == "tz"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_stale_file_is_rotated_and_not_returned(tmp_path):
    path = write_note(tmp_path, "a", "old", mtime=NOW - sp.HANDOFF_MAX_AGE_S - 100)
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == ""
    assert not path.exists() and len(list(path.parent.glob("a.*.md"))) == 1


def test_oversize_file_is_truncated(tmp_path):
    write_note(tmp_path, "a", "x" * (sp.HANDOFF_INJECT_MAX_CHARS + 500), mtime=NOW - 5)
    out = sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW)
    assert out.endswith("\n...[truncated]")
    assert len(out) == sp.HANDOFF_INJECT_MAX_CHARS + len("\n...[truncated]")


def test_rotated_copies_beyond_keep_are_pruned(tmp_path):
    for i in range(sp.HANDOFF_KEEP + 3):
        write_note(tmp_path, "a", f"n{i}", mtime=NOW - 500 + i)
        assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW + i) == f"n{i}"
    kept = sorted(sp.handoff_dir(tmp_path).glob("a.*.md"))
    assert len(kept) == sp.HANDOFF_KEEP
    assert {p.read_text() for p in kept} == {f"n{i}" for i in range(3, sp.HANDOFF_KEEP + 3)}


def test_same_second_rotations_do_not_collide(tmp_path):
    for i in range(3):
        write_note(tmp_path, "a", f"n{i}", mtime=NOW - 5)
        assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == f"n{i}"
    assert len(list(sp.handoff_dir(tmp_path).glob("a.*.md"))) == 3


def test_shards_do_not_touch_each_others_notes(tmp_path):
    write_note(tmp_path, "a", "A", mtime=NOW - 5)
    write_note(tmp_path, "a-2", "A2", mtime=NOW - 5)
    assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == "A"
    assert sp.handoff_path(tmp_path, "a-2").exists()
    assert sp.consume_handoff(tmp_path, "a-2", stamp(NOW), NOW) == "A2"


def test_unwritable_directory_returns_empty_without_raising(tmp_path):
    write_note(tmp_path, "a", "stuck", mtime=NOW - 5)
    d = sp.handoff_dir(tmp_path)
    d.chmod(0o500)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("directory permissions not enforced (running as root)")
        assert sp.consume_handoff(tmp_path, "a", stamp(NOW), NOW) == ""
    finally:
        d.chmod(0o700)


def test_garbage_inputs_never_raise(tmp_path):
    write_note(tmp_path, "a", mtime=NOW - 5)
    for bad in ("not a date", "", 12.5, object()):
        sp.consume_handoff(tmp_path, "a", bad, NOW)


# -- prompt and block ---------------------------------------------------------------

def test_handoff_prompt_contents(tmp_path):
    path = sp.handoff_path(tmp_path, "a")
    text = sp.build_handoff_prompt(path)
    assert text.startswith("[handoff]")
    assert str(path) in text.splitlines()          # alone on its own line
    assert sp.path_in_prompt(text) == str(path)
    for section in ("Open threads", "Commitments owed", "In-flight branches",
                    "Decisions already made", "Standing rules learned"):
        assert section in text
    assert "nobody is waiting" in text and "Nothing you write here is posted" in text
    assert "600 words" in text and "none" in text
    assert text.rstrip().endswith("exactly PASS.")


def test_handoff_prompt_is_free_of_household_coupling():
    text = sp.build_handoff_prompt("/data/handoff/a.md")
    checked = 0
    for line in (PACKAGE_ROOT / "system" / "coupling-denylist.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        name, regex = parts[0], parts[1] if len(parts) > 1 else ""
        path_scope = parts[2] if len(parts) > 2 else ""
        if not regex or path_scope:        # path-scoped rules do not apply to a string
            continue
        checked += 1
        assert not re.search(regex, text), name
    assert checked > 0


def test_format_handoff_block():
    assert sp.format_handoff_block("x") == (
        "--- handoff note from your previous session (written by you just "
        "before the reset) ---\nx")


def test_is_internal_batch():
    assert sp.is_internal_batch([{"channel": "handoff"}, {"channel": "general"}])
    assert not sp.is_internal_batch([{"channel": "general"}])
    assert not sp.is_internal_batch([])
    assert not sp.is_internal_batch([{}])


def test_channel_constants_agree_with_the_queue():
    assert sp.HANDOFF_CHANNEL == msgqueue.INTERNAL_CHANNEL
    assert sp.HANDOFF_PRIORITY == 90 and sp.RESET_MIN_INTERVAL_S == 600
    assert sp.COMPACT_VERIFIED is False


# -- the queue claims internal rows alone ---------------------------------------------

def _queue_db():
    import asyncio
    import aiosqlite

    async def make():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE message_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT,"
            " channel TEXT, channel_id TEXT, author TEXT, is_bot INTEGER DEFAULT 0,"
            " content TEXT, message_id TEXT, processed INTEGER DEFAULT 0, response TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processing_started_at,"
            " processed_at, not_before, call_id TEXT, reply_to_agent TEXT,"
            " priority INTEGER DEFAULT 0, expires_at TEXT, claimed_by TEXT)")
        return db
    return asyncio, make


def test_a_handoff_row_is_never_batched_with_human_rows():
    asyncio, make = _queue_db()

    async def scenario():
        db = await make()
        for ch, prio in (("general", 0), ("handoff", 90), ("general", 0)):
            await db.execute(
                "INSERT INTO message_queue (agent, channel, channel_id, author, content,"
                " message_id, priority) VALUES ('a', ?, '1', 'x', 'c', ?, ?)",
                (ch, f"m{prio}{ch}{time.time()}", prio))
        await db.commit()
        peek = await msgqueue.peek_claimable(db, "a")
        first = await msgqueue.claim_batch(db, "a", 20)
        second = await msgqueue.claim_batch(db, "a", 20)
        await db.close()
        return peek, first, second

    peek, first, second = asyncio.run(scenario())
    assert [r["channel"] for r in peek] == ["handoff"]
    assert [r["channel"] for r in first] == ["handoff"]
    assert [r["channel"] for r in second] == ["general", "general"]
