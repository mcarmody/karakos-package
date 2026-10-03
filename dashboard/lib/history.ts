/**
 * Searchable per-agent chat history (Task 8be6aa80).
 *
 * Reads `message_queue` (each row already holds both the inbound `content`
 * and the agent's `response`) plus `turn_events` (the inside-the-turn
 * detail for a given message_id) out of AGENT_SERVER_DB — the SAME sqlite
 * file bin/claude-agent-server.py writes to continuously — or a periodic mirror of it,
 * which may be REPLACED wholesale; nothing here may assume the file persists.
 *
 * Hard constraints:
 *  - Every connection here is opened OPEN_READONLY. This module must never
 *    take a write lock on the live file except the one-time, idempotent FTS
 *    index bootstrap (ensureFtsIndex), which is intentionally a SEPARATE
 *    connection opened read-write, does its work in milliseconds, and is
 *    never held open across a request.
 *  - No ALTER of message_queue / turn_events — the FTS5 table and its
 *    triggers are new, sibling objects. claude-agent-server.py's schema is
 *    untouched.
 *  - No export/download of query results, and nothing here logs message
 *    content server-side (message content may quote tokens/credentials).
 */

import { AGENT_SERVER_DB } from "@/lib/db";

const FTS_TABLE = "message_queue_fts";

/** In-flight bootstrap, so concurrent cold-start searches share one attempt.
 * Deliberately NOT a "done forever" memo: on the desktop AGENT_SERVER_DB is
 * a mirror (bin/mirror-agent-server-db.sh) that is replaced wholesale every
 * few minutes, so a table this process created can vanish between requests.
 * searchHistory checks sqlite_master on its own read-only connection every
 * call and only comes here when the table is actually missing. */
let ftsBootstrapInFlight: Promise<boolean> | null = null;

async function ftsTablePresent(db: { get: Function }): Promise<boolean> {
  const row = await db.get(
    `SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?`,
    FTS_TABLE
  );
  return !!row;
}

async function openReadonly(dbPath: string) {
  const sqlite3 = await import("sqlite3").then((m) => m.default);
  const { open } = await import("sqlite");
  return open({
    filename: dbPath,
    driver: sqlite3.Database,
    mode: sqlite3.OPEN_READONLY,
  });
}

async function openReadWrite(dbPath: string) {
  const sqlite3 = await import("sqlite3").then((m) => m.default);
  const { open } = await import("sqlite");
  return open({
    filename: dbPath,
    driver: sqlite3.Database,
    mode: sqlite3.OPEN_READWRITE,
  });
}

/**
 * Idempotently create the external-content FTS5 index over
 * message_queue(content, response) plus the triggers that keep it current,
 * and populate it once via the built-in 'rebuild' command. Safe to call on
 * every cold start — every statement is IF NOT EXISTS, and once the table
 * exists this is a single fast sqlite_master lookup.
 *
 * Opens its own short-lived read-write connection distinct from the
 * read-only connections search/detail use, and closes it immediately after.
 * Never called mid-request from the search path itself.
 */
export async function ensureFtsIndex(): Promise<boolean> {
  if (ftsBootstrapInFlight) return ftsBootstrapInFlight;
  ftsBootstrapInFlight = (async () => {
    let db;
    try {
      db = await openReadWrite(AGENT_SERVER_DB);
      if (!(await ftsTablePresent(db))) {
        await db.exec(
          `CREATE VIRTUAL TABLE IF NOT EXISTS ${FTS_TABLE} USING fts5(
             content, response,
             content='message_queue', content_rowid='id',
             tokenize='unicode61'
           )`
        );
        await db.exec(
          `CREATE TRIGGER IF NOT EXISTS message_queue_fts_ai AFTER INSERT ON message_queue BEGIN
             INSERT INTO ${FTS_TABLE}(rowid, content, response) VALUES (new.id, new.content, new.response);
           END`
        );
        await db.exec(
          `CREATE TRIGGER IF NOT EXISTS message_queue_fts_ad AFTER DELETE ON message_queue BEGIN
             INSERT INTO ${FTS_TABLE}(${FTS_TABLE}, rowid, content, response) VALUES('delete', old.id, old.content, old.response);
           END`
        );
        await db.exec(
          `CREATE TRIGGER IF NOT EXISTS message_queue_fts_au AFTER UPDATE ON message_queue BEGIN
             INSERT INTO ${FTS_TABLE}(${FTS_TABLE}, rowid, content, response) VALUES('delete', old.id, old.content, old.response);
             INSERT INTO ${FTS_TABLE}(rowid, content, response) VALUES (new.id, new.content, new.response);
           END`
        );
        // One-time backfill of existing rows via FTS5's built-in rebuild
        // command (reads message_queue as the external-content source).
        await db.exec(`INSERT INTO ${FTS_TABLE}(${FTS_TABLE}) VALUES('rebuild')`);
      }
      return true;
    } catch (err) {
      // Live file under contention, or FTS5 unavailable in this sqlite3
      // build — callers fall back to a plain LIKE scan rather than fail
      // the whole page.
      console.error("history: FTS index bootstrap failed, falling back to LIKE scan", err);
      return false;
    } finally {
      await db?.close().catch(() => {});
      ftsBootstrapInFlight = null;
    }
  })();
  return ftsBootstrapInFlight;
}

export interface HistoryFilters {
  q?: string;
  agent?: string;
  channel?: string;
  author?: string;
  mentionsAgent?: boolean;
  dateFrom?: string; // YYYY-MM-DD
  dateTo?: string; // YYYY-MM-DD
  /** Restrict to this set of agents (session's allowlist), or "*" for all. */
  allowedAgents: "*" | string[];
  page: number;
  pageSize: number;
}

export interface HistoryRow {
  id: number;
  messageId: string;
  agent: string;
  channel: string;
  author: string;
  createdAt: string;
  mentionsAgent: boolean;
  contentSnippet: string;
  responseSnippet: string | null;
  hasResponse: boolean;
}

export interface HistoryResult {
  rows: HistoryRow[];
  total: number;
  page: number;
  pageSize: number;
  ftsActive: boolean;
}

const MAX_PAGE_SIZE = 50;

/** Turn free-text search input into a safe FTS5 MATCH expression: each
 * whitespace-separated token is quoted as a literal string (doubling any
 * embedded quotes), then implicitly AND-ed by FTS5's default MATCH syntax.
 * This deliberately gives up FTS5's query-language features (NEAR, OR,
 * column filters, prefix `*`) in exchange for never throwing a syntax
 * error on arbitrary chat content ("it's", quotes, hyphens, etc). */
function toFtsQuery(q: string): string {
  return q
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((tok) => `"${tok.replace(/"/g, '""')}"`)
    .join(" ");
}

function agentFilterClause(
  allowedAgents: "*" | string[],
  agent: string | undefined,
  params: unknown[]
): string {
  const clauses: string[] = [];
  if (agent) {
    clauses.push("mq.agent = ?");
    params.push(agent);
  }
  if (allowedAgents !== "*") {
    if (allowedAgents.length === 0) {
      clauses.push("0"); // no agents allowed → no rows
    } else {
      clauses.push(`mq.agent IN (${allowedAgents.map(() => "?").join(",")})`);
      params.push(...allowedAgents);
    }
  }
  return clauses.length ? clauses.map((c) => `AND ${c}`).join(" ") : "";
}

function commonFilterClause(f: HistoryFilters, params: unknown[]): string {
  const clauses: string[] = [];
  if (f.channel) {
    clauses.push("mq.channel = ?");
    params.push(f.channel);
  }
  if (f.author) {
    clauses.push("mq.author LIKE ? ESCAPE '\\'");
    params.push(`%${f.author.replace(/[\\%_]/g, "\\$&")}%`);
  }
  if (f.mentionsAgent !== undefined) {
    clauses.push("mq.mentions_agent = ?");
    params.push(f.mentionsAgent ? 1 : 0);
  }
  if (f.dateFrom) {
    clauses.push("mq.created_at >= ?");
    params.push(`${f.dateFrom} 00:00:00`);
  }
  if (f.dateTo) {
    clauses.push("mq.created_at <= ?");
    params.push(`${f.dateTo} 23:59:59`);
  }
  return clauses.length ? clauses.map((c) => `AND ${c}`).join(" ") : "";
}

function snippet(text: string | null, needle: string | undefined, max = 200): string {
  if (!text) return "";
  const clean = text.replace(/\s+/g, " ").trim();
  if (!needle) return clean.length > max ? clean.slice(0, max) + "…" : clean;
  const idx = clean.toLowerCase().indexOf(needle.toLowerCase());
  if (idx === -1) return clean.length > max ? clean.slice(0, max) + "…" : clean;
  const start = Math.max(0, idx - max / 2);
  const end = Math.min(clean.length, idx + needle.length + max / 2);
  return (start > 0 ? "…" : "") + clean.slice(start, end) + (end < clean.length ? "…" : "");
}

/**
 * Search message_queue (optionally via the FTS5 index) with filters,
 * paginated. Opens a fresh read-only connection per call and closes it
 * before returning — no transaction spans the request.
 */
export async function searchHistory(f: HistoryFilters): Promise<HistoryResult> {
  const pageSize = Math.max(1, Math.min(MAX_PAGE_SIZE, f.pageSize || 25));
  const page = Math.max(1, f.page || 1);
  const offset = (page - 1) * pageSize;

  const db = await openReadonly(AGENT_SERVER_DB);
  try {
    // Check the index exists on THIS connection, every call — the file under
    // us may have been swapped for a fresh mirror since the last request.
    let ftsActive = false;
    if (f.q) {
      ftsActive = (await ftsTablePresent(db)) || (await ensureFtsIndex());
    }
    let rows: Array<{
      id: number;
      message_id: string;
      agent: string;
      channel: string;
      author: string;
      created_at: string;
      mentions_agent: number;
      content: string;
      response: string | null;
    }>;
    let total: number;

    if (f.q && ftsActive) {
      const matchParams: unknown[] = [toFtsQuery(f.q)];
      const filterParams: unknown[] = [];
      const agentClause = agentFilterClause(f.allowedAgents, f.agent, filterParams);
      const commonClause = commonFilterClause(f, filterParams);
      const where = `WHERE ${FTS_TABLE} MATCH ? ${agentClause} ${commonClause}`;

      const countRow = await db.get<{ c: number }>(
        `SELECT COUNT(*) AS c FROM ${FTS_TABLE} JOIN message_queue mq ON mq.id = ${FTS_TABLE}.rowid ${where}`,
        ...matchParams,
        ...filterParams
      );
      total = countRow?.c ?? 0;

      rows = await db.all(
        `SELECT mq.id, mq.message_id, mq.agent, mq.channel, mq.author, mq.created_at,
                mq.mentions_agent, mq.content, mq.response
         FROM ${FTS_TABLE}
         JOIN message_queue mq ON mq.id = ${FTS_TABLE}.rowid
         ${where}
         ORDER BY mq.created_at DESC
         LIMIT ? OFFSET ?`,
        ...matchParams,
        ...filterParams,
        pageSize,
        offset
      );
    } else {
      // No search text (or FTS unavailable) — filters alone, or an
      // unfiltered LIKE fallback if a query string was given but the FTS
      // index couldn't be built this process.
      const params: unknown[] = [];
      let likeClause = "";
      if (f.q) {
        likeClause = "AND (mq.content LIKE ? ESCAPE '\\' OR mq.response LIKE ? ESCAPE '\\')";
        const esc = `%${f.q.replace(/[\\%_]/g, "\\$&")}%`;
        params.push(esc, esc);
      }
      const agentClause = agentFilterClause(f.allowedAgents, f.agent, params);
      const commonClause = commonFilterClause(f, params);
      const where = `WHERE 1=1 ${likeClause} ${agentClause} ${commonClause}`;

      const countRow = await db.get<{ c: number }>(
        `SELECT COUNT(*) AS c FROM message_queue mq ${where}`,
        ...params
      );
      total = countRow?.c ?? 0;

      rows = await db.all(
        `SELECT mq.id, mq.message_id, mq.agent, mq.channel, mq.author, mq.created_at,
                mq.mentions_agent, mq.content, mq.response
         FROM message_queue mq
         ${where}
         ORDER BY mq.created_at DESC
         LIMIT ? OFFSET ?`,
        ...params,
        pageSize,
        offset
      );
    }

    return {
      rows: rows.map((r) => ({
        id: r.id,
        messageId: r.message_id,
        agent: r.agent,
        channel: r.channel,
        author: r.author,
        createdAt: r.created_at,
        mentionsAgent: !!r.mentions_agent,
        contentSnippet: snippet(r.content, f.q),
        responseSnippet: r.response ? snippet(r.response, f.q) : null,
        hasResponse: !!r.response,
      })),
      total,
      page,
      pageSize,
      ftsActive: !!(f.q && ftsActive),
    };
  } finally {
    await db.close();
  }
}

export interface HistoryDetail {
  id: number;
  messageId: string;
  agent: string;
  channel: string;
  channelId: string;
  author: string;
  authorId: string;
  createdAt: string;
  postedAt: string | null;
  mentionsAgent: boolean;
  content: string;
  response: string | null;
  turnEvents: Array<{ seq: number; kind: string; content: string; createdAt: string }>;
}

/** Full exchange plus turn_events for one message_id, for the expand/deep-link view. */
export async function getHistoryDetail(
  messageId: string,
  allowedAgents: "*" | string[]
): Promise<HistoryDetail | null | "forbidden"> {
  const db = await openReadonly(AGENT_SERVER_DB);
  try {
    const row = await db.get<{
      id: number;
      message_id: string;
      agent: string;
      channel: string;
      channel_id: string;
      author: string;
      author_id: string;
      created_at: string;
      posted_at: string | null;
      mentions_agent: number;
      content: string;
      response: string | null;
    }>(
      `SELECT id, message_id, agent, channel, channel_id, author, author_id,
              created_at, posted_at, mentions_agent, content, response
       FROM message_queue WHERE message_id = ?`,
      messageId
    );
    if (!row) return null;
    if (allowedAgents !== "*" && !allowedAgents.includes(row.agent)) return "forbidden";

    const events = await db.all<
      Array<{ seq: number; kind: string; content: string; created_at: string }>
    >(
      `SELECT seq, kind, content, created_at FROM turn_events
       WHERE message_id = ? ORDER BY seq ASC`,
      messageId
    );

    return {
      id: row.id,
      messageId: row.message_id,
      agent: row.agent,
      channel: row.channel,
      channelId: row.channel_id,
      author: row.author,
      authorId: row.author_id,
      createdAt: row.created_at,
      postedAt: row.posted_at,
      mentionsAgent: !!row.mentions_agent,
      content: row.content,
      response: row.response,
      turnEvents: events.map((e) => ({
        seq: e.seq,
        kind: e.kind,
        content: e.content,
        createdAt: e.created_at,
      })),
    };
  } finally {
    await db.close();
  }
}

/** Distinct agents seen in the data, restricted to the caller's allowlist.
 * Derived at runtime per spec — never hardcode the agent list. */
export async function listAgents(allowedAgents: "*" | string[]): Promise<string[]> {
  const db = await openReadonly(AGENT_SERVER_DB);
  try {
    const rows = await db.all<Array<{ agent: string }>>(
      `SELECT DISTINCT agent FROM message_queue ORDER BY agent ASC`
    );
    const all = rows.map((r) => r.agent);
    return allowedAgents === "*" ? all : all.filter((a) => allowedAgents.includes(a));
  } finally {
    await db.close();
  }
}

/** Distinct channels seen in the data (for the filter dropdown). */
export async function listChannels(): Promise<string[]> {
  const db = await openReadonly(AGENT_SERVER_DB);
  try {
    const rows = await db.all<Array<{ channel: string }>>(
      `SELECT DISTINCT channel FROM message_queue ORDER BY channel ASC`
    );
    return rows.map((r) => r.channel);
  } finally {
    await db.close();
  }
}
