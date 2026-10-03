"""Golden transcript for the agent-server turn loop (step 2.0).

Drives the 0.3 harness through two agents: a three-message batch, a tool turn,
an is_error turn, an interrupt, a respawn, and (agent b) a usage-wall turn.
Records queue rows, cost rows, session rows, posted Discord messages and the
server log lines (level + message, timestamps stripped) into
tests/golden/turn_loop.json. The refactor into lib/turn_loop.py must not change
the file. Regenerate deliberately with TURN_LOOP_GOLDEN_RECORD=1.
"""

import asyncio
import difflib
import json
import logging
import os
import re
from pathlib import Path

from harness import Harness

GOLDEN = Path(__file__).parent / "golden" / "turn_loop.json"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _scrub(value, workspace):
    """Strip run-specific noise (paths, uuids, pids, durations) from text."""
    s = str(value).replace(str(workspace), "<ws>")
    s = UUID_RE.sub("<uuid>", s)
    s = re.sub(r"\b(pid|PID)[= ]\s*\d+", r"\1=<n>", s)
    s = re.sub(r"\((pid|PID) \d+\)", "(PID <n>)", s)
    s = re.sub(r"\d\d:\d\d UTC", "<hh:mm> UTC", s)
    s = re.sub(r"session=[0-9a-f]{8}", "session=<sid8>", s)
    s = re.sub(r"\d{8}-\d{6}", "<stamp>", s)
    s = re.sub(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(\.\d+)?Z?", "<ts>", s)
    s = re.sub(r"\b1[78]\d{8}\b", "<epoch>", s)
    return s


def _ids(workspace):
    """Map each run-specific id to a stable ordinal as first seen."""
    seen = {}

    def tag(prefix, value):
        if value is None:
            return None
        return f"{prefix}{seen.setdefault((prefix, value), len(seen))}"
    return tag


async def _scenario(workspace):
    h = Harness(workspace, agents=["a", "b"], steering={"enabled": False})
    cap = _Capture()
    async with h:
        h.module.log.addHandler(cap)
        h.module.log.setLevel(logging.DEBUG)
        cap.records.clear()
        mod = h.module

        # 1. A batch of three: hold the state off IDLE while they queue.
        mod.agent_states["a"] = "HOLD"
        h.script(default={"text": "batch reply for {{text}}"})
        for i in range(3):
            await h.send("a", f"queued {i}", channel_id="10")
        mod.agent_states["a"] = "IDLE"
        await mod.process_agent_queue("a")
        await h.wait_idle("a")

        # 2. A tool turn.
        h.script(default={"text": "tooled", "tools": [
            {"name": "Read", "input": {"file_path": "/x"}, "message_id": "m1"},
            {"name": "Bash", "input": {"command": "ls"}, "message_id": "m2"}]})
        await h.send("a", "use tools", channel_id="10")
        await h.wait_idle("a")

        # 3. An is_error turn.
        h.script(default={"text": "raw cli failure", "is_error": True})
        await h.send("a", "break", channel_id="10")
        await h.wait_idle("a")

        # 4. An interrupt.
        h.script(default={"hang": True})
        await h.send("a", "stuck", channel_id="10")
        await h.wait_for(lambda: mod.agent_states.get("a") == "PROCESSING")
        await h.wait_for(lambda: h.sent_to("a"))
        await h.interrupt("a")
        await h.wait_idle("a")

        # 5. A respawn: the subprocess exits mid-turn.
        h.script(default={"text": "dying", "exit": True})
        await h.send("a", "boom", channel_id="10")
        await h.wait_for(lambda: any("restarted" in d["content"] for d in h.discord))
        h.script(default={"text": "back"})
        await h.wait_idle("a")
        await h.send("a", "after respawn", channel_id="10")
        await h.wait_idle("a")

        # 6. Agent b: a normal turn, then a usage wall that holds the batch.
        h.script(default={"text": "b ok"})
        await h.send("b", "hello b", channel_id="20")
        await h.wait_idle("b")
        h.script(default={"text": "You've hit your session limit", "is_error": True})
        await h.send("b", "walled", channel_id="20")
        await h.wait_for(lambda: any(
            r["processed"] == 0 and r["not_before"] for r in h.queue_rows("b")))
        await h.wait_for(lambda: mod.agent_states.get("b") == "IDLE")
        await asyncio.sleep(0.2)

        rows = {s: h.queue_rows(s) for s in ("a", "b")}
        cost = h.cost_rows()
        sessions = h._query("SELECT * FROM sessions ORDER BY agent")
        discord = list(h.discord)
        for task in list(mod.agent_hold_tasks.values()):
            task.cancel()
        mod.log.removeHandler(cap)
        sid_of = {s: h.session_id(s) for s in ("a", "b")}

    tag = _ids(workspace)

    def norm_row(r):
        return {
            "agent": r["agent"], "channel_id": r["channel_id"],
            "author": r["author"], "content": _scrub(r["content"], workspace),
            "message_id": tag("m", r["message_id"]),
            "processed": r["processed"],
            "response": _scrub(r["response"], workspace),
            "discord_response_id": r["discord_response_id"],
            "held": bool(r["not_before"]),
            "claimed": r["claimed_by"] is not None,
        }

    def sid(v):
        return tag("s", v) if v else None

    def scrub_ids(s):
        s = _scrub(s, workspace)
        for agent, v in sid_of.items():
            if v:
                s = s.replace(v, "<sid>")
        return s

    return {
        "queue": {s: [norm_row(r) for r in rows[s]] for s in rows},
        "cost": [{k: (sid(v) if k == "session_id" else v)
                  for k, v in r.items() if k not in ("id", "timestamp", "created_at")}
                 for r in cost],
        "sessions": [{k: (sid(v) if k == "session_id" else v)
                      for k, v in r.items()
                      if k not in ("id", "created_at", "updated_at", "last_active",
                                   "started_at", "last_used", "context_updated_at")}
                     for r in sessions],
        "discord": [{**d, "content": scrub_ids(d["content"])} for d in discord],
        "log": [[lvl, scrub_ids(msg)] for lvl, msg in cap.records],
    }


def test_turn_loop_golden(tmp_workspace):
    got = asyncio.run(_scenario(tmp_workspace))
    text = json.dumps(got, indent=1, sort_keys=True) + "\n"
    if os.environ.get("TURN_LOOP_GOLDEN_RECORD") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text)
    want = GOLDEN.read_text()
    if text != want:
        diff = "\n".join(difflib.unified_diff(
            want.splitlines(), text.splitlines(), "golden", "got", lineterm="", n=2))
        raise AssertionError("turn loop transcript changed:\n" + diff)
