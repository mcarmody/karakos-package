import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { allowedAgents, permissionsFor } from "@/lib/permissions";
import { searchHistory } from "@/lib/history";

/**
 * Per-agent full-text transcript search (Task 8be6aa80).
 * GET /api/history/search?q=&agent=&channel=&author=&mentionsAgent=&dateFrom=&dateTo=&page=&pageSize=
 *
 * Read-only against AGENT_SERVER_DB — see lib/history.ts for the
 * FTS5/locking constraints this route relies on.
 */
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const perms = permissionsFor(sessionUser(session));
  const allowed = allowedAgents(perms);
  if (allowed !== "*" && allowed.length === 0) {
    return NextResponse.json({ rows: [], total: 0, page: 1, pageSize: 25, ftsActive: false });
  }

  const params = request.nextUrl.searchParams;
  const requestedAgent = params.get("agent") || undefined;
  if (requestedAgent && allowed !== "*" && !allowed.includes(requestedAgent)) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const mentionsParam = params.get("mentionsAgent");
  const mentionsAgent =
    mentionsParam === "yes" ? true : mentionsParam === "no" ? false : undefined;

  try {
    const result = await searchHistory({
      q: params.get("q") || undefined,
      agent: requestedAgent,
      channel: params.get("channel") || undefined,
      author: params.get("author") || undefined,
      mentionsAgent,
      dateFrom: params.get("dateFrom") || undefined,
      dateTo: params.get("dateTo") || undefined,
      allowedAgents: allowed,
      page: parseInt(params.get("page") || "1", 10),
      pageSize: parseInt(params.get("pageSize") || "25", 10),
    });
    return NextResponse.json(result);
  } catch (error) {
    return NextResponse.json(
      { error: `Search failed: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
