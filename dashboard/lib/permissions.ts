/**
 * Per-account dashboard permissions.
 *
 * Every dashboard account resolves to an AccountPermissions record:
 *
 *   - `pages`  — which URL path prefixes (pages AND /api routes) the account
 *                may reach. Middleware enforces this server-side; nav
 *                components read the same list via /api/whoami as a courtesy
 *                so the UI doesn't render links that bounce.
 *   - `agents` — which agents the account may talk to / see. Enforced in
 *                /api/agents, /api/chat*, and the /api/agents/[name]/* ops
 *                routes. UI hiding alone is never the boundary.
 *   - `home`   — where the account lands after login.
 *
 * Resolution: the DASHBOARD_PERMISSIONS env (JSON map of username -> partial
 * record) overrides the default, which is unrestricted, so the primary
 * DASHBOARD_USER account works with zero config.
 *
 * This module is dependency-free and pure so it can be imported from
 * middleware (Node runtime), API routes, tests, and client components alike.
 */

export type PageAllowlist = "*" | string[];
export type AgentAllowlist = "*" | string[];

export interface AccountPermissions {
  /** Canonical (lower-cased) dashboard username this record belongs to. */
  account: string;
  pages: PageAllowlist;
  agents: AgentAllowlist;
  /** Where the account lands after login / when bounced off a denied page. */
  home: string;
}

/**
 * Account-hygiene paths every *authenticated* account gets regardless of its
 * page allowlist: login/logout, whoami, push subscribe.
 */
export const ALWAYS_ALLOWED_PATHS = ["/api/auth", "/api/whoami", "/login", "/api/push"];

function unrestricted(account: string): AccountPermissions {
  return { account, pages: "*", agents: "*", home: "/" };
}

interface PermissionsOverride {
  pages?: PageAllowlist;
  agents?: AgentAllowlist;
  home?: string;
}

function envOverrides(): Record<string, PermissionsOverride> {
  const raw = process.env.DASHBOARD_PERMISSIONS;
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, PermissionsOverride>;
    }
  } catch {
    // Malformed JSON is ignored: the account stays on the default.
  }
  return {};
}

function normalizeList(list: unknown): string[] | null {
  if (!Array.isArray(list)) return null;
  return list.map((x) => String(x)).filter(Boolean);
}

/**
 * Resolve a dashboard username to its permissions. Never throws; unknown or
 * missing usernames resolve to unrestricted.
 */
export function permissionsFor(username: string | null | undefined): AccountPermissions {
  const account = (username || "").trim().toLowerCase();
  const base = unrestricted(account);
  if (!account) return base;

  const override = envOverrides()[account];
  if (!override || typeof override !== "object") return base;

  const pages = override.pages === "*" ? "*" : normalizeList(override.pages) ?? base.pages;
  const agents = override.agents === "*" ? "*" : normalizeList(override.agents) ?? base.agents;
  const home = typeof override.home === "string" && override.home ? override.home : base.home;
  return { account, pages, agents, home };
}

/**
 * May this account reach `pathname`? Prefix semantics. Always-allowed hygiene
 * paths pass for every authenticated account.
 */
export function canAccessPath(perms: AccountPermissions, pathname: string): boolean {
  if (perms.pages === "*") return true;
  return [...ALWAYS_ALLOWED_PATHS, ...perms.pages].some((p) => pathname.startsWith(p));
}

/**
 * Client-side convenience for nav components: same prefix semantics as
 * canAccessPath, but takes the raw `pages` value /api/whoami returns.
 * null/undefined (whoami not loaded yet) errs open — this is UI courtesy,
 * middleware is the boundary.
 */
export function pageAllowed(pages: PageAllowlist | null | undefined, href: string): boolean {
  if (!pages || pages === "*") return true;
  return [...ALWAYS_ALLOWED_PATHS, ...pages].some((p) => href.startsWith(p));
}

/** May this account see / talk to / operate on agent `name`? */
export function agentAllowed(perms: AccountPermissions, name: string): boolean {
  if (perms.agents === "*") return true;
  if (!name) return false;
  return perms.agents.includes(name);
}

/** The agent allowlist — for SQL filters and UI lists. */
export function allowedAgents(perms: AccountPermissions): "*" | string[] {
  return perms.agents === "*" ? "*" : [...perms.agents];
}

/**
 * Where the account lands after login. A misconfigured home outside the
 * account's own allowlist would make the middleware bounce redirect-loop;
 * fail safe to /login (public) instead.
 */
export function homePathFor(perms: AccountPermissions): string {
  return canAccessPath(perms, perms.home) ? perms.home : "/login";
}
