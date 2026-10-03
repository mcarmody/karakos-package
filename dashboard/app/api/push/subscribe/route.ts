import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { addSubscription } from "@/lib/push-subscriptions";

export const dynamic = "force-dynamic";

function auth(request: NextRequest): boolean {
  return isAuthenticated(request.cookies.get("karakos_session")?.value);
}

/** A short, human-scannable device hint from the User-Agent — not a full UA dump. */
function labelFromUserAgent(ua: string | null): string | null {
  if (!ua) return null;
  const os = /iPhone|iPad/.test(ua)
    ? "iOS"
    : /Android/.test(ua)
      ? "Android"
      : /Mac OS X/.test(ua)
        ? "Mac"
        : /Windows/.test(ua)
          ? "Windows"
          : /Linux/.test(ua)
            ? "Linux"
            : "device";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Chrome\//.test(ua)
      ? "Chrome"
      : /CriOS\//.test(ua)
        ? "Chrome"
        : /Firefox\//.test(ua)
          ? "Firefox"
          : /Safari\//.test(ua)
            ? "Safari"
            : "browser";
  return `${browser} on ${os}`;
}

/**
 * POST /api/push/subscribe
 *   { endpoint, keys: { p256dh, auth }, label? }
 *
 * `endpoint` is the browser's PushSubscription.toJSON() shape. Re-subscribing
 * with the same endpoint (a re-opt-in, or the browser rotating keys) updates
 * the existing row rather than creating a duplicate — see addSubscription.
 */
export async function POST(request: NextRequest) {
  if (!auth(request)) return unauthorizedResponse();

  try {
    const body = await request.json();
    const endpoint = body?.endpoint;
    const p256dh = body?.keys?.p256dh;
    const authKey = body?.keys?.auth;

    if (
      typeof endpoint !== "string" || !endpoint ||
      typeof p256dh !== "string" || !p256dh ||
      typeof authKey !== "string" || !authKey
    ) {
      return NextResponse.json(
        { error: "endpoint and keys.p256dh/keys.auth are required" },
        { status: 400 }
      );
    }

    const label =
      (typeof body?.label === "string" && body.label.trim()) ||
      labelFromUserAgent(request.headers.get("user-agent"));

    const subscription = addSubscription({ endpoint, p256dh, auth: authKey, label });
    return NextResponse.json({ subscription }, { status: 201 });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "subscribe failed" },
      { status: 500 }
    );
  }
}
