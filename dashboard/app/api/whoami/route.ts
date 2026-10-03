import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { allowedAgents, permissionsFor } from "@/lib/permissions";

export const dynamic = "force-dynamic";

/**
 * Who is holding this session, and what may they see.
 *
 * The permissions model lives on the server only; the nav asks rather than
 * keeping a copy that drifts. This is presentation data — middleware and the
 * chat/agents routes do the actual enforcing, so a lie here would hide
 * links, not grant access.
 *
 * `pages`/`agents` are either "*" or an allowlist.
 */
export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) return unauthorizedResponse();

  const user = sessionUser(session);
  const perms = permissionsFor(user);

  return NextResponse.json({
    user,
    pages: perms.pages,
    agents: allowedAgents(perms),
    unrestricted: perms.pages === "*" && perms.agents === "*",
    home: perms.home,
  });
}
