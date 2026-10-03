"""A garbled stream-json line is logged and counted, not silently dropped."""

import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import pytest

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"


@pytest.fixture
def ags(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("ags_decode_under_test", AGENT_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["ags_decode_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    m.agent_config["amos"] = {}
    return m


class Stdout:
    def __init__(self, lines, raise_at_end=None):
        self.lines = list(lines)
        self.raise_at_end = raise_at_end

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        if self.raise_at_end:
            raise self.raise_at_end
        return b""


class Proc:
    def __init__(self, stdout):
        self.stdout = stdout


def _ev(**kw):
    return (json.dumps(kw) + "\n").encode()


RESULT = _ev(type="result", result="hi", session_id="s", usage={})


def _read(ags, stdout):
    async def post(*a, **k):
        return None
    ags.post_to_discord = post
    ags.agent_processes["amos"] = Proc(stdout)
    return asyncio.run(ags.read_agent_response("amos", "42", ["m1"]))


def test_garbled_line_between_valid_events(ags, caplog):
    secret = "sk-" + "a" * 30
    lines = [
        _ev(type="system", subtype="init", tools=[]),
        b'{"type": "assistant", "mess' + secret.encode() + b'\n',
        RESULT,
    ]
    with caplog.at_level(logging.WARNING):
        text, meta = _read(ags, Stdout(lines))
    assert text == "hi"
    assert meta["decode_errors"] == 1
    warns = [r for r in caplog.records if "decode error" in r.getMessage()]
    assert len(warns) == 1
    assert secret not in warns[0].getMessage()


def test_clean_stream_has_no_warning(ags, caplog):
    with caplog.at_level(logging.WARNING):
        text, meta = _read(ags, Stdout([RESULT]))
    assert text == "hi"
    assert "decode_errors" not in meta
    assert not [r for r in caplog.records if "decode error" in r.getMessage()]


def test_warning_is_truncated_to_200_chars(ags, caplog):
    with caplog.at_level(logging.WARNING):
        _read(ags, Stdout([b"x" * 5000 + b"\n", RESULT]))
    msg = [r.getMessage() for r in caplog.records if "decode error" in r.getMessage()][0]
    assert len(msg) < 400


def test_systemexit_is_logged_as_such(ags, caplog):
    with caplog.at_level(logging.WARNING):
        text, meta = _read(ags, Stdout([], raise_at_end=SystemExit(3)))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("SystemExit" in m for m in msgs)
    assert not any("Error reading response" in m for m in msgs)
