import { describe, expect, it } from "vitest";
import { buildSessionList, defaultSessionName, friendlyName, type RosterAgent } from "./sessionRoster";

describe("buildSessionList", () => {
  it("sorts the primary first, the rest alphabetically by display name", () => {
    const agents: RosterAgent[] = [
      { name: "gamma", state: "ACTIVE" },
      { name: "beta", state: "ACTIVE" },
      { name: "alpha", state: "ACTIVE", role: "primary" },
      { name: "delta", state: "ACTIVE" },
    ];
    const names = buildSessionList(agents).map((o) => o.name);
    expect(names).toEqual(["alpha", "beta", "delta", "gamma"]);
    const flipped = buildSessionList([{ name: "alpha", state: "ACTIVE" }, { name: "zed", state: "ACTIVE", role: "primary" }]);
    expect(flipped.map((o) => o.name)).toEqual(["zed", "alpha"]);
  });

  it("marks an agent DOWN or with subprocess_alive:false as not live", () => {
    const agents: RosterAgent[] = [
      { name: "alpha", state: "DOWN" },
      { name: "beta", state: "ACTIVE", subprocess_alive: false },
      { name: "gamma", state: "ACTIVE", subprocess_alive: true },
    ];
    const byName = Object.fromEntries(buildSessionList(agents).map((o) => [o.name, o.live]));
    expect(byName.alpha).toBe(false);
    expect(byName.beta).toBe(false);
    expect(byName.gamma).toBe(true);
  });

  it("carries host through, defaulting to 'local'", () => {
    const [a, b] = buildSessionList([
      { name: "alpha", state: "ACTIVE", host: "node-2" },
      { name: "beta", state: "ACTIVE" },
    ]);
    expect(a.host).toBe("node-2");
    expect(b.host).toBe("local");
  });

  it("capitalises the raw name for the display name", () => {
    const [row] = buildSessionList([{ name: "some-new-agent", state: "ACTIVE" }]);
    expect(row.displayName).toBe("Some-new-agent");
  });

  it("carries the agent's own definition/note through as-is", () => {
    const [row] = buildSessionList([{ name: "alpha", state: "ACTIVE", label: "research agent" }]);
    expect(row.definition).toBe("research agent");
  });
});

describe("defaultSessionName", () => {
  it("picks the primary when it's on the roster", () => {
    const options = buildSessionList([
      { name: "alpha", state: "ACTIVE" },
      { name: "beta", state: "ACTIVE", role: "primary" },
    ]);
    expect(defaultSessionName(options)).toBe("beta");
  });

  it("falls back to the first option when there is no primary", () => {
    const options = buildSessionList([{ name: "beta", state: "ACTIVE" }, { name: "alpha", state: "ACTIVE" }]);
    expect(defaultSessionName(options)).toBe("alpha");
  });

  it("returns an empty string on an empty roster rather than throwing", () => {
    expect(defaultSessionName([])).toBe("");
  });
});

describe("friendlyName", () => {
  it("capitalizes an agent name", () => {
    expect(friendlyName("telchar")).toBe("Telchar");
  });
});
