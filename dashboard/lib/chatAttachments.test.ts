import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

let root: string;
let mod: typeof import("./chatAttachments");

beforeEach(async () => {
  root = mkdtempSync(join(tmpdir(), "chat-att-"));
  process.env.WORKSPACE_ROOT = root;
  const { vi } = await import("vitest");
  vi.resetModules();
  mod = await import("./chatAttachments");
  mkdirSync(join(root, "data/attachments/2026-09-30"), { recursive: true });
});
afterEach(() => {
  delete process.env.WORKSPACE_ROOT;
  rmSync(root, { recursive: true, force: true });
});

describe("isPassResponse", () => {
  it("matches exactly PASS, trimmed", async () => {
    const { isPassResponse } = await import("./chatAttachments");
    expect(isPassResponse("PASS")).toBe(true);
    expect(isPassResponse("  PASS\n")).toBe(true);
    expect(isPassResponse("PASS it on")).toBe(false);
    expect(isPassResponse("pass")).toBe(false);
    expect(isPassResponse("")).toBe(false);
    expect(isPassResponse(null)).toBe(false);
  });
  it("hides a streaming prefix only in partial mode", async () => {
    const { isPassResponse } = await import("./chatAttachments");
    expect(isPassResponse("PA")).toBe(false);
    expect(isPassResponse("PA", true)).toBe(true);
    expect(isPassResponse("PAX", true)).toBe(false);
  });
});

describe("sanitizeAttachments", () => {
  it("keeps files under the attachments root and rewrites url", () => {
    const f = join(root, "data/attachments/2026-09-30/ab-cat.png");
    writeFileSync(f, "x");
    const out = mod.sanitizeAttachments([
      { filename: "cat.png", size: 1, content_type: "image/png", url: "http://evil", local_path: f },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].local_path).toContain("data/attachments/2026-09-30/ab-cat.png");
    expect(out[0].url).toBe("/api/chat/attachment?path=2026-09-30%2Fab-cat.png");
    expect(Object.keys(out[0]).sort()).toEqual(["content_type", "filename", "local_path", "size", "url"]);
  });
  it("drops paths outside the root and non-arrays", () => {
    writeFileSync(join(root, "secret.txt"), "s");
    expect(mod.sanitizeAttachments([{ local_path: join(root, "secret.txt") }])).toEqual([]);
    expect(mod.sanitizeAttachments([{ local_path: "../../secret.txt" }])).toEqual([]);
    expect(mod.sanitizeAttachments([{ local_path: "/etc/passwd" }])).toEqual([]);
    expect(mod.sanitizeAttachments("nope")).toEqual([]);
  });
});

describe("safeFilename / parseAttachmentsColumn", () => {
  it("strips path bits", () => {
    expect(mod.safeFilename("../../etc/pa ss?.png")).toBe("pa ss_.png");
    expect(mod.safeFilename("")).toBe("file");
  });
  it("parses the Discord-shaped column defensively", () => {
    const raw = JSON.stringify([{ filename: "a.png", size: 3, content_type: "image/png", url: "https://cdn", local_path: "/x/data/attachments/2026-09-30/a.png" }]);
    const out = mod.parseAttachmentsColumn(raw);
    expect(out[0].filename).toBe("a.png");
    expect(mod.parseAttachmentsColumn("not json")).toEqual([]);
    expect(mod.parseAttachmentsColumn(null)).toEqual([]);
  });
});
