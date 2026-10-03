import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { getAgentStatusAdapter } from "@/lib/agentAdapter";

// Thin caller of the roster adapter (lib/agentAdapter.ts): the agent-server
// /agents plus the registry file.
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value || "";
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  // Per-account agent allowlist (lib/permissions.ts): restricted accounts
  // only ever see their own agents in the roster. This is what trims the
  // chat page's agent picker too — it derives its list from this route.
  const perms = permissionsFor(sessionUser(session));

  try {
    const adapter = await getAgentStatusAdapter();
    const agents = await adapter.listAgents((name) => agentAllowed(perms, name));
    return NextResponse.json({ agents });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to fetch agents" },
      { status: 500 }
    );
  }
}
