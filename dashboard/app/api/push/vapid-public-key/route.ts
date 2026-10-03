import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";

export const dynamic = "force-dynamic";

/**
 * GET /api/push/vapid-public-key
 *
 * The public key isn't a secret (it's handed to the browser's PushManager
 * on every subscribe), but the endpoint sits under /api/ like everything
 * else so it inherits the standard auth gate rather than being a one-off
 * exception. Served at runtime instead of baked in as NEXT_PUBLIC_* so the
 * systemd-deployed standalone build doesn't need a rebuild whenever the
 * VAPID keypair changes.
 */
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }
  return NextResponse.json({ publicKey: process.env.VAPID_PUBLIC_KEY || null });
}
