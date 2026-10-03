import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "fs";
import { join } from "path";

/** README.md's environment table may only name variables the dashboard reads. */
const ROOT = join(__dirname, "..");
const SRC = ["package.json", "app", "lib", "middleware.ts", "next.config.mjs"];

function files(p: string): string[] {
  const full = join(ROOT, p);
  let st;
  try { st = statSync(full); } catch { return []; }
  if (st.isFile()) return [full];
  return readdirSync(full).flatMap((n) => (n === "node_modules" ? [] : files(join(p, n))));
}

describe("README environment table", () => {
  const md = readFileSync(join(ROOT, "README.md"), "utf8");
  const section = md.split(/\n## Environment variables\n/)[1]?.split(/\n## /)[0] ?? "";
  const names = [...section.matchAll(/^\| `([A-Z][A-Z0-9_]+)`/gm)].map((m) => m[1]);
  const source = SRC.flatMap(files)
    .filter((f) => !/\.test\.tsx?$/.test(f))
    .map((f) => readFileSync(f, "utf8"))
    .join("\n");

  it("lists the variables", () => {
    expect(names.length).toBeGreaterThan(10);
  });

  it("names only variables the source reads", () => {
    const unread = names.filter((n) => !source.includes(n));
    expect(unread).toEqual([]);
  });
});
