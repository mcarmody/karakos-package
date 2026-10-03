#!/usr/bin/env python3
"""Fake `claude` CLI for the integration harness.

Speaks just enough of Claude Code's stream-json protocol for bin/agent-server.py
to drive its real spawn path: reads one JSON user line per turn from stdin and
answers with `system/init` (once), `assistant` events, and a closing `result`.

Environment:
  FAKE_CLAUDE_LOG_DIR  where <session-id>.argv.json / .in.jsonl are written
  FAKE_CLAUDE_SCRIPT   JSON file: {"default": Step, "rules": [{"match": re,
                       "agent": re (optional), "step": Step}]}. Re-read every
                       turn so a test can change the script between messages.

Step keys (all optional): text, tools [{name, input, usage, message_id,
parent_tool_use_id}], usage, cost, delay_ms, is_error, exit, hang,
parent_tool_use_id (for the text event), rate_limit {status, type,
resets_at, windows: {type: {utilization, resets_at}}} (emits a recorded-shape
`rate_limit_event` just before the result), result_usage (the closing
result's usage; defaults to `usage`, as the real CLI sums across calls). Templates in `text`: {{text}} echoes
the user's input, {{env:NAME}} reads an environment variable.

Queued-stdin mode (step 0.3b) -- on only with `--replay-user-messages` or
FAKE_CLAUDE_QUEUED=1; otherwise the fake behaves exactly as above. It replays
the behaviour recorded from the real CLI (tests/harness/fixtures/real-cli):
stdin is read concurrently with a running turn, lines that arrive mid-turn are
held and merged at the next tool boundary (or coalesced into the next turn),
every turn opens with system/init and ends with one result, and an interrupt
control_request is answered without exiting. Extra Step keys in this mode:
tools [{name, input, ms, output}], pre_text (text block sharing the tool_use
message id), sidechain (bool) / sidechain_text, background_task {ms} with
after_text. Extra templates: {{queued}} (lines merged mid-turn),
{{system_prompt}} (the session's original prompt). Extra environment:
FAKE_CLAUDE_INIT_DELAY_MS, FAKE_CLAUDE_MCP_FAILED=a,b. Everything stdin/stdout
is also logged to $FAKE_CLAUDE_LOG_DIR/<session-id>.io.jsonl.
"""

import datetime
import json
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid

DEFAULT_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 1000,
    "output_tokens": 5,
}
DEFAULT_COST = 0.001


def parse_argv(argv):
    """Return {flag: [values]} for `--flag value` pairs; bare flags map to []."""
    flags = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags.setdefault(a, []).append(argv[i + 1])
                i += 2
                continue
            flags.setdefault(a, [])
        i += 1
    return flags


def rate_limit_event(sid, spec):
    """A `rate_limit_event` in the recorded real-CLI shape (spec 2.7). `spec` is
    {"status", "type", "resets_at", "windows": {type: {utilization, resets_at}}}."""
    t = spec.get("type", "five_hour")
    resets = spec.get("resets_at")
    info = {"status": spec.get("status", "allowed"), "resetsAt": resets,
            "rateLimitType": t, "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled", "isUsingOverage": False}
    if spec.get("windows"):
        info["unifiedWindows"] = {
            k: {"utilization": v.get("utilization"), "resetsAt": v.get("resets_at")}
            for k, v in spec["windows"].items()}
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "uuid": str(uuid.uuid4()), "session_id": sid}


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def render(template, text, extra=None):
    def sub(m):
        key = m.group(1).strip()
        if key == "text":
            return text
        if extra and key in extra:
            return extra[key]
        if key.startswith("env:"):
            return os.environ.get(key[4:], "")
        return m.group(0)
    return re.sub(r"\{\{(.*?)\}\}", sub, template)


def load_script():
    path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def pick_step(script, text):
    # A rule's "agent" regex is matched against the shard id (the agent id for a
    # default shard).
    agent = os.environ.get("KARAKOS_SHARD") or os.environ.get("KARAKOS_AGENT", "")
    for rule in script.get("rules", []):
        if rule.get("agent") and not re.search(rule["agent"], agent):
            continue
        if re.search(rule["match"], text):
            return rule["step"]
    return script.get("default", {})


def user_text(event):
    content = (event.get("message") or {}).get("content", "")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


SLICE_S = 0.02
VERSION = "2.1.287"
REJECTION = ("The user doesn't want to proceed with this tool use. The tool use "
             "was rejected (eg. if it was a file edit, the new_string was NOT "
             "written to the file). STOP what you are doing and wait for the "
             "user to tell you how to proceed.")


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


class Interrupted(Exception):
    """Raised inside a turn when a tool in flight is interrupted."""


class Queued:
    def __init__(self, flags):
        self.flags = flags
        self.t0 = time.time()
        resume = (flags.get("--resume") or [None])[0]
        self.sid = resume or (flags.get("--session-id") or ["no-session"])[0]
        self.resumed = bool(resume)
        self.replay = "--replay-user-messages" in flags
        self.log_dir = os.environ.get("FAKE_CLAUDE_LOG_DIR")
        self.q = queue.Queue()
        self.pending = []          # (iso_ts, text) user lines held, not consumed
        self.eof = False
        self.sigint = False
        self.result_index = 0
        self.turn_no = 0
        self.first_init = True
        self.tool_in_flight = None  # tool_use id while a tool "runs"
        self.out_lock = threading.Lock()
        self.system_prompt = self._system_prompt()

    # -- logging -----------------------------------------------------------

    def _io(self, direction, event, t=None):
        if not self.log_dir:
            return
        rec = {"t": round((t or time.time()) - self.t0, 3), "dir": direction}
        rec["event"] = event
        with open(os.path.join(self.log_dir, f"{self.sid}.io.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def out(self, event):
        with self.out_lock:
            self._io("out", event)
            emit(event)

    def base(self, **kw):
        kw.setdefault("session_id", self.sid)
        kw.setdefault("uuid", str(uuid.uuid4()))
        return kw

    # -- session / system prompt (Q5) ----------------------------------------

    def _system_prompt(self):
        f = self.flags
        cur = {"system": (f.get("--system-prompt") or [""])[0],
               "append": (f.get("--append-system-prompt") or [""])[0]}
        path = os.path.join(self.log_dir, f"{self.sid}.prompt.json") if self.log_dir else None
        if self.resumed and path and os.path.exists(path):
            with open(path) as fh:
                cur = json.load(fh)  # resume keeps the original; new flags ignored
        elif path:
            with open(path, "w") as fh:
                json.dump(cur, fh)
        return "\n".join(p for p in (cur["system"], cur["append"]) if p)

    # -- stdin ---------------------------------------------------------------

    def start_reader(self):
        def read():
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.q.put((time.time(), event))
            self.q.put(None)
        threading.Thread(target=read, daemon=True).start()

        def on_sigint(*_):
            self.sigint = True
            if self.log_dir:
                with open(os.path.join(self.log_dir, f"{self.sid}.io.jsonl"), "a") as f:
                    f.write(json.dumps({"t": round(time.time() - self.t0, 3),
                                        "dir": "in", "signal": "SIGINT"}) + "\n")
        signal.signal(signal.SIGINT, on_sigint)

    def pump(self, wait=0.0):
        """Drain stdin (blocking up to `wait` for the first item): answer
        control requests at once, hold user lines. Returns True if an
        interrupt request was received."""
        interrupted = False
        timeout = wait
        while True:
            try:
                item = self.q.get(timeout=timeout) if timeout else self.q.get_nowait()
            except queue.Empty:
                return interrupted
            timeout = 0
            if item is None:
                self.eof = True
                return interrupted
            ts, event = item
            self._io("in", event, ts)
            kind = event.get("type")
            if kind == "control_request":
                req = event.get("request") or {}
                if req.get("subtype") == "interrupt":
                    interrupted = True
                    self.out({"type": "control_response", "response": {
                        "subtype": "success", "request_id": event.get("request_id"),
                        "response": {"still_queued": []}}})
            elif kind == "user":
                text = user_text(event)
                if self.log_dir:
                    with open(os.path.join(self.log_dir, f"{self.sid}.in.jsonl"), "a") as f:
                        f.write(json.dumps({"ts": ts, "text": text}) + "\n")
                self.pending.append((_iso(ts), text))

    def sleep(self, secs, interruptible=False):
        """Sleep in <=20ms slices, pumping stdin. If `interruptible`, an
        interrupt request or SIGINT raises Interrupted."""
        end = time.time() + secs
        while True:
            if self.pump() and interruptible:
                raise Interrupted()
            if self.sigint and interruptible:
                raise Interrupted()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(SLICE_S, left))

    def take_pending(self):
        ts, texts = self.pending[0][0], [t for _, t in self.pending]
        self.pending = []
        return ts, "\n".join(texts)

    # -- turns -----------------------------------------------------------------

    def init_event(self):
        failed = [n for n in os.environ.get("FAKE_CLAUDE_MCP_FAILED", "").split(",") if n]
        servers = []
        cfg = (self.flags.get("--mcp-config") or [None])[0]
        if cfg and os.path.isfile(cfg):
            try:
                with open(cfg) as f:
                    servers = [n for n in (json.load(f).get("mcpServers") or {})]
            except (OSError, ValueError, AttributeError):
                servers = []
        names = servers + [n for n in failed if n not in servers]
        return self.base(
            type="system", subtype="init",
            model=(self.flags.get("--model") or [""])[0], tools=["Read", "Bash"],
            claude_code_version=VERSION,
            mcp_servers=[{"name": n, "status": "failed" if n in failed else "connected"}
                         for n in names])

    def open_turn(self, ts, text):
        if self.first_init:
            self.first_init = False
            delay = os.environ.get("FAKE_CLAUDE_INIT_DELAY_MS")
            if delay:
                self.sleep(int(delay) / 1000.0)
        self.out(self.init_event())
        self.replay_event(ts, text)

    def replay_event(self, ts, text):
        if self.replay:
            self.out(self.base(type="user", message={"role": "user", "content": text},
                               parent_tool_use_id=None, isReplay=True, timestamp=ts))

    def assistant(self, msg_id, block, usage, ptu=None):
        self.out(self.base(type="assistant", parent_tool_use_id=ptu, message={
            "id": msg_id, "role": "assistant", "content": [block],
            "stop_reason": None, "usage": usage}))

    def tool_result(self, tool_id, content, is_error=False):
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        if is_error:
            block["is_error"] = True
        self.out(self.base(type="user", parent_tool_use_id=None,
                           message={"role": "user", "content": [block]}))

    def finish(self, step, text, started, reply, tools_n, aborted=False):
        is_error = bool(step.get("is_error")) or aborted
        usage = step.get("usage") or DEFAULT_USAGE
        if step.get("rate_limit"):
            self.out(rate_limit_event(self.sid, step["rate_limit"]))
        res = self.base(
            type="result",
            subtype="error_during_execution" if aborted else ("error" if is_error else "success"),
            is_error=is_error, usage=usage,
            total_cost_usd=(step["cost"] if "cost" in step
                            else DEFAULT_COST * (self.result_index + 1)),
            duration_ms=int((time.time() - started) * 1000),
            num_turns=1 + tools_n, queued_turn_count=0,
            terminal_reason="aborted_tools" if aborted else "completed",
            result_index=self.result_index)
        if not aborted:
            res["result"] = reply
        self.result_index += 1
        self.out(res)

    def run_turn(self, ts, text):
        self.turn_no += 1
        n = self.turn_no
        started = time.time()
        step = pick_step(load_script(), text)
        if step.get("hang"):
            while True:  # unresponsive: never reads again, never answers
                time.sleep(3600)
        self.open_turn(ts, text)
        usage = step.get("usage") or DEFAULT_USAGE
        mid = f"msg_{self.sid[:8]}_{n}"
        tools = step.get("tools") or []
        queued_text = ""
        try:
            if step.get("delay_ms"):
                self.sleep(step["delay_ms"] / 1000.0)
            if tools:
                if step.get("pre_text"):
                    self.assistant(f"{mid}_a", {"type": "text", "text": render(
                        step["pre_text"], text, {"system_prompt": self.system_prompt})}, usage)
                ids = [f"toolu_{n}_{i}" for i in range(len(tools))]
                for i, tool in enumerate(tools):
                    self.assistant(f"{mid}_a",
                                   {"type": "tool_use", "id": ids[i], "name": tool["name"],
                                    "input": tool.get("input", {})}, usage)
                bg = step.get("background_task")
                if bg:
                    self.out(self.base(type="system", subtype="task_started",
                                       task_id=f"task_{n}", tool_use_id=ids[0],
                                       is_backgrounded=True))
                if step.get("sidechain"):
                    parent = next((ids[i] for i, t in enumerate(tools)
                                   if t["name"] in ("Task", "Agent")), ids[0])
                    self.assistant(f"{mid}_s", {"type": "text", "text": step.get(
                        "sidechain_text", "PONG")}, usage, ptu=parent)
                for i, tool in enumerate(tools):
                    self.tool_in_flight = ids[i]
                    self.sleep(tool.get("ms", 0) / 1000.0, interruptible=True)
                    self.tool_in_flight = None
                    self.tool_result(ids[i], tool.get("output", "ok"))
                    if self.pending:  # tool boundary: merge queued lines
                        qts, qtext = self.take_pending()
                        queued_text = (queued_text + "\n" + qtext) if queued_text else qtext
                        self.replay_event(qts, qtext)
                if bg:
                    self.sleep(bg.get("ms", 0) / 1000.0)
                    self.out(self.base(type="system", subtype="task_notification",
                                       task_id=f"task_{n}", tool_use_id=ids[0],
                                       status="completed"))
        except Interrupted:
            self.interrupted_events()
            self.finish(step, text, started, "", len(tools), aborted=True)
            return None
        reply = render(step.get("text", "ok"), text,
                       {"queued": queued_text, "system_prompt": self.system_prompt})
        self.assistant(f"{mid}_t", {"type": "text", "text": reply}, usage)
        if step.get("exit"):
            sys.exit(1)
        self.finish(step, text, started, reply, len(tools))
        return step

    def interrupted_events(self):
        tool_id, self.tool_in_flight = self.tool_in_flight, None
        self.tool_result(tool_id, REJECTION, is_error=True)
        self.out(self.base(type="user", parent_tool_use_id=None, message={
            "role": "user",
            "content": [{"type": "text",
                         "text": "[Request interrupted by user for tool use]"}]}))

    def background_second_turn(self, step):
        self.turn_no += 1
        started = time.time()
        self.out(self.init_event())
        text = render(step.get("after_text", "done"), "", {
            "system_prompt": self.system_prompt})
        self.assistant(f"msg_{self.sid[:8]}_{self.turn_no}_t",
                       {"type": "text", "text": text}, step.get("usage") or DEFAULT_USAGE)
        self.finish({}, "", started, text, 0)

    # -- main loop ---------------------------------------------------------------

    def run(self):
        self.start_reader()
        while True:
            self.pump(wait=SLICE_S)
            if self.sigint:
                return 0
            if self.pending:
                ts, text = self.take_pending()
                step = self.run_turn(ts, text)
                if self.sigint:
                    return 0
                if step and step.get("background_task"):
                    self.background_second_turn(step)
            elif self.eof:
                return 0


def main():
    flags = parse_argv(sys.argv[1:])
    if "--replay-user-messages" in flags or os.environ.get("FAKE_CLAUDE_QUEUED") == "1":
        sid = (flags.get("--resume") or flags.get("--session-id") or ["no-session"])[0]
        log_dir = os.environ.get("FAKE_CLAUDE_LOG_DIR")
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"{sid}.argv.json"), "w") as f:
                json.dump(sys.argv[1:], f)
        sys.exit(Queued(flags).run())
    sid = (flags.get("--session-id") or ["no-session"])[0]
    log_dir = os.environ.get("FAKE_CLAUDE_LOG_DIR")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, f"{sid}.argv.json"), "w") as f:
            json.dump(sys.argv[1:], f)

    emit({"type": "system", "subtype": "init", "session_id": sid,
          "model": (flags.get("--model") or [""])[0], "tools": ["Read", "Bash"]})

    n_msg = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "user":
            continue
        text = user_text(event)
        if log_dir:
            with open(os.path.join(log_dir, f"{sid}.in.jsonl"), "a") as f:
                f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")

        step = pick_step(load_script(), text)
        n_msg += 1
        started = time.time()
        if step.get("hang"):
            while True:
                time.sleep(3600)
        if step.get("delay_ms"):
            time.sleep(step["delay_ms"] / 1000.0)

        usage = step.get("usage") or DEFAULT_USAGE
        reply = render(step.get("text", "ok"), text)
        ptu = step.get("parent_tool_use_id")
        emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": ptu,
              "message": {"id": f"msg_{sid[:8]}_{n_msg}_t", "role": "assistant",
                          "content": [{"type": "text", "text": reply}],
                          "usage": usage}})
        for i, tool in enumerate(step.get("tools") or []):
            emit({"type": "assistant", "session_id": sid,
                  "parent_tool_use_id": tool.get("parent_tool_use_id"),
                  "message": {"id": tool.get("message_id") or f"msg_{sid[:8]}_{n_msg}_{i}",
                              "role": "assistant",
                              "content": [{"type": "tool_use",
                                           "id": f"toolu_{n_msg}_{i}",
                                           "name": tool["name"],
                                           "input": tool.get("input", {})}],
                              "usage": tool.get("usage") or usage}})
        if step.get("exit"):
            sys.exit(1)

        is_error = bool(step.get("is_error"))
        if step.get("rate_limit"):
            emit(rate_limit_event(sid, step["rate_limit"]))
        emit({"type": "result", "subtype": "error" if is_error else "success",
              "session_id": sid, "is_error": is_error, "result": reply,
              "usage": step.get("result_usage") or usage,
              "total_cost_usd": (step["cost"] if "cost" in step
                                 else DEFAULT_COST * n_msg),
              "duration_ms": int((time.time() - started) * 1000)})


if __name__ == "__main__":
    main()
