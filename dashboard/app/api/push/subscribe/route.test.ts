import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { NextRequest } from "next/server";

// SESSION_SECRET (lib/api.ts) and WORKSPACE_ROOT (lib/db.ts, via
// push-subscriptions) are both read at module-load time, so — same as
// lib/push-subscriptions.test.ts — env vars must be set and modules reset
// before each test's dynamic import, not via static top-of-file imports.
let workspaceRoot: string;
let apiMod: typeof import("@/lib/api");
let pushSubsMod: typeof import("@/lib/push-subscriptions");
let POST: typeof import("./route").POST;

async function freshEnv() {
  workspaceRoot = mkdtempSync(join(tmpdir(), "karakos-push-route-test-"));
  mkdirSync(join(workspaceRoot, "data"), { recursive: true });
  process.env.WORKSPACE_ROOT = workspaceRoot;
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  vi.resetModules();
  apiMod = await import("@/lib/api");
  pushSubsMod = await import("@/lib/push-subscriptions");
  ({ POST } = await import("./route"));
}

beforeEach(async () => {
  await freshEnv();
});

afterEach(() => {
  rmSync(workspaceRoot, { recursive: true, force: true });
});

function req(body: unknown, cookie?: string, ua?: string): NextRequest {
  const headers = new Headers({ "content-type": "application/json" });
  if (cookie) headers.set("cookie", `karakos_session=${cookie}`);
  if (ua) headers.set("user-agent", ua);
  return new NextRequest("http://localhost/api/push/subscribe", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

describe("POST /api/push/subscribe", () => {
  it("401s with no session cookie", async () => {
    const res = await POST(req({ endpoint: "https://push.example.com/a", keys: { p256dh: "p", auth: "a" } }));
    expect(res.status).toBe(401);
  });

  it("401s with a garbage session cookie", async () => {
    const res = await POST(
      req({ endpoint: "https://push.example.com/a", keys: { p256dh: "p", auth: "a" } }, "not-a-real-token")
    );
    expect(res.status).toBe(401);
  });

  it("400s when endpoint or keys are missing", async () => {
    const token = apiMod.generateSessionToken("tester");
    const res = await POST(req({ keys: { p256dh: "p", auth: "a" } }, token));
    expect(res.status).toBe(400);
  });

  it("201s and stores the subscription with a valid session", async () => {
    const token = apiMod.generateSessionToken("tester");
    const res = await POST(
      req(
        { endpoint: "https://push.example.com/a", keys: { p256dh: "p256", auth: "authkey" } },
        token,
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
    );
    expect(res.status).toBe(201);
    const body = await res.json();
    expect(body.subscription.endpoint).toBe("https://push.example.com/a");
    // No client-supplied label: derived from User-Agent server-side.
    expect(body.subscription.label).toBe("Chrome on Mac");

    expect(pushSubsMod.listSubscriptions()).toHaveLength(1);
  });

  it("dedupes on endpoint: subscribing twice from the same device updates, not duplicates", async () => {
    const token = apiMod.generateSessionToken("tester");
    await POST(req({ endpoint: "https://push.example.com/a", keys: { p256dh: "old", auth: "old" } }, token));
    await POST(req({ endpoint: "https://push.example.com/a", keys: { p256dh: "new", auth: "new" } }, token));

    const rows = pushSubsMod.listSubscriptions();
    expect(rows).toHaveLength(1);
    expect(rows[0].p256dh).toBe("new");
  });
});
