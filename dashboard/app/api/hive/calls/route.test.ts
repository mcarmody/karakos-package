import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const agentFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, agentFetch: agentFetchMock };
});

const call = (id: string, from: string, to: string) => ({
  call_id: id, from, to, from_agent: from, to_agent: to, depth: 1, status: "answered",
  created_at: "2026-10-03 10:00:00", started_at: null, answered_at: null, duration_ms: 5,
  question: "q", answer: "a", error: null,
});

let GET: typeof import("./route").GET;
let sign: typeof import("@/lib/api").generateSessionToken;

function req(user?: string, qs = ""): NextRequest {
  return new NextRequest(`http://localhost/api/hive/calls${qs}`, {
    headers: user ? { cookie: `karakos_session=${sign(user)}` } : {},
  });
}

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  agentFetchMock.mockReset();
  agentFetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ calls: [call("1", "a", "a"), call("2", "a", "b"), call("3", "b", "a")] }),
  });
  ({ GET } = await import("./route"));
  ({ generateSessionToken: sign } = await import("@/lib/api"));
});
afterEach(() => {
  delete process.env.DASHBOARD_PERMISSIONS;
});

describe("GET /api/hive/calls", () => {
  it("is 401 without a session", async () => {
    expect((await GET(req())).status).toBe(401);
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("returns every call to an unrestricted account and forwards only validated filters", async () => {
    const body = await (await GET(req("tester", "?limit=5&status=answered&junk=1"))).json();
    expect(body.calls).toHaveLength(3);
    expect(agentFetchMock.mock.calls[0][0]).toBe("/hive/calls?limit=5&status=answered");
  });

  it("hides a call unless both agents are allowed", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ guest: { agents: ["a"] } });
    const body = await (await GET(req("guest"))).json();
    expect(body.calls.map((c: { call_id: string }) => c.call_id)).toEqual(["1"]);
  });

  it("is 400 on a bad filter and never reaches the server", async () => {
    for (const qs of ["?limit=0", "?status=bogus", "?since=yesterday", "?shard=a%2Fb"]) {
      const res = await GET(req("tester", qs));
      expect(res.status).toBe(400);
      expect(await res.json()).toHaveProperty("error");
    }
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("is 502 when the server answers 500", async () => {
    agentFetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    const res = await GET(req("tester"));
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "agent-server unavailable" });
  });
});
