"""Two-agent integration harness: the real agent-server, a fake `claude`.

    h = Harness(tmp_workspace, agents=["a", "b"])
    async with h:
        await h.send("a", "hello")
        await h.wait_idle("a")
        assert h.sent_to("a") == [...]

Tests drive it with plain `asyncio.run` (CI does not install pytest-asyncio).
Everything is keyed by *shard id*; until the registry grows shards, the shard
id is the agent id.
"""

import asyncio
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
FAKE_BIN_DIR = HARNESS_DIR / "bin"
PACKAGE_ROOT = HARNESS_DIR.parent.parent
DEFAULT_TOKEN = "harness-token"
sys.path.insert(0, str(PACKAGE_ROOT))  # lib.migrate

_ENV_KEYS = ("PATH", "WORKSPACE_ROOT", "AGENT_SERVER_TOKEN", "FAKE_CLAUDE_LOG_DIR",
             "FAKE_CLAUDE_SCRIPT", "DISCORD_BOT_TOKEN", "OWNER_DISCORD_ID",
             "AGENT_SERVER_URL")


FAKE_ENV_KEYS = ("FAKE_CLAUDE_LOG_DIR", "FAKE_CLAUDE_SCRIPT", "FAKE_CLAUDE_QUEUED",
                 "FAKE_CLAUDE_MCP_FAILED", "FAKE_CLAUDE_INIT_DELAY_MS")


def write_agents_config(workspace: Path, agents, shards=None) -> None:
    """The one place the harness writes agent config. Emits config/agents.yaml
    (validated through lib/registry.py) and, because the server's readers move
    in 1.1b, the legacy agents.json derived from the registry's legacy_view().
    `agents` is a list of ids or {id: registry-schema overrides}; the first is
    the primary, the rest `custom`. A registry needs a monitor, so one is added
    to the yaml when none is given. `shards` maps an agent id to its shard ids
    (``{"a": ["a", "a-2"]}``); omitted agents get the default shard."""
    import yaml
    sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
    try:
        import registry
    finally:
        sys.path.pop(0)
    if not isinstance(agents, dict):
        agents = {name: {} for name in agents}
    entries = {}
    for i, (name, extra) in enumerate(agents.items()):
        prompt_dir = workspace / "agents" / name
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "SYSTEM_PROMPT.md").write_text(f"You are harness agent {name}.")
        entries[name] = {"name": name, "role": "primary" if i == 0 else "custom",
                         "system_prompt": f"agents/{name}/SYSTEM_PROMPT.md",
                         "model": "fake-model", **(extra or {})}
        # The server spawns claude with an allowlisted env, so the fake's own
        # knobs reach it the way a real secret would: a `${NAME}` reference in
        # the agent's `env:`, resolved from the server env at spawn.
        entries[name]["env"] = {**{k: "${%s}" % k for k in FAKE_ENV_KEYS},
                                **(entries[name].get("env") or {})}
        if shards and name in shards:
            entries[name]["shards"] = [{"id": sid, "channels": []} for sid in shards[name]]
    if not any(e["role"] == "monitor" for e in entries.values()):
        entries["monitor"] = {"name": "monitor", "role": "monitor", "model": "fake-model"}
    config = workspace / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "agents.yaml").write_text(
        yaml.safe_dump({"version": registry.REGISTRY_VERSION, "agents": entries},
                       sort_keys=False))
    reg = registry.load_registry(workspace)  # fails loudly on a bad harness config
    assert reg.primary()  # (the server reads agents.yaml directly)
    (config / "claude-settings.json").write_text(
        json.dumps({"permissions": {"allow": [], "deny": []}}))


class Harness:
    def __init__(self, tmp_workspace, agents=["a", "b"], shards=None):
        from conftest import import_script  # tests/ is on sys.path under pytest
        self.workspace = Path(tmp_workspace)
        self.agents = list(agents)
        self.log_dir = self.workspace / "fake-claude-logs"
        self.script_path = self.workspace / "fake-claude-script.json"
        self.discord = []
        self._saved_env = {}
        self._import_script = import_script
        self.module = None
        self.client = None
        self.shards = dict(shards or {})
        write_agents_config(self.workspace, agents, self.shards)
        # A 2.0 workspace is stamped; the server refuses to boot otherwise.
        from lib.migrate.guard import write_stamp
        write_stamp(self.workspace / "data")
        self.script()

    # -- lifecycle ---------------------------------------------------------

    async def start(self):
        from aiohttp.test_utils import TestClient, TestServer
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["PATH"] = f"{FAKE_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["WORKSPACE_ROOT"] = str(self.workspace)
        os.environ["AGENT_SERVER_TOKEN"] = DEFAULT_TOKEN
        os.environ["FAKE_CLAUDE_LOG_DIR"] = str(self.log_dir)
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script_path)
        # Nothing may reach Discord: no bot tokens, and the poster is stubbed.
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        os.environ.pop("OWNER_DISCORD_ID", None)
        self.module = self._import_script("agent-server")
        self.module.post_to_discord = self._record_discord
        self.client = TestClient(TestServer(self.module.create_app(), host="127.0.0.1"))
        await self.client.start_server()
        # The ephemeral port is only known now, after the shards have spawned, so
        # the fake's MCP tools server reads the base URL from this file (and a
        # respawned shard inherits it through AGENT_SERVER_URL). Step 2.3.
        url = str(self.client.make_url("/")).rstrip("/")
        os.environ["AGENT_SERVER_URL"] = url
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "server-url").write_text(url)
        return self

    async def stop(self):
        if self.client is not None:
            await self.client.close()
            self.client = None
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    async def _record_discord(self, agent, channel_id, content, reply_to=None,
                              dead_letter=False):
        self.discord.append({"agent": agent, "channel_id": channel_id,
                             "content": content, "reply_to": reply_to,
                             "dead_letter": dead_letter})
        return f"discord-{len(self.discord)}"

    # -- scripting the fake -------------------------------------------------

    def script(self, default=None, rules=None):
        """Write the fake's script (see tests/harness/fake_claude.py)."""
        self.script_path.write_text(json.dumps(
            {"default": default or {}, "rules": rules or []}))

    # -- driving ------------------------------------------------------------

    def _headers(self):
        return {"Authorization": f"Bearer {DEFAULT_TOKEN}"}

    async def send(self, agent, text, channel_id="1"):
        resp = await self.client.post(
            "/message", headers=self._headers(),
            json={"agent": agent, "content": text, "channel_id": channel_id,
                  "server": "local", "author": "harness"})
        assert resp.status == 202, await resp.text()
        return await resp.json()

    async def interrupt(self, agent):
        resp = await self.client.post(f"/agents/{agent}/interrupt",
                                      headers=self._headers())
        return await resp.json()

    async def hive_calls(self, **filters):
        """The `calls` list of GET /hive/calls (limit, since, shard, status)."""
        resp = await self.client.get("/hive/calls", headers=self._headers(),
                                     params={k: str(v) for k, v in filters.items()})
        assert resp.status == 200, await resp.text()
        return (await resp.json())["calls"]

    async def wait_idle(self, agent, timeout=5):
        """Wait until `agent` is IDLE with nothing queued or in progress."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = [r for r in self.queue_rows(agent) if r["processed"] in (0, 1)]
            if not busy and self.module.agent_states.get(agent) == "IDLE":
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(
            f"{agent} not idle after {timeout}s "
            f"(state={self.module.agent_states.get(agent)}, rows={self.queue_rows(agent)})")

    async def wait_for(self, predicate, timeout=5):
        """Poll `predicate()` until truthy; for conditions wait_idle can't see."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("condition not met")

    # -- observing ----------------------------------------------------------

    def _query(self, sql, params=()):
        conn = sqlite3.connect(str(self.module.DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def session_id(self, shard):
        rows = self._query("SELECT session_id FROM sessions WHERE agent = ?", (shard,))
        return rows[0]["session_id"] if rows else None

    def sent_to(self, shard):
        """Texts the fake received for this shard's current session, in order."""
        path = self.log_dir / f"{self.session_id(shard)}.in.jsonl"
        if not path.exists():
            return []
        return [json.loads(l)["text"] for l in path.read_text().splitlines() if l]

    def argv(self, shard):
        """argv (without the program name) of the shard's latest spawn."""
        path = self.log_dir / f"{self.session_id(shard)}.argv.json"
        return json.loads(path.read_text()) if path.exists() else None

    def io(self, shard):
        """Parsed <session>.io.jsonl the fake wrote in queued-stdin mode: every
        stdin line (dir "in") and emitted event (dir "out") with seconds since
        the fake started, in the recorded fixtures' {"t","dir","event"} shape."""
        path = self.log_dir / f"{self.session_id(shard)}.io.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l]

    def queue_rows(self, shard):
        return self._query(
            "SELECT * FROM message_queue WHERE agent = ? ORDER BY id", (shard,))

    def cost_rows(self, shard=None):
        """cost_events rows for one shard id, or all when omitted."""
        if shard is None:
            return self._query("SELECT * FROM cost_events ORDER BY id")
        return self._query(
            "SELECT * FROM cost_events WHERE agent = ? ORDER BY id", (shard,))

    def stream_events(self, shard):
        """Raw stream-json events the server tee'd for this shard (for 1.5)."""
        events = []
        pattern = str(self.module.STREAM_LOG_DIR / f"{shard}_*.jsonl")
        for path in sorted(glob.glob(pattern)):
            for line in Path(path).read_text().splitlines():
                if line.strip():
                    events.append(json.loads(line))
        return events
