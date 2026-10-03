import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { fetchUsage } from "@/lib/packageBackend";

// GET /usage passthrough (package profile). `windows`, `breaker` and
// `governor` are account facts and always returned; the per-agent `agents`
// map and `budgets` are trimmed to the account's agent allowlist.
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value || "";
  if (!isAuthenticated(session)) return unauthorizedResponse();
  const perms = permissionsFor(sessionUser(session));

  const result = await fetchUsage();
  if (!result.ok) return NextResponse.json({ error: "agent-server unavailable" }, { status: 502 });

  const body = result.data;
  const keep = <T,>(m: Record<string, T> | undefined): Record<string, T> | undefined =>
    m && Object.fromEntries(Object.entries(m).filter(([name]) => agentAllowed(perms, name)));
  return NextResponse.json({ ...body, agents: keep(body.agents), budgets: keep(body.budgets) });
}
