"""The stream-json keys the server actually reads (7.3a).

Derived by reading `lib/turn_loop.py` `read_events` (and the context rule in
1.5: assistant `message.id`/`usage`, main-thread only via `parent_tool_use_id`),
not from a spec. If `read_events` starts reading another key, add it here: the
daily real-CLI canary then fails the day the CLI stops sending it.

Each entry is a tuple of path segments; `[]` means "every element of a list".
`None` as an allowed-missing marker is not used: every key listed must be
present (a null value counts as present, e.g. `parent_tool_use_id` on the main
thread).
"""

INIT_KEYS = [
    ("session_id",), ("model",), ("tools",), ("mcp_servers",),
]
ASSISTANT_KEYS = [
    ("message", "id"), ("message", "content"), ("message", "usage"),
    ("parent_tool_use_id",),
]
ASSISTANT_BLOCK_KEYS = [("type",)]          # message.content[].type
RESULT_KEYS = [
    ("subtype",), ("is_error",), ("result",), ("total_cost_usd",),
    ("usage", "input_tokens"), ("usage", "output_tokens"),
    ("session_id",), ("duration_ms",),
]


def missing_keys(event: dict, keys) -> list:
    """The dotted paths in `keys` absent from `event` ([] when all present)."""
    out = []
    for path in keys:
        cur = event
        for seg in path:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                out.append(".".join(path))
                break
    return out


def check_events(events: list) -> list:
    """Problems found in one turn's events; [] when the schema holds. Each
    problem names the event so a failure prints what the CLI really sent."""
    import json
    problems = []
    seen = {"init": 0, "assistant": 0, "result": 0}
    for ev in events:
        t, sub = ev.get("type"), ev.get("subtype")
        if t == "system" and sub == "init":
            seen["init"] += 1
            keys = INIT_KEYS
        elif t == "assistant":
            seen["assistant"] += 1
            keys = ASSISTANT_KEYS
            for block in (ev.get("message") or {}).get("content") or []:
                miss = missing_keys(block, ASSISTANT_BLOCK_KEYS)
                if miss:
                    problems.append(f"assistant content block missing {miss}: {json.dumps(block)[:300]}")
        elif t == "result":
            seen["result"] += 1
            keys = RESULT_KEYS
        else:
            continue
        miss = missing_keys(ev, keys)
        if miss:
            problems.append(f"{t}/{sub} event missing {miss}: {json.dumps(ev)[:600]}")
    for kind, n in seen.items():
        if n == 0:
            problems.append(f"no {kind} event in the stream")
    return problems


# ---- shape comparison against the 0.4 fixtures (by (type, subtype), no ids/text/timing)

# Events that appear or not depending on model timing, not on CLI behaviour.
NOISE = {("system", "thinking_tokens"), ("rate_limit_event", None),
         ("system", "task_started"), ("system", "task_notification"),
         ("system", "task_progress")}


def shape(events: list) -> list:
    """`(type, subtype)` pairs of `events` with noise dropped and runs of the
    same pair collapsed (one API message arrives as one assistant event per
    block, and how many blocks is the model's choice)."""
    out = []
    for ev in events:
        pair = (ev.get("type"), ev.get("subtype"))
        if pair in NOISE:
            continue
        if ev.get("type") == "control_response":
            pair = ("control_response", (ev.get("response") or {}).get("subtype"))
        if out and out[-1] == pair:
            continue
        out.append(pair)
    return out


def fixture_events(stdout_jsonl) -> list:
    """Server-to-client events of a 0.4 fixture's stdout.jsonl."""
    import json
    from pathlib import Path
    events = []
    for line in Path(stdout_jsonl).read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("dir") == "out":
                events.append(rec["event"])
    return events


def raw_events(stdout_raw_jsonl) -> list:
    """Events from the recorder's `stdout.raw.jsonl` ({"t","line"} per line)."""
    import json
    from pathlib import Path
    events = []
    for line in Path(stdout_raw_jsonl).read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(json.loads(line)["line"]))
        except ValueError:
            pass
    return events
