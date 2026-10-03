"""Plan 'Build rules': harness tests must not touch HOME or real network.

Runs the smoke scenario with HOME pointed at a directory that is chmod 000
(so a stray create or read fails loudly, including in the fake-claude child)
and watched by an audit hook (so a swallowed error is still caught), and
asserts every socket bind is a 127.0.0.1 ephemeral port.
"""

import asyncio
import os
import socket
import sys

from harness import Harness

_watched = {"prefix": None, "hits": []}
_FS_EVENTS = {"open", "os.mkdir", "os.listdir", "os.scandir", "os.rename",
              "os.remove", "os.rmdir", "shutil.copyfile"}


def _audit(event, args):
    prefix = _watched["prefix"]
    if prefix is None or event not in _FS_EVENTS:
        return
    for a in args:
        if isinstance(a, (str, bytes, os.PathLike)):
            try:
                path = os.fsdecode(a)
            except Exception:
                continue
            if path.startswith(prefix):
                _watched["hits"].append((event, path))


sys.addaudithook(_audit)


def test_smoke_scenario_never_touches_home_or_foreign_sockets(
        tmp_workspace, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    fake_home.chmod(0o000)
    monkeypatch.setenv("HOME", str(fake_home))

    binds = []
    real_bind = socket.socket.bind

    def recording_bind(self, address, *a, **kw):
        binds.append(address)
        return real_bind(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "bind", recording_bind)

    h = Harness(tmp_workspace, agents=["a", "b"])

    async def scenario():
        async with h:
            h.script(default={"text": "hello from {{env:KARAKOS_AGENT}}"})
            await h.send("a", "ping")
            await h.send("b", "ping")
            await asyncio.gather(h.wait_idle("a"), h.wait_idle("b"))

    _watched["prefix"], _watched["hits"] = str(fake_home), []
    try:
        asyncio.run(scenario())
    finally:
        _watched["prefix"] = None
        fake_home.chmod(0o700)

    assert _watched["hits"] == []
    assert list(fake_home.iterdir()) == []
    assert h.queue_rows("a")[0]["response"] == "hello from a"
    assert binds, "expected the TestServer to bind a loopback port"
    for host, port, *_ in binds:
        assert host == "127.0.0.1", binds
        assert port == 0, f"fixed port bound: {binds}"
