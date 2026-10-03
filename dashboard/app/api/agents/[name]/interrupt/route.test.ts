import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const agentFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, agentFetch: agentFetchMock };
});

let POST: typeof import("./route").POST;
let sign: typeof import("@/lib/api").generateSessionToken;

const req = () =>
  new NextRequest("http://localhost/api/agents/alpha/interrupt", {
    method: "POST",
    headers: { cookie: `karakos_session=${sign("tester")}` },
  });
const ctx = (name: string) => ({ params: Promise.resolve({ name }) });

beforeEach(async () => {
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  agentFetchMock.mockReset();
  agentFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ status: "interrupted" }) });
  ({ POST } = await import("./route"));
  ({ generateSessionToken: sign } = await import("@/lib/api"));
});

describe("POST /api/agents/[name]/interrupt", () => {
  it("calls the contract path with no body", async () => {
    const res = await POST(req(), ctx("alpha"));
    expect(res.status).toBe(200);
    expect(agentFetchMock).toHaveBeenCalledWith("/agents/alpha/interrupt", { method: "POST" });
  });

  it("refuses a name that is not an agent id", async () => {
    const res = await POST(req(), ctx("a/b"));
    expect(res.status).toBe(400);
    expect(agentFetchMock).not.toHaveBeenCalled();
  });
});
