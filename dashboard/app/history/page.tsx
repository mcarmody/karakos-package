"use client";

/**
 * Searchable per-agent chat history. A DEDICATED page, deliberately separate from the agent
 * status/health surfaces (/agents, /fleet) — It is the
 * searchable-history view: transcript search kept separate from the
 * container/status HUD.
 *
 * Full-text search runs server-side over content AND response via a SQLite
 * FTS5 index (lib/history.ts), read-only against the live agent-server.db. Per-agent is the primary axis (agent selector first, "All
 * agents" supported); channel/author/mentions/date-range are secondary
 * filters. Every row deep-links to /history/[messageId].
 */

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { fieldStyle } from "@/app/components/lamplight-ui";
import HistoryDetailView from "./HistoryDetailView";

interface Row {
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

interface SearchResult {
  rows: Row[];
  total: number;
  page: number;
  pageSize: number;
  ftsActive: boolean;
}

function fmtTs(ts: string): string {
  const iso = ts.includes("T") ? ts : ts.replace(" ", "T") + "Z";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export default function HistoryPage() {
  const [agents, setAgents] = useState<string[]>([]);
  const [channels, setChannels] = useState<string[]>([]);

  const [q, setQ] = useState("");
  const [agent, setAgent] = useState("");
  const [channel, setChannel] = useState("");
  const [author, setAuthor] = useState("");
  const [mentionsAgent, setMentionsAgent] = useState<"" | "yes" | "no">("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(1);

  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/history/meta")
      .then((r) => r.json())
      .then((data) => {
        setAgents(data.agents || []);
        setChannels(data.channels || []);
      })
      .catch(() => {});
  }, []);

  const runSearch = useCallback(() => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ page: String(page), pageSize: "25" });
    if (q) params.set("q", q);
    if (agent) params.set("agent", agent);
    if (channel) params.set("channel", channel);
    if (author) params.set("author", author);
    if (mentionsAgent) params.set("mentionsAgent", mentionsAgent);
    if (dateFrom) params.set("dateFrom", dateFrom);
    if (dateTo) params.set("dateTo", dateTo);

    fetch(`/api/history/search?${params}`)
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then(setResult)
      .catch((err) => setError(err instanceof Error ? err.message : "Search failed"))
      .finally(() => setLoading(false));
  }, [q, agent, channel, author, mentionsAgent, dateFrom, dateTo, page]);

  useEffect(() => {
    const t = setTimeout(runSearch, 250); // debounce free-text typing
    return () => clearTimeout(t);
  }, [runSearch]);

  // Any filter change other than page itself resets to page 1.
  useEffect(() => {
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, agent, channel, author, mentionsAgent, dateFrom, dateTo]);

  const totalPages = result ? Math.max(1, Math.ceil(result.total / result.pageSize)) : 1;

  return (
    <div className="max-w-2xl xl:max-w-none">
      <div className="flex items-center gap-3.5 mb-1.5 flex-wrap">
        <h1 className="text-2xl font-semibold" style={{ color: "var(--text-primary)" }}>
          History
        </h1>
        <span className="xl:ml-auto text-xs" style={{ color: "var(--text-muted)" }}>
          {result ? `${result.total} match${result.total === 1 ? "" : "es"}` : ""}
        </span>
      </div>
      <p className="mb-4" style={{ fontSize: 12.5, color: "var(--text-muted)" }}>
        Searches inbound messages and agent responses together. Messages that were
        posted to Discord are purged after ~7 days server-side; this is not a full
        archive of every turn.
      </p>

      {/* Agent selector — the primary axis. */}
      <div className="flex flex-wrap gap-2 mb-3">
        <button
          onClick={() => setAgent("")}
          className="rounded-full"
          style={{
            ...fieldStyle,
            minHeight: 34,
            padding: "0 14px",
            fontSize: 13,
            cursor: "pointer",
            borderColor: agent === "" ? "var(--accent)" : "var(--border)",
            color: agent === "" ? "var(--accent)" : "var(--text-primary)",
          }}
        >
          All agents
        </button>
        {agents.map((a) => (
          <button
            key={a}
            onClick={() => setAgent(a)}
            className="rounded-full"
            style={{
              ...fieldStyle,
              minHeight: 34,
              padding: "0 14px",
              fontSize: 13,
              cursor: "pointer",
              borderColor: agent === a ? "var(--accent)" : "var(--border)",
              color: agent === a ? "var(--accent)" : "var(--text-primary)",
            }}
          >
            {a}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap gap-3 mb-3">
        <input
          type="text"
          placeholder="Search content and responses…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 220 }}
        />
      </div>

      <div className="flex flex-wrap gap-3 mb-6">
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 130 }}
        >
          <option value="">Any channel</option>
          {channels.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Author"
          value={author}
          onChange={(e) => setAuthor(e.target.value)}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 130 }}
        />
        <select
          value={mentionsAgent}
          onChange={(e) => setMentionsAgent(e.target.value as "" | "yes" | "no")}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 130 }}
        >
          <option value="">Mentions: any</option>
          <option value="yes">Mentions agent</option>
          <option value="no">Doesn&apos;t mention agent</option>
        </select>
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 130 }}
          aria-label="From date"
        />
        <input
          type="date"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          className="flex-1 rounded-lg"
          style={{ ...fieldStyle, minWidth: 130 }}
          aria-label="To date"
        />
      </div>

      {error && (
        <p className="mb-4" style={{ color: "var(--err)" }}>
          {error}
        </p>
      )}

      {loading && !result ? (
        <p style={{ color: "var(--text-muted)" }}>Loading…</p>
      ) : (
        <div className="flex flex-col gap-2.5 xl:max-w-[900px]">
          {result && result.rows.length === 0 ? (
            <p style={{ color: "var(--text-muted)" }}>No matches</p>
          ) : (
            result?.rows.map((row, idx) => {
              const isOpen = expanded === row.messageId;
              return (
                <div
                  key={row.messageId}
                  className={idx < 2 ? "slip-near lit anim-lift" : "slip-far anim-liftD66"}
                  style={{ padding: "14px 17px", animationDelay: `${Math.min(idx, 6) * 0.06}s` }}
                >
                  <div className="flex items-baseline gap-2.5 flex-wrap">
                    <span
                      className="font-semibold"
                      style={{ fontSize: 14, color: "var(--accent)" }}
                    >
                      {row.agent}
                    </span>
                    <span style={{ fontSize: 11.5, opacity: 0.45 }}>in #{row.channel}</span>
                    <span style={{ fontSize: 11.5, opacity: 0.45 }}>from {row.author}</span>
                    {row.mentionsAgent && (
                      <span style={{ fontSize: 11, color: "var(--info)" }}>@mention</span>
                    )}
                    <span className="ml-auto" style={{ fontSize: 11.5, opacity: 0.45 }}>
                      {fmtTs(row.createdAt)}
                    </span>
                  </div>
                  <p
                    className="whitespace-pre-wrap"
                    style={{ fontSize: 14, lineHeight: 1.5, marginTop: 7, color: "var(--text-primary)" }}
                  >
                    {row.contentSnippet}
                  </p>
                  {row.responseSnippet && (
                    <p
                      className="whitespace-pre-wrap"
                      style={{ fontSize: 13.5, lineHeight: 1.5, marginTop: 5, color: "var(--text-muted)" }}
                    >
                      → {row.responseSnippet}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-2.5">
                    <button
                      onClick={() => setExpanded(isOpen ? null : row.messageId)}
                      style={{ fontSize: 12, color: "var(--accent)", cursor: "pointer" }}
                    >
                      {isOpen ? "Collapse" : "Expand"}
                    </button>
                    <Link
                      href={`/history/${encodeURIComponent(row.messageId)}`}
                      style={{ fontSize: 12, color: "var(--text-muted)" }}
                    >
                      Permalink
                    </Link>
                  </div>
                  {isOpen && (
                    <div
                      className="mt-3 pt-3"
                      style={{ borderTop: "1px solid var(--border)" }}
                    >
                      <HistoryDetailView messageId={row.messageId} />
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}

      {result && result.total > 0 && (
        <div className="flex items-center gap-3 mt-5">
          <button
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded-lg"
            style={{ ...fieldStyle, minHeight: 36, padding: "0 12px", fontSize: 12.5, cursor: page <= 1 ? "default" : "pointer", opacity: page <= 1 ? 0.4 : 1 }}
          >
            Previous
          </button>
          <span style={{ fontSize: 12.5, color: "var(--text-muted)" }}>
            Page {page} of {totalPages}
          </span>
          <button
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            className="rounded-lg"
            style={{ ...fieldStyle, minHeight: 36, padding: "0 12px", fontSize: 12.5, cursor: page >= totalPages ? "default" : "pointer", opacity: page >= totalPages ? 0.4 : 1 }}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
