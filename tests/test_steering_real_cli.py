"""Real `claude` + haiku against the two scenarios steering rests on (step 2.5):
Q1 (a line written during a `sleep 5` tool turn is merged into that turn: one
result, both lines replayed) and Q3 (a control_request interrupt aborts the turn,
the same process answers the next line). Slow; run by hand before 2.5 merges.
Skips without a CLI and credential. Expected cost under $0.15.

The test speaks the stream-json protocol directly, exactly as lib/turn_loop.py
does (`--replay-user-messages`, user lines, control_request); the harness's fake
is held to the same shapes by tests/test_fake_claude_queued.py.
"""

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.realcli]

HAVE_CRED = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or (Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                     / ".credentials.json").exists())


def _need_cli():
    if shutil.which("claude") is None or not HAVE_CRED:
        if os.environ.get("CI") or os.environ.get("RELEASE_GATE"):
            pytest.fail("real claude CLI/credential required in the release gate")
        pytest.skip("no claude CLI or credential")


class Cli:
    def __init__(self, tmp_path):
        self.proc = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--replay-user-messages", "--model", "haiku",
             "--permission-mode", "bypassPermissions", "--max-budget-usd", "0.10",
             "--setting-sources", ""],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(tmp_path), text=True)
        self.events = []

    def write(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def user(self, text):
        self.write({"type": "user", "message": {"role": "user", "content": text}})

    def read_until(self, pred, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events:
                if pred(e):
                    return e
            r, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if r:
                line = self.proc.stdout.readline()
                if not line:
                    break
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass
        raise TimeoutError("event not seen")

    def drain(self, secs):
        """Keep reading for `secs` (a stray extra event would arrive meanwhile)."""
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if r:
                line = self.proc.stdout.readline()
                if not line:
                    return
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass

    def results(self):
        return [e for e in self.events if e.get("type") == "result"]

    def replays(self):
        return [e for e in self.events if e.get("type") == "user" and e.get("isReplay")]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def test_q1_line_written_mid_tool_is_merged_into_the_turn(tmp_path):
    _need_cli()
    cli = Cli(tmp_path)
    try:
        cli.user("Use the Bash tool to run exactly: sleep 5. Then reply DONE1.")
        cli.read_until(lambda e: e.get("type") == "assistant"
                       and any(b.get("type") == "tool_use"
                               for b in e["message"].get("content", [])))
        cli.user("Also include the word SECOND2 in your final reply.")
        cli.read_until(lambda e: e.get("type") == "result")
        cli.drain(3)    # a stray second result would arrive by now
        assert len(cli.results()) == 1
        texts = [e["message"]["content"] for e in cli.replays()]
        assert len(texts) == 2 and "SECOND2" in texts[1]
        assert "SECOND2" in (cli.results()[0].get("result") or "")
    finally:
        cli.close()


def test_q3_interrupt_keeps_the_process_and_next_line_runs(tmp_path):
    _need_cli()
    cli = Cli(tmp_path)
    pid = cli.proc.pid
    try:
        cli.user("Use the Bash tool to run exactly: sleep 20. Then reply DONE.")
        cli.read_until(lambda e: e.get("type") == "assistant"
                       and any(b.get("type") == "tool_use"
                               for b in e["message"].get("content", [])))
        cli.write({"type": "control_request", "request_id": "req-int-1",
                   "request": {"subtype": "interrupt"}})
        resp = cli.read_until(lambda e: e.get("type") == "control_response")
        assert resp["response"]["subtype"] == "success"
        res = cli.read_until(lambda e: e.get("type") == "result")
        assert res["subtype"] == "error_during_execution"
        assert res.get("terminal_reason") == "aborted_tools"
        cli.user("Reply with only the word AFTER.")
        cli.read_until(lambda e: e.get("type") == "result" and e is not res)
        assert "AFTER" in (cli.results()[-1].get("result") or "")
        assert cli.proc.pid == pid and cli.proc.poll() is None
    finally:
        cli.close()
