#!/usr/bin/env python3
"""Hive: buzz (fire and forget) and hive call (blocking question) between shards.

Pure functions and constants, stdlib only (step 2.3). Every address is a *shard
id*. A row in message_queue is, by definition:

    call row   call_id IS NOT NULL AND reply_to_agent IS NOT NULL
    reply row  call_id IS NOT NULL AND reply_to_agent IS NULL
    buzz row   call_id IS NULL, message_id 'buzz-<uuid>'

The server (bin/agent-server.py) is glue; the rules live here.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

HIVE_MAX_DEPTH = 2
HIVE_DEFAULT_TIMEOUT_S = 120
HIVE_MIN_TIMEOUT_S = 5
HIVE_MAX_TIMEOUT_S = 900
HIVE_MAX_QUESTION_CHARS = 8000
HIVE_MAX_ANSWER_CHARS = 20000
HIVE_MAX_BUZZ_PER_TURN = 5
HIVE_REPLY_TTL_S = 600
HIVE_POLL_WAIT_S = 20
# The caller's own deadline is the call's queue-wait deadline plus this slack, so
# 1.2's expiry reply (second resolution) always lands first.
HIVE_DEADLINE_SLACK_S = 2.0
# An open-call entry is dropped lazily this long after its deadline.
HIVE_OPEN_GRACE_S = 10.0

# Reply-body error codes. `callee_paused` is reserved for 2.7 (budget pause).
HIVE_ERRORS = ("expired", "callee_error", "callee_failed", "cancelled",
               "callee_paused")

_QUEUED, _IN_PROGRESS, _COMPLETE, _CRASHED, _SKIPPED = 0, 1, 2, 3, 4
_TS = "%Y-%m-%dT%H:%M:%SZ"


def new_call_id() -> str:
    return "c-" + uuid.uuid4().hex[:12]


def clamp_timeout(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = float(HIVE_DEFAULT_TIMEOUT_S)
    if v != v:  # NaN
        v = float(HIVE_DEFAULT_TIMEOUT_S)
    return max(float(HIVE_MIN_TIMEOUT_S), min(float(HIVE_MAX_TIMEOUT_S), v))


def _iso(when) -> str:
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, tz=timezone.utc)
    return when.astimezone(timezone.utc).strftime(_TS)


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


# --- prompts ----------------------------------------------------------------

def call_prompt(caller_label: str, question: str, depth: int) -> str:
    return (f"[hive call from {caller_label}, depth {depth} of {HIVE_MAX_DEPTH}] "
            f"{question}\n"
            "Your reply is returned to the caller inside its running turn and is "
            "not posted to any channel, so answer directly and briefly; a further "
            "hive call is allowed only while depth is below the cap.")


def buzz_prompt(caller_label: str, message: str) -> str:
    return f"[buzz from {caller_label}] {message}"


# --- row builders -----------------------------------------------------------

def call_row(call_id, caller_shard, caller_label, callee_shard, callee_agent,
             question, depth, timeout_s, now=None) -> dict:
    now = time.time() if now is None else now
    return {
        "agent": callee_shard, "channel": "hive", "channel_id": "0",
        "server": "local", "author": caller_label, "author_id": "0",
        "is_bot": 1, "content": call_prompt(caller_label, question, depth),
        "message_id": f"call-{call_id}", "call_id": call_id,
        "reply_to_agent": caller_shard, "depth": depth,
        "expires_at": _iso(now + timeout_s), "owner_agent": callee_agent,
        "priority": 0,
    }


def buzz_row(caller_label, callee_shard, callee_agent, message, depth) -> dict:
    return {
        "agent": callee_shard, "channel": "hive", "channel_id": "0",
        "server": "local", "author": caller_label, "author_id": "0",
        "is_bot": 1, "content": buzz_prompt(caller_label, message),
        "message_id": f"buzz-{uuid.uuid4()}", "call_id": None,
        "reply_to_agent": None, "depth": depth, "owner_agent": callee_agent,
        "priority": 0,
    }


def answer_body(call_id, text, duration_ms=None) -> dict:
    text = text or ""
    body = {"call_id": call_id, "answer": text[:HIVE_MAX_ANSWER_CHARS]}
    if len(text) > HIVE_MAX_ANSWER_CHARS:
        body["truncated"] = True
    if duration_ms is not None:
        body["duration_ms"] = int(duration_ms)
    return body


def error_body(call_id, code, detail=None) -> dict:
    body = {"call_id": call_id, "error": code}
    if detail:
        body["detail"] = detail
    return body


def reply_row(call, body, now=None) -> dict:
    """The reply to `call` (a call row mapping). `body` is answer_body/error_body."""
    cid = call["call_id"]
    return {
        "agent": call["reply_to_agent"], "channel": "call", "channel_id": "0",
        "server": "local", "author": call["agent"], "author_id": "0",
        "is_bot": 1, "content": json.dumps(body), "message_id": f"reply-{cid}",
        "call_id": cid, "reply_to_agent": None, "depth": _get(call, "depth", 0),
        "owner_agent": call["reply_to_agent"],
    }


def is_call_row(row) -> bool:
    return bool(_get(row, "call_id") and _get(row, "reply_to_agent"))


def is_reply_row(row) -> bool:
    return bool(_get(row, "call_id")) and not _get(row, "reply_to_agent")


def parse_body(content) -> dict:
    try:
        body = json.loads(content or "")
    except (TypeError, ValueError):
        return {"error": "callee_error", "detail": "unreadable reply"}
    return body if isinstance(body, dict) else {"answer": str(body)}


# --- depth, deadlock, target choice ----------------------------------------

def next_depth(batch_rows) -> int:
    return max((_get(r, "depth", 0) or 0 for r in batch_rows), default=0) + 1


def would_deadlock(waits_for: Mapping[str, Iterable[str]], caller: str,
                   callee: str) -> bool:
    """True when callee is the caller, or can already reach the caller through
    open calls (`waits_for[shard]` = shards it is blocked on)."""
    if callee == caller:
        return True
    seen: Set[str] = set()
    stack = [callee]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in waits_for.get(cur, ()):
            if nxt == caller:
                return True
            stack.append(nxt)
    return False


def pick_callee(specs, to, caller, states, depths, waits_for, kind="call",
                paused=()) -> Tuple[Optional[str], Optional[str]]:
    """(shard_id, None) or (None, error_code). `to` is an agent id (that
    agent's shards) or a shard id; an id that is both means the agent. `kind`
    is "call" or "buzz": a buzz never blocks, so it may target the caller's own
    shard and is not deadlock-checked."""
    by_id = {s.id: s for s in specs}
    agent_ids = {s.agent for s in specs}
    if to in agent_ids:
        cands = [s.id for s in specs if s.agent == to]
        named_shard = False
    elif to in by_id:
        cands = [to]
        named_shard = True
    else:
        return None, "unknown_target"

    others = [c for c in cands if c != caller]
    if caller in cands and not others or (named_shard and caller in cands):
        if kind == "call":
            return None, "self_call"
        return caller, None
    cands = others

    usable = [c for c in cands
              if states.get(c, "IDLE") != "ERROR_RECOVERY" and c not in paused]
    if not usable:
        if cands and all(c in paused for c in cands):
            return None, "callee_paused"
        return None, "no_available_shard"
    order = {c: i for i, c in enumerate(cands)}
    usable.sort(key=lambda c: (states.get(c, "IDLE") != "IDLE",
                               depths.get(c, 0), order[c]))
    if kind == "buzz":
        return usable[0], None
    for c in usable:
        if not would_deadlock(waits_for, caller, c):
            return c, None
    return None, "deadlock"


# --- open calls (server memory) --------------------------------------------

@dataclass
class OpenCall:
    caller: str
    callee: str
    depth: int
    deadline: float          # epoch seconds: queue-wait deadline
    started: float = field(default_factory=time.time)


class HiveState:
    """In-memory hive state held on ServerState (never persisted)."""

    def __init__(self):
        self.open: Dict[str, OpenCall] = {}
        self.buzzes_this_turn: Dict[str, int] = {}

    def waits_for(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for oc in self.open.values():
            out.setdefault(oc.caller, set()).add(oc.callee)
        return out

    def prune(self, now=None) -> List[str]:
        now = time.time() if now is None else now
        gone = [cid for cid, oc in self.open.items()
                if now > oc.deadline + HIVE_OPEN_GRACE_S + HIVE_DEADLINE_SLACK_S]
        for cid in gone:
            self.open.pop(cid, None)
        return gone

    def calls_of(self, caller: str) -> List[str]:
        return [cid for cid, oc in self.open.items() if oc.caller == caller]


# --- the call log -----------------------------------------------------------

def _epoch(ts) -> Optional[float]:
    """CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS') or utc_iso value -> epoch."""
    if not ts:
        return None
    try:
        s = str(ts).replace("T", " ").rstrip("Z")
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def call_status(call, reply, is_open, now=None) -> str:
    """pending | answered | expired | error | timeout | abandoned."""
    now = time.time() if now is None else now
    if reply is not None:
        if _get(reply, "processed") == _SKIPPED and _get(reply, "response") == "late":
            return "timeout"
        body = parse_body(_get(reply, "content", ""))
        err = body.get("error")
        if not err:
            return "answered"
        if err == "expired":
            return "expired"
        if err == "cancelled":
            return "timeout"
        return "error"
    proc = _get(call, "processed", _QUEUED)
    resp = _get(call, "response")
    if proc == _SKIPPED:
        if resp == "cancelled":
            return "timeout"
        if resp == "expired":
            return "expired"
        return "abandoned"
    if proc == _CRASHED:
        return "error"
    if proc in (_QUEUED, _IN_PROGRESS):
        if is_open:
            exp = _epoch(_get(call, "expires_at"))
            if exp is not None and now > exp + HIVE_DEADLINE_SLACK_S:
                return "timeout"
            return "pending"
        return "abandoned"
    return "abandoned"


def _clip(text, n=200):
    return (text or "")[:n]


def log_entry(call, reply, status, specs=()) -> dict:
    """One /hive/calls item. `specs` (optional) maps shards to agents."""
    agent_of = {s.id: s.agent for s in specs}
    frm, to = call["reply_to_agent"], call["agent"]
    body = parse_body(_get(reply, "content", "")) if reply is not None else {}
    created = _epoch(_get(call, "created_at"))
    answered = _epoch(_get(reply, "created_at")) if reply is not None else None
    duration = body.get("duration_ms") if status == "answered" else None
    if duration is None and answered is not None and created is not None:
        duration = int((answered - created) * 1000)
    return {
        "call_id": call["call_id"], "from": frm, "to": to,
        "from_agent": agent_of.get(frm, frm), "to_agent": agent_of.get(to, _get(call, "owner_agent", to)),
        "depth": _get(call, "depth", 0), "status": status,
        "created_at": _get(call, "created_at"),
        "started_at": _get(call, "processing_started_at"),
        "answered_at": _get(reply, "created_at") if reply is not None else None,
        "duration_ms": duration,
        "question": _clip(_strip_prompt(_get(call, "content", ""))),
        "answer": _clip(body.get("answer")) if reply is not None else None,
        "error": body.get("error") if reply is not None else (
            _get(call, "response") if status in ("timeout", "abandoned", "expired") else None),
    }


def _strip_prompt(content: str) -> str:
    """The question without the call_prompt framing."""
    if content.startswith("[hive call from "):
        _, _, rest = content.partition("] ")
        rest = rest.rsplit("\nYour reply is returned to the caller", 1)[0]
        return rest
    return content
