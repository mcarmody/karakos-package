import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { sendPushToAll } from "@/lib/push";

export const dynamic = "force-dynamic";

function auth(request: NextRequest): boolean {
  return isAuthenticated(request.cookies.get("karakos_session")?.value);
}

/**
 * POST /api/push/test — fires a test notification at every subscribed
 * device (there's no per-device targeting yet; installs have few
 * enough devices that "all of them" is the useful test).
 */
export async function POST(request: NextRequest) {
  if (!auth(request)) return unauthorizedResponse();

  const user = sessionUser(request.cookies.get("karakos_session")?.value);
  try {
    const result = await sendPushToAll(
      "Karakos",
      `Test notification triggered by ${user || "someone"} — if you can read this, push works.`
    );
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "test send failed" },
      { status: 500 }
    );
  }
}
