"use client";

import { useState } from "react";
import { alpha, ERR, GhostPill, StatusDot, Tag, WARN } from "@/app/components/lamplight-ui";
import { usePoll } from "@/lib/hooks";
import type { AgentRosterRow } from "@/lib/agentStatus";
import { formatTokens, pausedLabel, stateTone } from "@/lib/fleetView";
import { ShardHead, ShardRows } from "./ShardTable";
import { useNowSec } from "./PackageFleet";
import { TONE_COLOR } from "./tone";

// The contract's admin table (docs/package-backend-contract.md): POST, no body.
const ACTIONS = [
  { kind: "interrupt", label: "Interrupt", confirm: false },
  { kind: "reset", label: "Reset session", confirm: true },
  { kind: "reload", label: "Reload config", confirm: false },
] as const;

function AgentCard({ agent, now, onDone }: { agent: AgentRosterRow; now: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const shards = agent.shards ?? [];
  const reported = agent.reported !== false;

  async function act(kind: (typeof ACTIONS)[number]["kind"], confirmFirst: boolean) {
    if (confirmFirst && !window.confirm(`${kind} ${agent.name}?`)) return;
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent.name)}/${kind}`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      setMessage(res.ok ? `${kind}: ok` : `${kind} failed: ${body.error || res.status}`);
      onDone();
    } catch (e) {
      setMessage(`${kind} failed: ${e instanceof Error ? e.message : "error"}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="rounded-lg border overflow-hidden"
      style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
    >
      <div className="px-4 py-3 flex flex-wrap items-center gap-2">
        <StatusDot color={TONE_COLOR[stateTone(agent.state)]} />
        <span className="font-semibold" style={{ color: "var(--text-primary)" }}>{agent.label || agent.name}</span>
        {agent.label && agent.label !== agent.name && (
          <span className="text-sm font-mono" style={{ color: "var(--text-muted)" }}>{agent.name}</span>
        )}
        {agent.role && <Tag label={agent.role} color="var(--text-muted)" />}
        {agent.model && <Tag label={agent.model} color="var(--text-muted)" />}
        {agent.paused && <Tag label={pausedLabel(agent.paused, now)} color={WARN} filled />}
        <span className="ml-auto text-sm" style={{ color: "var(--text-secondary)" }}>
          {reported ? `${agent.state} · context ${formatTokens(agent.context_tokens)} · queue ${agent.total_pending}` : ""}
        </span>
      </div>
      {!reported ? (
        <p className="px-4 pb-3 text-sm" style={{ color: "var(--text-muted)" }}>Not reported by the server</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full sm:min-w-[640px] max-sm:block">
            <ShardHead />
            <tbody className="max-sm:block">
              <ShardRows shards={shards} now={now} />
            </tbody>
          </table>
        </div>
      )}
      {reported && (
        <div className="px-4 py-3 flex flex-wrap items-center gap-2" style={{ borderTop: "1px solid var(--border)" }}>
          {ACTIONS.map((a) => (
            <GhostPill key={a.kind} label={a.label} disabled={busy} onClick={() => act(a.kind, a.confirm)} />
          ))}
          {message && (
            <span className="text-sm" style={{ color: message.includes("failed") ? ERR : "var(--text-secondary)" }}>{message}</span>
          )}
        </div>
      )}
    </div>
  );
}

/** `/agents` under the package profile: roster cards with shard rows. */
export default function PackageAgents() {
  const { data, error, loading, refetch } = usePoll<{ agents: AgentRosterRow[] }>("/api/agents", 15000);
  const now = useNowSec();
  const agents = data?.agents ?? [];
  return (
    <div>
      <h1 className="text-2xl font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
        Agents
      </h1>
      <p className="text-sm mb-4" style={{ color: "var(--text-secondary)" }}>
        Each agent and its shards, from the agent server and the registry.
      </p>
      {loading && !data && <p style={{ color: "var(--text-secondary)" }}>Reading roster…</p>}
      {error && !data && <p style={{ color: ERR }}>Agent roster failed: {error}</p>}
      <div className="grid gap-4 items-start">
        {agents.map((a) => (
          <AgentCard key={a.name} agent={a} now={now} onDone={refetch} />
        ))}
      </div>
    </div>
  );
}
