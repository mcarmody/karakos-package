/**
 * Dashboard API utilities — shared fetch helpers for agent server proxy calls.
 */

import * as crypto from "crypto";
import { homePathFor, permissionsFor } from "@/lib/permissions";

const AGENT_SERVER_URL = process.env.AGENT_SERVER_URL || "http://localhost:18791";
const AGENT_SERVER_TOKEN = process.env.AGENT_SERVER_TOKEN || "";
const SESSION_SECRET = process.env.SESSION_SECRET || "";

if (!SESSION_SECRET) {
  console.warn(
    "WARNING: SESSION_SECRET is not set. Dashboard auth will not work. " +
    "Run setup.sh or add SESSION_SECRET to your .env file."
  );
}

/**
 * How long a session cookie stays valid, in seconds. Default 30 days.
 *
 * Was a hard-coded 86400 in TWO places — the age check below and the cookie's
 * maxAge in app/api/auth/route.ts — which is why the phone asked for a login
 * daily. They have to agree: a cookie that outlives the
 * age check leaves the browser holding a token the server already rejects,
 * and the redirect to /login looks like a random logout rather than an expiry.
 * One constant, imported by both, so they cannot drift apart again.
 */
export const SESSION_MAX_AGE = Number(process.env.SESSION_MAX_AGE_SECONDS) || 60 * 60 * 24 * 30;

/**
 * B40: whether the session cookie carries `Secure`. Opt-in via
 * KARAKOS_COOKIE_SECURE=1|true|yes. Default off: a Secure cookie is dropped by
 * the browser on plain http (localhost, LAN), which makes login silently loop.
 * Set it when the dashboard is served over HTTPS (reverse proxy or tunnel).
 */
function cookieSecure(): boolean {
  return ["1", "true", "yes"].includes(
    (process.env.KARAKOS_COOKIE_SECURE || "").trim().toLowerCase()
  );
}

if (process.env.KARAKOS_COOKIE_SECURE === undefined && process.env.NODE_ENV === "production") {
  console.warn(
    "session cookie is not Secure; set KARAKOS_COOKIE_SECURE=1 if this dashboard is served over HTTPS"
  );
}

/** Single source for every route that sets the session cookie. */
export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure: cookieSecure(),
    sameSite: "lax" as const,
    maxAge: SESSION_MAX_AGE,
    path: "/",
  };
}

/**
 * Where an account lands after signing in. The per-account permissions model
 * (lib/permissions.ts) owns this and is read here rather than copied into each login route: there are
 * two ways to sign in (password and passkey) and a second copy of this rule
 * is a second place for it to go stale.
 */
export function homeForUser(username: string): string {
  return homePathFor(permissionsFor(username));
}

/**
 * Generate a signed session token (HMAC-SHA256).
 * Token format: base64(username:timestamp:hmac_signature)
 */
export function generateSessionToken(username: string): string {
  const timestamp = Math.floor(Date.now() / 1000);
  const data = `${username}:${timestamp}`;
  const signature = crypto
    .createHmac('sha256', SESSION_SECRET)
    .update(data)
    .digest('hex');
  return Buffer.from(`${data}:${signature}`).toString('base64');
}

/**
 * Fetch from the agent server with bearer token auth.
 */
export async function agentFetch(
  path: string,
  options: RequestInit = {}
): Promise<Response> {
  const url = `${AGENT_SERVER_URL}${path}`;
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${AGENT_SERVER_TOKEN}`);

  return fetch(url, {
    ...options,
    headers,
  });
}

/**
 * Check if the request has a valid signed session token.
 * Token format: base64(username:timestamp:hmac_signature)
 */
export function isAuthenticated(cookieValue: string | undefined): boolean {
  // An empty secret would let anyone forge a token with an empty HMAC key.
  if (!cookieValue || !SESSION_SECRET) return false;

  try {
    const decoded = Buffer.from(cookieValue, 'base64').toString('utf-8');
    const [username, timestamp, signature] = decoded.split(':');

    if (!username || !timestamp || !signature) return false;

    // Verify HMAC signature
    const data = `${username}:${timestamp}`;
    const expectedSignature = crypto
      .createHmac('sha256', SESSION_SECRET)
      .update(data)
      .digest('hex');

    // Timing-safe HMAC comparison. Plain `!==` short-circuits on first
    // mismatch and leaks signature bytes via timing side-channel.
    const sigBuf = Buffer.from(signature, 'hex');
    const expBuf = Buffer.from(expectedSignature, 'hex');
    if (sigBuf.length !== expBuf.length || !crypto.timingSafeEqual(sigBuf, expBuf)) {
      return false;
    }

    // Check token age against the shared session lifetime.
    const ts = parseInt(timestamp, 10);
    if (Math.floor(Date.now() / 1000) - ts > SESSION_MAX_AGE) return false;

    return true;
  } catch {
    return false;
  }
}

/**
 * Return 401 response for unauthenticated requests.
 */
/**
 * The username inside a session cookie, or null. Signature is verified first —
 * this is never a place to trust the cookie's contents blindly.
 *
 * Used for write attribution: with several logins, "added by" only
 * means something if it records who was actually holding the phone.
 */
export function sessionUser(cookieValue: string | undefined): string | null {
  if (!cookieValue || !isAuthenticated(cookieValue)) return null;
  try {
    const [username] = Buffer.from(cookieValue, "base64").toString("utf-8").split(":");
    return username || null;
  } catch {
    return null;
  }
}

export function unauthorizedResponse(): Response {
  return new Response(JSON.stringify({ error: "Unauthorized" }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Agent-name validator. Any route that forwards a `[name]` path segment
 * into an agent-server URL must check input against this regex first to
 * prevent path traversal / endpoint hopping (`../interrupt`, etc.).
 */
export const AGENT_NAME_RE = /^[a-zA-Z0-9_-]+$/;

/**
 * Return 400 response for invalid agent-name input.
 */
export function invalidAgentNameResponse(): Response {
  return new Response(JSON.stringify({ error: "Invalid agent name" }), {
    status: 400,
    headers: { "Content-Type": "application/json" },
  });
}
