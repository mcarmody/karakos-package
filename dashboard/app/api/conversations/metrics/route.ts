import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse, AGENT_NAME_RE } from "@/lib/api";
import { AGENT_SERVER_DB } from "@/lib/db";
import { existsSync } from "fs";

// Read-only rollup of cost_events grouped by (agent, session_id) — one
// session_id is one context window (workspace commit 3ce820ff stamps it on
// every turn; NULL for pre-3ce820ff rows and callers that don't send one,
// so those are excluded here rather than grouped into a fake conversation).
// Same DB, same better-sqlite3-readonly pattern as app/api/cost/route.ts.

export interface ConversationMetric {
  agent: string;
  session_id: string;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number;
  started_at: string;
  last_at: string;
  duration_ms: number;
}

export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  const { searchParams } = new URL(request.url);

  const agent = searchParams.get("agent");
  if (agent && !AGENT_NAME_RE.test(agent)) {
    return NextResponse.json({ error: "Invalid agent name" }, { status: 400 });
  }

  const daysParam = parseInt(searchParams.get("days") || "7", 10);
  const days = Number.isFinite(daysParam) && daysParam > 0 ? Math.min(daysParam, 90) : 7;

  // Not spec'd, but an unbounded GROUP BY over the full cost_events
  // history on every 30s poll is a defensible thing to cap.
  const limitParam = parseInt(searchParams.get("limit") || "200", 10);
  const limit = Number.isFinite(limitParam) && limitParam > 0 ? Math.min(limitParam, 500) : 200;

  if (!existsSync(AGENT_SERVER_DB)) {
    return NextResponse.json(
      { error: "Agent server DB not found" },
      { status: 503 }
    );
  }

  const Database = (await import("better-sqlite3")).default;
  let db: InstanceType<typeof Database> | undefined;
  try {
    db = new Database(AGENT_SERVER_DB, { readonly: true, timeout: 5000 });

    // created_at is written via SQLite's datetime('now') (UTC, "YYYY-MM-DD
    // HH:MM:SS") — build the cutoff in the same format so the lexicographic
    // comparison lines up regardless of the server's local timezone.
    const cutoff = new Date(Date.now() - days * 86400000)
      .toISOString()
      .slice(0, 19)
      .replace("T", " ");

    let where = "session_id IS NOT NULL AND created_at >= ?";
    const params: Array<string | number> = [cutoff];
    if (agent) {
      where += " AND agent = ?";
      params.push(agent);
    }
    params.push(limit);

    const rows = db
      .prepare(
        `SELECT agent, session_id,
                COUNT(*) as turns,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(cost_delta), 0) as cost,
                MIN(created_at) as started_at,
                MAX(created_at) as last_at
         FROM cost_events
         WHERE ${where}
         GROUP BY agent, session_id
         ORDER BY last_at DESC
         LIMIT ?`
      )
      .all(...params) as Array<{
        agent: string;
        session_id: string;
        turns: number;
        input_tokens: number;
        output_tokens: number;
        cost: number;
        started_at: string;
        last_at: string;
      }>;

    // Append "Z" so Date.parse reads both timestamps as UTC — same rationale
    // as the cutoff above, and it makes duration_ms correct independent of
    // the Node process's local timezone.
    const conversations: ConversationMetric[] = rows.map((r) => ({
      ...r,
      total_tokens: r.input_tokens + r.output_tokens,
      duration_ms: Math.max(
        0,
        Date.parse(`${r.last_at.replace(" ", "T")}Z`) -
          Date.parse(`${r.started_at.replace(" ", "T")}Z`)
      ),
    }));

    return NextResponse.json({ conversations });
  } catch (error) {
    return NextResponse.json(
      {
        error: `Failed to read conversation metrics: ${
          error instanceof Error ? error.message : "unknown"
        }`,
      },
      { status: 500 }
    );
  } finally {
    db?.close();
  }
}
