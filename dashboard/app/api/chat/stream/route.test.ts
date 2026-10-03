import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { NextRequest } from "next/server";
import Database from "better-sqlite3";

// Server half: the stream route must send
// a typed terminal status — and must not conflate COMPLETE with CRASHED.
// Client half lives in app/components/ChatSurface.terminalStatus.test.ts.

let tempDir: string;
let dbPath: string;
let apiMod: typeof import("@/lib/api");
let GET: typeof import("./route").GET;
const saved = { db: process.env.AGENT_SERVER_DB_PATH, perms: process.env.DASHBOARD_PERMISSIONS };

function seed(processed: number, opts: { response?: string; agent?: string } = {}) {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE IF NOT EXISTS message_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      agent TEXT NOT NULL, channel TEXT NOT NULL, message_id TEXT NOT NULL,
      content TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL,
      posted_at TEXT, processed INTEGER NOT NULL DEFAULT 0
    );
  `);
  db.prepare(
    `INSERT INTO message_queue (agent, channel, message_id, content, response, created_at, processed)
     VALUES (?, 'dashboard', 'm1', 'hi', ?, '2026-10-03 10:00:00', ?)`
  ).run(opts.agent ?? "amos", opts.response ?? null, processed);
  db.close();
}

async function events(): Promise<Array<Record<string, unknown>>> {
  const req = new NextRequest("http://localhost/api/chat/stream?message_id=m1", {
    headers: { cookie: `karakos_session=${apiMod.generateSessionToken("alpha")}` },
  });
  const res = await GET(req);
  expect(res.status).toBe(200);
  const text = await res.text(); // stream closes itself on a terminal status
  return text
    .split("\n\n")
    .filter((l) => l.startsWith("data: "))
    .map((l) => JSON.parse(l.slice(6)));
}

const terminal = (evs: Array<Record<string, unknown>>) => evs.find((e) => e.done);

beforeEach(async () => {
  tempDir = mkdtempSync(join(tmpdir(), "karakos-chat-stream-test-"));
  dbPath = join(tempDir, "agent-server.db");
  process.env.AGENT_SERVER_DB_PATH = dbPath;
  process.env.SESSION_SECRET = "test-secret-for-vitest";
  delete process.env.DASHBOARD_PERMISSIONS;
  vi.resetModules();
  apiMod = await import("@/lib/api");
  ({ GET } = await import("./route"));
});

afterEach(() => {
  if (saved.db === undefined) delete process.env.AGENT_SERVER_DB_PATH;
  else process.env.AGENT_SERVER_DB_PATH = saved.db;
  if (saved.perms === undefined) delete process.env.DASHBOARD_PERMISSIONS;
  else process.env.DASHBOARD_PERMISSIONS = saved.perms;
  rmSync(tempDir, { recursive: true, force: true });
});

describe("GET /api/chat/stream — terminal status", () => {
  it("sends status complete for a finished turn, after its chunk", async () => {
    seed(2, { response: "all done" });
    const evs = await events();
    expect(evs[0]).toEqual({ chunk: "all done" });
    expect(terminal(evs)).toEqual({ done: true, status: "complete" });
  });

  it("sends status crashed (with error) — distinct from complete", async () => {
    seed(3, { response: "four sentences in" });
    const evs = await events();
    expect(evs[0]).toEqual({ chunk: "four sentences in" });
    expect(terminal(evs)).toEqual({ done: true, status: "crashed", error: "Agent crashed" });
  });

  it("sends status skipped", async () => {
    seed(4);
    expect(terminal(await events())).toEqual({ done: true, status: "skipped" });
  });

  it("sends a computed unknown:<n> status for an unrecognised queue state", async () => {
    seed(9);
    expect(terminal(await events())).toEqual({ done: true, status: "unknown:9" });
  });

  it("sends status forbidden for an agent the account may not read", async () => {
    process.env.DASHBOARD_PERMISSIONS = JSON.stringify({ alpha: { agents: ["relay"] } });
    vi.resetModules();
    apiMod = await import("@/lib/api");
    ({ GET } = await import("./route"));
    seed(2, { response: "secret", agent: "amos" });
    const evs = await events();
    expect(terminal(evs)).toEqual({ done: true, status: "forbidden" });
    expect(evs.some((e) => "chunk" in e)).toBe(false);
  });
});
