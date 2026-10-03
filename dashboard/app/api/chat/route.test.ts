import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/**
 * The session picker offers every live agent by its real name; these tests
 * pin down that the route forwards whatever agent name the caller (the
 * picker) actually sent, with no hidden redirect, while permission
 * enforcement (agentAllowed) still runs first.
 */

const enqueueForAgent = vi.fn();
vi.mock("@/lib/agentEnqueue", () => ({ enqueueForAgent: (...args: unknown[]) => enqueueForAgent(...args) }));

let apiMod: typeof import("@/lib/api");
let POST: typeof import("./route").POST;

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  enqueueForAgent.mockReset();
  enqueueForAgent.mockResolvedValue({ status: 200, body: { ok: true }, via: "http://broker" });
  vi.resetModules();
  apiMod = await import("@/lib/api");
  ({ POST } = await import("./route"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

function req(body: Record<string, unknown>, cookie?: string): NextRequest {
  const headers = new Headers({ "content-type": "application/json" });
  if (cookie) headers.set("cookie", `karakos_session=${cookie}`);
  return new NextRequest("http://localhost/api/chat", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

describe("POST /api/chat — literal agent routing", () => {
  it("sends to the literal agent the caller named — no redirect", async () => {
    const res = await POST(req({ agent: "alpha", content: "hi" }, apiMod.generateSessionToken("tester")));
    expect(res.status).toBe(200);
    expect(enqueueForAgent).toHaveBeenCalledTimes(1);
    const [payload] = enqueueForAgent.mock.calls[0];
    expect(payload.agent).toBe("alpha");
  });

  it("routes to any registered agent named literally, e.g. beta", async () => {
    const res = await POST(req({ agent: "beta", content: "hi" }, apiMod.generateSessionToken("tester")));
    expect(res.status).toBe(200);
    const [payload] = enqueueForAgent.mock.calls[0];
    expect(payload.agent).toBe("beta");
  });

  it("rejects a missing agent or content before ever touching the broker", async () => {
    const res = await POST(req({ agent: "alpha" }, apiMod.generateSessionToken("tester")));
    expect(res.status).toBe(400);
    expect(enqueueForAgent).not.toHaveBeenCalled();
  });

  it("401s an unauthenticated caller", async () => {
    const res = await POST(req({ agent: "alpha", content: "hi" }));
    expect(res.status).toBe(401);
    expect(enqueueForAgent).not.toHaveBeenCalled();
  });

  it("403s a restricted account reaching for an agent outside its allowlist", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ guest: { agents: ["beta"] } });
    vi.resetModules();
    apiMod = await import("@/lib/api");
    ({ POST } = await import("./route"));
    const res = await POST(req({ agent: "alpha", content: "hi" }, apiMod.generateSessionToken("guest")));
    expect(res.status).toBe(403);
    expect(enqueueForAgent).not.toHaveBeenCalled();
    delete process.env.DASHBOARD_PERMISSIONS;
  });
});
