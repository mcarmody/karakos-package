import { describe, expect, it } from "vitest";
import { existsSync } from "fs";
import { join } from "path";
import { ALL_NAV_ITEMS, DAILY_ITEMS, ICON_RAIL_ITEMS, NAV_GROUPS } from "./navGroups";

const APP = join(__dirname, "..");

function pageFor(href: string): string {
  return join(APP, href === "/" ? "" : href, "page.tsx");
}

describe("nav", () => {
  it("every nav href has a page in the app", () => {
    for (const item of ALL_NAV_ITEMS) expect(existsSync(pageFor(item.href)), item.href).toBe(true);
    for (const item of ICON_RAIL_ITEMS) expect(existsSync(pageFor(item.href)), item.href).toBe(true);
  });

  it("keeps the agent pages and has no empty groups", () => {
    const hrefs = ALL_NAV_ITEMS.map((i) => i.href);
    for (const h of ["/chat", "/", "/agents", "/fleet", "/memory", "/costs", "/settings"]) expect(hrefs).toContain(h);
    for (const g of NAV_GROUPS) expect(g.items.length).toBeGreaterThan(0);
  });

  it("the flat list is the daily items then every group item", () => {
    expect(ALL_NAV_ITEMS).toEqual([...DAILY_ITEMS, ...NAV_GROUPS.flatMap((g) => g.items)]);
  });
});
