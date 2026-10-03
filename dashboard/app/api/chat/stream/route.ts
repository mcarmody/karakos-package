import { NextRequest } from "next/server";
import { isAuthenticated, sessionUser } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { AGENT_SERVER_DB } from "@/lib/db";

// Mirror of bin/agent-server.py status constants.
const STATUS_QUEUED = 0;
const STATUS_IN_PROGRESS = 1;
const STATUS_COMPLETE = 2;
const STATUS_CRASHED = 3;
const STATUS_SKIPPED = 4;

const POLL_INTERVAL_MS = 200;
const STREAM_TIMEOUT_MS = 5 * 60 * 1000;

export async function GET(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return new Response("Unauthorized", { status: 401 });
  }

  // Per-account agent allowlist. The stream is keyed by message_id, so the
  // target agent is only knowable from the queue row itself — checked on
  // every poll below, before any chunk is forwarded.
  const perms = permissionsFor(sessionUser(session));

  const messageId = request.nextUrl.searchParams.get("message_id");
  if (!messageId) {
    return new Response("Missing message_id", { status: 400 });
  }

  const encoder = new TextEncoder();
  const dbPath = AGENT_SERVER_DB;

  // Hoisted so cancel() can hit the same cleanup path as start() when
  // the client disconnects mid-stream.
  let cleanup: (reason: string) => Promise<void> = async () => {};

  const stream = new ReadableStream({
    async start(controller) {
      // Lazy-load to keep cold-start light.
      const sqlite3 = await import("sqlite3").then((m) => m.default);
      const { open } = await import("sqlite");

      // Single connection reused across polls — avoids file-handle churn
      // and per-tick "open/close" cost. Closed in cleanup().
      let db;
      try {
        db = await open({ filename: dbPath, driver: sqlite3.Database });
      } catch (err) {
        controller.error(err);
        return;
      }

      let lastSize = 0;
      // Typed turn events (thinking / interstitial / tool) written by the
      // pty-supervisor's dashboard_events_pump. Cursor is the last relayed
      // rowid. The table may not exist yet on a fresh DB — the query is
      // guarded and the guard stays cheap by flipping this off after the
      // first missing-table error.
      let lastEventRowId = 0;
      let turnEventsAvailable = true;
      let polling = false;     // overlap guard — skip ticks if previous still in flight
      let closed = false;
      let pollHandle: ReturnType<typeof setInterval> | null = null;
      let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

      const send = (payload: unknown) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
        } catch {
          // Controller may be closed — caller has already disconnected.
          // Cleanup will be triggered via the cancel handler.
        }
      };

      cleanup = async (_reason: string) => {
        if (closed) return;
        closed = true;
        if (pollHandle) clearInterval(pollHandle);
        if (timeoutHandle) clearTimeout(timeoutHandle);
        try {
          await db.close();
        } catch {
          // best-effort
        }
        try {
          controller.close();
        } catch {
          // controller may already be closed
        }
      };

      const poll = async () => {
        if (polling || closed) return;
        polling = true;
        try {
          const row = await db.get(
            "SELECT agent, response, processed FROM message_queue WHERE message_id = ?",
            messageId
          );

          if (!row) return;

          // Never relay another principal's agent traffic — a restricted
          // account replaying someone else's message_id gets a typed
          // forbidden event and the stream closes.
          if (!agentAllowed(perms, (row.agent as string) || "")) {
            send({ done: true, status: "forbidden" });
            await cleanup("forbidden");
            return;
          }

          // Relay typed events BEFORE the final-text chunk check so the
          // client's ordering matches emission order (thinking and
          // interstitials always precede the final answer they produced).
          if (turnEventsAvailable) {
            try {
              const events = await db.all(
                `SELECT id, seq, kind, content FROM turn_events
                 WHERE message_id = ? AND id > ?
                 ORDER BY id ASC`,
                messageId,
                lastEventRowId
              );
              for (const ev of events) {
                send({ event: { kind: ev.kind, content: ev.content, seq: ev.seq } });
                lastEventRowId = ev.id;
              }
            } catch {
              // Table absent (supervisor predates the pump) — stop asking.
              turnEventsAvailable = false;
            }
          }

          const response: string = row.response || "";
          if (response.length > lastSize) {
            send({ chunk: response.substring(lastSize) });
            lastSize = response.length;
          }

          const processed = row.processed as number;
          if (processed === STATUS_QUEUED || processed === STATUS_IN_PROGRESS) {
            return; // keep polling
          }

          // Terminal status — emit a typed event so the client can
          // distinguish a clean finish from a crash.
          if (processed === STATUS_COMPLETE) {
            send({ done: true, status: "complete" });
            await cleanup("complete");
          } else if (processed === STATUS_CRASHED) {
            send({ done: true, status: "crashed", error: "Agent crashed" });
            await cleanup("crashed");
          } else if (processed === STATUS_SKIPPED) {
            send({ done: true, status: "skipped" });
            await cleanup("skipped");
          } else {
            send({ done: true, status: `unknown:${processed}` });
            await cleanup("unknown");
          }
        } catch (err) {
          if (closed) return;
          console.error("Stream poll error:", err);
          send({ done: true, status: "error", error: String(err) });
          await cleanup("error");
        } finally {
          polling = false;
        }
      };

      pollHandle = setInterval(poll, POLL_INTERVAL_MS);
      timeoutHandle = setTimeout(() => {
        if (!closed) {
          send({ done: true, status: "timeout" });
          void cleanup("timeout");
        }
      }, STREAM_TIMEOUT_MS);

      // Kick off an immediate first poll instead of waiting for the interval.
      void poll();
    },

    async cancel() {
      // Client disconnected before we hit a terminal status — close the
      // db handle and cancel the poll interval so we don't leak resources.
      await cleanup("client-cancel");
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      // nginx defaults to proxy_buffering on, which holds SSE events in its
      // buffer until close — the client saw nothing until the turn ended,
      // and nothing at all if the connection dropped first.
      "X-Accel-Buffering": "no",
    },
  });
}
