import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const agentFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, agentFetch: agentFetchMock };
});

const USAGE = {
  agents: { a: { status: "allowed" }, b: { status: "allowed" } },
  windows: { five_hour: { status: "allowed", resets_at: 1, utilization_pct: 41, updated_at: "x" } },
  breaker: { paused: false, until: null, types: [] },
  budgets: { a: { used: 1, budget: 10, paused_since: null, until: null }, b: { used: 9, budget: 10, paused_since: 5, until: 9 } },
  governor: { weekly_pct: 73, enabled: true, policy_broken: false },
};

let GET: typeof import("./route").GET;
let sign: typeof import("@/lib/api").generateSessionToken;

function req(user?: string): NextRequest {
  return new NextRequest("http://localhost/api/usage", {
    headers: user ? { cookie: `karakos_session=${sign(user)}` } : {},
  });
}

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  agentFetchMock.mockReset();
  agentFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => USAGE });
  ({ GET } = await import("./route"));
  ({ generateSessionToken: sign } = await import("@/lib/api"));
});
afterEach(() => {
  delete process.env.DASHBOARD_PERMISSIONS;
});

describe("GET /api/usage", () => {
  it("is 401 without a session", async () => {
    expect((await GET(req())).status).toBe(401);
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("returns the whole body to an unrestricted account", async () => {
    const body = await (await GET(req("tester"))).json();
    expect(body).toEqual(USAGE);
  });

  it("trims agents and budgets for a restricted account but keeps account facts", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ guest: { agents: ["a"] } });
    const body = await (await GET(req("guest"))).json();
    expect(Object.keys(body.agents)).toEqual(["a"]);
    expect(Object.keys(body.budgets)).toEqual(["a"]);
    expect(body.windows).toEqual(USAGE.windows);
    expect(body.breaker).toEqual(USAGE.breaker);
    expect(body.governor).toEqual(USAGE.governor);
  });

  it("is 502 when the server answers 500", async () => {
    agentFetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    const res = await GET(req("tester"));
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "agent-server unavailable" });
  });
});
