/**
 * Push subscription data layer.
 *
 * Owns the push_subscriptions table in DASHBOARD_DB — one row per browser
 * that has opted into web push on a device (not per user; a shared install
 * can have several phones subscribed at once). endpoint is the natural
 * key: the browser hands back the same endpoint on re-subscribe, so a
 * duplicate opt-in updates the row instead of piling up dead rows.
 */

import Database from "better-sqlite3";
import { DASHBOARD_DB } from "@/lib/db";

export interface PushSubscriptionRow {
  id: number;
  endpoint: string;
  p256dh: string;
  auth: string;
  label: string | null;
  created_at: string;
}

let conn: Database.Database | null = null;

function db(): Database.Database {
  if (conn) return conn;
  const next = new Database(DASHBOARD_DB, { timeout: 5000 });
  next.pragma("journal_mode = WAL");
  next.exec(`
    CREATE TABLE IF NOT EXISTS push_subscriptions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      endpoint TEXT NOT NULL UNIQUE,
      p256dh TEXT NOT NULL,
      auth TEXT NOT NULL,
      label TEXT,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
  `);
  conn = next;
  return conn;
}

export interface SubscribeInput {
  endpoint: string;
  p256dh: string;
  auth: string;
  label?: string | null;
}

/**
 * Insert a subscription, or refresh keys/label if the endpoint already
 * exists (a browser re-subscribing after key rotation, or a plain re-opt-in).
 */
export function addSubscription(input: SubscribeInput): PushSubscriptionRow {
  db()
    .prepare(
      `INSERT INTO push_subscriptions (endpoint, p256dh, auth, label)
       VALUES (@endpoint, @p256dh, @auth, @label)
       ON CONFLICT(endpoint) DO UPDATE SET
         p256dh = excluded.p256dh,
         auth = excluded.auth,
         label = excluded.label`
    )
    .run({
      endpoint: input.endpoint,
      p256dh: input.p256dh,
      auth: input.auth,
      label: input.label ?? null,
    });
  return db()
    .prepare(`SELECT * FROM push_subscriptions WHERE endpoint = ?`)
    .get(input.endpoint) as PushSubscriptionRow;
}

/** Returns true if a row was actually removed. */
export function removeSubscription(endpoint: string): boolean {
  const result = db()
    .prepare(`DELETE FROM push_subscriptions WHERE endpoint = ?`)
    .run(endpoint);
  return result.changes > 0;
}

export function listSubscriptions(): PushSubscriptionRow[] {
  return db()
    .prepare(`SELECT * FROM push_subscriptions ORDER BY created_at DESC`)
    .all() as PushSubscriptionRow[];
}
