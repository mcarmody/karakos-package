import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { AGENT_SERVER_DB } from "@/lib/db";
import { isPassResponse, parseAttachmentsColumn, type ChatAttachment } from "@/lib/chatAttachments";

// Terminal statuses, mirroring bin/claude-agent-server.py: 2 complete,
// 3 crashed, 4 skipped. 0/1 are queued/in-progress — still running.
const STATUS_COMPLETE = 2;

/**
 * When the agent's turn actually ENDED, or null if it hasn't yet.
 *
 * The real signal is `message_queue.posted_at`. Despite its
 * name, it is not "when we posted" — the supervisor's
 * mark_complete() stamps it `datetime('now')` on EVERY terminal path, at
 * the moment wait_turn_done() returns, including the paths that produce no
 * text at all (`PASS_SKIP`, empty/PASS response) and the ones that
 * deliberately suppress the post (`SILENT_SKIP`). That is exactly the case
 * this endpoint's consumer needs: a turn that ends without saying anything
 * still gets a timestamp here, so the client can draw a boundary for it.
 *
 * The one terminal path that does NOT stamp it is mark_failed() (crash,
 * processed=3), which leaves posted_at NULL. There is no recorded end time
 * for a crashed turn, so fall back to created_at — the boundary lands in
 * the right PLACE, and is early by the turn's duration rather than absent.
 *
 * Values are SQLite `datetime('now')` — space-separated and UTC with no
 * zone marker. Passed through verbatim; the client normalizes them (a bare
 * `new Date()` on that shape parses as LOCAL and lands hours off).
 */
function turnEndedAt(row: {
  created_at: string;
  posted_at: string | null;
  processed: number;
}): string | null {
  if (row.processed < STATUS_COMPLETE) return null;
  return row.posted_at ?? row.created_at;
}

export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const url = request.nextUrl;
  // Literal agent name, no alias redirect.
  const agent = url.searchParams.get("agent");
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);

  if (!agent) {
    return NextResponse.json({ error: "Missing agent" }, { status: 400 });
  }

  // Per-account agent allowlist — a transcript is exactly the thing a
  // confined account must not read for someone else's agent.
  const perms = permissionsFor(sessionUser(session));
  if (!agentAllowed(perms, agent)) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  try {
    const sqlite3 = await import("sqlite3").then((m) => m.default);
    const { open } = await import("sqlite");
    const db = await open({ filename: AGENT_SERVER_DB, driver: sqlite3.Database });

    const rows = await db.all<
      Array<{
        message_id: string;
        content: string;
        response: string | null;
        created_at: string;
        posted_at: string | null;
        processed: number;
        attachments?: string | null;
      }>
    >(
      `SELECT message_id, content, response, created_at, posted_at, processed, attachments
       FROM message_queue
       WHERE agent = ? AND channel = 'dashboard'
       ORDER BY created_at DESC
       LIMIT ?`,
      agent,
      limit
    ).catch(() =>
      // A DB predating the attachments column: same query without it.
      db.all<
        Array<{
          message_id: string;
          content: string;
          response: string | null;
          created_at: string;
          posted_at: string | null;
          processed: number;
          attachments?: string | null;
        }>
      >(
        `SELECT message_id, content, response, created_at, posted_at, processed
         FROM message_queue
         WHERE agent = ? AND channel = 'dashboard'
         ORDER BY created_at DESC
         LIMIT ?`,
        agent,
        limit
      )
    );
    await db.close();

    // Reverse so callers get chronological order
    const ordered = rows.reverse();
    const messages: {
      role: "user" | "assistant";
      content: string;
      ts: string;
      messageId: string;
      processed?: number;
      turnEndedAt?: string;
      attachments?: ChatAttachment[];
    }[] = [];
    for (const row of ordered) {
      const atts = parseAttachmentsColumn(row.attachments);
      if (row.content) {
        messages.push({
          role: "user",
          content: row.content,
          ts: row.created_at,
          messageId: row.message_id,
          ...(atts.length ? { attachments: atts } : {}),
        });
      }
      // Include in-progress assistant turns (processed=1) as well as completed
      // ones (processed=2) so a mid-stream reload can render the partial reply.
      if (row.processed >= 1) {
        const endedAt = turnEndedAt(row);
        messages.push({
          role: "assistant",
          // An agent answers ambient messages with the literal PASS — never a
          // bubble. Blanked rather than dropped so the turn boundary (which
          // is keyed off turnEndedAt, not content) still draws.
          content: isPassResponse(row.response) ? "" : row.response ?? "",
          ts: row.created_at,
          messageId: row.message_id,
          processed: row.processed,
          ...(endedAt ? { turnEndedAt: endedAt } : {}),
        });
      }
    }

    return NextResponse.json({ messages });
  } catch (error) {
    return NextResponse.json(
      { error: `Failed to load history: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
