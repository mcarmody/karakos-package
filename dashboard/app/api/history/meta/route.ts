import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { allowedAgents, permissionsFor } from "@/lib/permissions";
import { listAgents, listChannels } from "@/lib/history";

/**
 * Filter-dropdown data for /history: the agent and channel lists, derived
 * from the live data at request time (agents are shard-provisioned and
 * change — never hardcode the list, per the build spec).
 */
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const perms = permissionsFor(sessionUser(session));
  const allowed = allowedAgents(perms);

  try {
    const [agents, channels] = await Promise.all([listAgents(allowed), listChannels()]);
    return NextResponse.json({ agents, channels });
  } catch (error) {
    return NextResponse.json(
      { error: `Failed to load filters: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
