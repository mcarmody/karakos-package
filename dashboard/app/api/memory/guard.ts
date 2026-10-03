import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { permissionsFor } from "@/lib/permissions";
import type { MemoryResult } from "@/lib/memoryBackend";

/**
 * Shared gate for /api/memory/* 401 without a session, 403 for an agent-restricted account (graph
 * rows are not partitioned by agent, so a confined account must not read
 * them). Returns a response to send, or null to carry on.
 */
export function memoryGuard(request: NextRequest): Response | null {
  const session = request.cookies.get("karakos_session")?.value || "";
  if (!isAuthenticated(session)) return unauthorizedResponse();
  const perms = permissionsFor(sessionUser(session));
  // Same predicate as /api/whoami `unrestricted` (the settings page flag).
  if (!(perms.pages === "*" && perms.agents === "*")) return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  return null;
}

/** Pass 400, 404 and 503 through with the server's body; anything else is a 502. */
export function memoryResponse<T>(result: MemoryResult<T>): NextResponse {
  if (result.ok) return NextResponse.json(result.body);
  if ([400, 404, 503].includes(result.status)) {
    return NextResponse.json(result.body ?? { error: result.error }, { status: result.status });
  }
  return NextResponse.json({ error: "agent-server unavailable" }, { status: 502 });
}
