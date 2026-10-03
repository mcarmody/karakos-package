"""Long-turn threads (step 6.2): the tool-line branch of the turn loop with a
patched clock and a recording stub for Discord's REST calls. No network."""
import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
pytest.importorskip("aiohttp")

CHANNEL = "555000555"


@pytest.fixture
def ags(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("ags_ux_threads_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_ux_threads_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    module.agent_config = {"amos": {"model": "sonnet"}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    return module


class FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        return None


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class FakeProc:
    def __init__(self, stdout):
        self.stdin, self.stdout, self.pid = FakeStdin(), stdout, 4242


def stream(n):
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": f"step {i}"}}]}}).encode() + b"\n"
        for i in range(n)]
    lines.append(json.dumps({"type": "result", "session_id": "s", "result": "done",
                             "usage": {"input_tokens": 1, "output_tokens": 1}}).encode() + b"\n")
    return lines


class Resp:
    def __init__(self, status, payload=None):
        self.status, self._p = status, payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._p

    async def text(self):
        return ""


class FakeRest:
    def __init__(self, thread_status=201):
        self.thread_status, self.calls = thread_status, []

    def post(self, url, **kw):
        self.calls.append((url, kw.get("json")))
        return Resp(self.thread_status, {"id": "T-1"})


def drive(ags, monkeypatch, n, *, ux, channel_id=CHANNEL, rest=None, step=6.0, listed=True):
    ags.channels_config = {"channels": ({"general": {"id": CHANNEL, "ux": ux}} if listed else {})}
    posted = []

    async def fake_post(agent, cid, content, reply_to=None, dead_letter=False,
                        queue_message_id=None):
        posted.append((cid, content))
        return f"msg-{len(posted)}"

    ags.post_to_discord = fake_post
    ags.http_session = rest or FakeRest()
    t = {"n": -1}

    def clock():
        t["n"] += 1
        return t["n"] * step

    monkeypatch.setattr(sys.modules["turn_loop"], "time", SimpleNamespace(monotonic=clock))
    ags.agent_processes["amos"] = FakeProc(FakeStdout(stream(n)))
    asyncio.run(ags.read_agent_response("amos", channel_id, ["m1"]))
    return posted, ags.http_session


def test_below_threshold_everything_stays_in_channel(ags, monkeypatch):
    posted, rest = drive(ags, monkeypatch, 6, ux={"threads": {"after_s": 600}})
    assert [c for c, _ in posted] == [CHANNEL] * 6 and rest.calls == []


def test_after_threshold_one_thread_on_first_line_and_later_lines_go_there(ags, monkeypatch):
    posted, rest = drive(ags, monkeypatch, 14, ux={"threads": {"after_s": 60}})
    assert len(rest.calls) == 1
    url, body = rest.calls[0]
    assert url.endswith(f"/channels/{CHANNEL}/messages/msg-1/threads")
    assert body["auto_archive_duration"] == 60 and body["name"].startswith("Working on: ")
    chans = [c for c, _ in posted]
    assert chans == [CHANNEL] * 9 + ["T-1"] * 5
    assert ags.ux_threads.get("T-1") == CHANNEL


def test_line_13_posts_in_thread_mode_but_not_in_channel_mode(ags, monkeypatch):
    posted, _ = drive(ags, monkeypatch, 16, ux={"threads": {"after_s": 60}})
    assert len(posted) == 16
    ags2_posted, rest = drive(ags, monkeypatch, 16, ux={"threads": False})
    assert len(ags2_posted) == 12 and rest.calls == []


def test_max_lines_caps_thread_mode(ags, monkeypatch):
    posted, _ = drive(ags, monkeypatch, 30, ux={"threads": {"after_s": 60, "max_lines": 15}})
    assert len(posted) == 15


def test_403_on_creation_falls_back_to_channel_and_warns_once_per_hour(ags, monkeypatch, caplog):
    clock = {"t": 1000.0}
    ags._ux_now = lambda: clock["t"]
    ux = {"threads": {"after_s": 60}}
    with caplog.at_level(logging.WARNING):
        posted, rest = drive(ags, monkeypatch, 14, ux=ux, rest=FakeRest(403))
        assert len(rest.calls) == 1                       # no retry within the turn
        assert [c for c, _ in posted] == [CHANNEL] * 12   # channel cap again
        drive(ags, monkeypatch, 14, ux=ux, rest=FakeRest(403))
        warns = [r for r in caplog.records if "cannot create thread" in r.getMessage()]
        assert len(warns) == 1
        clock["t"] += 3601
        drive(ags, monkeypatch, 14, ux=ux, rest=FakeRest(400))
        warns = [r for r in caplog.records if "cannot create thread" in r.getMessage()]
        assert len(warns) == 2


def test_channel_not_listed_under_that_id_never_creates_a_thread(ags, monkeypatch):
    # A thread or DM id: not the id channels.json lists for any channel name.
    ags.ux_threads["777"] = CHANNEL
    posted, rest = drive(ags, monkeypatch, 14, ux={"threads": {"after_s": 60}},
                         channel_id="777")
    assert rest.calls == [] and {c for c, _ in posted} == {"777"} and len(posted) == 12


def test_switch_off_is_the_old_path(ags, monkeypatch):
    posted, rest = drive(ags, monkeypatch, 14, ux={})
    assert len(posted) == 12 and rest.calls == []


# -- suppress embeds ---------------------------------------------------------------

class MsgRest:
    """Records every channel-messages body; thread creation is not used here."""

    def __init__(self):
        self.bodies = []

    def post(self, url, **kw):
        if str(url).endswith("/messages"):
            self.bodies.append(kw.get("json"))
        return Resp(200, {"id": f"d-{len(self.bodies)}"})


def suppress_setup(ags, tmp_path, top=None, channel_ux=None):
    ags.POST_RETRY_BASE_SEC = 0.0
    ags.OUTBOX_PATH = tmp_path / "data" / "outbox" / "outbox.db"
    ags.channels_config = {"channels": {"general": {"id": CHANNEL, "ux": channel_ux or {}}},
                           **({"ux": top} if top is not None else {})}
    ags.http_session = MsgRest()
    return ags.http_session


@pytest.mark.parametrize("dead_letter", [False, True])
def test_suppress_embeds_flag_on_text_posts(ags, tmp_path, dead_letter):
    rest = suppress_setup(ags, tmp_path, channel_ux={"suppress_embeds": True})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, "see https://example.com/x",
                                    dead_letter=dead_letter))
    assert [b["flags"] for b in rest.bodies] == [4]


@pytest.mark.parametrize("dead_letter", [False, True])
def test_no_flag_when_off(ags, tmp_path, dead_letter):
    rest = suppress_setup(ags, tmp_path)
    asyncio.run(ags.post_to_discord("amos", CHANNEL, "see https://example.com/x",
                                    dead_letter=dead_letter))
    assert len(rest.bodies) == 1 and "flags" not in rest.bodies[0]


def test_install_wide_switch_and_channel_override(ags, tmp_path):
    rest = suppress_setup(ags, tmp_path, top={"suppress_embeds": True})
    asyncio.run(ags.post_to_discord("amos", CHANNEL, "a"))
    ags.channels_config["channels"]["general"]["ux"] = {"suppress_embeds": False}
    ags._ux_parsed["cfg"] = None
    asyncio.run(ags.post_to_discord("amos", CHANNEL, "b"))
    assert ["flags" in b for b in rest.bodies] == [True, False]


def test_payload_posts_never_carry_it(ags, tmp_path):
    rest = suppress_setup(ags, tmp_path, channel_ux={"suppress_embeds": True})
    asyncio.run(ags.post_discord_payload("amos", CHANNEL, {"content": "ask", "embeds": [{"title": "q"}]}))
    assert len(rest.bodies) == 1 and "flags" not in rest.bodies[0]


def test_registered_thread_inherits_parent_setting(ags, tmp_path):
    rest = suppress_setup(ags, tmp_path, channel_ux={"suppress_embeds": True})
    ags.ux_threads["T-9"] = CHANNEL
    asyncio.run(ags.post_to_discord("amos", "T-9", "in the thread"))
    asyncio.run(ags.post_to_discord("amos", "8888", "elsewhere"))
    assert [b.get("flags") for b in rest.bodies] == [4, None]
