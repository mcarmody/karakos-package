"""Friction sensor (MIRDAIN G6, build A). No LLM, read-only on the logs.

Scans the raw model streams in logs/agent-streams/*.jsonl for tool errors,
non-zero Bash exits, denied tools, rate-limit pressure and repeated Bash
commands, and appends one line per event to data/friction-log.jsonl. Progress is
kept in data/friction-sensor-state.json (byte offset per stream), so a rerun
only reads new lines. Nothing here edits a prompt, persona or config.
Transcripts named by KARAKOS_TRANSCRIPT_GLOBS are scanned beside the stream logs.
Rows carry both the package and the workspace field names (contract 07).

Repetition is counted two ways: REPEAT_AT (3) identical Bash commands in one
stream emit a `repeat` row; REPEAT_AT_WINDOW (5) across all streams within
WINDOW_DAYS (7) emit a `(cross-session)` `repeat` row, again at each further
multiple of 5. A hand-written `note` row is added by `note()` (bin/friction-sensor.py --note).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from lib.redact import redact_text

LOG_NAME = "friction-log.jsonl"
STATE_NAME = "friction-sensor-state.json"
SUMMARY_REL = Path("friction") / "weekly.md"
REPEAT_AT = 3            # same Bash command this many times in one stream
WINDOW_DAYS = 7          # cross-session repetition window
REPEAT_AT_WINDOW = 5     # same Bash command this many times across streams in the window
RATE_HIGH = 0.9          # five-hour or weekly utilisation worth noting
DETAIL_MAX = 200
NOTE_MAX = 1000
_SECRET_ASSIGN_RE = re.compile(r"\b(\w*(?:TOK|TOKEN|KEY|SECRET|PASS|PWD|CRED)\w*)=\S+", re.I)
_URL_CRED_RE = re.compile(r"https?://[^/\s@]+@")
PENDING_MAX = 200
DENIED_MARKERS = ("Blocked:", "permission denied", "was denied", "not allowed", "requires approval")
TRANSCRIPT_GLOBS_ENV = "KARAKOS_TRANSCRIPT_GLOBS"
KIND_TO_CATEGORY = {"repeat": "repetition", "bash_exit": "command_error", "denied": "permission",
                    "tool_error": "tool_error", "rate_limit": "rate_limit", "note": "note"}
CATEGORY_TO_KIND = {v: k for k, v in KIND_TO_CATEGORY.items()}
_AGENT_DIR_RE = re.compile(r"agents-([a-z0-9-]+)")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def transcript_files() -> list[Path]:
    """*.jsonl files in the directories matched by KARAKOS_TRANSCRIPT_GLOBS (os.pathsep-separated)."""
    found = set()
    for pattern in os.environ.get(TRANSCRIPT_GLOBS_ENV, "").split(os.pathsep):
        if not pattern.strip():
            continue
        for d in glob.glob(pattern.strip()):
            if os.path.isdir(d):
                found.update(Path(f).absolute() for f in glob.glob(os.path.join(d, "*.jsonl")))
    return sorted(found)


def _agent_of(path: Path, transcript: bool = False) -> str:
    if transcript:
        m = _AGENT_DIR_RE.search(str(path.parent))
        return m.group(1) if m else path.parent.name
    return path.name.rsplit("_", 1)[0]


def redact_detail(text: str) -> str:
    """redact_text, then mask `NAME_TOKEN=value` assignments and URL userinfo."""
    out = _SECRET_ASSIGN_RE.sub(r"\1=[redacted]", redact_text(text))
    return _URL_CRED_RE.sub("https://[redacted]@", out)


def _clip(text) -> str:
    one = " ".join(str(text or "").split())
    return redact_detail(one)[:DETAIL_MAX]


def _window_sig(cmd: str) -> str:
    """Readable, redacted, bounded form of a Bash command, used as the cross-session key."""
    return redact_detail(" ".join(cmd.split()))[:DETAIL_MAX]


def _result_text(block) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _sig(cmd: str) -> str:
    return hashlib.sha1(" ".join(cmd.split()).encode()).hexdigest()[:12]


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def both_names(row: dict) -> dict:
    """Fill the package and workspace field names from each other (contract 07). Never overwrites."""
    if "category" not in row and "kind" in row:
        row["category"] = KIND_TO_CATEGORY.get(row["kind"], row["kind"])
    if "kind" not in row and "category" in row:
        row["kind"] = CATEGORY_TO_KIND.get(row["category"], row["category"])
    if "evidence" not in row and "detail" in row:
        row["evidence"] = row["detail"]
    if "detail" not in row and "evidence" in row:
        row["detail"] = row["evidence"]
    if "session" not in row and "agent" in row:
        row["session"] = row["agent"]
    if "agent" not in row and "session" in row:
        row["agent"] = row["session"]
    return row


def _scan_file(path: Path, st: dict, transcript: bool = False, window: Optional[list] = None) -> list:
    """New friction events from `path`; updates `st` (offset, pending, counts, rl).
    Bash commands are also appended to `window` as [ts, signature] pairs."""
    if window is None:
        window = []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size < st.get("offset", 0):
        st.clear()  # truncated or replaced
    with open(path, "rb") as fh:
        fh.seek(st.get("offset", 0))
        data = fh.read()
    end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
    st["offset"] = st.get("offset", 0) + end
    pending = st.setdefault("pending", {})
    counts = st.setdefault("counts", {})
    agent, events = _agent_of(path, transcript), []

    def add(kind, tool, detail):
        events.append({"agent": agent, "stream": path.name, "kind": kind,
                       "tool": tool, "detail": _clip(detail), "ts": d_ts})

    for raw in data[:end].splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        d_ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else None
        t = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        if t == "assistant":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name, inp = b.get("name", "?"), b.get("input") or {}
                    cmd = inp.get("command") if name == "Bash" else None
                    pending[b.get("id", "")] = [name, cmd[:DETAIL_MAX] if cmd else ""]
                    if cmd:
                        s = _sig(cmd)
                        counts[s] = counts.get(s, 0) + 1
                        if counts[s] == REPEAT_AT:
                            add("repeat", "Bash", f"x{REPEAT_AT}: {cmd}")
                        window.append([d_ts or _now(), _window_sig(cmd)])
        elif t == "user":
            for b in blocks:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                name, cmd = pending.pop(b.get("tool_use_id", ""), ["unknown", ""])
                if not b.get("is_error"):
                    continue
                text = _result_text(b)
                if any(m.lower() in text.lower() for m in DENIED_MARKERS):
                    add("denied", name, text)
                elif name == "Bash" and text.startswith("Exit code"):
                    add("bash_exit", name, f"{text.splitlines()[0]} | {cmd}")
                else:
                    add("tool_error", name, text)
        elif t == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
            util = max([w.get("utilization", 0) or 0
                        for w in (info.get("unifiedWindows") or {}).values()
                        if isinstance(w, dict)] or [0])
            state = ("blocked" if info.get("status") != "allowed"
                     else "high" if util >= RATE_HIGH else "ok")
            if state != "ok" and st.get("rl") != state:
                add("rate_limit", info.get("rateLimitType", "?"),
                    f"status={info.get('status')} utilization={util:.2f}")
            st["rl"] = state
    while len(pending) > PENDING_MAX:
        pending.pop(next(iter(pending)))
    return events


def scan(workspace) -> dict:
    """Scan every stream once; append events; return {'scanned': n, 'events': n}."""
    ws = Path(workspace)
    state_path = ws / "data" / STATE_NAME
    state = _load(state_path, {})
    streams = state.setdefault("streams", {})
    window = state.setdefault("cmd_window", [])
    emitted = state.setdefault("emitted", {})
    out, scanned = [], 0
    for p in sorted((ws / "logs" / "agent-streams").glob("*.jsonl")):
        scanned += 1
        out += _scan_file(p, streams.setdefault(p.name, {}), window=window)
    for p in transcript_files():
        scanned += 1
        out += _scan_file(p, streams.setdefault(str(p), {}), transcript=True, window=window)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window[:] = [w for w in window if w[0] >= cutoff]
    for sig, n in Counter(w[1] for w in window).items():
        step = (n // REPEAT_AT_WINDOW) * REPEAT_AT_WINDOW
        if step >= REPEAT_AT_WINDOW and step > emitted.get(sig, 0):
            out.append({"agent": "(cross-session)", "stream": "", "kind": "repeat", "tool": "Bash",
                        "detail": f"'{sig}' ran {n}x in {WINDOW_DAYS}d", "signature": sig, "count": n})
            emitted[sig] = step
    stamp = _now()
    if out:
        log = ws / "data" / LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            for e in out:
                ts = e.pop("ts", None) or stamp
                if ts.endswith("+00:00"):
                    ts = ts[:-len("+00:00")] + "Z"
                fh.write(json.dumps(both_names({"ts": ts, **e})) + "\n")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)
    return {"scanned": scanned, "events": len(out)}


def note(workspace, text: str, tag: Optional[str] = None, agent: Optional[str] = None) -> dict:
    """Hand-logged `note` row (kind note, category note) appended to the friction log."""
    row = {"ts": _now(), "agent": agent or os.environ.get("KARAKOS_AGENT") or "(hand-logged)",
           "kind": "note", "tool": "friction",
           "detail": redact_detail(" ".join(text.split()))[:NOTE_MAX]}
    if tag:
        row["signature"] = tag
    row = both_names(row)
    log = Path(workspace) / "data" / LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read_log(workspace, days=7, stats=None) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    try:
        for line in (Path(workspace) / "data" / LOG_NAME).read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                r = None
            if not isinstance(r, dict):
                if isinstance(stats, dict):
                    stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            if r.get("ts", "") >= cutoff:
                rows.append(both_names(r))
    except OSError:
        pass
    return rows


def summary_text(rows, days=7) -> str:
    lines = [f"# Friction summary, last {days} days", f"{len(rows)} events."]
    if rows:
        lines += ["", "## By kind"] + [f"- {k}: {n}" for k, n in
                                       Counter(r["kind"] for r in rows).most_common()]
        lines += ["", "## By tool"] + [f"- {k}: {n}" for k, n in
                                       Counter(r["tool"] for r in rows).most_common(10)]
        top = Counter((r["kind"], r["tool"], r["detail"][:80]) for r in rows).most_common(5)
        lines += ["", "## Repeated"] + [f"- {n}x {k}/{t}: {d}" for (k, t, d), n in top if n > 1]
    return "\n".join(lines) + "\n"


def write_summary(workspace, days=7) -> Path:
    path = Path(workspace) / "data" / SUMMARY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(summary_text(read_log(workspace, days), days))
    os.replace(tmp, path)
    return path
