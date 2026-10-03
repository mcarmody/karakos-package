import { describe, expect, it } from "vitest";
import { formatTokens, pausedLabel, sortShardRows, statusTone, stateTone, windowLabel } from "@/lib/fleetView";

describe("pausedLabel", () => {
  const NOW = 1_791_000_000;
  it("names each reason with the lift time", () => {
    for (const reason of ["breaker", "budget", "governor"]) {
      const label = pausedLabel({ reason, until: NOW + 3600 }, NOW);
      expect(label).toContain(reason);
      expect(label).toContain("until 2026-10-03");
    }
  });
  it("says 'until usage drops' when until is null", () => {
    expect(pausedLabel({ reason: "governor", until: null }, NOW)).toBe("governor: until usage drops");
  });
  it("says resuming for an until in the past", () => {
    expect(pausedLabel({ reason: "budget", until: NOW - 5 }, NOW)).toBe("budget: resuming");
  });
  it("is empty when not paused", () => {
    expect(pausedLabel(null, NOW)).toBe("");
  });
});

describe("windowLabel", () => {
  it("shows 'no reading' for null and 0% for 0", () => {
    expect(windowLabel(null)).toBe("no reading");
    expect(windowLabel(undefined)).toBe("no reading");
    expect(windowLabel(0)).toBe("0%");
    expect(windowLabel(41.4)).toBe("41%");
  });
});

describe("sortShardRows", () => {
  it("puts paused rows first and keeps order otherwise", () => {
    const p = { reason: "budget", until: null };
    const rows = [
      { id: "a", paused: null },
      { id: "b", paused: p },
      { id: "c", paused: null },
      { id: "d", paused: p },
    ];
    expect(sortShardRows(rows).map((r) => r.id)).toEqual(["b", "d", "a", "c"]);
  });
});

describe("formatTokens and tones", () => {
  it("formats 0 as unknown", () => {
    expect(formatTokens(0)).toBe("unknown");
    expect(formatTokens(4200)).toBe("4.2k");
    expect(formatTokens(512)).toBe("512");
  });
  it("maps states and statuses, muted for the unknown", () => {
    expect(stateTone("IDLE")).toBe("ok");
    expect(stateTone("ERROR_RECOVERY")).toBe("err");
    expect(stateTone("SOMETHING_NEW")).toBe("muted");
    expect(statusTone("answered")).toBe("ok");
    expect(statusTone("error")).toBe("err");
    expect(statusTone("whatever")).toBe("muted");
  });
});
