"use client";

import { useState } from "react";
import { alpha, SectionLabel, Tag } from "@/app/components/lamplight-ui";
import { usePoll } from "@/lib/hooks";
import {
  formatDuration,
  formatHiveTime,
  hiveQuery,
  HIVE_STATUSES,
  statusTone,
  type HiveQuery,
  type HiveStatus,
} from "@/lib/fleetView";
import type { HiveCall } from "@/lib/packageBackend";
import { TONE_COLOR } from "./tone";

const TH = "text-left px-3 py-2 text-[11px] uppercase font-medium";
const PRESETS: { label: string; hours: number | null }[] = [
  { label: "any time", hours: null },
  { label: "1 h", hours: 1 },
  { label: "24 h", hours: 24 },
  { label: "7 d", hours: 168 },
];
const PAGE = 100;
const MAX = 500;

/** The table only: newest first, as the server returns it. */
export function HiveCallTable({ calls }: { calls: HiveCall[] }) {
  if (calls.length === 0) {
    return <p className="text-sm" style={{ color: "var(--text-muted)" }}>No hive calls in this window.</p>;
  }
  return (
    <div
      className="rounded-lg border overflow-hidden"
      style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px]">
          <thead style={{ backgroundColor: "var(--bg-elevated)", color: "var(--text-muted)" }}>
            <tr>
              <th className={TH}>From</th>
              <th className={TH}>To</th>
              <th className={TH}>Depth</th>
              <th className={TH}>Status</th>
              <th className={TH}>Created (UTC)</th>
              <th className={TH}>Duration</th>
              <th className={TH}>Question and answer</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c) => (
              <tr key={c.call_id} style={{ borderTop: "1px solid var(--border)", verticalAlign: "top" }}>
                <td className="px-3 py-2 text-sm font-mono" style={{ color: "var(--text-primary)" }}>{c.from ?? c.from_agent}</td>
                <td className="px-3 py-2 text-sm font-mono" style={{ color: "var(--text-primary)" }}>{c.to ?? c.to_agent}</td>
                <td className="px-3 py-2 text-sm" style={{ color: "var(--text-secondary)" }}>{c.depth}</td>
                <td className="px-3 py-2">
                  <Tag label={c.status} color={TONE_COLOR[statusTone(c.status)]} />
                </td>
                <td className="px-3 py-2 text-sm whitespace-nowrap" style={{ color: "var(--text-secondary)" }}>
                  {formatHiveTime(c.created_at)}
                </td>
                <td className="px-3 py-2 text-sm whitespace-nowrap" style={{ color: "var(--text-secondary)" }}>
                  {formatDuration(c.duration_ms)}
                </td>
                <td className="px-3 py-2 text-sm" style={{ color: "var(--text-secondary)", maxWidth: 420 }}>
                  {c.question && <div>Q: {c.question.slice(0, 200)}</div>}
                  {c.answer && <div>A: {c.answer.slice(0, 200)}</div>}
                  {c.error && <div style={{ color: "var(--err)" }}>{c.error.slice(0, 200)}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-surface)",
  color: "var(--text-primary)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "6px 8px",
  fontSize: 13,
  minHeight: 36,
};

/** Hive call log with filters. `shards` feeds the shard select (from the roster). */
export default function HiveCallLog({ shards }: { shards: string[] }) {
  const [shard, setShard] = useState("");
  const [status, setStatus] = useState<"" | HiveStatus>("");
  // The ISO time is fixed when a preset is picked, so the poll URL stays stable
  // between renders (a clock read in render would refetch on every render).
  const [sinceHours, setSinceHours] = useState<number | null>(null);
  const [since, setSince] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);

  const query: HiveQuery = { limit };
  if (shard) query.shard = shard;
  if (status) query.status = status;
  if (since) query.since = since;

  const { data, error } = usePoll<{ calls: HiveCall[] }>(`/api/hive/calls?${hiveQuery(query)}`, 10000);
  const calls = data?.calls ?? [];

  return (
    <section aria-label="Hive call log" className="mb-8">
      <SectionLabel style={{ marginBottom: 8 }}>hive call log</SectionLabel>
      <div className="flex flex-wrap gap-3 mb-3">
        <label className="text-xs flex flex-col gap-1" style={{ color: "var(--text-muted)" }}>
          Shard
          <select value={shard} onChange={(e) => { setShard(e.target.value); setLimit(PAGE); }} style={selectStyle}>
            <option value="">any</option>
            {shards.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="text-xs flex flex-col gap-1" style={{ color: "var(--text-muted)" }}>
          Status
          <select value={status} onChange={(e) => { setStatus(e.target.value as "" | HiveStatus); setLimit(PAGE); }} style={selectStyle}>
            <option value="">any</option>
            {HIVE_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="text-xs flex flex-col gap-1" style={{ color: "var(--text-muted)" }}>
          Since
          <select
            value={sinceHours ?? ""}
            onChange={(e) => {
              const h = e.target.value === "" ? null : Number(e.target.value);
              setSinceHours(h);
              setSince(h === null ? null : new Date(Date.now() - h * 3_600_000).toISOString());
              setLimit(PAGE);
            }}
            style={selectStyle}
          >
            {PRESETS.map((p) => <option key={p.label} value={p.hours ?? ""}>{p.label}</option>)}
          </select>
        </label>
      </div>
      {error && !data && <p className="text-sm mb-2" style={{ color: "var(--err)" }}>Hive log unavailable: {error}</p>}
      <HiveCallTable calls={calls} />
      {calls.length >= limit && limit < MAX && (
        <button
          type="button"
          onClick={() => setLimit((l) => Math.min(l + PAGE, MAX))}
          className="mt-3 text-sm rounded-md border px-3 py-2"
          style={{ borderColor: "var(--border)", color: "var(--text-primary)", minHeight: 36 }}
        >
          Load more
        </button>
      )}
    </section>
  );
}
