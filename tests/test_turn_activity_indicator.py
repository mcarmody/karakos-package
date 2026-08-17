"""
Tests for #76 (bottom-of-turn status indicators).

The dashboard chat page has exactly one liveness signal — a blinking cursor —
and it says the same thing during a 40ms gap and a four-minute Bash call. #91
answered that question for Discord, and only for Discord: the `tool_use`
branch in read_agent_response already sees every call, already redacts its
arguments, and posts to one surface. The observation point was fully wired at
one end again.

So this is not a new feature so much as a second consumer, plus the mechanism
the issue actually asks for: a *deferred* note, scheduled out and cancelled by
the next event, so a state that resolves faster than a human can read never
appears at all.

Three things are checked here, and the third is the one with teeth:

1. the note says which tool and on what, with the same redaction as #91,
2. it is deferred — fast tool calls never flicker a pill,
3. it can never outlive its turn. A pill still claiming the agent is working,
   on a turn that crashed twenty minutes ago, is a worse lie than the frozen
   cursor this replaces.
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
DASHBOARD_APP = PACKAGE_ROOT / "dashboard" / "app"
STREAM_ROUTE = DASHBOARD_APP / "api" / "chat" / "stream" / "route.ts"
CHAT_PAGE = DASHBOARD_APP / "chat" / "page.tsx"

pytest.importorskip("aiohttp")

CHANNEL = "555000555"


def _load(name, path, workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


@pytest.fixture
def ags(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    module = _load("ags_activity_under_test", AGENT_SERVER, tmp_path)
    module.agent_config = {"amos": {"model": "sonnet"}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    return module


def run(ags, coro_fn):
    """Run a scenario, closing the module's aiosqlite handle unconditionally.

    Teardown in a finally, not at the end of the body: aiosqlite runs a
    NON-DAEMON worker thread that stops only on .close(), so a failed
    assertion that skipped the close would hang the interpreter at shutdown
    instead of reporting the failure. In CI that reads as a wedged job rather
    than a caught regression (learned from #121's test file).
    """

    async def wrapper():
        try:
            await coro_fn()
        finally:
            db = getattr(ags, "db", None)
            if db is not None:
                await db.close()
                ags.db = None

    asyncio.run(wrapper())


class Recorder:
    """Stand-in for write_activity that remembers the sequence of notes."""

    def __init__(self):
        self.writes = []

    async def __call__(self, message_ids, note):
        self.writes.append(note)


# --------------------------------------------------------------------------
# The note itself, and the redaction it shares with #91
# --------------------------------------------------------------------------

def test_the_note_names_the_tool_and_what_it_is_working_on(ags):
    assert ags.describe_tool_call("Bash", {"command": "npm test"}) == "⚙ Bash — npm test"
    assert ags.describe_tool_call("Read", {"file_path": "/srv/app/main.py"}) == \
        "⚙ Read — /srv/app/main.py"


def test_the_discord_markup_stays_on_the_discord_surface(ags):
    """`-# ` is Discord subtext syntax. The dashboard renders the string as
    text, so a shared prefix would put a literal "-#" on the page."""
    plain = ags.describe_tool_call("Bash", {"command": "npm test"})
    assert not plain.startswith("-#")
    assert ags.summarize_tool_call("Bash", {"command": "npm test"}) == f"-# {plain}"


def test_both_surfaces_redact_through_the_same_function(ags):
    """The reason describe_tool_call exists rather than a second formatter.
    Tool inputs carry file contents, patch bodies and credentials; two
    formatters is two places for one of them to be redacted less."""
    secret = {"secret": "hunter2", "token": "ghp_realish", "body": "x" * 200}
    plain = ags.describe_tool_call("mcp__vault__read", secret)
    channel = ags.summarize_tool_call("mcp__vault__read", secret)
    assert plain == "⚙ mcp__vault__read"
    for rendered in (plain, channel):
        assert "hunter2" not in rendered and "ghp_realish" not in rendered


# --------------------------------------------------------------------------
# Deferral — the mechanism the issue is actually about
# --------------------------------------------------------------------------

def test_a_note_shorter_than_the_delay_never_reaches_the_row(ags):
    """The whole point of the deferred task. A turn making fifty fast tool
    calls would otherwise flicker fifty pills through a 200ms poll, which
    reads as noise rather than as progress."""

    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator(["m1"], delay=0.2, writer=rec)
        await ind.set("⚙ Bash — true")
        await asyncio.sleep(0.02)
        await ind.clear()
        await asyncio.sleep(0.3)
        assert rec.writes == []

    asyncio.run(scenario())


def test_a_note_that_outlives_the_delay_is_written(ags):
    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator(["m1"], delay=0.05, writer=rec)
        await ind.set("⚙ Bash — npm test")
        await asyncio.sleep(0.2)
        assert rec.writes == ["⚙ Bash — npm test"]

    asyncio.run(scenario())


def test_clearing_a_shown_note_erases_it(ags):
    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator(["m1"], delay=0.05, writer=rec)
        await ind.set("⚙ Bash — npm test")
        await asyncio.sleep(0.2)
        await ind.clear()
        assert rec.writes == ["⚙ Bash — npm test", None]

    asyncio.run(scenario())


def test_clearing_a_note_that_never_showed_writes_nothing(ags):
    """Every turn ends with a clear(), including the overwhelming majority
    that never displayed a pill. Those must not each cost an UPDATE."""

    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator(["m1"], delay=5.0, writer=rec)
        await ind.clear()
        await ind.clear()
        assert rec.writes == []

    asyncio.run(scenario())


def test_a_superseded_note_never_appears(ags):
    """Second tool call inside the delay window: the page should show what
    the agent is doing now, never a queue of what it did."""

    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator(["m1"], delay=0.15, writer=rec)
        await ind.set("⚙ Read — /a.py")
        await asyncio.sleep(0.02)
        await ind.set("⚙ Bash — npm test")
        await asyncio.sleep(0.3)
        assert rec.writes == ["⚙ Bash — npm test"]

    asyncio.run(scenario())


def test_a_cancelled_write_cannot_land_after_the_clear_that_supersedes_it(ags):
    """The ordering hazard, and the reason _settle() awaits the cancelled
    task instead of just cancelling it.

    A pending write cancelled *while its UPDATE is in flight* could otherwise
    complete after the clear that was meant to supersede it — stranding a
    stale pill for the rest of the turn, on a row nothing will touch again.
    The slow writer here makes that window wide enough to observe.
    """

    async def scenario():
        rec = Recorder()

        async def slow_writer(message_ids, note):
            await asyncio.sleep(0.1)
            rec.writes.append(note)

        ind = ags.ActivityIndicator(["m1"], delay=0.01, writer=slow_writer)
        await ind.set("⚙ Bash — npm test")
        await asyncio.sleep(0.05)   # the write is now in flight
        await ind.clear()
        await asyncio.sleep(0.3)
        # Whatever else happened, the LAST thing written is the clear.
        assert rec.writes[-1] is None, rec.writes

    asyncio.run(scenario())


def test_a_turn_with_no_rows_writes_nothing(ags):
    """The headless/oneshot lane calls read_agent_response with no message
    ids. There is no row to annotate and no page watching it."""

    async def scenario():
        rec = Recorder()
        ind = ags.ActivityIndicator([], delay=0.01, writer=rec)
        await ind.set("⚙ Bash — npm test")
        await asyncio.sleep(0.1)
        await ind.clear()
        assert rec.writes == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The turn: the note reaching the row, and never outliving the turn
# --------------------------------------------------------------------------

class FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        return None


class SlowStdout:
    """Feeds stream-json lines, pausing `gap` before each after the first.

    The pause is the point: a tool call that returns instantly is exactly
    what the deferred write is designed to hide, so a test driving lines back
    to back would observe nothing and prove nothing.

    Gaps sit *between* lines by default. `pause_first` moves one in front of
    the opening line too, which is the only way to observe the gap between
    the prompt going out and the first token coming back — the one the issue
    names first.
    """

    def __init__(self, lines, gap=0.0, on_gap=None, pause_first=False):
        self._lines = list(lines)
        self._gap = gap
        self._on_gap = on_gap
        self._first = not pause_first

    async def readline(self):
        if not self._lines:
            return b""
        if not self._first and self._gap:
            await asyncio.sleep(self._gap)
            if self._on_gap:
                await self._on_gap()
        self._first = False
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, stdout):
        self.stdin = FakeStdin()
        self.stdout = stdout
        self.pid = 4242


def tool_event(name, tool_input):
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name,
                                 "input": tool_input}]},
    }).encode() + b"\n"


def text_event(text):
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }).encode() + b"\n"


def result_event(text="done"):
    return json.dumps({
        "type": "result", "session_id": "s1", "result": text,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode() + b"\n"


async def seed_row(ags, message_id="msg-1"):
    await ags.init_db()
    await ags.db.execute(
        "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
        " author_id, is_bot, content, message_id, mentions_agent)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("amos", "c", CHANNEL, "discord", "Mike", "42", 0, "hi", message_id, 1),
    )
    await ags.db.commit()


async def read_activity(ags, message_id="msg-1"):
    async with ags.db.execute(
        "SELECT activity FROM message_queue WHERE message_id = ?", (message_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["activity"] if row else None


async def silence_discord(ags):
    async def fake_post(agent, cid, content, reply_to=None, dead_letter=False):
        return "discord-1"

    ags.post_to_discord = fake_post


def test_a_slow_tool_call_puts_its_note_on_the_row(ags):
    """The acceptance test: mid-turn, the page can say what the agent is
    doing rather than showing a cursor that means nothing."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05

        seen = []

        async def sample():
            seen.append(await read_activity(ags))

        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [tool_event("Bash", {"command": "npm test"}), result_event()],
            gap=0.3, on_gap=sample,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])

        assert seen == ["⚙ Bash — npm test"], seen

    run(ags, scenario)


def test_the_note_is_gone_by_the_end_of_the_turn(ags):
    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05
        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [tool_event("Bash", {"command": "npm test"}), result_event()],
            gap=0.2,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])
        assert await read_activity(ags) is None

    run(ags, scenario)


def test_a_turn_that_dies_mid_stream_still_clears_its_note(ags):
    """The one that matters. A pill outliving its turn is the failure mode
    this whole change is supposed to remove, and a crash is exactly when
    nothing downstream will ever tidy up — so the clear is unconditional and
    outside the try, not on the happy path."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05

        class ExplodingStdout(SlowStdout):
            async def readline(self):
                line = await super().readline()
                if not self._lines:
                    raise RuntimeError("stream died")
                return line

        ags.agent_processes["amos"] = FakeProc(ExplodingStdout(
            [tool_event("Bash", {"command": "npm test"}), result_event()],
            gap=0.2,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])
        assert await read_activity(ags) is None

    run(ags, scenario)


def test_arriving_text_clears_the_note(ags):
    """Text is its own liveness signal. A pill beside streaming prose is
    saying the same thing twice."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05

        seen = []

        async def sample():
            seen.append(await read_activity(ags))

        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [tool_event("Bash", {"command": "npm test"}),
             text_event("here is the answer"),
             result_event()],
            gap=0.2, on_gap=sample,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])

        # Sampled after the tool line, then after the text line.
        assert seen == ["⚙ Bash — npm test", None], seen

    run(ags, scenario)


def test_the_dashboard_note_is_not_gated_on_the_discord_flag(ags):
    """tool_streaming silences a *channel*. Reusing it here would make a
    deployment that turned off channel chatter also blind its own dashboard,
    which is not what anyone setting that flag asked for."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.agent_config = {"amos": {"model": "sonnet", "tool_streaming": False}}
        ags.ACTIVITY_DELAY_S = 0.05

        seen = []

        async def sample():
            seen.append(await read_activity(ags))

        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [tool_event("Bash", {"command": "npm test"}), result_event()],
            gap=0.3, on_gap=sample,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])
        assert seen == ["⚙ Bash — npm test"], seen

    run(ags, scenario)


def test_the_headless_lane_still_gets_a_note(ags):
    """channel_id "0" has no Discord channel to reassure — which makes the
    dashboard the only window onto the turn, not a redundant one."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05

        seen = []

        async def sample():
            seen.append(await read_activity(ags))

        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [tool_event("Bash", {"command": "npm test"}), result_event()],
            gap=0.3, on_gap=sample,
        ))
        await ags.read_agent_response("amos", "0", ["msg-1"])
        assert seen == ["⚙ Bash — npm test"], seen

    run(ags, scenario)


def test_the_gap_before_the_first_token_is_covered(ags):
    """The gap the issue names first: message sent, nothing written back
    yet, page showing an empty bubble and a cursor."""

    async def scenario():
        await seed_row(ags)
        await silence_discord(ags)
        ags.ACTIVITY_DELAY_S = 0.05

        seen = []

        async def sample():
            seen.append(await read_activity(ags))

        # The pause in front of the first line IS the opening gap.
        ags.agent_processes["amos"] = FakeProc(SlowStdout(
            [text_event("hi"), result_event()], gap=0.3, on_gap=sample,
            pause_first=True,
        ))
        await ags.read_agent_response("amos", CHANNEL, ["msg-1"])
        assert seen and seen[0] == ags.ACTIVITY_THINKING, seen

    run(ags, scenario)


def test_the_column_is_added_to_databases_that_predate_it(ags):
    """CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so a
    new column in the definition reaches an upgraded install only through
    ensure_column. Every install of this package has an existing table."""

    async def scenario():
        import aiosqlite

        ags.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        legacy = await aiosqlite.connect(str(ags.DB_PATH))
        await legacy.execute(
            "CREATE TABLE message_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent TEXT NOT NULL, channel TEXT NOT NULL, channel_id TEXT NOT NULL,"
            " author TEXT NOT NULL, content TEXT NOT NULL,"
            " message_id TEXT UNIQUE NOT NULL, processed INTEGER DEFAULT 0,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await legacy.commit()
        await legacy.close()

        await ags.init_db()
        async with ags.db.execute("PRAGMA table_info(message_queue)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        assert "activity" in columns

    run(ags, scenario)


# --------------------------------------------------------------------------
# The dashboard half. Half a fix across two files reads as a whole fix from
# either file alone -- #64's lesson, and this change has the same shape.
# --------------------------------------------------------------------------

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_comments(text):
    """Blank out comments, preserving offsets. The comments in both files
    below quote the very patterns these tests grep for."""

    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, text))


def _route_src():
    assert STREAM_ROUTE.exists(), f"{STREAM_ROUTE} is missing"
    return _strip_comments(STREAM_ROUTE.read_text())


def _page_src():
    assert CHAT_PAGE.exists(), f"{CHAT_PAGE} is missing"
    return _strip_comments(CHAT_PAGE.read_text())


def test_the_stream_route_reads_the_column():
    """Without this the server writes a note nothing ever reads — the same
    one-ended wire this change exists to finish."""
    src = _route_src()
    select = re.search(r"SELECT ([^\"]+) FROM message_queue", src)
    assert select, "the stream route no longer selects from message_queue"
    assert "activity" in select.group(1), (
        f"the stream poll does not read the activity column (selects: "
        f"{select.group(1).strip()})"
    )


def test_the_stream_route_forwards_the_note_to_the_client():
    assert re.search(r"send\(\{\s*activity", _route_src()), (
        "the stream route reads activity but never sends it"
    )


def test_the_stream_route_only_sends_the_note_when_it_changes():
    """A 200ms poll across a four-minute tool call is 1,200 identical SSE
    events. The dedup is what makes riding the existing poll cheap."""
    src = _route_src()
    assert "lastActivity" in src, "the activity send is not deduplicated"
    assert re.search(r"activity\s*!==\s*lastActivity", src), (
        "the activity send is not guarded on a change"
    )


def test_the_page_detects_the_note_by_presence_not_truthiness():
    """`{activity: null}` is how the server says the note is gone. An
    `else if (payload.activity)` branch would drop that message on the floor
    and leave the last pill on screen for the rest of the turn — pinned
    because it is the natural way to write this line and it is wrong."""
    src = _page_src()
    assert re.search(r'"activity"\s+in\s+payload', src), (
        "the chat page does not branch on the presence of payload.activity"
    )
    assert not re.search(r"else\s+if\s*\(\s*payload\.activity\s*\)", src), (
        "the chat page tests payload.activity for truthiness — a cleared "
        "note (null) would never be applied"
    )


def test_the_page_renders_the_note():
    assert "msg.activity" in _page_src(), (
        "the chat page never renders the activity note"
    )


def test_the_page_only_shows_the_note_on_the_streaming_turn():
    """Scrolled-back history must not sprout pills: `activity` is transient
    server-side, but a message object keeps whatever it was last given."""
    src = _page_src()
    assert re.search(r"isLastStreaming\s*&&\s*msg\.activity", src), (
        "the activity pill is not gated on the turn still streaming"
    )


def test_the_terminal_event_drops_the_note():
    """Belt and braces for the paths where the server never got to clear it
    — a crash, a dropped stream, a timeout. The client knows the turn is
    over regardless."""
    assert re.search(r"activity:\s*undefined", _page_src()), (
        "the terminal status update does not drop the activity note"
    )
