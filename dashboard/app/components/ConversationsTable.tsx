"use client";

/**
 * Conversations — last N conversations across agents, rolled up from
 * cost_events by (agent, session_id). Sits below the existing money panels
 * on /costs; same table chrome as AgentMoneyPanel's "by agent" table.
 */

import { usePoll } from "@/lib/hooks";
import { SectionLabel, alpha, elapsedMs } from "@/app/components/lamplight-ui";

interface ConversationMetric {
  agent: string;
  session_id: string;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number;
  started_at: string;
  last_at: string;
  duration_ms: number;
}

interface ConversationMetricsResponse {
  conversations: ConversationMetric[];
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

// created_at ships as SQLite's "YYYY-MM-DD HH:MM:SS" (UTC, no offset) — force
// UTC parsing the same way the API route does for duration_ms.
function toDate(sqliteTs: string): Date {
  return new Date(`${sqliteTs.replace(" ", "T")}Z`);
}

export default function ConversationsTable() {
  const { data, loading } = usePoll<ConversationMetricsResponse>(
    "/api/conversations/metrics?days=7&limit=50",
    30000
  );
  const conversations = data?.conversations ?? [];

  return (
    <div className="mt-8">
      <SectionLabel style={{ marginBottom: 12 }}>conversations</SectionLabel>
      {loading && !data ? (
        <p style={{ color: "var(--text-muted)" }}>Loading...</p>
      ) : conversations.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No conversations in the last 7 days</p>
      ) : (
        <div
          className="rounded-lg border overflow-hidden"
          style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border)" }}
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[600px]">
              <thead style={{ backgroundColor: "var(--bg-elevated)" }}>
                <tr>
                  <th className="text-left px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Agent
                  </th>
                  <th className="text-right px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Turns
                  </th>
                  <th className="text-right px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Tokens
                  </th>
                  <th className="text-right px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Cost
                  </th>
                  <th className="text-right px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Duration
                  </th>
                  <th className="text-right px-4 py-3 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                    Last active
                  </th>
                </tr>
              </thead>
              <tbody>
                {conversations.map((c, i) => (
                  <tr
                    key={`${c.agent}-${c.session_id}`}
                    style={{ borderTop: i === 0 ? "none" : `1px solid ${alpha("var(--ink)", 13)}` }}
                  >
                    <td className="px-4 py-3" style={{ color: "var(--text-primary)" }}>
                      {c.agent}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      {c.turns}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      {formatTokens(c.total_tokens)}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      ${c.cost.toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      {elapsedMs(c.duration_ms)}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      {toDate(c.last_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
