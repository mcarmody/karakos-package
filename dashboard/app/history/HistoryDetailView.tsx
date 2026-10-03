"use client";

/**
 * Shared "full exchange + turn_events" view — used inline (expanded search
 * result) and as the whole body of the /history/[messageId] deep-link page.
 * Task 8be6aa80.
 */

import { useEffect, useState } from "react";
import { fieldStyle } from "@/app/components/lamplight-ui";

interface TurnEvent {
  seq: number;
  kind: string;
  content: string;
  createdAt: string;
}

interface Detail {
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
  turnEvents: TurnEvent[];
}

function fmtTs(ts: string): string {
  // SQLite datetime('now') values are UTC with no zone marker — append Z so
  // Date parses them as UTC rather than local (see chat/history/route.ts).
  const iso = ts.includes("T") ? ts : ts.replace(" ", "T") + "Z";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export default function HistoryDetailView({ messageId }: { messageId: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showEvents, setShowEvents] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`/api/history/message/${encodeURIComponent(messageId)}`)
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [messageId]);

  if (loading) {
    return <p style={{ color: "var(--text-muted)" }}>Loading…</p>;
  }
  if (error || !detail) {
    return <p style={{ color: "var(--err)" }}>{error || "Not found"}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-2.5" style={{ fontSize: 12.5, opacity: 0.6 }}>
        <span className="font-semibold" style={{ color: "var(--accent)" }}>
          {detail.agent}
        </span>
        <span>in #{detail.channel}</span>
        <span>from {detail.author}</span>
        <span>{fmtTs(detail.createdAt)}</span>
        {detail.mentionsAgent && <span style={{ color: "var(--info)" }}>mentions agent</span>}
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wide font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>
          Inbound message
        </div>
        <p className="whitespace-pre-wrap" style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--text-primary)" }}>
          {detail.content}
        </p>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wide font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>
          Response
        </div>
        {detail.response ? (
          <p className="whitespace-pre-wrap" style={{ fontSize: 14.5, lineHeight: 1.55, color: "var(--text-primary)" }}>
            {detail.response}
          </p>
        ) : (
          <p style={{ fontSize: 13, color: "var(--text-muted)" }}>No response recorded for this turn.</p>
        )}
      </div>

      <div>
        <button
          onClick={() => setShowEvents((s) => !s)}
          className="rounded-lg"
          style={{ ...fieldStyle, minHeight: 36, padding: "0 12px", fontSize: 12.5, cursor: "pointer" }}
        >
          {showEvents ? "Hide" : "Show"} turn detail ({detail.turnEvents.length})
        </button>
        {showEvents && (
          <div className="flex flex-col gap-2 mt-2.5">
            {detail.turnEvents.length === 0 ? (
              <p style={{ fontSize: 12.5, color: "var(--text-muted)" }}>
                No recorded tool calls or interstitial text for this turn.
              </p>
            ) : (
              detail.turnEvents.map((ev) => (
                <div
                  key={ev.seq}
                  className="rounded-lg"
                  style={{ border: "1px solid var(--border)", padding: "8px 11px" }}
                >
                  <div className="flex items-baseline gap-2" style={{ fontSize: 11, opacity: 0.55 }}>
                    <span className="font-semibold uppercase" style={{ letterSpacing: "0.06em" }}>
                      {ev.kind}
                    </span>
                    <span>#{ev.seq}</span>
                    <span className="ml-auto">{fmtTs(ev.createdAt)}</span>
                  </div>
                  <p
                    className="whitespace-pre-wrap"
                    style={{ fontSize: 13, lineHeight: 1.5, marginTop: 5, color: "var(--text-primary)" }}
                  >
                    {ev.content}
                  </p>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <div style={{ fontSize: 11, opacity: 0.4 }}>
        message_id: {detail.messageId}
        {detail.postedAt ? ` · posted ${fmtTs(detail.postedAt)}` : ""}
      </div>
    </div>
  );
}
