/**
 * Scroll-follow policy for the parity chat list.
 *
 * - Incoming messages follow only while the reader is at the bottom;
 *   otherwise stay put and show the jump-to-latest pill.
 * - A local send always wins: it force-scrolls to the bottom and keeps
 *   following (across the assistant placeholder, streamed chunks and late
 *   image loads) until the list actually reaches the bottom or the user
 *   scrolls away on purpose.
 */
export const AT_BOTTOM_PX = 80;

export function isAtBottom(m: { scrollHeight: number; scrollTop: number; clientHeight: number }): boolean {
  return m.scrollHeight - m.scrollTop - m.clientHeight < AT_BOTTOM_PX;
}

export function shouldFollow(s: { atBottom: boolean; forced: boolean; lastIsUser: boolean }): boolean {
  return s.forced || s.atBottom || s.lastIsUser;
}

/**
 * Scroll-event resolution. While a forced scroll is in flight the smooth
 * animation passes through "not at bottom" positions; those must not flip
 * atBottom to false. Forced mode ends once the bottom is reached.
 */
export function onScrollState(
  prev: { atBottom: boolean; forced: boolean },
  nowAtBottom: boolean
): { atBottom: boolean; forced: boolean } {
  if (prev.forced) {
    return nowAtBottom ? { atBottom: true, forced: false } : { atBottom: true, forced: true };
  }
  return { atBottom: nowAtBottom, forced: false };
}
