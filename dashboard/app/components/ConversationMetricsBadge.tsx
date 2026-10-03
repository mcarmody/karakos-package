"use client";

/**
 * Current-conversation rollup — turns/tokens/cost/elapsed for the selected
 * agent's newest session_id (one context window). Lives
 * next to app/chat/page.tsx's formatClock span, same type scale, dropped
 * (not wrapped) below sm so a long line never pushes the header to two rows.
 */

import { usePoll } from "@/lib/hooks";
import { elapsedMs } from "@/app/components/lamplight-ui";

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

export default function ConversationMetricsBadge({ agent }: { agent: string }) {
  const { data } = usePoll<ConversationMetricsResponse>(
    agent ? `/api/conversations/metrics?agent=${encodeURIComponent(agent)}&days=7&limit=1` : "",
    30000
  );

  const current = data?.conversations?.[0];
  if (!current || current.turns === 0) return null;

  const summary = `${current.turns} turn${current.turns === 1 ? "" : "s"} · ${formatTokens(
    current.total_tokens
  )} tok · $${current.cost.toFixed(2)} · ${elapsedMs(current.duration_ms)}`;

  return (
    <span
      className="hidden sm:inline-block"
      title={summary}
      style={{
        fontSize: 12,
        color: "var(--text-muted)",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
        maxWidth: "34vw",
        paddingRight: 10,
        marginRight: 2,
        borderRight: "1px solid var(--border)",
      }}
    >
      {summary}
    </span>
  );
}
