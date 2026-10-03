"use client";

/**
 * Agent money — the spend panel on /costs. Self-contained (own /api/cost
 * poll), presentation only, with 75%/90% cost bands.
 */

import { usePoll } from "@/lib/hooks";
import { alpha, OK, WARN, ERR, SectionLabel, NotchBar } from "@/app/components/lamplight-ui";

interface CostData {
  daily: Record<string, number>;
  monthly: Record<string, number>;
  limits: {
    daily_limit: number;
    monthly_limit: number;
  };
}

// 75%/90% cost bands — thresholds unchanged.
function bandColor(pct: number): string {
  if (pct > 90) return ERR;
  if (pct > 75) return WARN;
  return OK;
}

function SpendTile({
  label,
  value,
  pct,
  pctExpected,
  caption,
  emphasis,
}: {
  label: string;
  value: string;
  pct: number;
  /** Screen 4j's notch — only the monthly tile has a month to pace against. */
  pctExpected?: number;
  caption: string;
  emphasis?: boolean;
}) {
  const color = bandColor(pct);
  return (
    <div
      className={emphasis ? "lit flex-1" : "flex-1"}
      style={{
        minWidth: 150,
        borderRadius: 14,
        padding: "14px 16px",
        background: emphasis ? "var(--slip-near)" : "var(--slip-far)",
        boxShadow: emphasis ? "var(--sh-near)" : "var(--sh-far)",
      }}
    >
      <SectionLabel style={{ fontSize: 11.5, textTransform: "none", letterSpacing: "normal" }}>
        {label}
      </SectionLabel>
      <div className="mt-1" style={{ fontSize: 22, fontWeight: 600, color: "var(--text-primary)" }}>
        {value}
      </div>
      <div className="mt-2">
        <NotchBar pctUsed={pct} pctExpected={pctExpected} fillColor={color} />
      </div>
      <p className="mt-1.5" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
        {caption}
      </p>
    </div>
  );
}

export default function AgentMoneyPanel({
  pctMonthExpected,
}: {
  /** Percent of the month elapsed; positions the pace notch on the bar. */
  pctMonthExpected?: number;
}) {
  const { data, loading } = usePoll<CostData>("/api/cost", 30000);

  if (loading) {
    return <p style={{ color: "var(--text-muted)" }}>Loading...</p>;
  }

  if (!data) {
    return <p style={{ color: "var(--err)" }}>Unable to fetch cost data</p>;
  }

  const dailyTotal = Object.values(data.daily || {}).reduce((a, b) => a + b, 0);
  const monthlyTotal = Object.values(data.monthly || {}).reduce((a, b) => a + b, 0);

  const dailyPercent = data.limits
    ? (dailyTotal / data.limits.daily_limit) * 100
    : 0;
  const monthlyPercent = data.limits
    ? (monthlyTotal / data.limits.monthly_limit) * 100
    : 0;

  // Compact per-agent breakdown line under the tiles — today's spend,
  // biggest spender first.
  const dailyEntries = Object.entries(data.daily || {}).sort((a, b) => b[1] - a[1]);

  return (
    <div>
      <SectionLabel style={{ opacity: 0.45 }}>the agents</SectionLabel>

      <div className="mt-2.5 flex flex-col sm:flex-row gap-3 mb-3">
        {data.limits && (
          <>
            <SpendTile
              label="today"
              value={`$${dailyTotal.toFixed(2)}`}
              pct={dailyPercent}
              caption={`${dailyPercent.toFixed(0)}% of $${data.limits.daily_limit}`}
              emphasis
            />
            <SpendTile
              label="this month"
              value={`$${monthlyTotal.toFixed(2)}`}
              pct={monthlyPercent}
              pctExpected={pctMonthExpected}
              caption={`${monthlyPercent.toFixed(0)}% of $${data.limits.monthly_limit}`}
            />
          </>
        )}
      </div>

      {dailyEntries.length > 0 && (
        <p className="mb-8" style={{ fontSize: 11.5, color: alpha("var(--ink)", 50) }}>
          {dailyEntries.map(([agent, v]) => `${agent} $${v.toFixed(2)}`).join(" · ")}
        </p>
      )}

      <div>
        <SectionLabel style={{ marginBottom: 12 }}>by agent</SectionLabel>
        <div
          className="rounded-lg border overflow-hidden"
          style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border)" }}
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[280px]">
              <thead style={{ backgroundColor: "var(--bg-elevated)" }}>
                <tr>
                  <th
                    className="text-left px-4 py-3 text-sm font-semibold"
                    style={{ color: "var(--text-primary)" }}
                  >
                    Agent
                  </th>
                  <th
                    className="text-right px-4 py-3 text-sm font-semibold"
                    style={{ color: "var(--text-primary)" }}
                  >
                    Daily
                  </th>
                  <th
                    className="text-right px-4 py-3 text-sm font-semibold"
                    style={{ color: "var(--text-primary)" }}
                  >
                    Monthly
                  </th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(data.daily || {}).map((agent, i) => (
                  <tr
                    key={agent}
                    style={{
                      borderTop: i === 0 ? "none" : `1px solid ${alpha("var(--ink)", 13)}`,
                    }}
                  >
                    <td className="px-4 py-3" style={{ color: "var(--text-primary)" }}>
                      {agent}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      ${(data.daily[agent] || 0).toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--text-secondary)" }}>
                      ${(data.monthly[agent] || 0).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
