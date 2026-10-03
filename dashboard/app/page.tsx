"use client";

/**
 * Ops Board: the home page. Layout mechanics: the wide (xl+) view is two
 * independent `flex-direction: column` stacks side by side in a
 * `flex items-start` row (not a CSS grid, whose rows size to their tallest
 * cell), so each column's height is exactly the sum of its own cards. Below
 * xl the same cards stack into one column in reading order.
 *
 * Every number here comes from a real fetch; where there is no data the card
 * says so rather than showing a placeholder.
 */

import React, { useMemo } from "react";
import Link from "next/link";
import { usePoll } from "@/lib/hooks";
import { hostLabels, masthead } from "@/lib/branding";
import {
  OK,
  WARN,
  ERR,
  INFO,
  alpha,
  StatusDot,
  SectionLabel,
  NotchBar,
  slipFar,
  slipNear,
} from "@/app/components/lamplight-ui";

// ── Shapes (mirrors the route files — see their own field comments) ──────

interface AgentInfo {
  name: string;
  state: string;
  label?: string;
  cost?: number;
}

interface HealthComponent {
  name: string;
  status: "healthy" | "stale" | "unknown";
  last_check?: string;
}

interface HealthData {
  uptime_seconds?: number;
  queue_depth?: number;
  components?: HealthComponent[];
  agent_server?: string;
  host_metrics?: {
    cpu_percent?: number;
    memory_percent?: number;
    disk_percent?: number;
    temp_c?: number;
  };
}

interface CostData {
  daily: Record<string, number>;
  monthly: Record<string, number>;
  limits: { daily_limit: number; monthly_limit: number };
}


// ── Small shared bits ─────────────────────────────────────────────────────

const SWARM_HEALTHY_STATES = new Set(["IDLE", "ACTIVE", "PROCESSING"]);

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso.replace(" ", "T") + "Z");
  if (Number.isNaN(ms)) return "—";
  const min = Math.round((Date.now() - ms) / 60000);
  if (min <= 0) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  return hr < 24 ? `${hr}h ago` : `${Math.round(hr / 24)}d ago`;
}

/** Section shell every card on this page shares — a slip, a label row, and
 * content that sizes to itself (no min-height, no flex-grow). */
function Panel({
  label,
  href,
  hrefLabel,
  right,
  near,
  children,
}: {
  label: string;
  href?: string;
  hrefLabel?: string;
  right?: React.ReactNode;
  near?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="p-4" style={near ? slipNear : slipFar}>
      <div className="flex items-center justify-between mb-3 gap-2">
        <SectionLabel>{label}</SectionLabel>
        <div className="flex items-center gap-2">
          {right}
          {href && (
            <Link
              href={href}
              className="text-xs no-underline hover:opacity-80"
              style={{ color: "var(--text-secondary)", flexShrink: 0 }}
            >
              {hrefLabel || "View →"}
            </Link>
          )}
        </div>
      </div>
      {children}
    </div>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs" style={{ color: "var(--text-muted)" }}>
      {children}
    </p>
  );
}

// ── Masthead + section grouping ──────────────────────────────────────────
//
// Colours come from the --bg/--ink/--accent tokens so the whole product
// shifts palette through the day.

function Masthead({ ok }: { ok: boolean }) {
  const brand = masthead();
  const [now, setNow] = React.useState<string>("");
  React.useEffect(() => {
    const tick = () =>
      setNow(
        new Date().toLocaleString(undefined, {
          weekday: "short",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        })
      );
    tick();
    const id = setInterval(tick, 30000);
    return () => clearInterval(id);
  }, []);

  return (
    <div
      className="flex items-center justify-between gap-4 flex-wrap pb-4 mb-4"
      style={{ borderBottom: `1px solid ${alpha("var(--ink)", 12)}` }}
    >
      <div className="flex items-baseline gap-3.5 flex-wrap">
        <span style={{ fontSize: 20, fontWeight: 700, letterSpacing: "0.02em" }}>
          <span style={{ color: "var(--accent)" }}>{brand.lead}</span>{brand.rest}
        </span>
        <span
          className="text-xs uppercase"
          style={{ letterSpacing: "0.08em", color: "var(--text-muted)" }}
        >
          {brand.subtitle}
        </span>
      </div>
      <div className="flex items-center gap-4">
        <div
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold uppercase"
          style={{
            letterSpacing: "0.06em",
            background: alpha(ok ? OK : WARN, 14),
            color: ok ? OK : WARN,
          }}
        >
          <StatusDot color={ok ? OK : WARN} halo size={7} />
          {ok ? "Nominal" : "Degraded"}
        </div>
        {now && (
          <span
            className="text-xs"
            style={{ fontVariantNumeric: "tabular-nums", color: "var(--text-muted)" }}
          >
            {now}
          </span>
        )}
      </div>
    </div>
  );
}

/** Eyebrow + count + rule section header, wrapping its cards in a
 * newspaper-style CSS multi-column flow (mock's `columns:360px`) — cards
 * size to their own content and pack left-to-right without row-stretch,
 * same "no forced deadspace" property the file-level comment describes for
 * the xl flex-column layout, applied inside one section instead of across
 * the whole page. */
function Section({
  label,
  count,
  children,
}: {
  label: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-2.5 mb-2.5">
        <SectionLabel style={{ opacity: 0.7 }}>{label}</SectionLabel>
        <span className="text-xs" style={{ color: "var(--text-muted)", opacity: 0.6 }}>
          {count}
        </span>
        <div className="flex-1 h-px" style={{ background: alpha("var(--ink)", 8) }} />
      </div>
      <div style={{ columns: "360px", columnGap: 16 }}>
        {React.Children.toArray(children).map((child, i) => (
          <div key={i} style={{ breakInside: "avoid", marginBottom: 16 }}>{child}</div>
        ))}
      </div>
    </div>
  );
}

// ── System health (right rail) — derived entirely from data this page
// already fetches (agents + health.components), no new endpoint. ────────

function SystemHealthCard({ agents, health }: { agents: AgentInfo[]; health: HealthData | null }) {
  const healthy = agents.filter((a) => SWARM_HEALTHY_STATES.has(a.state));
  const watch = agents.find((a) => !SWARM_HEALTHY_STATES.has(a.state));
  const badComponent = (health?.components || []).find((c) => c.status !== "healthy");
  return (
    <Panel label="System Health">
      <div className="flex flex-col gap-2 text-sm">
        <div className="flex items-center justify-between">
          <span style={{ color: "var(--text-secondary)" }}>Shards nominal</span>
          <span
            style={{
              color: agents.length && healthy.length === agents.length ? OK : "var(--text-primary)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {agents.length > 0 ? `${healthy.length} / ${agents.length}` : "—"}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span style={{ color: "var(--text-secondary)" }}>Watch</span>
          <span style={{ color: watch ? WARN : "var(--text-primary)" }}>
            {watch ? watch.name : badComponent ? badComponent.name : "—"}
          </span>
        </div>
      </div>
    </Panel>
  );
}

// ── Pinned status strip (requirement 3: bars grouped in one place) ───────

function StatusStrip({
  health,
  cost,
}: {
  health: HealthData | null;
  cost: CostData | null;
}) {
  const host = health?.host_metrics;
  const hostNames = hostLabels();
  const dailyTotal = Object.values(cost?.daily || {}).reduce((a, b) => a + b, 0);
  const dailyLimit = cost?.limits?.daily_limit;
  const dailyPct = dailyLimit ? (dailyTotal / dailyLimit) * 100 : undefined;

  const bars: Array<{ label: string; pctUsed?: number; pctExpected?: number; fillColor?: string; readout: string }> = [
    {
      label: hostNames.cpu,
      pctUsed: host?.cpu_percent,
      fillColor: host?.cpu_percent != null ? (host.cpu_percent > 80 ? ERR : INFO) : undefined,
      readout: host?.cpu_percent != null ? `${host.cpu_percent.toFixed(0)}%` : "—",
    },
    {
      label: hostNames.memory,
      pctUsed: host?.memory_percent,
      fillColor: host?.memory_percent != null ? (host.memory_percent > 85 ? ERR : INFO) : undefined,
      readout: host?.memory_percent != null ? `${host.memory_percent.toFixed(0)}%` : "—",
    },
    {
      label: hostNames.disk,
      pctUsed: host?.disk_percent,
      fillColor: host?.disk_percent != null ? (host.disk_percent > 90 ? ERR : INFO) : undefined,
      readout: host?.disk_percent != null ? `${host.disk_percent.toFixed(0)}%` : "—",
    },
    {
      label: "Cost today",
      pctUsed: dailyPct,
      fillColor: dailyPct != null ? (dailyPct > 90 ? ERR : dailyPct > 75 ? WARN : OK) : undefined,
      readout: dailyLimit ? `$${dailyTotal.toFixed(2)} / $${dailyLimit}` : "—",
    },
  ];

  return (
    <div className="flex flex-wrap gap-2.5 mb-5">
      {bars.map((b) => (
        <div key={b.label} className="p-2.5 min-w-0" style={{ ...slipNear, flex: "1 1 170px" }}>
          <div className="flex items-baseline justify-between mb-1.5 gap-2">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
              {b.label}
            </span>
            <span
              className="text-xs font-medium whitespace-nowrap"
              style={{ color: "var(--text-primary)", fontVariantNumeric: "tabular-nums" }}
            >
              {b.readout}
            </span>
          </div>
          <NotchBar
            pctUsed={b.pctUsed ?? 0}
            pctExpected={b.pctExpected}
            fillColor={b.fillColor}
          />
        </div>
      ))}
    </div>
  );
}

// ── Fleet roster ───────────────────────────────────────────────────────

function FleetCard({ agents }: { agents: AgentInfo[] }) {
  const up = agents.filter((a) => SWARM_HEALTHY_STATES.has(a.state)).length;
  return (
    <Panel
      label="Fleet"
      href="/fleet"
      right={
        <span className="text-xs" style={{ color: "var(--text-secondary)", fontVariantNumeric: "tabular-nums" }}>
          {up}/{agents.length} up
        </span>
      }
    >
      {agents.length === 0 ? (
        <EmptyNote>No agent status available.</EmptyNote>
      ) : (
        <div className="flex flex-col gap-1.5">
          {agents.map((a, i) => {
            const healthy = SWARM_HEALTHY_STATES.has(a.state);
            const processing = a.state === "PROCESSING";
            // Staggered halo delay (mock: 0s, .25s, .5s, ...) so a column of
            // dots doesn't beat in sync — "alive," not "list of dots."
            const delay = (i % 8) * 0.25;
            return (
              <div key={a.name} className="row flex items-center gap-2 text-xs">
                <StatusDot
                  color={healthy ? OK : processing ? WARN : ERR}
                  halo={healthy || processing}
                  haloDelay={delay}
                  size={7}
                />
                <span className="font-medium" style={{ color: "var(--text-primary)" }}>
                  {a.name}
                </span>
                {a.label && (
                  <span className="truncate" style={{ color: "var(--text-muted)" }}>
                    {a.label}
                  </span>
                )}
                <span className="ml-auto flex-shrink-0" style={{ color: "var(--text-muted)" }}>
                  {a.cost !== undefined ? `$${a.cost.toFixed(2)}` : a.state}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function CostCard({ cost }: { cost: CostData | null }) {
  const daily = cost?.daily || {};
  const monthly = cost?.monthly || {};
  const dailyTotal = Object.values(daily).reduce((a, b) => a + b, 0);
  const monthlyTotal = Object.values(monthly).reduce((a, b) => a + b, 0);
  const topAgents = Object.entries(monthly)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  return (
    <Panel label="Usage & Cost" href="/costs">
      {!cost ? (
        <EmptyNote>Cost data unavailable.</EmptyNote>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
              Today
            </span>
            <span className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
              ${dailyTotal.toFixed(2)}
            </span>
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
              This month
            </span>
            <span className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
              ${monthlyTotal.toFixed(2)}
            </span>
          </div>
          {topAgents.length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs mt-1" style={{ color: "var(--text-secondary)" }}>
              {topAgents.map(([agent, c]) => (
                <span key={agent}>
                  {agent} ${c.toFixed(2)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function findProblem(health: HealthData | null): { title: string; detail: string } | null {
  const badComponent = (health?.components || []).find((c) => c.status !== "healthy");
  if (badComponent) {
    return {
      title: badComponent.status === "stale" ? `${badComponent.name} went quiet` : `${badComponent.name} status unknown`,
      detail: badComponent.last_check ? `Last check ${timeAgo(badComponent.last_check)}.` : "No recent check-in.",
    };
  }
  const host = health?.host_metrics;
  if (host?.temp_c !== undefined && host.temp_c > 75) return { title: "Host running hot", detail: `${host.temp_c.toFixed(1)}°C.` };
  if (host?.disk_percent !== undefined && host.disk_percent > 90)
    return { title: "Disk almost full", detail: `${host.disk_percent.toFixed(1)}% used.` };
  if (host?.memory_percent !== undefined && host.memory_percent > 85)
    return { title: "Memory pressure high", detail: `${host.memory_percent.toFixed(1)}% used.` };
  if (host?.cpu_percent !== undefined && host.cpu_percent > 80) return { title: "CPU running hot", detail: `${host.cpu_percent.toFixed(1)}% load.` };
  return null;
}

// ── Page ───────────────────────────────────────────────────────────────

export default function OpsBoardPage() {
  const { data: agentsData } = usePoll<{ agents: AgentInfo[] }>("/api/agents", 15000);
  const { data: health } = usePoll<HealthData>("/api/health", 15000);
  const { data: cost } = usePoll<CostData>("/api/cost", 30000);

  const problem = useMemo(() => findProblem(health), [health]);
  const agents = agentsData?.agents || [];

  // Card elements are built once and reused in both layouts below — CSS
  // toggles which wrapper is visible, so nothing is fetched or computed twice.
  const fleetCard = <FleetCard agents={agents} />;
  const costCard = <CostCard cost={cost} />;
  const systemHealthCard = <SystemHealthCard agents={agents} health={health} />;

  return (
    <div className="mx-auto w-full" style={{ color: "var(--ink)" }}>
      <Masthead ok={!problem} />

      {problem && (
        <div className="p-3 mb-4 flex items-start gap-3" style={slipNear}>
          <StatusDot color={WARN} breathe size={9} />
          <div>
            <div className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
              {problem.title}
            </div>
            <div className="text-xs mt-0.5" style={{ color: "var(--text-secondary)" }}>
              {problem.detail}
            </div>
          </div>
        </div>
      )}

      <StatusStrip health={health} cost={cost} />

      {/* Wide (xl+): main column plus a sticky rail. */}
      <div className="hidden xl:flex gap-5 items-start">
        <div style={{ flex: 1, minWidth: 0 }}>
          <Section label="Engineering" count={1}>
            {fleetCard}
          </Section>
          <Section label="Usage" count={1}>
            {costCard}
          </Section>
        </div>
        <div className="flex flex-col gap-4" style={{ width: 272, flexShrink: 0, position: "sticky", top: 20 }}>
          {systemHealthCard}
        </div>
      </div>

      {/* Narrower than xl: everything stacks in one reading-order column. */}
      <div className="xl:hidden flex flex-col gap-4">
        {fleetCard}
        {costCard}
        {systemHealthCard}
      </div>
    </div>
  );
}
