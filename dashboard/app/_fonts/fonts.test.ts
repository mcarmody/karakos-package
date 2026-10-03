import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, existsSync, statSync } from "fs";
import { dirname, join, resolve } from "path";
import { fontStack } from "./stack";

// Fonts are self-hosted from app/_fonts because
// next/font/google fetches from Google at BUILD time, and Google's CSS API
// intermittently returned extensionless font URLs that crashed next's loader
// ("Cannot read properties of null (reading '1')"). That failed about half of
// this repo's CI builds in September 2026. These tests keep it that way: the
// next page to copy an old layout would otherwise bring the import back.

const REPO = resolve(__dirname, "..", "..");
const SCAN_DIRS = ["app", "components", "lib"].map((d) => join(REPO, d));

function sourceFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p));
    else if (/\.(tsx?|jsx?|mjs|cjs)$/.test(name)) out.push(p);
  }
  return out;
}

const files = SCAN_DIRS.flatMap(sourceFiles).filter((f) => !f.endsWith("fonts.test.ts"));

describe("self-hosted fonts", () => {
  it("no source file imports next/font/google", () => {
    const offenders = files
      .filter((f) => /from\s+["']next\/font\/google["']/.test(readFileSync(f, "utf8")))
      .map((f) => f.slice(REPO.length + 1));
    expect(offenders).toEqual([]);
  });

  it("every next/font/local path points at a real woff2 file", () => {
    const missing: string[] = [];
    let checked = 0;
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      if (!src.includes("next/font/local")) continue;
      for (const m of src.matchAll(/path:\s*"([^"]+\.woff2)"/g)) {
        checked++;
        const p = resolve(dirname(f), m[1]);
        if (!existsSync(p) || readFileSync(p).subarray(0, 4).toString("latin1") !== "wOF2") {
          missing.push(`${f.slice(REPO.length + 1)} -> ${m[1]}`);
        }
      }
    }
    expect(checked).toBeGreaterThan(0);
    expect(missing).toEqual([]);
  });

  it("fontStack puts extra-subset faces before the metric fallback", () => {
    const main = { style: { fontFamily: "'lora', 'lora Fallback'" } };
    const math = { style: { fontFamily: "'loraMath'" } };
    const symbols = { style: { fontFamily: "'loraSymbols'" } };
    expect(fontStack(main, math, symbols)).toBe("'lora', 'loraMath', 'loraSymbols', 'lora Fallback'");
    expect(fontStack(main)).toBe("'lora', 'lora Fallback'");
  });
});
