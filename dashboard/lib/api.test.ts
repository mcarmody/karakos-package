import crypto from "crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * session cookies minted by the old package dashboard must keep
 * working after the swap. The token below is built with the OLD algorithm,
 * written out literally (not imported): base64(username:timestamp:hmac-sha256-hex).
 */
function oldPackageToken(secret: string, username: string, timestamp: number): string {
  const data = `${username}:${timestamp}`;
  const sig = crypto.createHmac("sha256", secret).update(data).digest("hex");
  return Buffer.from(`${data}:${sig}`).toString("base64");
}

const SECRET = "test-secret-not-real";
const now = () => Math.floor(Date.now() / 1000);

async function load(env: Record<string, string | undefined>) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  return import("@/lib/api");
}

describe("isAuthenticated: old package token compatibility", () => {
  const saved = { ...process.env };
  beforeEach(() => vi.spyOn(console, "warn").mockImplementation(() => {}));
  afterEach(() => {
    process.env = { ...saved };
    vi.restoreAllMocks();
  });

  it("accepts a token minted by the old algorithm with the same secret", async () => {
    const { isAuthenticated, sessionUser } = await load({ SESSION_SECRET: SECRET });
    const tok = oldPackageToken(SECRET, "admin", now() - 3600);
    expect(isAuthenticated(tok)).toBe(true);
    expect(sessionUser(tok)).toBe("admin");
  });

  it("accepts a token inside the old 24h window and beyond (new default is longer)", async () => {
    const { isAuthenticated } = await load({ SESSION_SECRET: SECRET, SESSION_MAX_AGE_SECONDS: undefined });
    expect(isAuthenticated(oldPackageToken(SECRET, "admin", now() - 86000))).toBe(true);
    expect(isAuthenticated(oldPackageToken(SECRET, "admin", now() - 86400 * 10))).toBe(true);
  });

  it("round-trips: tokens generated now equal the old algorithm's output", async () => {
    const { generateSessionToken } = await load({ SESSION_SECRET: SECRET });
    const tok = generateSessionToken("admin");
    const [u, ts] = Buffer.from(tok, "base64").toString().split(":");
    expect(tok).toBe(oldPackageToken(SECRET, u, Number(ts)));
  });

  it("rejects a tampered signature", async () => {
    const { isAuthenticated } = await load({ SESSION_SECRET: SECRET });
    const good = Buffer.from(oldPackageToken(SECRET, "admin", now()), "base64").toString();
    const bad = good.slice(0, -1) + (good.endsWith("0") ? "1" : "0");
    expect(isAuthenticated(Buffer.from(bad).toString("base64"))).toBe(false);
  });

  it("rejects an expired token", async () => {
    const { isAuthenticated } = await load({ SESSION_SECRET: SECRET, SESSION_MAX_AGE_SECONDS: "86400" });
    expect(isAuthenticated(oldPackageToken(SECRET, "admin", now() - 86400 - 60))).toBe(false);
  });

  it("rejects everything when SESSION_SECRET is empty", async () => {
    const { isAuthenticated } = await load({ SESSION_SECRET: "" });
    // Forged with the empty key: must not validate.
    expect(isAuthenticated(oldPackageToken("", "admin", now()))).toBe(false);
  });
});

describe("sessionCookieOptions (B40)", () => {
  const saved = { ...process.env };
  beforeEach(() => vi.spyOn(console, "warn").mockImplementation(() => {}));
  afterEach(() => {
    process.env = { ...saved };
    vi.restoreAllMocks();
  });

  it("is not Secure by default", async () => {
    const { sessionCookieOptions } = await load({ KARAKOS_COOKIE_SECURE: undefined });
    expect(sessionCookieOptions()).toMatchObject({ httpOnly: true, secure: false, sameSite: "lax", path: "/" });
  });

  it.each(["1", "true", "yes", "TRUE"])("is Secure for %s", async (v) => {
    const { sessionCookieOptions } = await load({ KARAKOS_COOKIE_SECURE: v });
    expect(sessionCookieOptions().secure).toBe(true);
  });

  it.each(["0", "", "no"])("is not Secure for %j", async (v) => {
    const { sessionCookieOptions } = await load({ KARAKOS_COOKIE_SECURE: v });
    expect(sessionCookieOptions().secure).toBe(false);
  });

  it("warns once at startup in production when unset", async () => {
    await load({ KARAKOS_COOKIE_SECURE: undefined, NODE_ENV: "production", SESSION_SECRET: SECRET });
    const msgs = (console.warn as any).mock.calls.map((c: any[]) => String(c[0]));
    expect(msgs.filter((m: string) => m.includes("session cookie is not Secure"))).toHaveLength(1);
  });

  it("does not warn when set", async () => {
    await load({ KARAKOS_COOKIE_SECURE: "1", NODE_ENV: "production", SESSION_SECRET: SECRET });
    const msgs = (console.warn as any).mock.calls.map((c: any[]) => String(c[0]));
    expect(msgs.some((m: string) => m.includes("not Secure"))).toBe(false);
  });
});
