import { defineConfig } from "vitest/config";
import { fileURLToPath } from "url";

// vitest doesn't read tsconfig "paths" on its own — the "@/" alias used
// throughout app/ and lib/ needs an explicit resolve.alias, or any test
// that imports a route/lib module (which import each other via "@/...")
// fails with "Cannot find package '@/...'" before it gets anywhere near
// an assertion.
export default defineConfig({
  // tsconfig says jsx: preserve (Next compiles it); tests that render a
  // component to a string need the transformer to transform JSX itself.
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
  test: {
    // e2e/ is Playwright specs, run via `npx playwright test` — vitest's
    // default *.spec.ts glob picks them up too and crashes on test.use()
    // (a Playwright-only API), pre-dating this file. Excluding here is the
    // actual fix, not a new gap.
    exclude: ["**/node_modules/**", "e2e/**"],
  },
});
