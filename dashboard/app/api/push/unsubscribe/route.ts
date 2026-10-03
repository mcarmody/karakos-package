import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { removeSubscription } from "@/lib/push-subscriptions";

export const dynamic = "force-dynamic";

function auth(request: NextRequest): boolean {
  return isAuthenticated(request.cookies.get("karakos_session")?.value);
}

/**
 * POST /api/push/unsubscribe
 *   { endpoint }
 */
export async function POST(request: NextRequest) {
  if (!auth(request)) return unauthorizedResponse();

  try {
    const body = await request.json();
    const endpoint = body?.endpoint;

    if (typeof endpoint !== "string" || !endpoint) {
      return NextResponse.json({ error: "endpoint is required" }, { status: 400 });
    }

    const removed = removeSubscription(endpoint);
    return NextResponse.json({ removed });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "unsubscribe failed" },
      { status: 500 }
    );
  }
}
