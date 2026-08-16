"""
Tests for the deferred inbound-message spool (#88).

The case this exists for: the agent server is down or rate-limited at the
moment a message arrives. Before this, `relay.py` logged the error and moved
on, and `poke.sh` exited 1 — either way the message was gone, and the sender
had no idea. Confirmed live on 2026-08-05: an install lost a Discord message
to a 429 cost cap, no retry, no notice.

Now both senders spool the exact /message payload to
data/deferred-messages/, and bin/flush-deferred-messages.py (scheduler,
every 5 minutes) re-fires it once the server answers again.

The flusher and poke.sh are driven for real against a fake HTTP server,
because the thing under test is behaviour across status codes, and source
text cannot tell you that. The relay call sites and scheduler wiring are
read out of the AST — booting either module is heavy, and substring greps
match comments (see test_agent_server_routes.py for the incident report).

Acceptance test from #88, pinned in test_flush_delivers_spooled_payload:
stop the agent server, send a message, start it again within five minutes —
the message arrives without the user resending anything.
"""

import ast
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT, import_script

FLUSHER = PACKAGE_ROOT / "bin" / "flush-deferred-messages.py"
RELAY = PACKAGE_ROOT / "bin" / "relay.py"
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
SCHEDULER = PACKAGE_ROOT / "bin" / "scheduler.py"
POKE = PACKAGE_ROOT / "bin" / "poke.sh"


# ---------------------------------------------------------------------------
# Fake agent server
# ---------------------------------------------------------------------------

class FakeAgentServer:
    """One-endpoint stand-in for agent-server's POST /message."""

    def __init__(self, status=202):
        self.status = status
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                })
                body = json.dumps({"status": "queued"}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port():
    """A port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _payload(message_id="msg-1", **overrides):
    payload = {
        "agent": "testagent",
        "channel": "general",
        "channel_id": "123",
        "server": "discord",
        "author": "someone",
        "author_id": "42",
        "is_bot": False,
        "content": "hello from the outage",
        "message_id": message_id,
        "mentions_agent": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def spool(tmp_path):
    """A workspace with one spooled payload; returns (workspace, spool_dir)."""
    spool_dir = tmp_path / "data" / "deferred-messages"
    spool_dir.mkdir(parents=True)
    return tmp_path, spool_dir


def _run_flush(workspace, port, token="test-token", **extra_env):
    prev = {}
    env = {
        "WORKSPACE_ROOT": str(workspace),
        "AGENT_SERVER_URL": f"http://127.0.0.1:{port}",
        "AGENT_SERVER_TOKEN": token,
        **extra_env,
    }
    for key, value in env.items():
        prev[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        return import_script("flush-deferred-messages").flush()
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Flusher behaviour
# ---------------------------------------------------------------------------

def test_flush_delivers_spooled_payload(spool):
    """#88's acceptance test, server-back leg: a payload spooled during the
    outage is POSTed verbatim, with the bearer token, and the file removed."""
    workspace, spool_dir = spool
    payload = _payload()
    (spool_dir / "100-msg-1.json").write_text(json.dumps(payload))

    server = FakeAgentServer(status=202)
    try:
        summary = _run_flush(workspace, server.port)
    finally:
        server.stop()

    assert summary["fired"] == 1
    assert list(spool_dir.glob("*.json")) == []
    assert server.requests[0]["path"] == "/message"
    assert server.requests[0]["auth"] == "Bearer test-token"
    assert server.requests[0]["body"] == payload


@pytest.mark.parametrize("status", [429, 500, 503])
def test_flush_keeps_payload_on_transient_failure(spool, status):
    workspace, spool_dir = spool
    (spool_dir / "100-msg-1.json").write_text(json.dumps(_payload()))

    server = FakeAgentServer(status=status)
    try:
        summary = _run_flush(workspace, server.port)
    finally:
        server.stop()

    assert summary["kept"] == 1
    assert (spool_dir / "100-msg-1.json").exists()


def test_flush_keeps_payload_when_server_still_down(spool):
    workspace, spool_dir = spool
    (spool_dir / "100-msg-1.json").write_text(json.dumps(_payload()))

    summary = _run_flush(workspace, _closed_port())

    assert summary["kept"] == 1
    assert (spool_dir / "100-msg-1.json").exists()


def test_flush_moves_permanently_rejected_payload_aside(spool):
    """A 400 answers identically on every refire; retrying it for 24 hours
    buys nothing. It goes to invalid/ where a human can look at it."""
    workspace, spool_dir = spool
    (spool_dir / "100-msg-1.json").write_text(json.dumps(_payload()))

    server = FakeAgentServer(status=400)
    try:
        summary = _run_flush(workspace, server.port)
    finally:
        server.stop()

    assert summary["invalid"] == 1
    assert not (spool_dir / "100-msg-1.json").exists()
    assert (spool_dir / "invalid" / "100-msg-1.json").exists()


def test_flush_moves_unparseable_file_aside_without_posting(spool):
    workspace, spool_dir = spool
    (spool_dir / "100-msg-1.json").write_text("not json{")

    server = FakeAgentServer(status=202)
    try:
        summary = _run_flush(workspace, server.port)
    finally:
        server.stop()

    assert summary["invalid"] == 1
    assert server.requests == []
    assert (spool_dir / "invalid" / "100-msg-1.json").exists()


def test_flush_ages_out_stale_payload_without_firing_it(spool):
    """A day-old message resurfacing unannounced is worse than staying lost;
    past MAX_AGE it is retired to stale/, never POSTed."""
    workspace, spool_dir = spool
    old = spool_dir / "100-msg-1.json"
    old.write_text(json.dumps(_payload()))
    day_plus = time.time() - 25 * 3600
    os.utime(old, (day_plus, day_plus))

    server = FakeAgentServer(status=202)
    try:
        summary = _run_flush(workspace, server.port)
    finally:
        server.stop()

    assert summary["stale"] == 1
    assert server.requests == []
    assert (spool_dir / "stale" / "100-msg-1.json").exists()


def test_flush_prunes_old_bucket_files(spool):
    workspace, spool_dir = spool
    for bucket in ("stale", "invalid"):
        (spool_dir / bucket).mkdir()
        aged = spool_dir / bucket / "ancient.json"
        aged.write_text("{}")
        week_plus = time.time() - 8 * 86400
        os.utime(aged, (week_plus, week_plus))

    summary = _run_flush(workspace, _closed_port())

    assert summary["pruned"] == 2
    assert not (spool_dir / "stale" / "ancient.json").exists()
    assert not (spool_dir / "invalid" / "ancient.json").exists()


def test_flush_handles_missing_spool_dir(tmp_path):
    summary = _run_flush(tmp_path, _closed_port())
    assert summary == {"fired": 0, "kept": 0, "stale": 0, "invalid": 0, "pruned": 0}


# ---------------------------------------------------------------------------
# poke.sh
# ---------------------------------------------------------------------------

_POKE_DEPS = all(shutil.which(dep) for dep in ("bash", "curl", "jq"))


def _run_poke(workspace, port, *args):
    return subprocess.run(
        ["bash", str(POKE), "--agent", "testagent", "--silent", *args],
        env={
            **os.environ,
            "WORKSPACE_ROOT": str(workspace),
            "AGENT_SERVER_PORT": str(port),
            "AGENT_SERVER_TOKEN": "test-token",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(not _POKE_DEPS, reason="poke.sh needs bash, curl and jq")
def test_poke_spools_and_exits_zero_when_server_is_down(tmp_path):
    """#88's outage leg: a poke into a dead server is spooled, not lost, and
    the caller sees success — the message is durably queued."""
    result = _run_poke(tmp_path, _closed_port(), "hello from the outage")

    assert result.returncode == 0, result.stderr
    spooled = list((tmp_path / "data" / "deferred-messages").glob("*.json"))
    assert len(spooled) == 1
    payload = json.loads(spooled[0].read_text())
    assert payload["agent"] == "testagent"
    assert payload["content"] == "hello from the outage"
    assert payload["message_id"] in spooled[0].name


@pytest.mark.skipif(not _POKE_DEPS, reason="poke.sh needs bash, curl and jq")
def test_poke_spools_on_rate_limit(tmp_path):
    server = FakeAgentServer(status=429)
    try:
        result = _run_poke(tmp_path, server.port, "capped")
    finally:
        server.stop()

    assert result.returncode == 0, result.stderr
    assert len(list((tmp_path / "data" / "deferred-messages").glob("*.json"))) == 1


@pytest.mark.skipif(not _POKE_DEPS, reason="poke.sh needs bash, curl and jq")
def test_poke_still_fails_loudly_on_permanent_rejection(tmp_path):
    """A 400 is a caller bug; spooling it would retry a payload the server
    will never take. That path must stay a visible failure."""
    server = FakeAgentServer(status=400)
    try:
        result = _run_poke(tmp_path, server.port, "malformed by assumption")
    finally:
        server.stop()

    assert result.returncode == 1
    assert not (tmp_path / "data" / "deferred-messages").exists()


@pytest.mark.skipif(not _POKE_DEPS, reason="poke.sh needs bash, curl and jq")
def test_poke_delivery_unchanged_when_server_accepts(tmp_path):
    server = FakeAgentServer(status=202)
    try:
        result = _run_poke(tmp_path, server.port, "normal day")
    finally:
        server.stop()

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    assert not (tmp_path / "data" / "deferred-messages").exists()


# ---------------------------------------------------------------------------
# Call-site and wiring checks (AST, not substring — comments are source text)
# ---------------------------------------------------------------------------

def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls_to(node, func_name):
    return [
        call for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == func_name
    ]


def test_relay_spools_on_error_status_and_on_exception():
    tree = ast.parse(RELAY.read_text())
    send = _function(tree, "send_to_agent_server")

    spool_calls = _calls_to(send, "spool_deferred_message")
    assert len(spool_calls) >= 2, (
        "send_to_agent_server must spool both when the server answers a "
        "transient error and when the POST raises"
    )
    in_handler = {
        id(call)
        for handler in ast.walk(send)
        if isinstance(handler, ast.ExceptHandler)
        for call in _calls_to(handler, "spool_deferred_message")
    }
    assert in_handler, "no spool call inside the except handler — a downed server raises, it does not answer"
    assert any(id(c) not in in_handler for c in spool_calls), (
        "no spool call outside the except handler — a 429/5xx answer is not an exception"
    )


def test_agent_server_accepts_duplicate_message_id_as_202():
    """A refired payload whose first POST landed must not error: the flusher
    deletes on 202 and retries anything else until stale-out."""
    tree = ast.parse(AGENT_SERVER.read_text())
    handle = _function(tree, "handle_message")

    for handler in ast.walk(handle):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        names = [
            node.attr for node in ast.walk(handler.type) if isinstance(node, ast.Attribute)
        ]
        if "IntegrityError" not in names:
            continue
        statuses = [
            kw.value.value
            for ret in ast.walk(handler)
            if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Call)
            for kw in ret.value.keywords
            if kw.arg == "status" and isinstance(kw.value, ast.Constant)
        ]
        assert statuses == [202], f"duplicate insert answers {statuses}, expected [202]"
        return
    raise AssertionError("handle_message has no IntegrityError handler for duplicate message_ids")


def test_scheduler_runs_flusher_every_five_minutes():
    tree = ast.parse(SCHEDULER.read_text())

    runner = _function(tree, "run_flush_deferred_messages")
    constants = [
        node.value for node in ast.walk(runner)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert any("flush-deferred-messages.py" in value for value in constants), (
        "run_flush_deferred_messages does not invoke the flusher script"
    )

    for node in ast.walk(_function(tree, "main")):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "do"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "run_flush_deferred_messages"
        ):
            continue
        # Walk down schedule.every(5).minutes.do(...): .do's owner is the
        # .minutes attribute, whose owner is the every(5) call.
        minutes = node.func.value
        assert isinstance(minutes, ast.Attribute) and minutes.attr == "minutes"
        every = minutes.value
        assert (
            isinstance(every, ast.Call)
            and every.args
            and isinstance(every.args[0], ast.Constant)
            and every.args[0].value == 5
        ), "flusher is scheduled, but not at the 5-minute cadence the spool promises"
        return
    raise AssertionError("main() never schedules run_flush_deferred_messages")
