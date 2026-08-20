import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { join } from "path";

const WORKSPACE_ROOT = process.env.WORKSPACE_ROOT || "/workspace";

// Authoritative single-message fetch. The live SSE stream (/api/chat/stream)
// can die before a turn finishes — a backgrounded mobile browser suspends
// its network, a reverse proxy drops the connection — leaving the chat
// bubble empty even though the turn kept running server-side. This lets the
// client reconcile a bubble against the server DB, which always holds the
// final response once bin/agent-server.py marks the row complete.
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  const messageId = request.nextUrl.searchParams.get("message_id");
  if (!messageId) {
    return NextResponse.json({ error: "Missing message_id" }, { status: 400 });
  }

  try {
    const sqlite3 = await import("sqlite3").then((m) => m.default);
    const { open } = await import("sqlite");
    const dbPath = join(WORKSPACE_ROOT, "data/memory/agent-server.db");
    const db = await open({ filename: dbPath, driver: sqlite3.Database });
    const row = await db.get<{ response: string | null; processed: number }>(
      "SELECT response, processed FROM message_queue WHERE message_id = ?",
      messageId
    );
    await db.close();

    if (!row) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    return NextResponse.json({
      response: row.response ?? "",
      processed: row.processed,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Failed: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
