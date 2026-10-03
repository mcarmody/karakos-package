"""Context budget, handoff note and reset policy (step 2.6). stdlib only.

Pure functions and constants; the server wires them (bin/agent-server.py,
`register_session_policy`). Everything is keyed by *shard id*: the handoff file
is data/handoff/<shard>.md and the in-memory state is per shard.

The rules kept from the deployment this was modelled on: every step is best
effort and a failed handoff never blocks the reset; the note is consumed once
(rotated, never injected twice); a stale note is rotated away without being
injected; the handoff prompt says nothing is posted and nobody is waiting; a
reply to the handoff turn is never posted.

Module constants are read at call time (never bound as default arguments) so a
test can patch them.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("agent-server")

HANDOFF_TURN_TIMEOUT_S = 240
HANDOFF_PRIORITY = 90            # above ordinary rows, below 2.5's interrupt (100)
HANDOFF_MAX_AGE_S = 86400
HANDOFF_KEEP = 5
HANDOFF_INJECT_MAX_CHARS = 8000
RESET_MIN_INTERVAL_S = 600
SETTLE_WAIT_S = 30
HANDOFF_CHANNEL = "handoff"      # lib/msgqueue.INTERNAL_CHANNEL is the same string
COMPACT_TURN_TIMEOUT_S = 300
# `/compact` over stream-json is unverified against the real CLI (0.4 recorded
# nothing about slash commands). While False, reset_mode "compact" behaves as
# "reset". Flipped by a follow-up PR that adds a real-CLI fixture.
COMPACT_VERIFIED = False

REASON_BUDGET = "context_budget"
REASON_OVERFLOW = "context_overflow"
REASON_AGENT = "agent_request"

COMPACT_PROMPT = (
    "/compact Preserve: open threads and who waits on whom, commitments owed, "
    "in-flight branches, PRs and deploys, decisions already made, standing "
    "rules learned.")

_SECTIONS = (
    "Open threads and who waits on whom",
    "Commitments owed",
    "In-flight branches, PRs and deploys",
    "Decisions already made",
    "Standing rules learned",
)


@dataclass
class SessionPolicyState:
    """In-memory only; nothing here is persisted."""
    last_reset_at: Dict[str, float] = field(default_factory=dict)
    inflight: Dict[str, str] = field(default_factory=dict)         # shard -> reason
    resetting: Dict[str, bool] = field(default_factory=dict)       # restart under way
    reset_requested: Dict[str, str] = field(default_factory=dict)  # shard -> reason
    compacting: Dict[str, bool] = field(default_factory=dict)
    compact_event: Dict[str, dict] = field(default_factory=dict)
    compact_warned: bool = False


def should_reset(context_tokens, budget, is_overflow_error) -> Optional[str]:
    """Reason a shard's session should be reset, or None. Unknown context
    (0, per 1.5) never triggers a budget reset."""
    if is_overflow_error:
        return REASON_OVERFLOW
    if budget and context_tokens and context_tokens >= budget:
        return REASON_BUDGET
    return None


def handoff_dir(workspace) -> Path:
    return Path(workspace) / "data" / "handoff"


def handoff_path(workspace, shard) -> Path:
    return handoff_dir(workspace) / f"{shard}.md"


def build_handoff_prompt(path) -> str:
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(_SECTIONS, 1))
    return (
        "[handoff] This is an internal turn. Nothing you write here is posted "
        "anywhere and nobody is waiting for a reply. Do not post, do not reply "
        "to anyone, and do not start new work.\n"
        "Your session is about to be reset. Use the Write tool to write a "
        "handoff note, overwriting any existing file, to this exact path:\n"
        f"{path}\n"
        "Keep it to about 600 words of plain markdown, with these five "
        "sections, writing \"none\" under any that is empty:\n"
        f"{sections}\n"
        "When the file is written, end the turn with exactly PASS.")


def format_handoff_block(text) -> str:
    return ("--- handoff note from your previous session (written by you just "
            "before the reset) ---\n" + text)


def is_internal_batch(rows) -> bool:
    """True for a batch the policy itself inserted (handoff or compact turn)."""
    try:
        return bool(rows) and rows[0]["channel"] == HANDOFF_CHANNEL
    except (KeyError, IndexError, TypeError):
        return False


def _cleared_epoch(session_cleared_at) -> Optional[int]:
    """Whole-second UTC epoch of sessions.last_compacted, or None.
    SQLite's CURRENT_TIMESTAMP is naive UTC ('YYYY-MM-DD HH:MM:SS')."""
    v = session_cleared_at
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    if isinstance(v, (int, float)):
        return int(v)
    try:
        dt = datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _rotated(dirpath: Path, shard: str):
    return sorted(dirpath.glob(f"{shard}.*.md"), key=lambda p: (p.stat().st_mtime, p.name))


def _rotate(path: Path, shard: str, now) -> bool:
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{shard}.{stamp}.md")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{shard}.{stamp}-{n}.md")
    try:
        path.rename(target)
    except OSError as e:
        log.error(f"handoff rotate failed for {shard}: {e}")
        return False
    try:
        for old in _rotated(path.parent, shard)[:-HANDOFF_KEEP]:
            old.unlink()
    except OSError as e:
        log.warning(f"handoff prune failed for {shard}: {e}")
    return True


def consume_handoff(workspace, shard, session_cleared_at, now) -> str:
    """The note for a fresh session, or "". Rotates the file it reads. Never
    raises. A file newer than the session's last clear belongs to a session
    that has not been reset yet and is left alone (a reload, or a crash before
    the reset, must not consume a note meant for the next fresh session).
    Comparison is at whole-second resolution because last_compacted has no
    sub-second part: a note written in the same second as the clear counts as
    written before it."""
    try:
        path = handoff_path(workspace, shard)
        if not path.is_file():
            return ""
        cleared = _cleared_epoch(session_cleared_at)
        if cleared is None:
            return ""
        mtime = path.stat().st_mtime
        if int(mtime) > cleared:
            return ""
        if now - mtime > HANDOFF_MAX_AGE_S:
            _rotate(path, shard, now)
            return ""
        text = path.read_text(errors="replace").strip()
        if len(text) > HANDOFF_INJECT_MAX_CHARS:
            text = text[:HANDOFF_INJECT_MAX_CHARS] + "\n...[truncated]"
        if not _rotate(path, shard, now):
            return ""
        return text
    except Exception as e:  # best effort: never block a spawn
        log.error(f"handoff consume failed for {shard}: {type(e).__name__}: {e}")
        return ""


def pending_handoff(workspace, shard) -> str:
    """The note on disk, unconsumed (for the `session` tool's load_last)."""
    try:
        path = handoff_path(workspace, shard)
        return path.read_text(errors="replace").strip() if path.is_file() else ""
    except OSError:
        return ""


_PATH_RE = re.compile(r"^(/\S+\.md)$", re.M)


def path_in_prompt(text: str) -> Optional[str]:
    """The note path inside a handoff prompt (the fake `claude` uses this)."""
    m = _PATH_RE.search(text or "")
    return m.group(1) if m else None
