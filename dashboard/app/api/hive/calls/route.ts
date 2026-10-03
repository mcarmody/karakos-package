import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { fetchHiveCalls, parseHiveFilter } from "@/lib/packageBackend";

// GET /hive/calls, validated passthrough (package profile). `question` and
// `answer` carry message text, so a call is returned only when the account
// may see both agents.
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value || "";
  if (!isAuthenticated(session)) return unauthorizedResponse();
  const perms = permissionsFor(sessionUser(session));

  const filter = parseHiveFilter(request.nextUrl.searchParams);
  if (!filter.ok) return NextResponse.json({ error: filter.error }, { status: 400 });

  const result = await fetchHiveCalls(filter.query);
  if (!result.ok) return NextResponse.json({ error: "agent-server unavailable" }, { status: 502 });

  const calls = (Array.isArray(result.data.calls) ? result.data.calls : []).filter(
    (c) => agentAllowed(perms, c.from_agent) && agentAllowed(perms, c.to_agent)
  );
  return NextResponse.json({ calls });
}
