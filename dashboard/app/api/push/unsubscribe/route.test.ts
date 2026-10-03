import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { NextRequest } from "next/server";

let workspaceRoot: string;
let apiMod: typeof import("@/lib/api");
let pushSubsMod: typeof import("@/lib/push-subscriptions");
let POST: typeof import("./route").POST;

beforeEach(async () => {
  workspaceRoot = mkdtempSync(join(tmpdir(), "karakos-unsub-route-test-"));
  mkdirSync(join(workspaceRoot, "data"), { recursive: true });
  process.env.WORKSPACE_ROOT = workspaceRoot;
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  vi.resetModules();
  apiMod = await import("@/lib/api");
  pushSubsMod = await import("@/lib/push-subscriptions");
  ({ POST } = await import("./route"));
});

afterEach(() => {
  rmSync(workspaceRoot, { recursive: true, force: true });
});

function req(body: unknown, cookie?: string): NextRequest {
  const headers = new Headers({ "content-type": "application/json" });
  if (cookie) headers.set("cookie", `karakos_session=${cookie}`);
  return new NextRequest("http://localhost/api/push/unsubscribe", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

describe("POST /api/push/unsubscribe", () => {
  it("401s with no session cookie", async () => {
    const res = await POST(req({ endpoint: "https://push.example.com/a" }));
    expect(res.status).toBe(401);
  });

  it("400s when endpoint is missing", async () => {
    const token = apiMod.generateSessionToken("tester");
    const res = await POST(req({}, token));
    expect(res.status).toBe(400);
  });

  it("returns removed:false for an endpoint that was never subscribed", async () => {
    const token = apiMod.generateSessionToken("tester");
    const res = await POST(req({ endpoint: "https://push.example.com/nope" }, token));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ removed: false });
  });

  it("removes a subscribed endpoint", async () => {
    pushSubsMod.addSubscription({ endpoint: "https://push.example.com/a", p256dh: "p", auth: "a" });
    const token = apiMod.generateSessionToken("tester");

    const res = await POST(req({ endpoint: "https://push.example.com/a" }, token));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ removed: true });
    expect(pushSubsMod.listSubscriptions()).toHaveLength(0);
  });
});
