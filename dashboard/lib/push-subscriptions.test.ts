import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

// DASHBOARD_DB (lib/db.ts) is derived from WORKSPACE_ROOT at module-load
// time, so each test gets a fresh temp directory + a fresh module instance
// (vi.resetModules) rather than sharing one sqlite file/connection across
// tests, which would make dedupe assertions depend on test order.
let workspaceRoot: string;
let mod: typeof import("./push-subscriptions");

beforeEach(async () => {
  workspaceRoot = mkdtempSync(join(tmpdir(), "karakos-push-test-"));
  mkdirSync(join(workspaceRoot, "data"), { recursive: true });
  process.env.WORKSPACE_ROOT = workspaceRoot;
  vi.resetModules();
  mod = await import("./push-subscriptions");
});

afterEach(() => {
  rmSync(workspaceRoot, { recursive: true, force: true });
});

describe("addSubscription", () => {
  it("inserts a new row", () => {
    const row = mod.addSubscription({
      endpoint: "https://push.example.com/a",
      p256dh: "p256dh-a",
      auth: "auth-a",
      label: "Chrome on Mac",
    });
    expect(row.endpoint).toBe("https://push.example.com/a");
    expect(row.label).toBe("Chrome on Mac");
    expect(mod.listSubscriptions()).toHaveLength(1);
  });

  it("dedupes on endpoint: re-subscribing updates instead of duplicating", () => {
    mod.addSubscription({
      endpoint: "https://push.example.com/a",
      p256dh: "old-p256dh",
      auth: "old-auth",
      label: "old label",
    });
    const updated = mod.addSubscription({
      endpoint: "https://push.example.com/a",
      p256dh: "new-p256dh",
      auth: "new-auth",
      label: "new label",
    });

    const all = mod.listSubscriptions();
    expect(all).toHaveLength(1);
    expect(updated.p256dh).toBe("new-p256dh");
    expect(updated.auth).toBe("new-auth");
    expect(updated.label).toBe("new label");
  });

  it("treats different endpoints as different devices", () => {
    mod.addSubscription({ endpoint: "https://push.example.com/a", p256dh: "1", auth: "1" });
    mod.addSubscription({ endpoint: "https://push.example.com/b", p256dh: "2", auth: "2" });
    expect(mod.listSubscriptions()).toHaveLength(2);
  });
});

describe("removeSubscription", () => {
  it("removes an existing endpoint and returns true", () => {
    mod.addSubscription({ endpoint: "https://push.example.com/a", p256dh: "1", auth: "1" });
    expect(mod.removeSubscription("https://push.example.com/a")).toBe(true);
    expect(mod.listSubscriptions()).toHaveLength(0);
  });

  it("returns false for an endpoint that was never subscribed", () => {
    expect(mod.removeSubscription("https://push.example.com/nope")).toBe(false);
  });
});
