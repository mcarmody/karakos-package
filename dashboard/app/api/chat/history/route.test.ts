import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { NextRequest } from "next/server";
import Database from "better-sqlite3";

// Module-reset dance: both
// SESSION_SECRET (lib/api.ts) and AGENT_SERVER_DB (lib/db.ts) are read at
// module-load time, so env has to be in place before the dynamic import.
const ENV_KEYS = ["AGENT_SERVER_DB_PATH", "DASHBOARD_PERMISSIONS"] as const;
let saved: Record<string, string | undefined>;
let tempDir: string;
let dbPath: string;
let apiMod: typeof import("@/lib/api");
let GET: typeof import("./route").GET;

interface Row {
  message_id: string;
  content: string;
  response?: string | null;
  created_at: string;
  posted_at?: string | null;
  processed?: number;
  agent?: string;
}

function seed(rows: Row[]) {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE IF NOT EXISTS message_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      agent TEXT NOT NULL,
      channel TEXT NOT NULL,
      message_id TEXT NOT NULL,
      content TEXT NOT NULL,
      response TEXT,
      created_at TEXT NOT NULL,
      posted_at TEXT,
      processed INTEGER NOT NULL DEFAULT 0
    );
  `);
  const insert = db.prepare(
    `INSERT INTO message_queue (agent, channel, message_id, content, response, created_at, posted_at, processed)
     VALUES (@agent, 'dashboard', @message_id, @content, @response, @created_at, @posted_at, @processed)`
  );
  for (const row of rows) {
    insert.run({
      // Seed under the literal name the route queries.
      agent: "alpha",
      response: null,
      posted_at: null,
      processed: 0,
      ...row,
    });
  }
  db.close();
}

beforeEach(async () => {
  saved = {};
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
  tempDir = mkdtempSync(join(tmpdir(), "karakos-chat-history-test-"));
  dbPath = join(tempDir, "agent-server.db");
  seed([]);
  process.env.AGENT_SERVER_DB_PATH = dbPath;
  process.env.SESSION_SECRET = "test-secret-for-vitest";

  vi.resetModules();
  apiMod = await import("@/lib/api");
  ({ GET } = await import("./route"));
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  rmSync(tempDir, { recursive: true, force: true });
});

function req(cookie?: string): NextRequest {
  const headers = new Headers();
  if (cookie) headers.set("cookie", `karakos_session=${cookie}`);
  return new NextRequest("http://localhost/api/chat/history?agent=alpha", { headers });
}

interface OutMessage {
  role: "user" | "assistant";
  content: string;
  ts: string;
  messageId: string;
  processed?: number;
  turnEndedAt?: string;
}

async function history(): Promise<OutMessage[]> {
  const res = await GET(req(apiMod.generateSessionToken("tester")));
  expect(res.status).toBe(200);
  const { messages } = await res.json();
  return messages as OutMessage[];
}

const assistants = (msgs: OutMessage[]) => msgs.filter((m) => m.role === "assistant");

describe("GET /api/chat/history — turn-end signal", () => {
  it("reports posted_at as the turn's end time, not created_at", async () => {
    seed([
      {
        message_id: "m1",
        content: "how goes it?",
        response: "fine",
        created_at: "2026-08-26 17:18:16",
        posted_at: "2026-08-26 17:28:21",
        processed: 2,
      },
    ]);
    const [a] = assistants(await history());
    expect(a.turnEndedAt).toBe("2026-08-26 17:28:21");
    // The distinction is the whole point: created_at is when the message was
    // queued, which for a long turn is many minutes before it ended.
    expect(a.turnEndedAt).not.toBe(a.ts);
  });

  it("stamps a SILENT turn — one that completed with no response text at all", async () => {
    seed([
      {
        message_id: "m1",
        content: "thanks, any ETA?",
        response: "", // supervisor's PASS_SKIP path: complete, said nothing
        created_at: "2026-08-26 17:05:58",
        posted_at: "2026-08-26 17:07:41",
        processed: 2,
      },
    ]);
    const [a] = assistants(await history());
    expect(a).toBeDefined();
    expect(a.content).toBe("");
    // This is the case the feature exists for. No text to anchor to, but a
    // real recorded end time — so the client can still draw the boundary.
    expect(a.turnEndedAt).toBe("2026-08-26 17:07:41");
  });

  it("leaves a still-running turn unstamped", async () => {
    seed([
      {
        message_id: "m1",
        content: "working on it",
        response: "partial…",
        created_at: "2026-08-26 17:30:00",
        processed: 1, // in progress
      },
    ]);
    const [a] = assistants(await history());
    expect(a.processed).toBe(1);
    expect(a.turnEndedAt).toBeUndefined();
  });

  it("falls back to created_at on the crash path, which never writes posted_at", async () => {
    seed([
      {
        message_id: "m1",
        content: "boom",
        response: "[sidecar error: ...]",
        created_at: "2026-08-26 17:40:00",
        posted_at: null, // mark_failed() does not stamp it
        processed: 3,
      },
    ]);
    const [a] = assistants(await history());
    expect(a.turnEndedAt).toBe("2026-08-26 17:40:00");
  });

  it("stamps every turn in a run of consecutive silent ones", async () => {
    seed([
      { message_id: "m1", content: "one", response: "", created_at: "2026-08-26 17:00:00", posted_at: "2026-08-26 17:01:00", processed: 2 },
      { message_id: "m2", content: "two", response: "", created_at: "2026-08-26 17:02:00", posted_at: "2026-08-26 17:03:00", processed: 2 },
      { message_id: "m3", content: "three", response: "", created_at: "2026-08-26 17:04:00", posted_at: "2026-08-26 17:05:00", processed: 2 },
    ]);
    const ends = assistants(await history()).map((m) => m.turnEndedAt);
    expect(ends).toEqual(["2026-08-26 17:01:00", "2026-08-26 17:03:00", "2026-08-26 17:05:00"]);
  });

  it("keeps the existing shape — user line then assistant, chronological", async () => {
    seed([
      { message_id: "m1", content: "first", response: "r1", created_at: "2026-08-26 17:00:00", posted_at: "2026-08-26 17:00:30", processed: 2 },
      { message_id: "m2", content: "second", response: "r2", created_at: "2026-08-26 17:05:00", posted_at: "2026-08-26 17:05:30", processed: 2 },
    ]);
    const msgs = await history();
    expect(msgs.map((m) => [m.role, m.content])).toEqual([
      ["user", "first"],
      ["assistant", "r1"],
      ["user", "second"],
      ["assistant", "r2"],
    ]);
  });
});

describe("GET /api/chat/history — PASS handling", () => {
  it("blanks a literal PASS reply but keeps the turn boundary; leaves real replies alone", async () => {
    seed([
      { message_id: "p1", content: "ambient chatter", response: " PASS\n", created_at: "2026-09-30 17:00:00", posted_at: "2026-09-30 17:00:05", processed: 2 },
      { message_id: "p2", content: "hi", response: "PASS the salt", created_at: "2026-09-30 17:01:00", posted_at: "2026-09-30 17:01:05", processed: 2 },
    ]);
    const [a, b] = assistants(await history());
    expect(a.content).toBe("");
    expect(a.turnEndedAt).toBe("2026-09-30 17:00:05");
    expect(b.content).toBe("PASS the salt");
  });
});
