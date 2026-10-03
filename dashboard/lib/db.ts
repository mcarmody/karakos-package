/**
 * Database access utilities. Every database is opened read-only except the
 * dashboard-owned one.
 */

import { join } from "path";
import { homedir } from "os";

export const WORKSPACE_ROOT = process.env.WORKSPACE_ROOT || join(homedir(), ".karakos/workspace");
// The agent-server's database (messages, turns, memory graph). Set
// AGENT_SERVER_DB_PATH to point at it; the default matches a bare-metal layout.
export const AGENT_SERVER_DB =
  process.env.AGENT_SERVER_DB_PATH || join(homedir(), ".karakos/data/agent-server.db");
// Dashboard-owned: no other process writes here. First table:
// push_subscriptions (lib/push-subscriptions.ts).
export const DASHBOARD_DB = join(WORKSPACE_ROOT, "data/dashboard.db");

/**
 * Open a sqlite database for reading.
 * Lazy-loads the sqlite/sqlite3 modules to keep cold-start overhead low.
 */
export async function openDb(dbPath: string) {
  const sqlite3 = await import("sqlite3").then((m) => m.default);
  const { open } = await import("sqlite");
  const readOnly = dbPath !== DASHBOARD_DB;
  return open({
    filename: dbPath,
    driver: sqlite3.Database,
    ...(readOnly ? { mode: sqlite3.OPEN_READONLY } : {}),
  });
}
