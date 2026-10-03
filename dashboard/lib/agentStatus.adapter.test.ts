import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { NextRequest } from "next/server";

/**
 * The agent-roster adapter, driven by a mocked agent-server (global fetch)
 * and a registry file in a temp dir. No real ports, no HOME, no network.
 */

const REGISTRY = `version: 2
agents:
  alpha:
    name: alpha
    role: primary
    label: Alpha
    model: opus
    shards:
      - id: alpha
        channels: [general]
      - id: alpha-2
        channels: [ops]
  relay:
    name: relay
    role: monitor
    dashboard_chat: false
  planned:
    name: planned
    role: custom
`;

const AGENTS_BODY = {
  agents: [
    {
      name: "alpha",
      context_tokens: 4200,
      shards: { alpha: { context_tokens: 4200 }, "alpha-2": { context_tokens: 100 } },
      model: "opus",
      max_turns: 200,
      timeout: 10800,
      state: "IDLE",
      has_discord_token: true,
      dashboard_chat: true,
      label: "Alpha",
    },
    {
      name: "relay",
      context_tokens: 0,
      shards: { relay: { context_tokens: 0 } },
      model: "haiku",
      state: "PROCESSING",
      has_discord_token: true,
      dashboard_chat: false,
      label: "relay",
    },
  ],
};

const HEALTH_BODY = {
  status: "healthy",
  agents: {
    alpha: { state: "IDLE", alive: true, queue_depth: 2, session_id: "abcd1234", context_tokens: 4200 },
    relay: { state: "PROCESSING", alive: true, queue_depth: 0, session_id: "", context_tokens: 0 },
  },
};

let dir: string;
let fetchMock: ReturnType<typeof vi.fn>;

function serverUp(opts: { health?: boolean } = {}) {
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).endsWith("/agents")) return { ok: true, status: 200, json: async () => AGENTS_BODY };
    if (String(url).endsWith("/health")) {
      return opts.health === false
        ? { ok: false, status: 500, json: async () => ({}) }
        : { ok: true, status: 200, json: async () => HEALTH_BODY };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "agent-status-"));
  writeFileSync(join(dir, "agents.yaml"), REGISTRY);
  process.env.KARAKOS_REGISTRY_PATH = join(dir, "agents.yaml");
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  vi.resetModules();
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  delete process.env.KARAKOS_REGISTRY_PATH;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("agent roster adapter", () => {
  it("returns agents and shards from /agents plus the registry", async () => {
    serverUp();
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    const adapter = await getAgentStatusAdapter();
    const rows = await adapter.listAgents(() => true);

    expect(rows.map((r) => r.name)).toEqual(["alpha", "relay", "planned"]);
    const alpha = rows[0];
    expect(alpha).toMatchObject({
      state: "IDLE",
      host: "local",
      label: "Alpha",
      role: "primary",
      model: "opus",
      context_tokens: 4200,
      subprocess_alive: true,
      total_pending: 2,
      session_id: "abcd1234",
    });
    // Server shard info with the registry's channels laid over it.
    // (A 1.5 server reports a dict; it normalises to one row per key.)
    expect(alpha.shards?.map((x) => [x.id, x.context_tokens, x.channels])).toEqual([
      ["alpha", 4200, ["general"]],
      ["alpha-2", 100, ["ops"]],
    ]);
    expect(rows[1]).toMatchObject({ dashboard_chat: false, role: "monitor", state: "PROCESSING" });
    // Declared in the registry but not known to the server yet.
    expect(rows[2]).toMatchObject({ name: "planned", state: "UNKNOWN", context_tokens: 0 });
    expect(rows[2].shards).toMatchObject([{ id: "planned", state: "UNKNOWN", channels: [] }]);
  });

  it("calls only /agents and /health, with the bearer token", async () => {
    process.env.AGENT_SERVER_TOKEN = "t";
    serverUp();
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    await (await getAgentStatusAdapter()).listAgents(() => true);
    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get("Authorization")).toBe("Bearer t");
    }
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.sort()).toEqual([expect.stringMatching(/\/agents$/), expect.stringMatching(/\/health$/)]);
    delete process.env.AGENT_SERVER_TOKEN;
  });

  it("applies the per-account allowlist", async () => {
    serverUp();
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    const rows = await (await getAgentStatusAdapter()).listAgents((n) => n === "relay");
    expect(rows.map((r) => r.name)).toEqual(["relay"]);
  });

  it("degrades to /agents alone when /health fails", async () => {
    serverUp({ health: false });
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    const rows = await (await getAgentStatusAdapter()).listAgents(() => true);
    expect(rows[0]).toMatchObject({ name: "alpha", state: "IDLE", total_pending: 0, context_tokens: 4200 });
    expect(rows[0].subprocess_alive).toBeUndefined();
  });

  it("works with no registry file (server roster only)", async () => {
    process.env.KARAKOS_REGISTRY_PATH = join(dir, "missing.yaml");
    serverUp();
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    const rows = await (await getAgentStatusAdapter()).listAgents(() => true);
    expect(rows.map((r) => r.name)).toEqual(["alpha", "relay"]);
    expect(rows[0].shards?.map((x) => [x.id, x.context_tokens])).toEqual([["alpha", 4200], ["alpha-2", 100]]);
  });

  it("throws when /agents is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    const { getAgentStatusAdapter } = await import("@/lib/agentAdapter");
    await expect((await getAgentStatusAdapter()).listAgents(() => true)).rejects.toThrow(/agent-server \/agents/);
  });

  it("parseRegistry ignores a malformed file", async () => {
    const { parseRegistry } = await import("@/lib/agentStatus.package");
    expect(parseRegistry("not: [valid")).toEqual([]);
    expect(parseRegistry("version: 2\nagents: 7\n")).toEqual([]);
  });
});

describe("GET /api/agents", () => {
  it("serves the roster from the adapter", async () => {
    serverUp();
    const { generateSessionToken } = await import("@/lib/api");
    const { GET } = await import("../app/api/agents/route");
    const res = await GET(
      new NextRequest("http://localhost/api/agents", {
        headers: { cookie: `karakos_session=${generateSessionToken("tester")}` },
      })
    );
    expect(res.status).toBe(200);
    const { agents } = await res.json();
    expect(agents.map((a: { name: string }) => a.name)).toContain("alpha");
  });
});
