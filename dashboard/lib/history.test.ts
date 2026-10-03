import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import Database from "better-sqlite3";

// Same module-reset dance as app/api/chat/history/route.test.ts — AGENT_SERVER_DB
// is read at module-load time in lib/db.ts, so env has to be set before the
// dynamic import of lib/history.
const ENV_KEYS = ["AGENT_SERVER_DB_PATH"] as const;
let saved: Record<string, string | undefined>;
let tempDir: string;
let dbPath: string;
let historyMod: typeof import("./history");

function seedSchema() {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE message_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      agent TEXT NOT NULL,
      channel TEXT NOT NULL,
      channel_id TEXT NOT NULL,
      server TEXT DEFAULT 'karakos',
      author TEXT NOT NULL,
      author_id TEXT NOT NULL,
      is_bot INTEGER DEFAULT 0,
      content TEXT NOT NULL,
      message_id TEXT NOT NULL UNIQUE,
      mentions_agent INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now')),
      processed INTEGER DEFAULT 0,
      response TEXT DEFAULT NULL,
      discord_response_id TEXT DEFAULT NULL,
      posted_at TEXT DEFAULT NULL,
      attachments TEXT DEFAULT NULL,
      typing_channel_id TEXT DEFAULT NULL
    );
    CREATE TABLE turn_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      kind TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  `);
  db.close();
}

interface Row {
  agent: string;
  channel?: string;
  channel_id?: string;
  author?: string;
  author_id?: string;
  content: string;
  response?: string | null;
  message_id: string;
  mentions_agent?: number;
  created_at?: string;
}

function seedMessages(rows: Row[]) {
  const db = new Database(dbPath);
  const insert = db.prepare(
    `INSERT INTO message_queue
       (agent, channel, channel_id, author, author_id, content, response, message_id, mentions_agent, created_at)
     VALUES (@agent, @channel, @channel_id, @author, @author_id, @content, @response, @message_id, @mentions_agent, @created_at)`
  );
  for (const row of rows) {
    insert.run({
      channel: "general",
      channel_id: "c1",
      author: "tester",
      author_id: "u1",
      response: null,
      mentions_agent: 0,
      created_at: "2026-09-01 12:00:00",
      ...row,
    });
  }
  db.close();
}

function seedTurnEvents(messageId: string, events: { seq: number; kind: string; content: string }[]) {
  const db = new Database(dbPath);
  const insert = db.prepare(
    `INSERT INTO turn_events (message_id, seq, kind, content) VALUES (?, ?, ?, ?)`
  );
  for (const e of events) insert.run(messageId, e.seq, e.kind, e.content);
  db.close();
}

beforeEach(async () => {
  saved = {};
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
  tempDir = mkdtempSync(join(tmpdir(), "karakos-history-test-"));
  dbPath = join(tempDir, "agent-server.db");
  seedSchema();
  process.env.AGENT_SERVER_DB_PATH = dbPath;

  // AGENT_SERVER_DB (lib/db.ts) and this module's FTS-ready memo are both
  // read/set at module-load time, so each test needs a fresh module graph
  // pointed at its own temp DB — otherwise the second test's queries hit the
  // first test's (now-deleted) database file.
  vi.resetModules();
  historyMod = await import("./history");
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  rmSync(tempDir, { recursive: true, force: true });
});

describe("searchHistory", () => {
  it("finds matches in content and in response via FTS", async () => {
    seedMessages([
      { agent: "alpha", content: "please water the ledger", message_id: "m1" },
      { agent: "alpha", content: "unrelated", response: "watered the ledger already", message_id: "m2" },
      { agent: "alpha", content: "totally different topic", message_id: "m3" },
    ]);

    const result = await historyMod.searchHistory({
      q: "ledger",
      allowedAgents: "*",
      page: 1,
      pageSize: 25,
    });

    expect(result.ftsActive).toBe(true);
    expect(result.total).toBe(2);
    expect(result.rows.map((r) => r.messageId).sort()).toEqual(["m1", "m2"]);
  });

  it("filters by agent as the primary axis", async () => {
    seedMessages([
      { agent: "alpha", content: "ledger update", message_id: "m1" },
      { agent: "beta", content: "ledger update too", message_id: "m2" },
    ]);

    const result = await historyMod.searchHistory({
      q: "ledger",
      agent: "beta",
      allowedAgents: "*",
      page: 1,
      pageSize: 25,
    });

    expect(result.rows).toHaveLength(1);
    expect(result.rows[0].agent).toBe("beta");
  });

  it("respects the caller's agent allowlist even without an explicit agent filter", async () => {
    seedMessages([
      { agent: "alpha", content: "ledger update", message_id: "m1" },
      { agent: "gamma", content: "ledger update too", message_id: "m2" },
    ]);

    const result = await historyMod.searchHistory({
      q: "ledger",
      allowedAgents: ["gamma"],
      page: 1,
      pageSize: 25,
    });

    expect(result.rows).toHaveLength(1);
    expect(result.rows[0].agent).toBe("gamma");
  });

  it("supports channel, author, mentions_agent and date-range filters without a query", async () => {
    seedMessages([
      {
        agent: "alpha",
        content: "no query needed",
        message_id: "m1",
        channel: "ops",
        author: "gamma",
        mentions_agent: 1,
        created_at: "2026-09-05 09:00:00",
      },
      {
        agent: "alpha",
        content: "different channel",
        message_id: "m2",
        channel: "general",
        author: "tester",
        mentions_agent: 0,
        created_at: "2026-01-01 09:00:00",
      },
    ]);

    const result = await historyMod.searchHistory({
      channel: "ops",
      author: "gam",
      mentionsAgent: true,
      dateFrom: "2026-09-01",
      dateTo: "2026-09-10",
      allowedAgents: "*",
      page: 1,
      pageSize: 25,
    });

    expect(result.rows.map((r) => r.messageId)).toEqual(["m1"]);
  });

  it("paginates without loading everything at once", async () => {
    seedMessages(
      Array.from({ length: 30 }, (_, i) => ({
        agent: "alpha",
        content: `message number ${i}`,
        message_id: `m${i}`,
        created_at: `2026-09-01 ${String(10 + Math.floor(i / 60)).padStart(2, "0")}:${String(i).padStart(2, "0")}:00`,
      }))
    );

    const page1 = await historyMod.searchHistory({ allowedAgents: "*", page: 1, pageSize: 10 });
    const page2 = await historyMod.searchHistory({ allowedAgents: "*", page: 2, pageSize: 10 });

    expect(page1.rows).toHaveLength(10);
    expect(page2.rows).toHaveLength(10);
    expect(page1.total).toBe(30);
    expect(page1.rows[0].messageId).not.toBe(page2.rows[0].messageId);
  });

  it("survives the DB file being replaced by a fresh mirror between searches", async () => {
    // On the desktop AGENT_SERVER_DB is bin/mirror-agent-server-db.sh's
    // output, swapped wholesale every 5 minutes. A process-lifetime "FTS is
    // built" memo would then MATCH against a table that no longer exists.
    seedMessages([{ agent: "alpha", content: "first snapshot ledger", message_id: "m1" }]);
    const first = await historyMod.searchHistory({ q: "ledger", allowedAgents: "*", page: 1, pageSize: 25 });
    expect(first.ftsActive).toBe(true);

    rmSync(dbPath);
    seedSchema();
    seedMessages([{ agent: "alpha", content: "second snapshot ledger", message_id: "m9" }]);

    const second = await historyMod.searchHistory({ q: "ledger", allowedAgents: "*", page: 1, pageSize: 25 });
    expect(second.ftsActive).toBe(true);
    expect(second.rows.map((r) => r.messageId)).toEqual(["m9"]);
  });

  it("handles FTS-hostile query text (quotes, hyphens) without throwing", async () => {
    seedMessages([{ agent: "alpha", content: `it's "test" run-through`, message_id: "m1" }]);

    await expect(
      historyMod.searchHistory({ q: `it's "test" run-through`, allowedAgents: "*", page: 1, pageSize: 25 })
    ).resolves.toBeDefined();
  });
});

describe("getHistoryDetail", () => {
  it("returns the full exchange plus ordered turn_events", async () => {
    seedMessages([{ agent: "alpha", content: "do the thing", response: "done", message_id: "m1" }]);
    seedTurnEvents("m1", [
      { seq: 2, kind: "tool_call", content: "ran a tool" },
      { seq: 1, kind: "text", content: "thinking out loud" },
    ]);

    const detail = await historyMod.getHistoryDetail("m1", "*");
    expect(detail).not.toBeNull();
    expect(detail).not.toBe("forbidden");
    if (detail && detail !== "forbidden") {
      expect(detail.content).toBe("do the thing");
      expect(detail.response).toBe("done");
      expect(detail.turnEvents.map((e) => e.seq)).toEqual([1, 2]);
    }
  });

  it("returns null for an unknown message_id", async () => {
    const detail = await historyMod.getHistoryDetail("does-not-exist", "*");
    expect(detail).toBeNull();
  });

  it("returns 'forbidden' when the agent is outside the caller's allowlist", async () => {
    seedMessages([{ agent: "gamma", content: "private", message_id: "m1" }]);
    const detail = await historyMod.getHistoryDetail("m1", ["alpha"]);
    expect(detail).toBe("forbidden");
  });
});

describe("listAgents / listChannels", () => {
  it("derives agents from the data, restricted to the allowlist", async () => {
    seedMessages([
      { agent: "alpha", content: "a", message_id: "m1" },
      { agent: "beta", content: "b", message_id: "m2" },
      { agent: "gamma", content: "c", message_id: "m3" },
    ]);

    const all = await historyMod.listAgents("*");
    expect(all.sort()).toEqual(["alpha", "beta", "gamma"]);

    const restricted = await historyMod.listAgents(["alpha"]);
    expect(restricted).toEqual(["alpha"]);
  });

  it("lists distinct channels", async () => {
    seedMessages([
      { agent: "alpha", content: "a", message_id: "m1", channel: "ops" },
      { agent: "alpha", content: "b", message_id: "m2", channel: "general" },
    ]);
    const channels = await historyMod.listChannels();
    expect(channels.sort()).toEqual(["general", "ops"]);
  });
});
