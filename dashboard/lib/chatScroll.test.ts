import { describe, it, expect } from "vitest";
import { isAtBottom, shouldFollow, onScrollState } from "./chatScroll";

describe("chatScroll", () => {
  it("isAtBottom uses an 80px threshold", () => {
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 530, clientHeight: 400 })).toBe(true);
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 100, clientHeight: 400 })).toBe(false);
  });
  it("incoming message while scrolled up does not follow", () => {
    expect(shouldFollow({ atBottom: false, forced: false, lastIsUser: false })).toBe(false);
  });
  it("forced send follows even when the newest message is the assistant placeholder", () => {
    expect(shouldFollow({ atBottom: false, forced: true, lastIsUser: false })).toBe(true);
  });
  it("mid-animation scroll events don't drop a forced scroll", () => {
    expect(onScrollState({ atBottom: true, forced: true }, false)).toEqual({ atBottom: true, forced: true });
    expect(onScrollState({ atBottom: true, forced: true }, true)).toEqual({ atBottom: true, forced: false });
  });
  it("normal scroll events track position", () => {
    expect(onScrollState({ atBottom: true, forced: false }, false)).toEqual({ atBottom: false, forced: false });
  });
});
