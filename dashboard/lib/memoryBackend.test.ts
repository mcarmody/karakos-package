import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseMemoryFilter } from "./memoryBackend";

const qs = (s: string, target?: "observations" | "entities") => parseMemoryFilter(new URLSearchParams(s), target);

describe("parseMemoryFilter", () => {
  it("accepts the documented values and rebuilds the query", () => {
    const r = qs("q=tea&kind=fact&state=archived&entity=7&domain=home&agent=alpha&limit=100&cursor=b:12");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const out = new URLSearchParams(r.query);
    expect(Object.fromEntries(out)).toEqual({
      q: "tea", kind: "fact", state: "archived", entity: "7", domain: "home", agent: "alpha", limit: "100", cursor: "b:12",
    });
    expect(r.filter.entity).toBe(7);
    for (const state of ["active", "archived", "superseded", "all"]) expect(qs(`state=${state}`).ok).toBe(true);
    for (const kind of ["fact", "episode", "pattern"]) expect(qs(`kind=${kind}`).ok).toBe(true);
    expect(qs("cursor=o:200").ok).toBe(true);
    expect(qs("limit=1").ok).toBe(true);
  });

  it("rejects bad values", () => {
    for (const bad of [
      "kind=bogus", "state=nope", "entity=abc", "entity=-1", "limit=101", "limit=0", "limit=x", "cursor=x:1", "cursor=b:", "cursor=b:1;2",
      `q=${"x".repeat(201)}`, `domain=${"d".repeat(81)}`, `agent=${"a".repeat(81)}`,
    ]) {
      expect(qs(bad).ok, bad).toBe(false);
    }
    expect(qs(`q=${"x".repeat(200)}`).ok).toBe(true);
  });

  it("drops unknown parameters and empty values", () => {
    const r = qs("junk=1&kind=&q=a&__proto__=x");
    expect(r.ok && r.query).toBe("q=a");
  });

  it("entities: free-text kind, no superseded state, observation-only params dropped", () => {
    const r = qs("kind=person&state=all&entity=3&domain=x&q=al", "entities");
    expect(r.ok && Object.fromEntries(new URLSearchParams(r.query))).toEqual({ kind: "person", state: "all", q: "al" });
    expect(qs("state=superseded", "entities").ok).toBe(false);
  });
});

describe("wrappers", () => {
  const fetchMock = vi.fn();
  let mod: typeof import("./memoryBackend");
  beforeEach(async () => {
    process.env.AGENT_SERVER_TOKEN = "tok-123";
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.resetModules();
    mod = await import("./memoryBackend");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("send the bearer header, no-store and a timeout", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ observations: [], next: null }) });
    const r = await mod.fetchObservations("kind=fact");
    expect(r).toEqual({ ok: true, body: { observations: [], next: null } });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/graph\/observations\?kind=fact$/);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-123");
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    await mod.fetchGraphStatus();
    await mod.fetchEntities("q=a");
    await mod.fetchEntity(5);
    expect(fetchMock.mock.calls.slice(1).map((c) => String(c[0]).replace(/^https?:\/\/[^/]+/, ""))).toEqual([
      "/graph/status", "/graph/entities?q=a", "/graph/entities/5",
    ]);
  });

  it("return 502 on a rejected fetch", async () => {
    fetchMock.mockRejectedValue(new Error("down"));
    expect(await mod.fetchGraphStatus()).toEqual({ ok: false, status: 502, error: "agent-server unavailable" });
  });

  it("keep the server status and body on an error answer", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 503, json: async () => ({ error: "graph_not_initialised", detail: "x" }) });
    expect(await mod.fetchEntity(1)).toEqual({
      ok: false, status: 503, error: "graph_not_initialised", body: { error: "graph_not_initialised", detail: "x" },
    });
  });
});
