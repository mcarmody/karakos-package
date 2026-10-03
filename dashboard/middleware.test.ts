import { beforeAll, describe, expect, it } from "vitest";
import { NextRequest } from "next/server";

/** Pure: builds NextRequests, binds no port. */
let middleware: (req: NextRequest) => Response;
let cookie: string;

beforeAll(async () => {
  process.env.SESSION_SECRET = "test-secret-not-real";
  const api = await import("@/lib/api");
  middleware = (await import("./middleware")).middleware as unknown as (req: NextRequest) => Response;
  cookie = api.generateSessionToken("tester");
});

const get = (path: string, withSession = false) =>
  middleware(
    new NextRequest(`http://localhost:3000${path}`, {
      headers: withSession ? { cookie: `karakos_session=${cookie}` } : {},
    })
  );

describe("middleware", () => {
  it("does not gate /login or the public PWA files", () => {
    for (const p of ["/login", "/api/auth", "/sw.js", "/manifest.webmanifest", "/offline.html", "/icons/a.png"]) {
      expect(get(p).status, p).toBe(200);
    }
  });

  it("answers 401 JSON to an API call without a session", async () => {
    const res = get("/api/agents");
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "Unauthorized" });
    expect(get("/api/agents", true).status).toBe(200);
  });

  it("redirects a page request without a session to /login", () => {
    const res = get("/fleet");
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/login");
    expect(get("/fleet", true).status).toBe(200);
  });
});
