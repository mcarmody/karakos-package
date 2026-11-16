/**
 * In-memory throttle for failed dashboard logins.
 *
 * 5 failures per client address, and 50 across all addresses, inside a 15 minute
 * sliding window. The global cap exists because the client address is taken from
 * request headers a direct client can forge. State lives in this process only and
 * resets on restart.
 */
export const WINDOW_MS = 15 * 60 * 1000;
export const MAX_FAILURES_PER_CLIENT = 5;
export const MAX_FAILURES_GLOBAL = 50;
const MAX_TRACKED_CLIENTS = 1000;

let now: () => number = () => Date.now();
const perClient = new Map<string, number[]>();
let global: number[] = [];

function live(list: number[], t: number): number[] {
  return list.filter((ts) => t - ts < WINDOW_MS);
}

/** The caller's address: first X-Forwarded-For entry, else X-Real-IP, else "local". */
export function clientIp(headers: Headers): string {
  const xff = headers.get("x-forwarded-for")?.split(",")[0].trim();
  if (xff) return xff;
  const real = headers.get("x-real-ip")?.trim();
  return real || "local";
}

/** Seconds the caller must wait before trying again; 0 means it may try now. */
export function retryAfterSeconds(ip: string): number {
  const t = now();
  const mine = live(perClient.get(ip) ?? [], t);
  global = live(global, t);
  if (mine.length) perClient.set(ip, mine);
  else perClient.delete(ip);
  const waits: number[] = [];
  if (mine.length >= MAX_FAILURES_PER_CLIENT) waits.push(mine[0] + WINDOW_MS - t);
  if (global.length >= MAX_FAILURES_GLOBAL) waits.push(global[0] + WINDOW_MS - t);
  if (!waits.length) return 0;
  return Math.max(1, Math.ceil(Math.max(...waits) / 1000));
}

export function recordFailure(ip: string): void {
  const t = now();
  const mine = live(perClient.get(ip) ?? [], t);
  mine.push(t);
  perClient.delete(ip);
  perClient.set(ip, mine); // re-insert so the Map's first key is always the stalest client
  if (perClient.size > MAX_TRACKED_CLIENTS) {
    const oldest = perClient.keys().next().value;
    if (oldest !== undefined) perClient.delete(oldest);
  }
  global = live(global, t);
  global.push(t);
}

export function recordSuccess(ip: string): void {
  perClient.delete(ip);
}

/** Test hook: clear all state and optionally replace the clock. */
export function resetForTests(clock?: () => number): void {
  perClient.clear();
  global = [];
  now = clock ?? (() => Date.now());
}
