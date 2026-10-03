import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

async function load(env: Record<string, string | undefined>) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  return import("./route");
}

const post = (body: unknown) =>
  new NextRequest("http://localhost/api/auth", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

describe("POST /api/auth", () => {
  const saved = { ...process.env };
  beforeEach(() => vi.spyOn(console, "warn").mockImplementation(() => {}));
  afterEach(() => {
    process.env = { ...saved };
    vi.restoreAllMocks();
  });

  const base = { SESSION_SECRET: "s3cret", DASHBOARD_USER: "admin", DASHBOARD_PASSWORD: "pw", DASHBOARD_USERS: undefined };

  it("sets a non-Secure HttpOnly SameSite=Lax cookie by default", async () => {
    const { POST } = await load({ ...base, KARAKOS_COOKIE_SECURE: undefined });
    const res = await POST(post({ username: "admin", password: "pw" }));
    expect(res.status).toBe(200);
    const c = res.headers.get("set-cookie")!;
    expect(c).toMatch(/^karakos_session=/);
    expect(c).toMatch(/HttpOnly/i);
    expect(c).toMatch(/SameSite=lax/i);
    expect(c).not.toMatch(/;\s*Secure/i);
  });

  it("sets Secure when KARAKOS_COOKIE_SECURE=1", async () => {
    const { POST } = await load({ ...base, KARAKOS_COOKIE_SECURE: "1" });
    const c = (await POST(post({ username: "admin", password: "pw" }))).headers.get("set-cookie")!;
    expect(c).toMatch(/;\s*Secure/i);
    expect(c).toMatch(/HttpOnly/i);
    expect(c).toMatch(/SameSite=lax/i);
  });

  it("returns 401 for a wrong password", async () => {
    const { POST } = await load(base);
    const res = await POST(post({ username: "admin", password: "nope" }));
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("never authenticates when DASHBOARD_PASSWORD is empty", async () => {
    const { POST } = await load({ ...base, DASHBOARD_PASSWORD: "" });
    for (const password of ["", "x", "undefined"]) {
      const res = await POST(post({ username: "admin", password }));
      expect([400, 401]).toContain(res.status);
      expect(res.headers.get("set-cookie")).toBeNull();
    }
  });
});
