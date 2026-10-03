import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { AGENT_SERVER_DB } from "@/lib/db";
import { existsSync } from "fs";

// Root cause: the agent server has no GET /cost endpoint.
// Cost events are written to agent-server.db cost_events table.
// We read directly from there.

const DAILY_LIMIT = parseFloat(process.env.DAILY_COST_LIMIT || "20");
const MONTHLY_LIMIT = parseFloat(process.env.MONTHLY_COST_LIMIT || "500");

export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

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

    const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    const monthStart = today.slice(0, 7); // YYYY-MM

    // Daily: sum cost_delta per agent for today
    const dailyRows = db
      .prepare(
        `SELECT agent, SUM(cost_delta) as total
         FROM cost_events
         WHERE created_at >= ?
         GROUP BY agent
         ORDER BY total DESC`
      )
      .all(today + " 00:00:00") as Array<{ agent: string; total: number }>;

    // Monthly: sum cost_delta per agent since start of this month
    const monthlyRows = db
      .prepare(
        `SELECT agent, SUM(cost_delta) as total
         FROM cost_events
         WHERE created_at >= ?
         GROUP BY agent
         ORDER BY total DESC`
      )
      .all(monthStart + "-01 00:00:00") as Array<{ agent: string; total: number }>;

    const daily: Record<string, number> = {};
    for (const r of dailyRows) daily[r.agent] = r.total;

    const monthly: Record<string, number> = {};
    for (const r of monthlyRows) monthly[r.agent] = r.total;

    // Merge agent keys so both daily and monthly tables have the same agents
    const allAgents = new Set([...Object.keys(daily), ...Object.keys(monthly)]);
    for (const a of allAgents) {
      if (!(a in daily)) daily[a] = 0;
      if (!(a in monthly)) monthly[a] = 0;
    }

    return NextResponse.json({
      daily,
      monthly,
      limits: {
        daily_limit: DAILY_LIMIT,
        monthly_limit: MONTHLY_LIMIT,
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Failed to read cost data: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  } finally {
    db?.close();
  }
}
