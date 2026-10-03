"""Step 0.3b: the fake claude's queued-stdin mode replays the behaviour recorded
from the real CLI (tests/harness/fixtures/real-cli). Each scenario replays the
recorded stdin.jsonl against the fake with timing scaled by 0.01 and compares
the output with the recorded stdout.jsonl by shape (see shape()). Ordering
assertions only; no absolute times below 50 ms.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "harness"
FAKE = HARNESS / "fake_claude.py"
FIXTURES = HARNESS / "fixtures" / "real-cli"
SCALE = 0.01

pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason=f"recorded real-CLI fixtures missing at {FIXTURES} (they arrive with step 0.4's PR)")


# -- helpers -----------------------------------------------------------------

def recorded(scenario, name):
    path = FIXTURES / scenario / name
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def shape(events):
    """Order-preserving comparison shape: types/subtypes, isReplay, terminal
    reason, is_error, replay text. Ids, times, usage, costs, thinking blocks and
    housekeeping system events are ignored."""
    out = []
    for e in events:
        t = e.get("type")
        if t == "system":
            if e.get("subtype") == "init":
                out.append(("init",))
        elif t == "user":
            content = (e.get("message") or {}).get("content")
            if e.get("isReplay"):
                out.append(("replay", content))
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result":
                        out.append(("tool_result", bool(b.get("is_error"))))
                    elif b.get("type") == "text":
                        out.append(("user_text", b["text"]))
        elif t == "assistant":
            for b in e["message"]["content"]:
                if b.get("type") in ("text", "tool_use"):
                    out.append(("assistant", b["type"], e.get("parent_tool_use_id")))
        elif t == "result":
            out.append(("result", e.get("subtype"), e.get("is_error"),
                        e.get("terminal_reason")))
        elif t == "control_response":
            r = e["response"]
            out.append(("control_response", r["subtype"],
                        (r.get("response") or {}).get("still_queued")))
    return out


class Fake:
    """A fake claude subprocess driven through pipes."""

    def __init__(self, tmp_path, script=None, args=("--replay-user-messages",),
                 sid="sess-1", env=None):
        self.log_dir = tmp_path / "logs"
        script_path = tmp_path / "script.json"
        script_path.write_text(json.dumps(script or {}))
        full_env = {"PATH": os.environ.get("PATH", ""),
                    "FAKE_CLAUDE_LOG_DIR": str(self.log_dir),
                    "FAKE_CLAUDE_SCRIPT": str(script_path), **(env or {})}
        self.sid = sid
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--session-id", sid, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=full_env, cwd=str(tmp_path))
        self.events = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        deadline = time.time() + 5   # wait for the fake to be up and reading
        while not (self.log_dir / f"{sid}.argv.json").exists() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)

    def _read(self):
        for line in self.proc.stdout:
            if line.strip():
                self.events.append(json.loads(line))

    def send(self, event):
        self.proc.stdin.write(json.dumps(event) + "\n")
        self.proc.stdin.flush()

    def user(self, text):
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def interrupt(self, rid="req-1"):
        self.send({"type": "control_request", "request_id": rid,
                   "request": {"subtype": "interrupt"}})

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(list(self.events)):
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out; events so far: {shape(self.events)}")

    def results(self, n):
        self.wait_for(lambda ev: sum(e.get("type") == "result" for e in ev) >= n)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reader.join(timeout=2)
        return self.proc.returncode

    def io(self):
        path = self.log_dir / f"{self.sid}.io.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines() if l]


def replay_stdin(fake, stdin_events, wait_after=None):
    """Feed recorded stdin entries at recorded offsets * SCALE."""
    t0 = time.time()
    for rec in stdin_events:
        delay = rec["t"] * SCALE - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        if rec.get("signal"):
            fake.proc.send_signal(getattr(signal, rec["signal"]))
        else:
            fake.send(rec["event"])


def recorded_shape(scenario):
    return shape([r["event"] for r in recorded(scenario, "stdout.jsonl") if r["dir"] == "out"])


def tool_step(ms, text="DONE"):
    return {"tools": [{"name": "Bash", "input": {"command": "sleep 20"}, "ms": ms}],
            "text": text}


TOOL_MS = int(20 * 1000 * SCALE)   # a recorded `sleep 20` becomes 200 ms


# -- Q1: merge at the tool boundary ------------------------------------------------

def test_q1_line_mid_tool_is_merged_in_the_same_turn(tmp_path):
    script = {"rules": [{"match": "sleep 20", "step": tool_step(TOOL_MS, "DONE1\n{{queued}}")}]}
    fake = Fake(tmp_path, script)
    replay_stdin(fake, recorded("1-second-user-line-mid-turn", "stdin.jsonl"))
    fake.results(1)
    time.sleep(0.15)
    fake.close()
    assert shape(fake.events) == recorded_shape("1-second-user-line-mid-turn")
    s = shape(fake.events)
    assert sum(1 for x in s if x[0] == "result") == 1
    assert s.index(("replay", "Also, after that, reply with the word SECOND2 on its own line.")) \
        > s.index(("tool_result", False))
    final = [e for e in fake.events if e["type"] == "assistant"][-1]
    text = final["message"]["content"][0]["text"]
    assert "DONE1" in text and "SECOND2" in text


# -- Q2: coalescing -----------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["2-burst", "2-burst/after-idle-turn"])
def test_q2_burst_is_two_turns_and_second_is_coalesced(tmp_path, scenario):
    script = {"rules": [{"match": "ALPHA", "step": {"delay_ms": 150, "text": "ALPHA"}}]}
    fake = Fake(tmp_path, script)
    replay_stdin(fake, recorded(scenario, "stdin.jsonl"))
    n_results = sum(e["event"].get("type") == "result"
                    for e in recorded(scenario, "stdout.jsonl"))
    fake.results(n_results)
    time.sleep(0.15)
    fake.close()
    got = shape(fake.events)
    assert got == recorded_shape(scenario)
    assert sum(1 for x in got if x[0] == "result") == n_results
    assert got.count(("init",)) == n_results
    assert got[-3][1] == ("Reply with only the word BRAVO.\n"
                          "Reply with only the word CHARLIE.")


# -- Q3: interrupt -----------------------------------------------------------------

def test_q3_control_interrupt_aborts_tool_and_process_survives(tmp_path):
    script = {"rules": [{"match": "sleep 20", "step": tool_step(TOOL_MS * 3)}],
              "default": {"text": "AFTER"}}
    fake = Fake(tmp_path, script)
    replay_stdin(fake, recorded("3-interrupt", "stdin.jsonl"))
    fake.results(2)
    fake.close()
    assert shape(fake.events) == recorded_shape("3-interrupt")
    resp = next(e for e in fake.events if e["type"] == "control_response")
    assert resp["response"] == {"subtype": "success", "request_id": "req-int-1",
                                "response": {"still_queued": []}}
    aborted = [e for e in fake.events if e["type"] == "result"][0]
    assert aborted["subtype"] == "error_during_execution" and aborted["is_error"]
    assert aborted["terminal_reason"] == "aborted_tools" and "result" not in aborted
    rej = [e for e in fake.events if e["type"] == "user" and not e.get("isReplay")]
    assert rej[0]["message"]["content"][0]["content"].startswith(
        "The user doesn't want to proceed with this tool use")
    assert rej[1]["message"]["content"][0]["text"] == \
        "[Request interrupted by user for tool use]"
    assert fake.proc.returncode == 0


def test_q3_sigint_emits_same_events_then_exits_zero(tmp_path):
    script = {"rules": [{"match": "sleep 20", "step": tool_step(TOOL_MS * 3)}]}
    fake = Fake(tmp_path, script)
    replay_stdin(fake, recorded("3-interrupt/sigint", "stdin.jsonl"))
    fake.proc.wait(timeout=10)
    assert fake.proc.returncode == 0
    fake._reader.join(timeout=2)
    assert shape(fake.events) == recorded_shape("3-interrupt/sigint")


def test_interrupt_when_idle_answers_and_does_nothing_else(tmp_path):
    fake = Fake(tmp_path)
    fake.interrupt("idle-1")
    fake.wait_for(lambda ev: any(e["type"] == "control_response" for e in ev))
    time.sleep(0.1)
    assert shape(fake.events) == [("control_response", "success", [])]
    fake.close()


def test_hang_stops_the_reader_so_interrupt_is_unanswered(tmp_path):
    fake = Fake(tmp_path, {"default": {"hang": True}})
    fake.user("go")
    time.sleep(0.15)          # the fake has picked the turn up and hung
    fake.interrupt()
    time.sleep(0.3)
    assert not any(e["type"] == "control_response" for e in fake.events)
    fake.proc.kill()
    fake.proc.wait()


def test_text_only_turn_has_no_boundary_so_queued_line_waits(tmp_path):
    fake = Fake(tmp_path, {"rules": [{"match": "one", "step": {"delay_ms": 150, "text": "r1"}}]})
    fake.user("one")
    time.sleep(0.05)
    fake.user("two")
    fake.results(2)
    fake.close()
    s = shape(fake.events)
    assert s.count(("init",)) == 2 and s[-1][0] == "result"
    assert s.index(("replay", "two")) > s.index(("result", "success", False, "completed"))


# -- Q6: duplicates, sidechain, background task ---------------------------------

def test_q6_duplicate_ids_sidechain_and_background_second_turn(tmp_path):
    step = {"pre_text": "thinking out loud",
            "tools": [{"name": "Task", "input": {"prompt": "PONG"}, "ms": 0}],
            "sidechain": True, "background_task": {"ms": 30},
            "text": "launched", "after_text": "agent returned PONG"}
    fake = Fake(tmp_path, {"default": step})
    fake.user("go")           # one stdin line ...
    fake.results(2)           # ... two results
    fake.close()
    ev = fake.events
    main = [e for e in ev if e["type"] == "assistant" and e["parent_tool_use_id"] is None]
    first = [e for e in main if e["message"]["id"] == main[0]["message"]["id"]]
    assert [e["message"]["content"][0]["type"] for e in first] == ["text", "tool_use"]
    assert len({json.dumps(e["message"]["usage"], sort_keys=True) for e in first}) == 1
    assert all(e["message"]["stop_reason"] is None for e in first)
    tool_id = first[1]["message"]["content"][0]["id"]
    side = [e for e in ev if e["type"] == "assistant" and e["parent_tool_use_id"]]
    assert [e["parent_tool_use_id"] for e in side] == [tool_id]
    results = [e for e in ev if e["type"] == "result"]
    assert [r["result_index"] for r in results] == [0, 1]
    assert results[1]["result"] == "agent returned PONG"
    subtypes = [e.get("subtype") for e in ev if e["type"] == "system"]
    assert subtypes == ["init", "task_started", "task_notification", "init"]
    assert next(e for e in ev if e.get("subtype") == "task_started")["is_backgrounded"] is True
    assert sum(1 for e in fake.io() if e["dir"] == "in") == 1


def test_recorded_q6_duplicates_match_the_fake_shape():
    """The recorded capture really has the property the fake reproduces."""
    evs = [r["event"] for r in recorded("6-sidechain-and-duplicates/duplicate-ids-bash",
                                        "stdout.jsonl") if r["dir"] == "out"]
    by_id = {}
    for e in evs:
        if e["type"] == "assistant":
            by_id.setdefault(e["message"]["id"], []).append(e["message"]["usage"])
    assert any(len(u) > 1 and all(x == u[0] for x in u) for u in by_id.values())


# -- Q4 / Q5 -----------------------------------------------------------------------

def test_q4_init_lists_failed_mcp_servers_and_version(tmp_path):
    fake = Fake(tmp_path, env={"FAKE_CLAUDE_MCP_FAILED": "alpha,beta",
                               "FAKE_CLAUDE_INIT_DELAY_MS": "60"})
    t0 = time.time()
    fake.user("hi")
    fake.results(1)
    fake.close()
    assert time.time() - t0 >= 0.05
    init = fake.events[0]
    assert init["subtype"] == "init" and init["claude_code_version"] == "2.1.287"
    assert init["mcp_servers"] == [{"name": "alpha", "status": "failed"},
                                   {"name": "beta", "status": "failed"}]


def test_q5_resume_keeps_original_system_prompt(tmp_path):
    script = {"default": {"text": "prompt={{system_prompt}}"}}
    env = {"FAKE_CLAUDE_QUEUED": "1"}   # env gate, no --replay-user-messages

    def run(*args, sid):
        fake = Fake(tmp_path, script, args=args, sid=sid, env=env)
        fake.user("hi")
        fake.results(1)
        fake.close()
        return next(e for e in fake.events if e["type"] == "result")["result"]

    assert run("--system-prompt", "ORIGINAL", sid="s1") == "prompt=ORIGINAL"
    resumed = Fake(tmp_path, script, args=("--resume", "s1", "--system-prompt", "NEW",
                                           "--append-system-prompt", "EXTRA"),
                   sid="s1", env=env)
    resumed.user("hi")
    resumed.results(1)
    resumed.close()
    assert next(e for e in resumed.events if e["type"] == "result")["result"] \
        == "prompt=ORIGINAL"
    assert resumed.events[0]["session_id"] == "s1"
    assert run("--system-prompt", "NEW", sid="s2") == "prompt=NEW"


# -- io log ----------------------------------------------------------------------------

def test_io_log_records_stdin_and_emitted_events(tmp_path):
    fake = Fake(tmp_path)
    fake.user("hello")
    fake.results(1)
    fake.close()
    io = fake.io()
    assert io[0]["dir"] == "in" and io[0]["event"]["message"]["content"] == "hello"
    outs = [r["event"] for r in io if r["dir"] == "out"]
    assert shape(outs) == shape(fake.events)
    assert all(isinstance(r["t"], float) for r in io)
