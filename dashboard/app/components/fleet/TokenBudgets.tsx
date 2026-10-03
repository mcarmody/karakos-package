import { alpha, ERR, NotchBar, OK, SectionLabel, WARN } from "@/app/components/lamplight-ui";
import { formatEpoch, formatTokens, percent } from "@/lib/fleetView";
import type { UsageBudget } from "@/lib/packageBackend";

function band(pct: number): string {
  return pct > 90 ? ERR : pct > 75 ? WARN : OK;
}

/** Per-agent token budget for the 4-hour window. Agents without a budget are
 * not listed (the server omits them). Read-only. */
export default function TokenBudgets({ budgets }: { budgets: Record<string, UsageBudget> | null | undefined }) {
  const entries = Object.entries(budgets ?? {});
  return (
    <section aria-label="Token budgets" className="mt-8">
      <SectionLabel style={{ marginBottom: 12 }}>token budgets</SectionLabel>
      {entries.length === 0 ? (
        <p style={{ color: "var(--text-muted)" }}>No token budgets set.</p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {entries.map(([agent, b]) => {
            const pct = percent(b.used, b.budget);
            return (
              <div
                key={agent}
                className="rounded-lg border px-4 py-3"
                style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
              >
                <div className="flex justify-between text-sm mb-2" style={{ color: "var(--text-primary)" }}>
                  <span className="font-semibold">{agent}</span>
                  <span>
                    {formatTokens(b.used)} / {formatTokens(b.budget)} · {pct}%
                  </span>
                </div>
                <NotchBar pctUsed={Math.min(pct, 100)} fillColor={band(pct)} />
                {b.paused_since !== null && b.paused_since !== undefined && (
                  <p className="text-xs mt-2" style={{ color: WARN }}>
                    paused since {formatEpoch(b.paused_since)}
                    {b.until ? `, until ${formatEpoch(b.until)}` : ""}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
