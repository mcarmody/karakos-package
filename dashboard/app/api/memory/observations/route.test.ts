import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const agentFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, agentFetch: agentFetchMock };
});

const OK_BODY = { observations: [], next: null };
const reply = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

let GET: typeof import("./route").GET;
let sign: typeof import("@/lib/api").generateSessionToken;

function req(user?: string, qs = ""): NextRequest {
  return new NextRequest(`http://localhost/api/memory/observations${qs}`, {
    headers: user ? { cookie: `karakos_session=${sign(user)}` } : {},
  });
}
const call = (user?: string, qs?: string) => GET(req(user, qs), { params: Promise.resolve({ id: "12" }) } as never);

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  agentFetchMock.mockReset();
  agentFetchMock.mockResolvedValue(reply(200, OK_BODY));
  ({ GET } = await import("./route"));
  ({ generateSessionToken: sign } = await import("@/lib/api"));
});
afterEach(() => {
  delete process.env.DASHBOARD_PERMISSIONS;
});

describe("GET /api/memory/observations", () => {
  it("is 401 without a session", async () => {
    expect((await call()).status).toBe(401);
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("is 403 for an agent-restricted account", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ guest: { agents: ["a"] } });
    const res = await call("guest");
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "Forbidden" });
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("is 400 on a bad filter and never calls the server", async () => {
    for (const qs of ["?kind=bogus", "?state=nope", "?entity=abc", "?limit=101", "?cursor=x:1"]) {
      const res = await call("tester", qs);
      expect(res.status).toBe(400);
      expect(await res.json()).toHaveProperty("error");
    }
    expect(agentFetchMock).not.toHaveBeenCalled();
  });

  it("returns a success body unchanged", async () => {
    const res = await call("tester");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(OK_BODY);
    expect(agentFetchMock.mock.calls[0][0]).toMatch(/^\/graph\//);
  });

  it("passes 503 and 404 bodies through", async () => {
    for (const [status, body] of [[503, { error: "graph_not_initialised", detail: "d" }], [404, { error: "unknown_entity", detail: "d" }]] as const) {
      agentFetchMock.mockResolvedValue(reply(status, body));
      const res = await call("tester");
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual(body);
    }
  });

  it("maps any other status or a network error to 502", async () => {
    agentFetchMock.mockResolvedValue(reply(500, {}));
    expect((await call("tester")).status).toBe(502);
    agentFetchMock.mockRejectedValue(new Error("down"));
    const res = await call("tester");
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "agent-server unavailable" });
  });
});
