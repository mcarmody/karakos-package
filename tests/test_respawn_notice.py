"""
Tests for #90: subprocess respawns were silent, so the user saw amnesia with
no explanation.

Before the fix nothing at all watched for an idle subprocess dying.
`stderr_reader` was the only task that observed the exit — it hit EOF, broke
out of its loop, and discarded the fact. The agent stayed dead until the next
message failed to send, and the reply after that had no memory of the
conversation with nobody having said why.

These exercise `respawn_watcher` / `notify_respawn` directly against
start/kill with a fake subprocess, without booting the aiohttp server — same
approach as test_stderr_reader_lifecycle.py.

The load-bearing case is the *negative* one: every deliberate restart also
makes proc.wait() return, so a watcher that only checked "did the process
exit" would announce a crash on every /restart, /reload and /interrupt.
"""

import asyncio
import ast
from pathlib import Path

import pytest

from conftest import import_script

AGENT_SERVER = Path(__file__).parent.parent / "bin" / "agent-server.py"


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    workspace = tmp_path
    (workspace / "agents" / "testagent" / "persona").mkdir(parents=True)
    (workspace / "config").mkdir()
    (workspace / "system_prompt.md").write_text("You are a test agent.")

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    mod = import_script("agent-server")
    mod.WORKSPACE_ROOT = workspace
    mod.agent_config["testagent"] = {
        "system_prompt": "system_prompt.md",
        "model": "sonnet",
        "max_turns": 5,
    }

    async def fake_get_or_create_session(agent):
        return "session-1234"

    async def fake_load_last_session(agent):
        return {"status": "not_found"}

    monkeypatch.setattr(mod, "get_or_create_session", fake_get_or_create_session)
    monkeypatch.setattr(mod, "load_last_session", fake_load_last_session)

    # Fresh per-test state — these are module globals and the module is cached.
    mod.deliberate_kills.clear()
    mod.agent_last_channel.clear()
    mod.agent_processes.clear()
    mod.respawn_history.clear()
    mod.shutting_down = False

    yield mod

    for registry in (mod.stderr_reader_tasks, mod.respawn_watcher_tasks):
        for task in registry.values():
            if not task.done():
                task.cancel()
        registry.clear()


class FakeStderr:
    async def readline(self):
        await asyncio.sleep(3600)
        return b""  # pragma: no cover - never reached in tests


class FakeProcess:
    """proc.wait() blocks until exit() is called, like a real subprocess that
    is still running. Tests drive the death explicitly."""

    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.stderr = FakeStderr()
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()
        self._returncode = returncode

    def exit(self, returncode=1):
        """Simulate the process dying on its own."""
        self._returncode = returncode
        self._exited.set()

    def terminate(self):
        self.terminated = True
        self.exit(-15)

    def kill(self):  # pragma: no cover - terminate always succeeds here
        self.killed = True
        self.exit(-9)

    async def wait(self):
        await self._exited.wait()
        return self._returncode


@pytest.fixture
def posted(agent_server, monkeypatch):
    """Capture everything the module tries to post to Discord."""
    calls = []

    async def fake_post_to_discord(agent, channel_id, content, **kwargs):
        calls.append((agent, channel_id, content))
        return "discord-msg-1"

    monkeypatch.setattr(agent_server, "post_to_discord", fake_post_to_discord)
    return calls


def _patch_subprocess(mod, monkeypatch, processes):
    it = iter(processes)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return next(it)

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


async def _settle():
    """Give the watcher task room to observe the exit and finish respawning."""
    for _ in range(10):
        await asyncio.sleep(0)


def test_start_tracks_respawn_watcher(agent_server, monkeypatch):
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1)])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")

        task = mod.respawn_watcher_tasks.get("testagent")
        assert task is not None
        assert not task.done()

    asyncio.run(scenario())


def test_unexpected_exit_respawns_and_notifies(agent_server, monkeypatch, posted):
    """The acceptance test from #90: kill the subprocess while the agent is
    idle, the channel gets told the agent restarted."""
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        first.exit(returncode=1)  # died on its own, nobody asked
        await _settle()

        assert mod.agent_processes["testagent"] is second, "should have respawned"
        assert len(posted) == 1
        agent, channel_id, content = posted[0]
        assert agent == "testagent"
        assert channel_id == "chan-42"
        assert "restarted" in content

    asyncio.run(scenario())


def test_deliberate_kill_does_not_respawn_or_notify(agent_server, monkeypatch, posted):
    """kill_agent_subprocess is the /kill endpoint's path — it is meant to
    leave the agent down and silent."""
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1), FakeProcess(pid=2)])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        await mod.kill_agent_subprocess("testagent")
        await _settle()

        assert "testagent" not in mod.agent_processes
        assert posted == []

    asyncio.run(scenario())


def test_kill_racing_a_self_death_leaves_the_agent_down(agent_server, monkeypatch, posted):
    """A process that dies on its own at the moment an operator issues a kill
    must still end up down and silent — the operator's intent wins.

    Note on coverage: this passes with `deliberate_kills.add()` mutated out,
    because the watcher-cancel in kill_agent_subprocess already covers this
    interleaving. It is kept as a contract test on the observable outcome, not
    as proof the flag is load-bearing. The window the flag actually guards —
    a watcher already past its checks and inside start_agent_subprocess — is
    not deterministically reachable from here.
    """
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"
        await _settle()  # let the watcher actually reach `await proc.wait()`

        first.exit(returncode=1)  # died on its own...
        await mod.kill_agent_subprocess("testagent")  # ...as the operator killed it
        await _settle()

        assert "testagent" not in mod.agent_processes, "must stay down"
        assert posted == []

    asyncio.run(scenario())


def test_restart_does_not_announce_a_crash(agent_server, monkeypatch, posted):
    """The regression this fix could most easily introduce: a deliberate
    restart also makes proc.wait() return."""
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1), FakeProcess(pid=2)])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        async def noop(*args, **kwargs):
            return None

        monkeypatch.setattr(mod, "clear_session", noop)
        await mod.restart_agent("testagent")
        await _settle()

        assert posted == [], "a deliberate restart is not a crash"
        # And the flag did not leak: the fresh process cleared it, so a real
        # crash after this restart is still reported.
        assert "testagent" not in mod.deliberate_kills

    asyncio.run(scenario())


def test_respawn_waits_for_an_in_flight_turn(agent_server, monkeypatch, posted):
    """A mid-turn crash must not respawn underneath the turn that is still
    unwinding — the watcher takes the agent lock."""
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def scenario():
        lock = asyncio.Lock()
        mod.agent_locks["testagent"] = lock
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        async with lock:  # stand in for a turn still in process_agent_queue
            first.exit(returncode=1)
            await _settle()
            assert mod.agent_processes["testagent"] is first, "blocked on the lock"
            assert posted == []

        await _settle()
        assert mod.agent_processes["testagent"] is second
        assert len(posted) == 1

    asyncio.run(scenario())


def test_no_known_channel_is_not_an_error(agent_server, monkeypatch, posted):
    """An agent that has never spoken still gets respawned; there is simply
    nowhere to announce it."""
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")

        first.exit(returncode=1)
        await _settle()

        assert mod.agent_processes["testagent"] is second
        assert posted == []

    asyncio.run(scenario())


def test_silent_mode_channel_is_not_notified(agent_server, monkeypatch, posted):
    """channel_id "0" is the package's silent-mode sentinel; post_to_discord
    already skips it, but the notice should not reach that far."""
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "0"

        first.exit(returncode=1)
        await _settle()

        assert mod.agent_processes["testagent"] is second
        assert posted == []

    asyncio.run(scenario())


def test_shutdown_does_not_respawn(agent_server, monkeypatch, posted):
    """During graceful shutdown every subprocess exits. None of them should
    come back, and none should page the channel on the way out."""
    mod = agent_server
    _patch_subprocess(mod, monkeypatch, [FakeProcess(pid=1), FakeProcess(pid=2)])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"
        first = mod.agent_processes["testagent"]

        mod.shutting_down = True
        first.exit(returncode=0)
        await _settle()

        assert mod.agent_processes["testagent"] is first, "no respawn during shutdown"
        assert posted == []

    asyncio.run(scenario())


def test_notice_failure_does_not_break_the_respawn(agent_server, monkeypatch):
    """A notice is a courtesy; the respawn is the recovery. Losing Discord
    must not cost the agent."""
    mod = agent_server
    first, second = FakeProcess(pid=1), FakeProcess(pid=2)
    _patch_subprocess(mod, monkeypatch, [first, second])

    async def exploding_post(*args, **kwargs):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(mod, "post_to_discord", exploding_post)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        first.exit(returncode=1)
        await _settle()

        assert mod.agent_processes["testagent"] is second
        watcher = mod.respawn_watcher_tasks["testagent"]
        assert not watcher.done() or watcher.exception() is None

    asyncio.run(scenario())


def test_process_agent_queue_records_the_last_channel():
    """Everything above can pass with the notice having nowhere to go.

    `notify_respawn` reads `agent_last_channel`, and the only thing that ever
    writes it is the turn loop. If that write is dropped the respawn still
    happens, the tests above still pass (they set the channel by hand), and
    every real user gets a silent restart — the original bug, intact, under a
    green suite. Checked through the AST so a mention in a comment cannot
    satisfy it. Same technique as
    test_attachments.py::test_the_batch_formatter_actually_calls_format_attachments.
    """
    tree = ast.parse(AGENT_SERVER.read_text())
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "process_agent_queue"
    )
    assigns_last_channel = any(
        isinstance(t, ast.Subscript)
        and getattr(t.value, "id", None) == "agent_last_channel"
        for n in ast.walk(target) if isinstance(n, ast.Assign)
        for t in n.targets
    )
    assert assigns_last_channel, "process_agent_queue must record agent_last_channel"


def test_notify_respawn_reads_the_last_channel():
    """The other half of the pair: the writer above is pointless if the notice
    stops consulting it and hardcodes a channel."""
    tree = ast.parse(AGENT_SERVER.read_text())
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "notify_respawn"
    )
    reads_last_channel = any(
        getattr(n.value, "id", None) == "agent_last_channel"
        for n in ast.walk(target) if isinstance(n, ast.Attribute)
    ) or any(
        getattr(getattr(n, "func", None), "value", None) is not None
        and getattr(n.func.value, "id", None) == "agent_last_channel"
        for n in ast.walk(target) if isinstance(n, ast.Call)
    )
    assert reads_last_channel, "notify_respawn must address the last known channel"


def test_crashloop_stops_respawning_and_says_it_is_down(agent_server, monkeypatch, posted):
    """A subprocess that dies immediately on spawn — bad model name, missing
    MCP binary, unreadable settings — must not respawn forever.

    Without the brake the recovery path becomes the bug: one broken config
    produces an unbounded stream of restart notices, which is worse than the
    silence #90 set out to fix.
    """
    mod = agent_server
    limit = mod.RESPAWN_MAX_IN_WINDOW
    # One more process than the brake will allow, so the run hits the limit.
    procs = [FakeProcess(pid=n) for n in range(1, limit + 3)]
    _patch_subprocess(mod, monkeypatch, procs)

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        # Crash every process the moment it comes up.
        for _ in range(limit + 1):
            current = mod.agent_processes["testagent"]
            current.exit(returncode=1)
            await _settle()

        assert len(mod.respawn_history["testagent"]) == limit + 1

        restarts = [c for c in posted if "restarted" in c[2]]
        downs = [c for c in posted if "is down" in c[2]]
        assert len(restarts) == limit, "respawns should stop at the limit"
        assert len(downs) == 1, "and say so, exactly once"
        assert "left down" in downs[0][2]

    asyncio.run(scenario())


def test_crashes_outside_the_window_do_not_accumulate(agent_server, monkeypatch, posted):
    """The brake counts crashes in a window, not for all time. An agent that
    dies once a week is recovering normally, not crashlooping."""
    mod = agent_server
    procs = [FakeProcess(pid=n) for n in range(1, 6)]
    _patch_subprocess(mod, monkeypatch, procs)

    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])

    async def scenario():
        mod.agent_locks["testagent"] = asyncio.Lock()
        await mod.start_agent_subprocess("testagent")
        mod.agent_last_channel["testagent"] = "chan-42"

        for _ in range(4):
            clock[0] += mod.RESPAWN_WINDOW_SECONDS + 1  # each crash well clear of the last
            mod.agent_processes["testagent"].exit(returncode=1)
            await _settle()

        assert len(mod.respawn_history["testagent"]) == 1, "window should have been pruned"
        assert all("restarted" in c[2] for c in posted)
        assert len(posted) == 4, "every one is a normal recovery"

    asyncio.run(scenario())
