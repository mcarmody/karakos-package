"use client";

import { usePoll } from "@/lib/hooks";
import { formatDuration, formatTokens, formatRelativeTime } from "@/lib/format";

interface CostData {
  daily: Record<string, number>;
  monthly: Record<string, number>;
  limits: {
    daily_limit: number;
    monthly_limit: number;
  };
}

// One row per (agent, session_id) — a conversation is one context window,
// not one turn. See handle_cost_conversations in bin/agent-server.py.
interface Conversation {
  agent: string;
  session_id: string;
  cost: number;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  turns: number;
  started_at: string;
  ended_at: string;
  current: boolean;
}

interface ConversationsData {
  conversations: Conversation[];
}

export default function CostsPage() {
  const { data, loading } = usePoll<CostData>("/api/cost", 30000);
  const { data: conversationsData } = usePoll<ConversationsData>(
    "/api/cost/conversations",
    30000
  );

  if (loading) {
    return <p className="text-gray-400">Loading...</p>;
  }

  if (!data) {
    return <p className="text-red-400">Unable to fetch cost data</p>;
  }

  const dailyTotal = Object.values(data.daily || {}).reduce((a, b) => a + b, 0);
  const monthlyTotal = Object.values(data.monthly || {}).reduce((a, b) => a + b, 0);

  const dailyPercent = data.limits
    ? (dailyTotal / data.limits.daily_limit) * 100
    : 0;
  const monthlyPercent = data.limits
    ? (monthlyTotal / data.limits.monthly_limit) * 100
    : 0;

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Costs</h1>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4">Daily Spending</h2>
          <p className="text-3xl font-bold mb-2">${dailyTotal.toFixed(2)}</p>
          {data.limits && (
            <>
              <div className="w-full bg-gray-800 rounded-full h-2 mb-2">
                <div
                  className={`h-2 rounded-full ${
                    dailyPercent > 90
                      ? "bg-red-500"
                      : dailyPercent > 75
                      ? "bg-yellow-500"
                      : "bg-green-500"
                  }`}
                  style={{ width: `${Math.min(dailyPercent, 100)}%` }}
                />
              </div>
              <p className="text-sm text-gray-400">
                {dailyPercent.toFixed(0)}% of ${data.limits.daily_limit} limit
              </p>
            </>
          )}
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4">Monthly Spending</h2>
          <p className="text-3xl font-bold mb-2">${monthlyTotal.toFixed(2)}</p>
          {data.limits && (
            <>
              <div className="w-full bg-gray-800 rounded-full h-2 mb-2">
                <div
                  className={`h-2 rounded-full ${
                    monthlyPercent > 90
                      ? "bg-red-500"
                      : monthlyPercent > 75
                      ? "bg-yellow-500"
                      : "bg-green-500"
                  }`}
                  style={{ width: `${Math.min(monthlyPercent, 100)}%` }}
                />
              </div>
              <p className="text-sm text-gray-400">
                {monthlyPercent.toFixed(0)}% of ${data.limits.monthly_limit} limit
              </p>
            </>
          )}
        </div>
      </div>

      <div>
        <h2 className="text-xl font-semibold mb-4">Cost Breakdown by Agent</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-800">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-semibold">Agent</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Daily</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Monthly</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {Object.keys(data.daily || {}).map((agent) => (
                <tr key={agent} className="hover:bg-gray-850">
                  <td className="px-4 py-3">{agent}</td>
                  <td className="px-4 py-3 text-right">
                    ${(data.daily[agent] || 0).toFixed(2)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    ${(data.monthly[agent] || 0).toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-8">
        <h2 className="text-xl font-semibold mb-4">Conversations</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-800">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-semibold">Agent</th>
                <th className="text-left px-4 py-3 text-sm font-semibold">Session</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Turns</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Tokens</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Duration</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Cost</th>
                <th className="text-right px-4 py-3 text-sm font-semibold">Last Activity</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {(conversationsData?.conversations ?? []).length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-gray-500">
                    No conversations recorded yet
                  </td>
                </tr>
              ) : (
                (conversationsData?.conversations ?? []).map((c) => (
                  <tr key={`${c.agent}-${c.session_id}`} className="hover:bg-gray-850">
                    <td className="px-4 py-3">{c.agent}</td>
                    <td className="px-4 py-3 font-mono text-xs text-gray-400">
                      {c.session_id}
                      {c.current && (
                        <span className="ml-2 px-1.5 py-0.5 rounded bg-green-900/50 border border-green-800 text-green-400 text-[10px] uppercase tracking-wide align-middle">
                          current
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right">{c.turns}</td>
                    <td className="px-4 py-3 text-right">
                      {formatTokens(c.input_tokens + c.output_tokens)}
                    </td>
                    <td className="px-4 py-3 text-right">{formatDuration(c.duration_ms)}</td>
                    <td className="px-4 py-3 text-right">${c.cost.toFixed(2)}</td>
                    <td className="px-4 py-3 text-right text-gray-400">
                      {formatRelativeTime(c.ended_at)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
