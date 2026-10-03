import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { allowedAgents, permissionsFor } from "@/lib/permissions";
import { getHistoryDetail } from "@/lib/history";

/**
 * Full exchange + turn_events drill-down for one message_id (Task 8be6aa80).
 * Deep-link target: /history/[messageId] fetches this.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ messageId: string }> }
) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const { messageId } = await params;
  if (!messageId) {
    return NextResponse.json({ error: "Missing messageId" }, { status: 400 });
  }

  const perms = permissionsFor(sessionUser(session));
  const allowed = allowedAgents(perms);

  try {
    const detail = await getHistoryDetail(messageId, allowed);
    if (detail === null) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    if (detail === "forbidden") {
      return NextResponse.json({ error: "Forbidden" }, { status: 403 });
    }
    return NextResponse.json(detail);
  } catch (error) {
    return NextResponse.json(
      { error: `Failed to load message: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
