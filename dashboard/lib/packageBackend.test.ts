import { beforeEach, describe, expect, it, vi } from "vitest";

const agentFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, agentFetch: agentFetchMock };
});

import { fetchHiveCalls, fetchUsage, hiveQuery, parseHiveFilter } from "@/lib/packageBackend";

const parse = (q: string) => parseHiveFilter(new URLSearchParams(q));

describe("parseHiveFilter", () => {
  it("accepts the documented values", () => {
    expect(parse("")).toEqual({ ok: true, query: { limit: 100 } });
    expect(parse("limit=1&shard=alpha-2&status=answered&since=2026-10-03T10:00:00Z")).toEqual({
      ok: true,
      query: { limit: 1, shard: "alpha-2", status: "answered", since: "2026-10-03T10:00:00Z" },
    });
    expect(parse("limit=500")).toMatchObject({ ok: true, query: { limit: 500 } });
    for (const status of ["pending", "answered", "expired", "error", "timeout", "abandoned"]) {
      expect(parse(`status=${status}`)).toMatchObject({ ok: true });
    }
  });

  it("rejects out-of-range and malformed values", () => {
    expect(parse("limit=0")).toMatchObject({ ok: false });
    expect(parse("limit=501")).toMatchObject({ ok: false });
    expect(parse("limit=ten")).toMatchObject({ ok: false });
    expect(parse("status=bogus")).toMatchObject({ ok: false });
    expect(parse("shard=a%2Fb")).toMatchObject({ ok: false });
    expect(parse("since=yesterday")).toMatchObject({ ok: false });
  });

  it("drops unknown parameters", () => {
    const r = parse("limit=5&evil=1&shard=a");
    expect(r).toEqual({ ok: true, query: { limit: 5, shard: "a" } });
    expect(hiveQuery((r as { query: Parameters<typeof hiveQuery>[0] }).query)).not.toContain("evil");
  });

  it("round-trips through hiveQuery", () => {
    for (const q of ["limit=100", "limit=7&since=2026-10-03T10%3A00%3A00.000Z&shard=a&status=error"]) {
      const r = parse(q);
      if (!r.ok) throw new Error("expected ok");
      expect(parse(hiveQuery(r.query))).toEqual(r);
    }
  });
});

describe("fetchUsage and fetchHiveCalls", () => {
  beforeEach(() => {
    agentFetchMock.mockReset();
  });

  it("returns the body on 200 and calls the right paths", async () => {
    agentFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ calls: [], windows: {} }) });
    expect(await fetchUsage()).toEqual({ ok: true, data: { calls: [], windows: {} } });
    expect(agentFetchMock.mock.calls[0][0]).toBe("/usage");
    await fetchHiveCalls({ limit: 5, shard: "a" });
    expect(agentFetchMock.mock.calls[1][0]).toBe("/hive/calls?limit=5&shard=a");
    expect(agentFetchMock.mock.calls[1][1]).toMatchObject({ cache: "no-store" });
  });

  it("returns {ok:false, status:502} on a rejected fetch", async () => {
    agentFetchMock.mockImplementation(async () => { throw new Error("ECONNREFUSED"); });
    const r = await fetchUsage(); expect(r).toEqual({ ok: false, status: 502 });
    expect(await fetchHiveCalls({ limit: 1 })).toEqual({ ok: false, status: 502 });
  });

  it("returns {ok:false, status:502} on a 500", async () => {
    agentFetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    expect(await fetchUsage()).toEqual({ ok: false, status: 502 });
    expect(await fetchHiveCalls({ limit: 1 })).toEqual({ ok: false, status: 502 });
  });
});

describe("bearer header", () => {
  it("is added by agentFetch itself (the real one)", async () => {
    vi.resetModules();
    vi.doUnmock("@/lib/api");
    process.env.AGENT_SERVER_TOKEN = "tok";
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const real = await import("@/lib/api");
    await real.agentFetch("/usage");
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization")).toBe("Bearer tok");
    vi.unstubAllGlobals();
    delete process.env.AGENT_SERVER_TOKEN;
  });
});
