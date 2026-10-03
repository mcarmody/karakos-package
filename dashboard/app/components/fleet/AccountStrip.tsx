import { alpha, ERR, SectionLabel, Tag, WARN } from "@/app/components/lamplight-ui";
import { formatEpoch, windowLabel, windowName } from "@/lib/fleetView";
import type { UsageBody } from "@/lib/packageBackend";

const ORDER = ["five_hour", "seven_day"];

/** Account-level usage: one chip per rate-limit window, the breaker banner
 * and the governor's weekly figure. Read-only. `now` is epoch seconds. */
export default function AccountStrip({ usage, now }: { usage: UsageBody | null | undefined; now: number }) {
  if (!usage) {
    return (
      <section aria-label="Account usage" className="mb-6">
        <SectionLabel style={{ marginBottom: 8 }}>account usage</SectionLabel>
        <p className="text-sm" style={{ color: "var(--text-muted)" }}>No usage reading.</p>
      </section>
    );
  }
  const windows = Object.entries(usage.windows ?? {}).sort(([a], [b]) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia < 0 ? ORDER.length : ia) - (ib < 0 ? ORDER.length : ib) || a.localeCompare(b);
  });
  const breaker = usage.breaker;
  const governor = usage.governor;
  const breakerUntil =
    breaker?.until === null || breaker?.until === undefined || breaker.until <= now
      ? "until usage drops"
      : `until ${formatEpoch(breaker.until)}`;

  return (
    <section aria-label="Account usage" className="mb-6">
      <SectionLabel style={{ marginBottom: 8 }}>account usage</SectionLabel>

      {breaker?.paused && (
        <p
          role="alert"
          className="text-sm font-semibold mb-3 rounded-md border px-3 py-2"
          style={{ color: ERR, borderColor: alpha(ERR, 45), backgroundColor: alpha(ERR, 8) }}
        >
          Account limit: dispatch paused {breakerUntil}
          {breaker.types?.length ? `, ${breaker.types.join(", ")}` : ""}
        </p>
      )}

      <div className="flex flex-wrap gap-3">
        {windows.length === 0 && (
          <span className="text-sm" style={{ color: "var(--text-muted)" }}>No usage windows reported yet.</span>
        )}
        {windows.map(([type, w]) => (
          <div
            key={type}
            className="rounded-lg border px-3 py-2"
            style={{ minWidth: 150, backgroundColor: "var(--bg-surface)", borderColor: "var(--border)" }}
          >
            <div className="text-[11px] uppercase" style={{ color: "var(--text-muted)", letterSpacing: "0.08em" }}>
              {windowName(type)}
            </div>
            <div className="text-xl font-semibold" style={{ color: "var(--text-primary)" }}>
              {windowLabel(w.utilization_pct)}
            </div>
            <div className="text-xs" style={{ color: "var(--text-secondary)" }}>
              {w.resets_at ? `resets ${formatEpoch(w.resets_at)}` : "no reset time"}
              {w.status && w.status !== "allowed" ? ` · ${w.status}` : ""}
            </div>
          </div>
        ))}
      </div>

      {governor && (
        <p className="text-sm mt-3 flex flex-wrap items-center gap-2" style={{ color: "var(--text-secondary)" }}>
          {governor.weekly_pct !== null && governor.weekly_pct !== undefined && (
            <span>Weekly usage {Math.round(governor.weekly_pct)}%</span>
          )}
          {governor.enabled === false && <Tag label="governor off" color="var(--text-muted)" />}
          {governor.policy_broken && <Tag label="policy broken" color={WARN} filled />}
          {governor.policy_broken && (
            <span style={{ color: WARN }}>config/governor.yaml is invalid; the governor is not deferring work.</span>
          )}
        </p>
      )}
    </section>
  );
}
