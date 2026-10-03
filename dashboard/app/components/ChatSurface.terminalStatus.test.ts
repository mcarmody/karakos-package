import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import { terminalStatusMessage, STATUS_COMPLETE } from "@/lib/chatMessage";

// The client must honour payload.status from /api/chat/stream, not
// just payload.done. There is no component-render harness for ChatSurface,
// so the wiring is checked from source (comments stripped) plus behaviour
// tests on the pure helper.

const strip = (t: string) =>
  t.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " ")).replace(/\/\/[^\n]*/g, (m) => " ".repeat(m.length));
const read = (p: string) => strip(readFileSync(join(__dirname, "../..", p), "utf8"));
const route = read("app/api/chat/stream/route.ts");
const surface = read("app/components/ChatSurface.tsx");

const routeStatuses = new Set([...route.matchAll(/status:\s*"([a-z]+)"/g)].map((m) => m[1]));

describe("ChatSurface honours the stream's terminal status", () => {
  it("premise: the route still sends typed complete and crashed statuses", () => {
    expect(routeStatuses.has("complete")).toBe(true);
    expect(routeStatuses.has("crashed")).toBe(true);
  });

  it("the done branch reads payload.status", () => {
    const done = surface.match(/if \(payload\.done\) \{([\s\S]*?)\n        \} else if \(payload\.event\)/);
    expect(done, "payload.done branch not found — re-point this test").toBeTruthy();
    expect(done![1]).toContain("payload.status");
  });

  it("every non-complete status the route sends maps to visible copy", () => {
    for (const s of routeStatuses) {
      if (s === "complete") continue;
      const msg = terminalStatusMessage(s);
      expect(msg, `status "${s}" has no banner`).toBeTruthy();
    }
  });

  it("crashed, skipped and timeout read as such, not as a normal reply", () => {
    expect(terminalStatusMessage("crashed", "Agent crashed")).toMatch(/crashed.*incomplete.*Agent crashed/);
    expect(terminalStatusMessage("skipped")).toMatch(/skipped/);
    expect(terminalStatusMessage("timeout")).toMatch(/timed out/);
  });

  it("unknown:<n> falls through to a default that names the status", () => {
    expect(terminalStatusMessage("unknown:9")).toContain("unknown:9");
  });

  it("complete (and absent status) renders no banner", () => {
    expect(terminalStatusMessage(STATUS_COMPLETE)).toBeNull();
    expect(terminalStatusMessage(undefined)).toBeNull();
  });

  it("the banner is rendered from the stored note, and only set for non-complete", () => {
    expect(surface).toMatch(/const note = terminalStatusMessage\(payload\.status, payload\.error\);\s*if \(note\)/);
    expect(surface).toMatch(/if \(msg\.terminalNote\)/);
  });

  it("transport error does not claim a terminal status", () => {
    const onerror = surface.match(/eventSource\.onerror = \(\) => \{([\s\S]*?)\n      \};/);
    expect(onerror).toBeTruthy();
    // EventSource fires onerror on normal close too; it must only reconcile.
    expect(onerror![1]).not.toContain("terminalNote");
    expect(onerror![1]).toContain("reconcileMessage");
  });
});
