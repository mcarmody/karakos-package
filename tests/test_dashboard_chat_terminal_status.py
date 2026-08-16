"""
Tests that the chat UI honors the terminal status the stream route sends it.

The bug this exists for (#64): `dashboard/app/api/chat/stream/route.ts` was
fixed in #56 to stop conflating COMPLETE and CRASHED -- it now sends
`{done: true, status: "complete" | "crashed" | ...}`. But the client's
`onmessage` read only `payload.done`:

    if (payload.done) { eventSource.close(); setStreaming(false); }

`payload.status` was parsed and thrown away. So the server-side fix bought
nothing a user could see: an agent that crashed four sentences into a ten
sentence answer rendered exactly like one that finished, and the reader's only
clue was that the answer stopped making sense. Half a fix across two files
reads as a whole fix from either file alone, which is why this is checked from
both ends rather than from the route.

These tests read the source the way test_dashboard_agents_contract.py does.
There is no JS test harness in this package and the dashboard is typechecked
only by `next build` inside the Docker image -- and TypeScript would not have
caught this anyway. Ignoring a field is not a type error.
"""

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent
DASHBOARD_APP = PACKAGE_ROOT / "dashboard" / "app"
STREAM_ROUTE = DASHBOARD_APP / "api" / "chat" / "stream" / "route.ts"
CHAT_PAGE = DASHBOARD_APP / "chat" / "page.tsx"


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_comments(text):
    """Blank out comments, preserving line numbers and offsets.

    Same trap as in test_dashboard_agents_contract.py, and it bites harder
    here: the fix for this bug landed with a comment quoting the old broken
    code, so a scan that reads prose as code finds the defect in the file that
    just fixed it.
    """

    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, text))


def _route_src():
    assert STREAM_ROUTE.exists(), f"{STREAM_ROUTE} is missing"
    return _strip_comments(STREAM_ROUTE.read_text())


def _page_src():
    assert CHAT_PAGE.exists(), f"{CHAT_PAGE} is missing"
    return _strip_comments(CHAT_PAGE.read_text())


def _statuses_the_route_emits():
    """Status string literals the stream route sends on a terminal event.

    Deliberately only the quoted literals. The route also sends a computed
    `unknown:${processed}`, which no client can enumerate -- that one is the
    reason the client needs a default branch, checked separately below.
    """
    return set(re.findall(r'status:\s*"([a-z]+)"', _route_src()))


def test_the_route_still_sends_a_typed_terminal_status():
    """The premise of every check below. If #56 is ever reverted -- back to a
    bare `{done: true}` -- these tests are asserting a contract that no longer
    exists and should fail loudly rather than quietly pass."""
    statuses = _statuses_the_route_emits()

    assert "complete" in statuses and "crashed" in statuses, (
        "the chat stream route no longer distinguishes complete from crashed "
        f"(found statuses: {sorted(statuses)}) -- #56 may have been reverted, "
        "in which case the client-side banner has nothing to render from"
    )


def test_the_client_does_not_discard_the_terminal_status():
    """The bug itself. `payload.done` handled, `payload.status` ignored."""
    src = _page_src()

    done_branch = re.search(
        r"if\s*\(\s*payload\.done\s*\)\s*\{(.*?)\n(\s*)\}", src, re.DOTALL
    )
    assert done_branch, (
        "could not find the `if (payload.done)` branch in the chat page -- "
        "if the SSE handler was restructured, re-point this test at it"
    )

    assert "payload.status" in done_branch.group(1), (
        "the chat page closes the stream on `payload.done` without reading "
        "`payload.status`. That is the #64 defect exactly: a crashed agent "
        "renders identically to one that finished, and the user reads a "
        "partial answer as a complete one."
    )


def test_every_status_the_route_sends_has_banner_copy():
    """Drift guard. A new terminal status added server-side with no client
    branch falls through to a generic message -- survivable -- but a status
    the client mishandles is not. Checked as a set so the two files cannot
    drift apart silently."""
    src = _page_src()

    for status in sorted(_statuses_the_route_emits()):
        if status == "complete":
            continue  # the success path renders no banner, by design
        assert f'case "{status}"' in src, (
            f'the stream route can send status "{status}" but '
            "terminalStatusMessage() in the chat page has no case for it"
        )


def test_the_client_has_a_default_branch_for_unknown_statuses():
    """The route sends `unknown:${processed}` for a status constant the client
    cannot enumerate. Without a default the switch returns undefined and the
    banner renders empty -- visibly broken, but silently so."""
    src = _page_src()

    switch = re.search(r"function terminalStatusMessage\b.*?\n\}", src, re.DOTALL)
    assert switch, "terminalStatusMessage() not found in the chat page"

    assert re.search(r"\bdefault:", switch.group(0)), (
        "terminalStatusMessage() has no default branch, but the stream route "
        "sends a computed `unknown:${processed}` this client cannot enumerate"
    )


def test_the_banner_is_gated_on_a_non_complete_status():
    """A banner that renders on every terminal status would flag every
    successful turn as an error -- the same defect inverted, and much more
    annoying."""
    src = _page_src()

    assert re.search(
        r"msg\.status\s*&&\s*msg\.status\s*!==\s*STATUS_COMPLETE", src
    ), (
        "the crashed-turn banner is not gated on `msg.status !== "
        "STATUS_COMPLETE` -- confirm it cannot fire on a clean finish"
    )


def test_transport_error_does_not_clobber_a_real_terminal_status():
    """EventSource fires onerror on a normal close too. An unguarded
    `markLastAssistant("disconnected")` in onerror would overwrite the real
    status on every single successful turn, so the banner would claim a
    dropped connection on turns that completed fine."""
    src = _page_src()

    onerror = re.search(r"eventSource\.onerror\s*=\s*\(\)\s*=>\s*\{(.*?)\n(\s*)\};", src, re.DOTALL)
    assert onerror, "could not find the eventSource.onerror handler"

    body = onerror.group(1)
    if "markLastAssistant" in body:
        assert "sawTerminal" in body, (
            "onerror marks the message without checking whether a terminal "
            "status already arrived. EventSource fires onerror on normal "
            "close, so this fires on every clean turn."
        )
