import { describe, expect, it, vi } from "vitest";
import { parseRegistry } from "@/lib/agentStatus.package";

// agentFetch is not used by the pure functions under test; keep the module
// graph free of the network anyway.
vi.mock("@/lib/api", () => ({ agentFetch: vi.fn() }));

const REGISTRY = parseRegistry(`version: 2
agents:
  alpha:
    name: alpha
    shards:
      - id: alpha
        channels: [general]
      - id: alpha-2
        channels: [ops]
      - id: alpha-3
        channels: [late]
  solo:
    name: solo
`);

describe("normalizeShards", () => {
  it("maps the 2.1 list row by row, keeping real ids and unknown keys", async () => {
    const { normalizeShards } = await import("@/lib/agentStatus.package");
    const rows = normalizeShards([
      {
        id: "alpha",
        is_default: true,
        state: "PROCESSING",
        alive: true,
        pid: 123,
        session_id: "s1",
        queue_depth: 3,
        context_tokens: 9000,
        channels: ["general"],
        last_channel: "general",
        paused: null,
        future_key: 7,
      },
      { id: "alpha-2", state: "IDLE", paused: { reason: "budget", until: 1791020000 } },
    ]);
    expect(rows.map((r) => r.id)).toEqual(["alpha", "alpha-2"]);
    expect(rows[0]).toMatchObject({ is_default: true, state: "PROCESSING", alive: true, pid: 123, queue_depth: 3, future_key: 7 });
    expect(rows[1]).toMatchObject({
      state: "IDLE",
      alive: false,
      pid: null,
      session_id: "",
      queue_depth: 0,
      context_tokens: 0,
      channels: [],
      last_channel: null,
      is_default: false,
      paused: { reason: "budget", until: 1791020000 },
    });
  });

  it("turns a legacy dict into one defaulted row per key", async () => {
    const { normalizeShards } = await import("@/lib/agentStatus.package");
    const rows = normalizeShards({ alpha: { context_tokens: 4200 } });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ id: "alpha", context_tokens: 4200, state: "UNKNOWN", queue_depth: 0, paused: null });
  });

  it("returns [] for anything else", async () => {
    const { normalizeShards } = await import("@/lib/agentStatus.package");
    for (const v of [undefined, null, 5, "x"]) expect(normalizeShards(v)).toEqual([]);
  });

  it("keeps an until of null (paused until usage drops)", async () => {
    const { normalizeShards } = await import("@/lib/agentStatus.package");
    expect(normalizeShards([{ id: "a", paused: { reason: "breaker", until: null } }])[0].paused).toEqual({
      reason: "breaker",
      until: null,
    });
  });
});

describe("buildRoster shards", () => {
  const server = [
    {
      name: "alpha",
      state: "IDLE",
      shards: [
        { id: "alpha", is_default: true, queue_depth: 2, channels: [], paused: { reason: "budget", until: 1791020000 } },
        { id: "alpha-2", queue_depth: 3, channels: ["server-owned"] },
      ],
    },
    { name: "solo", state: "IDLE", shards: [] },
  ];

  it("keys rows by real shard ids and appends registry-only shards as UNKNOWN", async () => {
    const { buildRoster } = await import("@/lib/agentStatus.package");
    const [alpha] = buildRoster(server, {}, REGISTRY, () => true);
    expect(alpha.shards?.map((s) => s.id)).toEqual(["alpha", "alpha-2", "alpha-3"]);
    expect(alpha.shards?.[0].paused).toEqual({ reason: "budget", until: 1791020000 });
    expect(alpha.shards?.[2]).toMatchObject({ state: "UNKNOWN", channels: ["late"] });
  });

  it("lets registry channels fill an empty list and never overwrite a non-empty one", async () => {
    const { buildRoster } = await import("@/lib/agentStatus.package");
    const [alpha] = buildRoster(server, {}, REGISTRY, () => true);
    expect(alpha.shards?.[0].channels).toEqual(["general"]);
    expect(alpha.shards?.[1].channels).toEqual(["server-owned"]);
  });

  it("sums shard queue depth for the agent and falls back to /health without shard rows", async () => {
    const { buildRoster } = await import("@/lib/agentStatus.package");
    const rows = buildRoster(server, { alpha: { queue_depth: 99 }, solo: { queue_depth: 4 } }, REGISTRY, () => true);
    expect(rows[0].total_pending).toBe(5);
    // solo: no server shard rows (registry fills one), so /health depth.
    expect(rows[1].total_pending).toBe(4);
  });

  it("marks the agent paused only when every shard is", async () => {
    const { buildRoster } = await import("@/lib/agentStatus.package");
    const paused = { reason: "breaker", until: null };
    const all = buildRoster(
      [{ name: "a", shards: [{ id: "a", paused }, { id: "a-2", paused }] }],
      {},
      [],
      () => true
    );
    expect(all[0].paused).toEqual(paused);
    const some = buildRoster([{ name: "a", shards: [{ id: "a", paused }, { id: "a-2" }] }], {}, [], () => true);
    expect(some[0].paused).toBeNull();
  });

  it("still yields one row with id alpha for a legacy dict body", async () => {
    const { buildRoster } = await import("@/lib/agentStatus.package");
    const [row] = buildRoster([{ name: "alpha", shards: { alpha: { context_tokens: 4200 } } }], { alpha: { queue_depth: 2 } }, [], () => true);
    expect(row.shards).toHaveLength(1);
    expect(row.shards?.[0]).toMatchObject({ id: "alpha", context_tokens: 4200 });
    expect(row.total_pending).toBe(2);
  });
});
