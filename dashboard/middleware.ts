import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser } from "@/lib/api";
import { agentAllowed, canAccessPath, homePathFor, permissionsFor } from "@/lib/permissions";

// The PWA plumbing is public: the matcher below excludes _next/static but
// nothing else under public/ actually lives at "/public/*" (Next serves
// public/ files at the root), so /sw.js, /manifest.webmanifest, /offline.html
// and /icons/* would otherwise fall through to the redirect-to-/login branch.
// That breaks SW registration pre-login, corrupts the offline-page cache entry
// and 404s every manifest icon in an unauthenticated tab. None of it is data.
const PUBLIC_PATHS = ["/login", "/api/auth", "/sw.js", "/manifest.webmanifest", "/offline.html", "/icons"];

/**
 * Per-account confinement.
 *
 * Each account resolves to a page allowlist and an agent allowlist
 * (lib/permissions.ts). Accounts are unrestricted unless
 * DASHBOARD_PERMISSIONS says otherwise.
 *
 * Enforced here rather than by hiding nav links, because a hidden link is
 * decoration: /costs is still one typed URL away. The agent-ops routes
 * (/api/agents/<name>/...) additionally check the *agent* allowlist here so
 * a page grant on /api/agents can never become "interrupt any agent".
 * Body-carried agent targets (/api/chat) are checked in their own routes,
 * where the body is readable.
 */

/** /api/agents/<name>[/op] — the segment middleware can enforce per-agent. */
const AGENT_OPS_RE = /^\/api\/agents\/([^/]+)/;

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Let public paths through
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  const session = request.cookies.get("karakos_session")?.value;

  // API routes: return 401 if not authenticated (don't redirect)
  if (pathname.startsWith("/api/")) {
    if (!isAuthenticated(session)) {
      return new NextResponse(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }
    const perms = permissionsFor(sessionUser(session));
    // Confined accounts get a hard 403 on every API outside their allowlist.
    if (!canAccessPath(perms, pathname)) {
      return forbidden();
    }
    // Agent-ops routes carry the target agent in the path — enforce the
    // agent allowlist too (page allowlist alone would let a /api/agents
    // grant reach /api/agents/<someone-else>/interrupt).
    const agentMatch = pathname.match(AGENT_OPS_RE);
    if (agentMatch && !agentAllowed(perms, decodeURIComponent(agentMatch[1]))) {
      return forbidden();
    }
    return NextResponse.next();
  }

  // Pages: redirect to /login if not authenticated
  if (!isAuthenticated(session)) {
    return NextResponse.redirect(publicUrl(request, "/login"));
  }

  // Confined accounts land on their home page and cannot navigate off
  // their allowlist.
  const perms = permissionsFor(sessionUser(session));
  if (!canAccessPath(perms, pathname)) {
    return NextResponse.redirect(publicUrl(request, homePathFor(perms)));
  }

  return NextResponse.next();
}

/**
 * Absolute URL on the host the browser actually used. request.url reflects
 * whatever Host nginx forwards, which is the public host only if the proxy
 * passes it; honouring x-forwarded-host/proto keeps /login redirects on the
 * same hostname either way.
 */
function publicUrl(request: NextRequest, path: string): URL {
  const host = request.headers.get("x-forwarded-host")?.split(",")[0].trim();
  if (!host) return new URL(path, request.url);
  const proto =
    request.headers.get("x-forwarded-proto")?.split(",")[0].trim() ||
    request.nextUrl.protocol.replace(":", "");
  return new URL(path, `${proto}://${host}`);
}

function forbidden(): NextResponse {
  return new NextResponse(JSON.stringify({ error: "Forbidden" }), {
    status: 403,
    headers: { "Content-Type": "application/json" },
  });
}

export const config = {
  // Node.js runtime so the HMAC auth check (Node `crypto`) works.
  runtime: "nodejs",
  matcher: ["/((?!_next/static|_next/image|favicon.ico|public/).*)"],
};
