import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/** The /chat session picker and the roster pages are fed by this route. */

let GET: typeof import("./route").GET;
let sign: typeof import("@/lib/api").generateSessionToken;

const req = (user = "tester") =>
  new NextRequest("http://localhost/api/agents", {
    headers: { cookie: `karakos_session=${sign(user)}` },
  });

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  process.env.KARAKOS_REGISTRY_PATH = "/nonexistent/agents.yaml";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).endsWith("/agents")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            agents: [
              { name: "alpha", state: "IDLE", shards: { alpha: {} } },
              { name: "beta", state: "PROCESSING", shards: { beta: {} } },
            ],
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    })
  );
  vi.resetModules();
  ({ generateSessionToken: sign } = await import("@/lib/api"));
  ({ GET } = await import("./route"));
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.KARAKOS_REGISTRY_PATH;
  delete process.env.DASHBOARD_PERMISSIONS;
});

describe("GET /api/agents", () => {
  it("lists every agent the server reports", async () => {
    const res = await GET(req());
    expect(res.status).toBe(200);
    const { agents } = await res.json();
    expect(agents.map((a: { name: string }) => a.name)).toEqual(["alpha", "beta"]);
  });

  it("trims the roster to the account's agent allowlist", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ guest: { agents: ["beta"] } });
    const res = await GET(req("guest"));
    const { agents } = await res.json();
    expect(agents.map((a: { name: string }) => a.name)).toEqual(["beta"]);
  });

  it("401s an unauthenticated caller", async () => {
    const res = await GET(new NextRequest("http://localhost/api/agents"));
    expect(res.status).toBe(401);
  });
});
