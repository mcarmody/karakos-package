import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { AGENT_SERVER_DB } from "@/lib/db";
import { isPassResponse } from "@/lib/chatAttachments";

// Authoritative single-message fetch. The live SSE stream can die (iOS
// suspends network when the PWA is backgrounded; nginx buffering can eat
// mid-stream events) — this lets the client reconcile a bubble against the
// server DB, which always holds the final response (mark_complete writes it
// even for silent turns).
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const messageId = request.nextUrl.searchParams.get("message_id");
  if (!messageId) {
    return NextResponse.json({ error: "Missing message_id" }, { status: 400 });
  }

  try {
    const sqlite3 = await import("sqlite3").then((m) => m.default);
    const { open } = await import("sqlite");
    const db = await open({ filename: AGENT_SERVER_DB, driver: sqlite3.Database });
    const row = await db.get<{
      agent: string | null;
      response: string | null;
      processed: number;
      created_at: string;
      posted_at: string | null;
    }>(
      "SELECT agent, response, processed, created_at, posted_at FROM message_queue WHERE message_id = ?",
      messageId
    );
    await db.close();

    if (!row) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    // Per-account agent allowlist — same boundary as /api/chat/stream.
    const perms = permissionsFor(sessionUser(session));
    if (!agentAllowed(perms, row.agent || "")) {
      return NextResponse.json({ error: "Forbidden" }, { status: 403 });
    }
    return NextResponse.json({
      // Literal PASS (ambient-message skip) is never shown.
      response: isPassResponse(row.response) ? "" : row.response ?? "",
      processed: row.processed,
      // Turn-end stamp, same field and same semantics as /api/chat/history
      // (see turnEndedAt() there). When the SSE stream dies before `done`,
      // this is how the client still learns the turn is over and draws its
      // boundary — the case that matters most, since a stream that dies
      // silently looks identical to a turn that ended silently.
      turnEndedAt: row.processed >= 2 ? row.posted_at ?? row.created_at : null,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Failed: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
