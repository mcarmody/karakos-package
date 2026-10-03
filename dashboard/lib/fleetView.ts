/**
 * Pure formatting for the package fleet, agents and costs views.
 * No imports from server code: client components and the
 * route helpers both use it.
 */

import type { AgentShardRow, PausedInfo } from "@/lib/agentStatus";

export const HIVE_STATUSES = ["pending", "answered", "expired", "error", "timeout", "abandoned"] as const;
export type HiveStatus = (typeof HIVE_STATUSES)[number];

export interface HiveQuery {
  limit: number;
  since?: string;
  shard?: string;
  status?: HiveStatus;
}

/** Query string for a validated filter; the inverse of `parseHiveFilter`
 * (lib/packageBackend.ts). */
export function hiveQuery(q: HiveQuery): string {
  const p = new URLSearchParams();
  p.set("limit", String(q.limit));
  if (q.since) p.set("since", q.since);
  if (q.shard) p.set("shard", q.shard);
  if (q.status) p.set("status", q.status);
  return p.toString();
}

export type Tone = "ok" | "info" | "warn" | "err" | "muted";

/** Time as `YYYY-MM-DD HH:MM UTC` from epoch seconds. UTC on purpose: the
 * strings are stable across viewers and tests. */
export function formatEpoch(sec: number): string {
  return new Date(sec * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

/** A usage window chip: "no reading" when null (never 0), else `41%`. */
export function windowLabel(utilizationPct: number | null | undefined): string {
  if (utilizationPct === null || utilizationPct === undefined || Number.isNaN(utilizationPct)) return "no reading";
  return `${Math.round(utilizationPct)}%`;
}

const WINDOW_NAMES: Record<string, string> = { five_hour: "5-hour", seven_day: "7-day" };

/** Human name for a `rate_limit_type`; unknown types show by name. */
export function windowName(type: string): string {
  return WINDOW_NAMES[type] ?? type;
}

/** Why a shard is held: reason plus when it lifts. `now` is epoch seconds. */
export function pausedLabel(paused: PausedInfo | null | undefined, now: number): string {
  if (!paused) return "";
  if (paused.until === null || paused.until === undefined) return `${paused.reason}: until usage drops`;
  if (paused.until <= now) return `${paused.reason}: resuming`;
  return `${paused.reason}: until ${formatEpoch(paused.until)}`;
}

const STATE_TONES: Record<string, Tone> = {
  IDLE: "ok",
  PROCESSING: "info",
  ERROR_RECOVERY: "err",
  UNKNOWN: "muted",
};

/** Tone for a shard state (an open string: anything unlisted is muted). */
export function stateTone(state: string): Tone {
  return STATE_TONES[state] ?? "muted";
}

const STATUS_TONES: Record<string, Tone> = {
  pending: "info",
  answered: "ok",
  expired: "warn",
  error: "err",
  timeout: "warn",
  abandoned: "muted",
};

/** Tone for a hive call status chip. */
export function statusTone(status: string): Tone {
  return STATUS_TONES[status] ?? "muted";
}

/** Token count; 0 means the server has no reading. */
export function formatTokens(n: number | null | undefined): string {
  if (!n) return "unknown";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/** Paused rows first; otherwise the given order is kept (stable). */
export function sortShardRows<T extends Pick<AgentShardRow, "paused">>(rows: T[]): T[] {
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => Number(Boolean(b.r.paused)) - Number(Boolean(a.r.paused)) || a.i - b.i)
    .map((x) => x.r);
}

/** UTC `YYYY-MM-DD HH:MM:SS` (the hive log's format) to `MM-DD HH:MM`. */
export function formatHiveTime(raw: string | null | undefined): string {
  if (!raw) return "";
  const m = /^\d{4}-(\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(raw);
  return m ? `${m[1]} ${m[2]}` : raw;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60_000)} min`;
}

export function percent(used: number, budget: number): number {
  return budget > 0 ? Math.round((used / budget) * 100) : 0;
}
