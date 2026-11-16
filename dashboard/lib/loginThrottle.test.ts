import { beforeEach, describe, expect, it } from "vitest";
import {
  MAX_FAILURES_GLOBAL, MAX_FAILURES_PER_CLIENT, WINDOW_MS,
  clientIp, recordFailure, recordSuccess, resetForTests, retryAfterSeconds,
} from "./loginThrottle";

let t = 1_000_000;
beforeEach(() => { t = 1_000_000; resetForTests(() => t); });

describe("loginThrottle", () => {
  it("allows the first failures and blocks at the limit", () => {
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT - 1; i++) {
      recordFailure("a");
      expect(retryAfterSeconds("a")).toBe(0);
    }
    recordFailure("a");
    expect(retryAfterSeconds("a")).toBeGreaterThan(0);
    expect(retryAfterSeconds("b")).toBe(0);
  });

  it("lets the client back in after the window", () => {
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT; i++) recordFailure("a");
    expect(retryAfterSeconds("a")).toBeGreaterThan(0);
    t += WINDOW_MS + 1;
    expect(retryAfterSeconds("a")).toBe(0);
  });

  it("a success clears that client's failures", () => {
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT; i++) recordFailure("a");
    recordSuccess("a");
    expect(retryAfterSeconds("a")).toBe(0);
  });

  it("the global cap blocks every address", () => {
    for (let i = 0; i < MAX_FAILURES_GLOBAL; i++) recordFailure(`ip-${i}`);
    expect(retryAfterSeconds("brand-new")).toBeGreaterThan(0);
  });

  it("reads the client address from forwarding headers", () => {
    expect(clientIp(new Headers({ "x-forwarded-for": "1.2.3.4, 10.0.0.1" }))).toBe("1.2.3.4");
    expect(clientIp(new Headers({ "x-real-ip": "5.6.7.8" }))).toBe("5.6.7.8");
    expect(clientIp(new Headers())).toBe("local");
  });
});
