import { NextRequest, NextResponse } from "next/server";
import { generateSessionToken, sessionCookieOptions, homeForUser } from "@/lib/api";

const DASHBOARD_USER = process.env.DASHBOARD_USER || "admin";
const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD || "";

/**
 * Additional logins, as `user:password` pairs separated by commas in
 * DASHBOARD_USERS, so writes are attributed to a person rather than to
 * whoever's session happened to be open.
 *
 * Usernames are compared case-insensitively (nobody types a capital L on a
 * phone keyboard reliably); passwords are not.
 */
const EXTRA_USERS: Record<string, string> = Object.fromEntries(
  (process.env.DASHBOARD_USERS || "")
    .split(",")
    .map((pair) => pair.trim())
    .filter(Boolean)
    .map((pair) => {
      const i = pair.indexOf(":");
      return i === -1
        ? ["", ""]
        : [pair.slice(0, i).trim().toLowerCase(), pair.slice(i + 1)];
    })
    .filter(([u, p]) => u && p)
);

/** Constant-time-ish compare so a wrong password does not leak its length. */
function sameSecret(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Resolve credentials to a canonical username, or null. */
function authenticate(username: string, password: string): string | null {
  if (username === DASHBOARD_USER && DASHBOARD_PASSWORD && sameSecret(password, DASHBOARD_PASSWORD)) {
    return DASHBOARD_USER;
  }
  const key = username.trim().toLowerCase();
  const expected = EXTRA_USERS[key];
  if (expected && sameSecret(password, expected)) return key;
  return null;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { username, password } = body;

    if (!username || !password) {
      return NextResponse.json(
        { error: "Missing credentials" },
        { status: 400 }
      );
    }

    const resolved = authenticate(username, password);
    if (resolved) {
      // The server owns the confined-account list; the client just follows
      // `home` rather than keeping its own copy that can drift.
      const home = homeForUser(resolved);
      const response = NextResponse.json({ success: true, user: resolved, home });

      // Generate signed session token
      const sessionToken = generateSessionToken(resolved);

      // Set session cookie with signed token (SESSION_MAX_AGE, httpOnly)
      response.cookies.set("karakos_session", sessionToken, sessionCookieOptions());

      return response;
    }

    return NextResponse.json(
      { error: "Invalid credentials" },
      { status: 401 }
    );
  } catch (error) {
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}

