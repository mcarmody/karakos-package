"use client";

import { usePoll } from "@/lib/hooks";

// Mirrors what agent-server's GET /health actually returns. `agents` is a
// dict keyed by name here (unlike /api/agents, which is an array -- see
// tests/test_dashboard_agents_contract.py). This interface used to declare
// per-agent messages_processed and last_activity, and top-level
// messages_today and cost_today; /health serves none of them, so the card
// below rendered the literal string "undefined messages processed".
interface HealthData {
  uptime_seconds: number;
  queue_depth: number;
  agents: Record<
    string,
    { state: string; alive?: boolean; queue_depth?: number; session_id?: string }
  >;
  dead_letters?: number;
}

export default function HomePage() {
  const { data: health, loading } = usePoll<HealthData>("/api/health", 15000);

  if (loading) {
    return <p className="text-gray-400">Loading...</p>;
  }

  if (!health) {
    return <p className="text-red-400">Unable to connect to agent server</p>;
  }

  const uptime = Math.floor((health.uptime_seconds || 0) / 3600);
  const agents = health.agents || {};

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <Card label="Uptime" value={`${uptime}h`} />
        <Card label="Queue Depth" value={String(health.queue_depth || 0)} />
        <Card label="Agents" value={String(Object.keys(agents).length)} />
      </div>

      <h2 className="text-xl font-semibold mb-4">Agents</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {Object.entries(agents).map(([name, info]) => (
          <div
            key={name}
            className="bg-gray-900 border border-gray-800 rounded-lg p-4 hover:border-gray-700 transition-colors"
          >
            <div className="flex justify-between items-center mb-2">
              <strong className="text-lg">{name}</strong>
              <span
                className={`text-xs px-2 py-1 rounded-full ${
                  info.state === "IDLE"
                    ? "bg-green-900 text-green-300"
                    : info.state === "PROCESSING"
                    ? "bg-yellow-900 text-yellow-300"
                    : "bg-red-900 text-red-300"
                }`}
              >
                {info.state}
              </span>
            </div>
            <p className="text-sm text-gray-400 mb-1">
              {info.queue_depth ?? 0} queued
            </p>
            {info.alive === false && (
              <p className="text-xs text-red-400">subprocess down</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className="text-3xl font-bold">{value}</p>
    </div>
  );
}
