import { describe, expect, it } from "vitest";
import { hostLabels, masthead, navSubtitle } from "./branding";

describe("branding", () => {
  it("text is neutral, with an optional operator name", () => {
    expect(navSubtitle("")).toBe("");
    expect(navSubtitle("Acme Ops")).toBe("Acme Ops");
    expect(masthead("")).toEqual({ lead: "KARAKOS", rest: " OPS BOARD", subtitle: "All Systems" });
    expect(masthead("Acme Ops").subtitle).toBe("Acme Ops — All Systems");
    expect(hostLabels()).toEqual({ cpu: "CPU", memory: "Memory", disk: "Disk" });
  });
});
